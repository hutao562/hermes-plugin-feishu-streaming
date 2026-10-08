"""feishu-streaming 自检（doctor）— 只读诊断，零安装依赖.

用法（在仓库或已部署的插件目录均可）::

    python3 plugin/doctor.py            # 仓库内运行，额外比对部署目录一致性
    python3 ~/.hermes/plugins/feishu-streaming/doctor.py   # 生产环境自检
    python3 plugin/doctor.py --json     # 机器可读输出（CI/脚本友好）

检查面 = 这个项目历史上真实静默失效过的每一类问题：
config 开关（streaming.enabled / display streaming / plugins.enabled）、
multiplex profile 退出开关、凭据（env / .env，不回显值）、插件目录完整性、
部署目录与仓库漂移、上游 hermes 契约锚点（plugin/contract.py）、运行时
lark-oapi 可用性（PM venv 才是运行时）、网关进程与装配日志、错误病征扫描、
注入模式残留（watchdog plist）。

退出码：有 FAIL 项 → 1，否则 0。绝不修改任何文件。
"""

from __future__ import annotations

import argparse
import json as _json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .contract import check_tree, render_misses
except ImportError:  # 直接脚本运行（python3 plugin/doctor.py）：脚本目录即 import 根
    from contract import check_tree, render_misses  # type: ignore[no-redef]

OK, WARN, FAIL = "ok", "warn", "fail"

# 插件目录必须齐全的核心文件（相对 plugin 目录）
_PLUGIN_CORE_FILES = (
    "plugin.yaml", "__init__.py", "adapter.py", "engine.py",
    "_clarify.py", "_compat.py", "contract.py", "doctor.py",
    "_vendor/cardkit/builder.py", "_vendor/cardkit/markdown.py",
    "_vendor/feishu.py", "_vendor/config.py",
    "_vendor/streaming/segments.py", "_vendor/streaming/flush.py",
    "_vendor/streaming/tooluse.py", "_vendor/streaming/segment_helper.py",
    "_vendor/streaming/text.py",
)

# gateway.log 里的错误病征（AGENTS.md 诊断知识的产品化）
_LOG_SIGNATURES = (
    ("card over max size", "卡片超体积上限（200860）——工具输出截断口径可能被绕过"),
    ("300317", "cardkit close/update sequence 同号——升级后 sequence 语义漂移"),
    ("230001", "reply 目标非法——卡片锚用了非消息 id（chat_id 当 reply 目标）"),
    ("not support tag: file", "卡片塞了 file 组件——文档交付必须走上传+reply"),
)

_PLATFORM_ENTRY = "feishu-streaming-platform"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    def render(self) -> str:
        icon = {OK: "✓", WARN: "⚠️", FAIL: "✗"}[self.status]
        line = f"{icon} {self.name}"
        if self.detail:
            line += f" — {self.detail}"
        if self.hint:
            line += f"\n    ↳ {self.hint}"
        return line


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", hint: str = "") -> Check:
        check = Check(name, status, detail, hint)
        self.checks.append(check)
        return check

    @property
    def worst(self) -> str:
        if any(c.status == FAIL for c in self.checks):
            return FAIL
        if any(c.status == WARN for c in self.checks):
            return WARN
        return OK


