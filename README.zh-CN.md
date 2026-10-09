# Hermes Lark Streaming

[English](README.md) | 简体中文


[![Tests](https://github.com/Cheerwhy/hermes-lark-streaming/actions/workflows/test.yml/badge.svg)](https://github.com/Cheerwhy/hermes-lark-streaming/actions/workflows/test.yml)
[![Hermes Compat](https://github.com/Cheerwhy/hermes-lark-streaming/actions/workflows/hermes-check.yml/badge.svg)](https://github.com/Cheerwhy/hermes-lark-streaming/actions/workflows/hermes-check.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[Hermes](https://github.com/NousResearch/hermes-agent) Gateway 飞书流式卡片插件 — 基于 CardKit v2.0 的 platform 插件，把每回合回复渲染成打字机效果的实时流式卡片。

灵感来源于 [openclaw-lark](https://github.com/larksuite/openclaw-lark) 和 [hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card)。

[English](README.md)

![](assets/cover.jpg)

---

## 形态说明

本插件自 v0.14.0 起只有一种形态：**hermes platform 插件**（`plugin/` 目录，`kind: platform`）。它子类化官方 `FeishuAdapter`、以同名 `feishu` 注册顶替内置适配器，**自包含、零 pip 安装**——把目录拷到 `~/.hermes/plugins/` 即完成部署。

> 早期的 AST 注入形态（向 gateway 源码插 hook）已于 v0.14.0 归档，完整实现见
> git tag `archive/injection-mode`。如需回滚：`git checkout archive/injection-mode`
> 后按该版本的 README 操作。

---

## 功能

- **流式输出** — AI 回复实时显示在交互卡片中，打字机效果（CardKit v2 draft-streaming）
- **单卡单回合** — 思考、工具调用、回答按事件顺序在同一张卡片内动态渲染
- **思考过程** — 思考型模型的推理增量实时进卡片折叠面板（头尾摘录防超体积）
- **工具调用面板** — 结构化工具事件实时渲染状态图标与结果块，步数封顶防体积爆炸
- **打断即开新卡** — busy redirect 时旧卡红标收尾，新卡立刻以纠正消息为锚开出，思考/工具/正文全程流进新卡
- **跨回合合并** — 后台回合、watcher 通知、文档交付合并进最近卡片，不散落纯文本
- **Clarify 内联单选** — 澄清问题渲染为飞书按钮卡，点击即回调，无需打字
- **心跳状态行** — busy ack（↪ 重定向 / ⏳ 排队）与进度文本进卡片状态行，完成即消失
- **终态卡片** — 完成后重渲完整结果，含 token 用量、耗时、t/s、上下文信息
- **Cron 卡片推送** — 定时任务结果以飞书卡片形式推送
- **多 profile** — multiplex 网关下每个 profile 独立引擎与凭据（footer/header 按 profile 配置）
- **多语言** — 卡片文本内置中英双语，按飞书客户端语言自动切换

---

## 卡片展示

![](assets/streaming.jpg)

---

## 运行要求

- Hermes `>= 0.21.1`，已配置飞书平台（官方飞书适配器可用）
- 飞书应用权限：消息卡片（CardKit）读写、消息发送与回复、文件上传
- 无需 pip 安装任何东西——插件自包含，仅依赖 hermes 运行环境已有的 `lark-oapi`

---

## 安装

完整步骤见 [INSTALL.md](INSTALL.md)。概要：

```bash
# 1. 部署插件目录（自包含）
git clone https://github.com/Cheerwhy/hermes-lark-streaming.git
cp -R hermes-lark-streaming/plugin ~/.hermes/plugins/feishu-streaming

# 2. config.yaml 启用（两处开关缺一不可）
#    plugins.enabled: [feishu-streaming-platform]
#    streaming.enabled: true  +  display.platforms.feishu.streaming: true

# 3. 重启网关
hermes gateway restart

# 4. 自检
python3 ~/.hermes/plugins/feishu-streaming/doctor.py
```

## 自检（doctor）

只读诊断脚本，部署在插件目录内、随仓库分发，零安装依赖：

```bash
python3 ~/.hermes/plugins/feishu-streaming/doctor.py          # 人读报告
python3 ~/.hermes/plugins/feishu-streaming/doctor.py --json   # CI/脚本友好
```

检查面 = 这个项目历史上真实静默失效过的每一类问题：两处 config 开关、multiplex
profile 退出开关、凭据（env / `.env`，不回显值）、插件目录完整性与部署漂移、
上游 hermes 契约锚点、运行时 venv 的 lark-oapi、网关进程与装配日志、错误病征
扫描（卡片超体积 / sequence 冲突 / 非法 reply 目标）、注入模式残留。

---

## 配置

在 `~/.hermes/config.yaml`：

```yaml
plugins:
  enabled:
    - feishu-streaming-platform   # platform 插件是 opt-in，必须显式启用
streaming:
  enabled: true                   # 官方 draft transport 总开关
  transport: auto                 # auto/draft 走 draft-streaming 卡片
  width_mode: default             # 卡片宽度：default / compact / fill
  header:
    enabled: false                # 卡片 header（异常态红标不受此项约束，强制显示）
  body:
    text_size: normal_v2
  footer:
    enabled: true
    text_size: notation
    fields:
      - [status, elapsed, context, model]
    show_label: false
  panel_expanded: false           # 完成态面板保持展开
  clarify_inline: true            # clarify 内联单选按钮卡（false 回退文本列表）
display:
  platforms:
    feishu:
      streaming: true             # 官方 draft 契约开关（关着流式不启动）
      show_tool_use: true
      show_reasoning: true        # 思考型模型的 reasoning 增量进卡
```

**凭据**不在本插件配置——复用官方飞书平台凭据（环境变量 `FEISHU_APP_ID` /
`FEISHU_APP_SECRET` 或 `~/.hermes/.env`），按 profile 作用域绑定。

**Header**（`streaming.header.enabled`）：完成后卡片顶部状态栏，按状态着色（流式蓝/完成绿/打断红）。打断收尾的红标不依赖此开关——异常态强制显示 header。默认关闭。

**Footer 字段**（`footer.fields`）：二维数组，每个子数组一行，字段间用 `·` 连接。可用字段：`status` / `elapsed` / `model` / `tokens` / `context`。

---

## 更新

```bash
cd hermes-lark-streaming
git pull
rm -rf ~/.hermes/plugins/feishu-streaming
cp -R plugin ~/.hermes/plugins/feishu-streaming
hermes gateway restart          # 重启窗口约 50s，会打断进行中的回合
python3 ~/.hermes/plugins/feishu-streaming/doctor.py
```

## 卸载

```bash
rm -rf ~/.hermes/plugins/feishu-streaming
# config.yaml 的 plugins.enabled 移除 feishu-streaming-platform 后重启网关，
# 即回退官方内置飞书适配器（无流式卡片）
```

---

## 工作原理

插件以 `kind: platform` 注册同名 `feishu` 平台（registry last-writer-wins 顶替内置适配器），子类化官方 `FeishuAdapter` 实现 draft-streaming 契约：

```
用户消息 → hermes 回合开始（探针时机即建卡，卡片 reply 用户消息）
  → send_draft 全量快照帧 → 引擎整段置换 ANSWER（100ms 节流 flush）
  → reasoning 增量 / 工具事件 → 折叠面板 / 工具面板流式更新
  → 回合终态 → CardKit close + 完成卡整体重渲（footer 统计）
```

关键边界：draft 锚变化 = 新回合（followup drain）→ 旧卡绿色收尾开新卡；↪ redirect
ack = 同回合改锚续跑 → **立即**收旧开新（锚取自 ack 的 reply_to，即用户纠正消息）；
`message_id=None` 的后台回合 → 跨回合合并进最近卡片。上游契约锚点集中在
[`plugin/contract.py`](plugin/contract.py)，由三个通道共同守护：

- CI 对固定 revision 上游样本回归（`tests/test_upstream_compat.py`）
- 每日对上游 main 的 [hermes-check](https://github.com/Cheerwhy/hermes-lark-streaming/actions/workflows/hermes-check.yml)，失败自动开 issue
- 本机 `doctor.py` 实时验证

## 开发测试

```bash
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3

$HERMES_PYTHON -m ruff check plugin tests
$HERMES_PYTHON -m mypy
$HERMES_PYTHON -m pytest tests/ -q        # 上游样本按 pinned commit 缓存于 tests/samples/（见 tests/HERMES_SAMPLES.md）

# E2E（需 Hermes 运行 + lark-cli，默认跳过）
HERMES_HOME=~/.hermes $HERMES_PYTHON -m pytest -m e2e tests/e2e/ -v
```

改动 `plugin/` 后部署即生效：`rm -rf ~/.hermes/plugins/feishu-streaming && cp -R plugin ~/.hermes/plugins/feishu-streaming && hermes gateway restart`。

## 贡献者

感谢以下贡献者的 Issue 和 PR：
## 贡献者

感谢以下贡献者的 Issue 和 PR：

<a href="https://github.com/Mxin-9527"><img src="https://avatars.githubusercontent.com/u/178271393?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/gitteeee"><img src="https://avatars.githubusercontent.com/u/128769493?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/Bandersnatch0x"><img src="https://avatars.githubusercontent.com/u/13325067?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/runfali"><img src="https://avatars.githubusercontent.com/u/39327978?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/thunderfight127-svg"><img src="https://avatars.githubusercontent.com/u/275854191?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/willggy"><img src="https://avatars.githubusercontent.com/u/74762604?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/atomperson"><img src="https://avatars.githubusercontent.com/u/14934637?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/linjunxin01"><img src="https://avatars.githubusercontent.com/u/63715504?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/mouxangithub"><img src="https://avatars.githubusercontent.com/u/48978046?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/numuly"><img src="https://avatars.githubusercontent.com/u/137970054?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/wzgrx"><img src="https://avatars.githubusercontent.com/u/39661556?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/zhaomingcheng01"><img src="https://avatars.githubusercontent.com/u/46734892?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>

---

## 许可证

[MIT](LICENSE)
