## 目标
修复「hermes 升级重建 venv 导致插件 editable 安装丢失 → 三层自愈全部静默失效」的根因。让升级后无需手动跑 reinstall 脚本即可自动恢复。

## 核心思路
新增一个「插件可用性检测 + 自动重装」机制，植入三层防御。关键前提：代码库当前**没有**定位插件源码目录的 helper，需要新增。

---

## 改动清单（5 个文件 + 测试）

### 1. 新增 `hermes_lark_streaming/_source.py`（新文件，~40 行）
提供定位插件源码目录 + 检测 venv 可用性 + 重装插件的单一入口。

```python
def source_dir() -> Path | None:
    """定位插件源码目录（含 pyproject.toml）。
    优先 importlib.metadata.distribution().locate_file；
    回退 Path(__file__).resolve().parent.parent（editable 安装时 __file__ 在源码树内）。"""

def venv_has_plugin() -> bool:
    """插件是否在 hermes venv 里可 import（gateway 进程视角，不依赖 cwd）。
    用 importlib.util.find_spec 在 hermes_python() 环境下检测。"""

def reinstall_into_venv() -> bool:
    """pip install -e <source_dir> 到 hermes venv，写死清华源。
    返回 True 成功。失败只记日志不抛（调用方决定是否降级）。"""
```
- 写死 `-i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30`（你确认的决策；Clash 对 pypi 转发不通）
- `source_dir()` 两策略：① `distribution("hermes-lark-streaming").locate_file("")` ② `Path(__file__).resolve().parents[1]`（editable 安装时源码就在 parents[1]）

### 2. 改 `hermes_lark_streaming/__init__.py`（第 1 层 register 自愈）
在 `_run_self_heal()` 开头新增**插件可用性检测**（在检测 run.py 补丁之前）：
- `if not venv_has_plugin():` → 调 `reinstall_into_venv()`，成功则记日志并返回 True（触发 restart 让新装的插件被加载）；失败则记 WARNING，继续走原有 run.py 补丁逻辑（尽力而为）。
- 这样升级重建 venv 后，gateway 启动 → register() → 发现插件没了 → 自动 pip install -e + restart。

### 3. 改 `hermes_lark_streaming/watchdog.py`（第 2 层守护）
两处改动：
- **a) `_render_repatch_script()`**：生成的 bash 在 `uninstall/install` 之前，加一段「插件不在 venv 就 pip install -e <源码> -i 清华源」。脚本里硬编码源码路径（由 `_source.source_dir()` 在生成时解析注入，不是运行时查找——脚本要独立可执行）。
- **b) `status()`**：loaded 检测从单一 `launchctl print` 改为 `launchctl list <label>` 优先 + `print` 兜底（Explore 建议的加固，应对非 gui 域加载的边界）。**注意**：之前看到的 NOT loaded 是真实状态（当时没 bootstrap），不是 bug——这里只做加固。

### 4. 改 `reinstall_after_upgrade.sh`（第 3 层手动兜底脚本）
修复两个静默失败点：
- **a) 第 17 行 `pip install`**：去掉 `| tail -2`（管道吞了退出码），改用临时文件捕获输出 + 检查 `${PIPESTATUS[0]}`。失败时打印完整错误并 `exit 1`。
- **b) pip 源**：加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`（与代码一致，避免默认源 SSL 失败）。
- **c) 第 84 行 `gateway restart`**：检查退出码，失败提示。
- 顶部 shebang 后加 `set -euo pipefail`（让管道失败也能被捕获）。

### 5. 改 `hermes_lark_streaming/__main__.py`（status 命令增强）
在 `_cmd_status()` 里新增「插件是否在 venv 可 import」检测项（最有用的诊断信息，能直接暴露本次失效的根因）：
```
Plugin in venv: yes / NO (run: pip install -e <source_dir>)
```
位置放在 `Patched:` 检测之后。这样下次失效时 status 第一眼就能看出是补丁问题还是插件丢失。

### 6. 改 `tests/test_selfheal.py`（测试覆盖）
新增测试：
- `test_source_dir_locates_pyproject`：`source_dir()` 返回的路径含 pyproject.toml
- `test_reinstall_uses_tsinghua_mirror`：mock subprocess，断言 pip 命令含 `-i https://pypi.tuna.tsinghua.edu.cn/simple`
- `test_run_self_heal_reinstalls_when_plugin_missing`：mock `venv_has_plugin` 返回 False + `reinstall_into_venv` 返回 True → 断言返回 True（触发 restart）
- `test_run_self_heal_proceeds_when_reinstall_fails`：reinstall 失败 → 继续走 run.py 补丁逻辑
- watchdog status 的双检测（mock launchctl，验证 list 优先）

---

## 不改的部分
- **不动 patcher.py**（AST 注入逻辑无关本次失效）
- **不动 config.py**（self_heal/clarify_inline 默认值已正确）
- **不改 pyproject.toml 的 entry_points**（结构正确）
- **不修「Clash 代理对 pypi 转发」本身**（那是你的网络环境，通过写死清华源绕过）

## 风险与缓解
- **风险**：register() 在 gateway 进程里跑 `pip install`（子进程），若卡住会阻塞启动。**缓解**：`reinstall_into_venv()` 用 `subprocess.run(timeout=120)`，超时则放弃降级。
- **风险**：watchdog 生成的 bash 脚本里硬编码源码路径，若用户 clone 到别的目录会失效。**缓解**：源码路径在 `_render_repatch_script()` 生成时由 `source_dir()` 解析，reinstall 时也会重新生成脚本（install 命令调 `_install_watchdog` → 重渲染）。
- **改了 `__init__.py`/`watchdog.py` 后**：只需 `gateway restart`（editable install 即时生效），不需 reinstall 脚本（因为本次改动就是让 reinstall 自动化）。

## 验证步骤（实现后）
1. `pytest tests/test_selfheal.py -q` 全绿
2. 模拟失效：`pip uninstall hermes-lark-streaming` → 重启 gateway → 确认 register() 自动重装
3. `status` 命令显示 `Plugin in venv: yes`
4. watchdog 触发测试：`touch gateway/run.py` → 看日志触发重打（已有机制，不动）