def _load_yaml(path: Path) -> tuple[dict | None, str]:
    """解析 YAML；PyYAML 缺失时返回 (None, 原因)."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return None, "当前解释器没有 PyYAML"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"解析失败: {exc}"
    return (data if isinstance(data, dict) else {}), ""


def _config_get(config: dict, *path: str) -> Any:
    node: object = config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _hermes_source_root(home: Path) -> Path | None:
    """定位 hermes 源码根（契约检查用）；找不到返回 None."""
    candidates = [
        Path(os.environ.get("HERMES_AGENT_HOME", "")) if os.environ.get("HERMES_AGENT_HOME") else None,
        home / "hermes-agent",
        home / "hermes" / "hermes-agent",
        Path.home() / ".hermes" / "hermes-agent",
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "gateway" / "stream_consumer_transport.py").is_file():
            return candidate
    return None


def check_config_section(report: Report, home: Path, label: str, config: dict | None,
                         why_unavailable: str = "") -> dict:
    """单个 home/profile 的 config 开关检查；返回读到的关键开关（供调用方汇总）."""
    flags: dict = {}
    if config is None:
        report.add(f"{label}: config 读取", WARN, why_unavailable or "config.yaml 不存在",
                   "用带 PyYAML 的解释器运行 doctor（如 hermes venv 的 python3）")
        return flags

    enabled = _config_get(config, "plugins", "enabled") or []
    if isinstance(enabled, str):
        enabled = [enabled]
    if _PLATFORM_ENTRY in enabled:
        report.add(f"{label}: plugins.enabled", OK, f"含 {_PLATFORM_ENTRY}")
    else:
        report.add(f"{label}: plugins.enabled", FAIL, f"缺 {_PLATFORM_ENTRY}",
                   f"在 {label} config.yaml 的 plugins.enabled 加入 {_PLATFORM_ENTRY} 并重启网关")

    disabled = _config_get(config, "plugins", "disabled") or []
    if isinstance(disabled, str):
        disabled = [disabled]
    if _PLATFORM_ENTRY in disabled:
        report.add(f"{label}: plugins.disabled", FAIL, "该 profile 显式退出本插件",
                   f"从 plugins.disabled 移除 {_PLATFORM_ENTRY}")

    streaming_on = _config_get(config, "streaming", "enabled")
    flags["streaming_on"] = streaming_on
    if streaming_on is True:
        report.add(f"{label}: streaming.enabled", OK)
    else:
        report.add(f"{label}: streaming.enabled", FAIL, f"当前值 {streaming_on!r}",
                   "设 streaming.enabled: true（官方 draft transport 的总开关）")

    transport = str(_config_get(config, "streaming", "transport") or "auto")
    if transport in ("auto", "draft"):
        report.add(f"{label}: streaming.transport", OK, transport)
    else:
        report.add(f"{label}: streaming.transport", WARN, transport,
                   "transport: auto（或 draft）才会走 draft-streaming 卡片")

    display_on = _config_get(config, "display", "platforms", "feishu", "streaming")
    flags["display_on"] = display_on
    if display_on is True:
        report.add(f"{label}: display.platforms.feishu.streaming", OK)
    else:
        report.add(f"{label}: display.platforms.feishu.streaming", FAIL, f"当前值 {display_on!r}",
                   "设 display.platforms.feishu.streaming: true（关着官方 draft 契约不启动，family 踩过）")
    return flags


def check_credentials(report: Report, home: Path) -> None:
    env_id, env_secret = os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")
    if env_id and env_secret:
        report.add("凭据: 环境变量", OK, "FEISHU_APP_ID/SECRET 已设置")
        return
    for env_file in (home / ".env", Path.home() / ".hermes" / ".env"):
        if not env_file.is_file():
            continue
        try:
            text = env_file.read_text(encoding="utf-8")
        except OSError:
            continue
        has_id = re.search(r"^FEISHU_APP_ID=.", text, re.M)
        has_secret = re.search(r"^FEISHU_APP_SECRET=.", text, re.M)
        if has_id and has_secret:
            report.add("凭据: .env", OK, str(env_file))
            return
    report.add("凭据", FAIL, "env 与 ~/.hermes/.env 都没有 FEISHU_APP_ID/SECRET",
               "配置飞书应用凭据后重启网关")


def check_plugin_dir(report: Report, home: Path) -> Path | None:
    plugin_dir = home / "plugins" / "feishu-streaming"
    if not plugin_dir.is_dir():
        report.add("插件目录", FAIL, f"{plugin_dir} 不存在",
                   "按 README 部署：cp -R plugin ~/.hermes/plugins/feishu-streaming")
        return None
    manifest = plugin_dir / "plugin.yaml"
    data, err = _load_yaml(manifest) if manifest.is_file() else (None, "plugin.yaml 不存在")
    if data is None:
        report.add("插件目录: plugin.yaml", FAIL, err, "重新部署插件目录")
        return plugin_dir
    report.add("插件目录", OK, f"name={data.get('name')} version={data.get('version')} kind={data.get('kind')}")
    missing = [rel for rel in _PLUGIN_CORE_FILES
               if rel != "doctor.py" and not (plugin_dir / rel).is_file()]
    if missing:
        report.add("插件目录: 核心文件", FAIL, f"缺 {', '.join(missing)}", "重新部署插件目录")
    else:
        report.add("插件目录: 核心文件", OK, f"{len(_PLUGIN_CORE_FILES) - 1} 个核心文件齐全")
    return plugin_dir


def check_deploy_freshness(report: Report, repo_plugin_dir: Path | None, deployed: Path | None) -> None:
    """仓库内运行 doctor 时，比对部署目录是否落后于仓库."""
    if repo_plugin_dir is None or deployed is None:
        return
    stale: list[str] = []
    for rel in _PLUGIN_CORE_FILES:
        repo_file, deployed_file = repo_plugin_dir / rel, deployed / rel
        if not repo_file.is_file():
            continue
        if not deployed_file.is_file() or repo_file.read_bytes() != deployed_file.read_bytes():
            stale.append(rel)
    if stale:
        report.add("部署一致性", WARN, f"{len(stale)} 个文件落后于仓库（如 {stale[0]}）",
                   "删除旧的部署目录后重新拷贝 plugin/（命令见 README「更新」章节），并重启网关")
    else:
        report.add("部署一致性", OK, "部署目录与仓库一致")


def check_contract(report: Report, home: Path) -> None:
    root = _hermes_source_root(home)
    if root is None:
        report.add("上游契约", WARN, "未找到 hermes 源码（gateway/stream_consumer_transport.py）",
                   "契约无法本机验证；CI 每日 hermes-check 会对上游 main 验证")
        return
    misses = check_tree(root)
    if misses:
        report.add("上游契约", FAIL, f"{root} 缺 {len(misses)} 个契约锚点",
                   "上游可能改了 draft-streaming 契约，见 AGENTS.md「上游兼容」段：\n" + render_misses(misses))
    else:
        report.add("上游契约", OK, f"draft-streaming 契约锚点齐全（{root}）")


def check_runtime_deps(report: Report, home: Path) -> None:
    try:
        import lark_oapi  # noqa: F401

        report.add("lark-oapi（当前解释器）", OK, sys.executable)
    except ImportError:
        report.add("lark-oapi（当前解释器）", WARN, f"{sys.executable} 没有 lark-oapi",
                   "doctor 不在运行时环境不重要——关键是网关进程的 venv（见下一项）")
    # PM 布局：installs/*/environments/*/venv 才是网关运行时
    venvs = sorted((home / "installs").glob("*/environments/*/venv")) if (home / "installs").is_dir() else []
    if venvs:
        healthy = [v for v in venvs if list(v.glob("lib/python*/site-packages/lark_oapi"))]
        if healthy:
            report.add("lark-oapi（运行时 venv）", OK, f"{len(healthy)}/{len(venvs)} 个 PM venv 可用")
        else:
            report.add("lark-oapi（运行时 venv）", FAIL, f"{len(venvs)} 个 PM venv 都没有 lark-oapi",
                       "在运行时 venv 安装依赖后重启网关")


def check_injection_residue(report: Report, home: Path) -> None:
    plist = Path.home() / "Library" / "LaunchAgents" / "com.hermes-lark.watchdog.plist"
    if plist.exists():
        report.add("注入模式残留", WARN, "watchdog plist 仍在（注入模式已归档）",
                   f"launchctl bootout gui/$(id -u)/com.hermes-lark.watchdog 2>/dev/null; rm {plist}")
    else:
        report.add("注入模式残留", OK, "无 watchdog plist")
    gateway_run = home / "hermes-agent" / "gateway" / "run.py"
    if gateway_run.is_file() and "HERMES_LARK" in gateway_run.read_text(encoding="utf-8", errors="ignore"):
        report.add("注入模式残留", WARN, f"{gateway_run} 仍有注入 marker",
                   "该副本不被运行时 import 可忽略；若在 live workspace，用归档 tag 里的 Patcher.remove() 清理")


def check_gateway(report: Report, home: Path) -> None:
    try:
        running = subprocess.run(["pgrep", "-f", "gateway run"], capture_output=True, text=True)
    except OSError:
        running = None
    if running is None:
        report.add("网关进程", WARN, "无法探测（无 pgrep）")
    elif running.returncode == 0:
        report.add("网关进程", OK, f"pid {running.stdout.split()[0]}")
    else:
        report.add("网关进程", WARN, "未运行", "启动网关后卡片才会出现")

    log = home / "logs" / "gateway.log"
    if not log.is_file():
        report.add("装配日志", WARN, f"{log} 不存在（网关可能从未启动）")
        return
    try:
        tail = log.read_text(encoding="utf-8", errors="ignore")[-300_000:]
    except OSError as exc:
        report.add("装配日志", WARN, f"读取失败: {exc}")
        return
    factories = [line for line in tail.splitlines() if "adapter factory" in line and "StreamingFeishuAdapter" in line]
    if factories:
        report.add("装配日志", OK, f"最近装配 {len(factories)} 个 profile 的 StreamingFeishuAdapter"
                                  f"（multiplex 下每 profile 一行，最后: {factories[-1][:80]}…）")
    else:
        report.add("装配日志", FAIL, "日志里没有 adapter factory 行——插件没被加载",
                   "检查 plugins.enabled 与插件目录，重启网关后 grep 'adapter factory'")

    for pattern, why in _LOG_SIGNATURES:
        hits = len(re.findall(re.escape(pattern), tail))
        if hits:
            report.add(f"病征: {pattern}", WARN, f"日志尾部出现 {hits} 次 — {why}",
                       "见 AGENTS.md 对应诊断段")
    recent = [line for line in tail.splitlines() if "[feishu-streaming]" in line]
    if recent:
        report.add("近期活动", OK, f"最近 [feishu-streaming] 日志: {recent[-1][:100]}")
    else:
        report.add("近期活动", WARN, "日志尾部没有 [feishu-streaming] 活动",
                   "发一条消息后重跑 doctor；持续无活动 = 流式路径未触发")


def run_checks(home: Path, repo_plugin_dir: Path | None) -> Report:
    """全部检查（只读）. repo_plugin_dir 为 None 表示 doctor 在部署目录运行."""
    report = Report()
    if not home.is_dir():
        report.add("hermes home", FAIL, f"{home} 不存在", "确认 HERMES_HOME 或安装位置")
        return report
    report.add("hermes home", OK, str(home))

    config, why = _load_yaml(home / "config.yaml")
    check_config_section(report, home, "default", config, why)
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        for profile in sorted(p for p in profiles_dir.iterdir() if p.is_dir()):
            pconfig, pwhy = _load_yaml(profile / "config.yaml")
            check_config_section(report, home, f"profile:{profile.name}", pconfig, pwhy)

    check_credentials(report, home)
    deployed = check_plugin_dir(report, home)
    check_deploy_freshness(report, repo_plugin_dir, deployed)
    check_contract(report, home)
    check_runtime_deps(report, home)
    check_injection_residue(report, home)
    check_gateway(report, home)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="feishu-streaming 插件自检（只读）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（CI 友好）")
    parser.add_argument("--home", type=Path, default=None, help="HERMES_HOME（默认读环境/ ~/.hermes）")
    args = parser.parse_args(argv)

    home = args.home or Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    script_dir = Path(__file__).resolve().parent
    deployed = home / "plugins" / "feishu-streaming"
    repo_plugin_dir = script_dir if (script_dir.parent / "AGENTS.md").is_file() else None
    if repo_plugin_dir is not None and script_dir == deployed:
        repo_plugin_dir = None  # 在部署目录内运行，没有第二份可比

    report = run_checks(home, repo_plugin_dir)

    if args.json:
        print(_json.dumps({
            "worst": report.worst,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail, "hint": c.hint}
                       for c in report.checks],
        }, ensure_ascii=False, indent=2))
    else:
        print("feishu-streaming doctor（只读自检）\n" + "=" * 40)
        for check in report.checks:
            print(check.render())
        icons = {OK: "✓ 全部通过", WARN: "⚠️ 有警告（可用，建议处理）", FAIL: "✗ 有失败项（功能受损）"}
        print("=" * 40 + f"\n结论: {icons[report.worst]}")
    return 1 if report.worst == FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
