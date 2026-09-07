"""AST Patcher — 在 Hermes gateway 多文件架构中注入 Hook 调用 (2026-09 适配版).

Hermes v0.21 (2026-09-06) 把单文件 gateway/run.py 拆成 facade + siblings：
  run_turn.py            — GatewayTurnMixin（回合生命周期）
  run_turn_runner.py     — TurnRunner（流式回调装配 _wire_turn_agent_callbacks）
  run_inbound.py         — GatewayInboundMixin（入站 _handle_message）
  run_startup.py         — GatewayStartupMixin（gateway:startup）
  run_notifications.py   — GatewayNotificationsMixin（bg watcher）
  cron/scheduler_delivery.py — cron 投递（原 scheduler.py 内函数拆出）

适配策略（ADAPT_NEW_GATEWAY.md）：
- 5 个流式 hook（tool/answer/thinking/reasoning/background_review）不再分散注入
  各回调函数体，而是收敛到 TurnRunner._wire_turn_agent_callbacks 方法尾部：
  每回合该方法先由 Hermes 装配 agent.X_callback，我们在方法尾包 wrapper 覆盖，
  语义与旧版"每回合绑定"完全一致且更抗漂移。
- 生命周期 hook（normalize/start/complete/abort/followup）注入到对应 mixin 方法。
- adapter_init / bg_watcher / cron 分别注入到各自归属文件。
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import hermes_home

_logger = logging.getLogger("hermes_lark_streaming")


PREFIX = "HERMES_LARK"

_HOOK_NAMES = [
    "NORMALIZE",
    "START",
    "COMPLETE",
    "FOLLOWUP_COMPLETE",
    "FOLLOWUP_RESULT",
    "TOOL",
    "ANSWER",
    "ANSWER_GUARD",
    "THINKING",
    "REASONING",
    "BACKGROUND_REVIEW",
    "ABORT",
    "INTERRUPT",
    "BG_DELIVER",
    "ADAPTER_INIT",
    "HEARTBEAT",
]
MARKERS: list[tuple[str, str]] = [(f"# {PREFIX}_{n}_BEGIN", f"# {PREFIX}_{n}_END") for n in _HOOK_NAMES]

MK_NORMALIZE, MK_NORMALIZE_END = MARKERS[0]
MK_START, MK_START_END = MARKERS[1]
MK_COMPLETE, MK_COMPLETE_END = MARKERS[2]
MK_FOLLOWUP_COMPLETE, MK_FOLLOWUP_COMPLETE_END = MARKERS[3]
MK_FOLLOWUP_RESULT, MK_FOLLOWUP_RESULT_END = MARKERS[4]
MK_TOOL, MK_TOOL_END = MARKERS[5]
MK_ANSWER, MK_ANSWER_END = MARKERS[6]
MK_ANSWER_GUARD, MK_ANSWER_GUARD_END = MARKERS[7]
MK_THINKING, MK_THINKING_END = MARKERS[8]
MK_REASONING, MK_REASONING_END = MARKERS[9]
MK_BACKGROUND_REVIEW, MK_BACKGROUND_REVIEW_END = MARKERS[10]
MK_ABORT, MK_ABORT_END = MARKERS[11]
MK_INTERRUPT, MK_INTERRUPT_END = MARKERS[12]
MK_BG_DELIVER, MK_BG_DELIVER_END = MARKERS[13]
MK_ADAPTER_INIT, MK_ADAPTER_INIT_END = MARKERS[14]
MK_HEARTBEAT, MK_HEARTBEAT_END = MARKERS[15]

# bg_watcher 注入点 12（background watcher text-only 通知覆盖）拆成两处独立 marker：
# ① finished（进程完成通知）② running（still-running 进度推送）。新版两分支都走
# GatewayNotificationsMixin._send_watcher_message，收敛到该方法内注入。
MK_BG_WATCHER_FINISHED = f"# {PREFIX}_BG_WATCHER_FINISHED_BEGIN"
MK_BG_WATCHER_FINISHED_END = f"# {PREFIX}_BG_WATCHER_FINISHED_END"
MK_BG_WATCHER_RUNNING = f"# {PREFIX}_BG_WATCHER_RUNNING_BEGIN"
MK_BG_WATCHER_RUNNING_END = f"# {PREFIX}_BG_WATCHER_RUNNING_END"

_BG_WATCHER_PAIRS: tuple[tuple[str, str], ...] = (
    (MK_BG_WATCHER_FINISHED, MK_BG_WATCHER_FINISHED_END),
    (MK_BG_WATCHER_RUNNING, MK_BG_WATCHER_RUNNING_END),
)
MARKERS.extend(_BG_WATCHER_PAIRS)

MK_CRON_DELIVER = f"# {PREFIX}_CRON_DELIVER_BEGIN"
MK_CRON_DELIVER_END = f"# {PREFIX}_CRON_DELIVER_END"

_BACKUP_SUFFIX = ".hermes_lark.bak"


def _valid_source(path: Path) -> Path | None:
    try:
        candidate = path.resolve()
        if candidate.is_file() and candidate.suffix == ".py":
            return candidate
    except (OSError, RuntimeError):
        pass
    return None


def _module_to_path(module_name: str) -> Path:
    """gateway.run → gateway/run.py."""
    return Path(*module_name.split(".")).with_suffix(".py")


# 候选代码根目录，按优先级排列（来源: Hermes 官方安装文档 Install Layout）。
# - per-user git installer: <HERMES_HOME>/hermes-agent/
# - root-mode (sudo curl|bash): /usr/local/lib/hermes-agent/
def _code_roots() -> list[Path]:
    return [hermes_home() / "hermes-agent", Path("/usr/local/lib/hermes-agent")]


# venv 内 python 解释器候选（覆盖 venv/.venv 命名变体）。
_VENV_PYTHONS: tuple[tuple[str, ...], ...] = (
    ("venv", "bin", "python3"),
    ("venv", "bin", "python"),
    (".venv", "bin", "python3"),
    (".venv", "bin", "python"),
)


def _python_from_hermes_cli() -> Path | None:
    """从 which hermes 反推 venv 内的 python3。"""
    cli = shutil.which("hermes")
    if cli is None:
        return None
    cli_path = Path(cli)
    # 读脚本内容，依次试 exec 行(bash wrapper) 和 shebang(console_scripts)
    try:
        text = cli_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    # 1. bash wrapper: exec "venv/bin/hermes" → 同目录 python3
    m = re.search(r'''exec\s+["']([^"']+)["']''', text)
    if m:
        venv_bin = Path(m.group(1)).parent  # venv/bin
        for name in ("python3", "python"):
            py = venv_bin / name
            if py.exists():
                return py
    # 2. console_scripts: shebang #!/path/to/python3 直接指向 python
    m = re.match(r'^#!\s*(\S+)', text)
    if m:
        py = Path(m.group(1))
        if py.exists() and "python" in py.name.lower():
            return py
    return None


def hermes_python() -> Path | None:
    """定位 Hermes 的 Python: which hermes 优先, _code_roots 兜底."""
    # 1. which hermes (覆盖所有官方安装方式，跨平台)
    if py := _python_from_hermes_cli():
        return py
    # 2. 兜底: 已知代码根下的 venv
    for root in _code_roots():
        for parts in _VENV_PYTHONS:
            py = root.joinpath(*parts)
            if py.exists():
                return py
    return None


def hermes_install_dir() -> Path | None:
    """定位 Hermes 安装目录 (含 gateway/run.py): hermes_constants 优先, _code_roots 兜底."""
    # 1. 用 Hermes Python 调用官方 API (single source of truth)
    py = hermes_python()
    if py is not None:
        try:
            result = subprocess.run(
                [str(py), "-c", "from hermes_constants import get_hermes_home; print(get_hermes_home())"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            _logger.debug("hermes_constants lookup failed", exc_info=True)
        else:
            if result.returncode == 0:
                home = Path(result.stdout.strip())
                install = home / "hermes-agent"
                if install.exists():
                    return install
    # 2. 兜底: _code_roots 里含 gateway/run.py 的那个
    rel = _module_to_path("gateway.run")
    for root in _code_roots():
        if (root / rel).exists():
            return root
    return None


def _resolve_module_path(module_name: str, roots: list[Path]) -> Path:
    """定位 Hermes 模块文件，候选代码根优先，importlib 兜底."""
    rel = _module_to_path(module_name)
    for root in roots:
        if candidate := _valid_source(root / rel):
            return candidate

    package = module_name.partition(".")[0]
    try:
        spec = importlib.util.find_spec(package)
        locations = spec.submodule_search_locations if spec else None
        # submodule_search_locations 指向包目录（如 .../gateway），
        # 因此需剥掉包名前缀，得到包内子路径。
        in_pkg = rel.relative_to(Path(package)) if rel.parts[0] == package else rel
        for location in locations or []:
            if candidate := _valid_source(Path(location) / in_pkg):
                return candidate
    except Exception:
        _logger.debug("Failed to resolve Hermes module %s", module_name, exc_info=True)
    return roots[0] / rel if roots else rel


# ── 2026-09 Hermes facade/siblings 重构：注入目标多文件化 ────────────────────
_GATEWAY_TURN_FILE = "gateway.run_turn"
_GATEWAY_RUNNER_FILE = "gateway.run_turn_runner"
_GATEWAY_INBOUND_FILE = "gateway.run_inbound"
_GATEWAY_STARTUP_FILE = "gateway.run_startup"
_GATEWAY_NOTIF_FILE = "gateway.run_notifications"
_CRON_DELIVERY_FILE = "cron.scheduler_delivery"


def _module_path(module_name: str) -> Path:
    """模块名 → 绝对文件路径（code roots 内）."""
    return _resolve_module_path(module_name, _code_roots())


def _default_run_path() -> Path:
    # 兼容旧名：主注入文件现在按 Hermes 形态自动判定（facade 有 _handle_message_with_agent
    # 时为单文件旧版；否则走新版多文件）。此处仅保留 run.py 路径供 status/自愈使用。
    return _module_path("gateway.run")


def _default_turn_path() -> Path:
    return _module_path(_GATEWAY_TURN_FILE)


def _default_turn_runner_path() -> Path:
    return _module_path(_GATEWAY_RUNNER_FILE)


def _default_inbound_path() -> Path:
    return _module_path(_GATEWAY_INBOUND_FILE)


def _default_startup_path() -> Path:
    return _module_path(_GATEWAY_STARTUP_FILE)


def _default_notif_path() -> Path:
    return _module_path(_GATEWAY_NOTIF_FILE)


def _default_cron_path() -> Path:
    # 旧 scheduler.py 的 _deliver_result 已拆到 scheduler_delivery.py（0.21+）。
    return _module_path(_CRON_DELIVERY_FILE)


# 注入点锚定（字符串）——新版按文件分组。
# 每个条目: (文件名函数, hook 注入定位函数名, [依赖的字符串锚点], label)
# 具体定位逻辑在各 _find_*_site 函数中按文件实现。


def _make_hook(indent: str, begin: str, end: str, body_lines: list[str]) -> str:
    return f"{indent}{begin}\n" + "".join(f"{indent}{line}\n" for line in body_lines) + f"{indent}{end}\n"


# ════════════════════════════════════════════════════════════════════════════
# Hook 代码生成（新版注入形态）
#
# TurnRunner 方法内注入的代码通过 self._ctx 访问 TurnContext：
#   self._ctx.source / self._ctx.event_message_id / self._ctx._run_still_current()
# GatewayTurnMixin 方法内 source/event 等是形参。
# ════════════════════════════════════════════════════════════════════════════


def _feishu_normalize_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_NORMALIZE,
        MK_NORMALIZE_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_feishu_normalize",
            "    on_feishu_normalize(",
            "        message_id=event.message_id,",
            "        source=source,",
            "        event=event,",
            "        reply_anchor_id=self._reply_anchor_for_event(event),",
            "    )",
            "except Exception:",
            "    pass",
        ],
    )


def _start_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_START,
        MK_START_END,
        [
            "try:",
            "    if source.platform.value.lower() in ('feishu', 'lark'):",
            "        from hermes_lark_streaming.patch import on_message_started",
            "        _lark_anchor_id = self._reply_anchor_for_event(event)",
            "        on_message_started(",
            "            message_id=event.message_id,",
            "            chat_id=source.chat_id,",
            "            anchor_id=_lark_anchor_id,",
            "            thread_id=getattr(source, 'thread_id', None),",
            "        )",
            "except Exception:",
            "    pass",
        ],
    )


def _complete_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_COMPLETE,
        MK_COMPLETE_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_message_completed_wait, on_message_needs_text_fallback",
            "    from hermes_lark_streaming.patch import collect_image_media",
            "    _lark_completion_id = agent_result.get('_hermes_lark_completion_id') or event.message_id",
            "    _lark_image_paths = collect_image_media(agent_messages)",
            "    _lark_card_sent = await on_message_completed_wait(",
            "        message_id=_lark_completion_id,",
            "        chat_id=source.chat_id,",
            "        answer=response,",
            "        duration=_turn_seconds,",
            "        model=agent_result.get('model', ''),",
            "        tokens={",
            "            'input_tokens': agent_result.get('input_tokens', 0),",
            "            'output_tokens': agent_result.get('output_tokens', 0),",
            "        },",
            "        context={",
            "            'used_tokens': agent_result.get('last_prompt_tokens', 0),",
            "            'max_tokens': agent_result.get('context_length', 0),",
            "        },",
            "        image_paths=_lark_image_paths,",
            "    )",
            "    if _lark_card_sent:",
            "        agent_result['already_sent'] = True",
            "        if _lark_image_paths:",
            "            response = ''  # 图已进卡片，清 response 避免 Hermes _deliver_media_from_response 重复发",
            "    elif on_message_needs_text_fallback(message_id=_lark_completion_id):",
            "        agent_result.pop('already_sent', None)",
            "except Exception:",
            "    pass",
        ],
    )


def _followup_complete_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_FOLLOWUP_COMPLETE,
        MK_FOLLOWUP_COMPLETE_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_queued_followup_boundary",
            "    await on_queued_followup_boundary(message_id=event_message_id, result=result)",
            "except Exception:",
            "    pass",
        ],
    )


def _followup_result_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_FOLLOWUP_RESULT,
        MK_FOLLOWUP_RESULT_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_queued_followup_result",
            "    _lark_followup_completion_id = next_message_id or getattr(pending_event, 'message_id', None)",
            "    if _lark_followup_completion_id:",
            "        on_queued_followup_result(",
            "            message_id=_lark_followup_completion_id,",
            "            followup_result=followup_result,",
            "        )",
            "except Exception:",
            "    pass",
        ],
    )


def _tool_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_TOOL,
        MK_TOOL_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_tool_updated",
            "    if self._ctx._run_still_current() and event_type in ('tool.started', 'tool.completed'):",
            "        if on_tool_updated(",
            "            message_id=self._ctx.event_message_id,",
            "            chat_id=self._ctx.source.chat_id,",
            "            tool_name=tool_name or '',",
            "            status='started' if event_type == 'tool.started' else 'completed',",
            "            detail=preview or '',",
            "        ):",
            "            return",
            "except Exception:",
            "    pass",
        ],
    )


def _answer_hook(indent: str) -> str:
    # 新版 (0.21+)：answer delta 由 ANSWER_GUARD wrapper 统一接管（guard 在
    # Hermes 装配 stream_delta_callback 后包一层，无论原生流式开关都先调
    # on_answer_delta）。本 ANSWER marker 保留为空壳说明块——避免双份 wrapper
    # 造成双发，同时让 status/is_fully_patched 对 MARKERS 的检查保持全绿。
    return _make_hook(
        indent,
        MK_ANSWER,
        MK_ANSWER_END,
        [
            "# [0.21+] answer delta handled by ANSWER_GUARD wrapper below "
            "(on_answer_delta called before native stream_callback).",
        ],
    )


def _answer_guard_hook(indent: str) -> str:
    """兜底注入：在 agent.stream_delta_callback 装配后包一层 wrapper。

    无论 Hermes 原生是否启用流式（stream_delta_cb 是否 None），wrapper 都先调
    on_answer_delta（进卡片）；卡片处理了就 return（不同步原生流，根治 streaming=true
    双发）；否则 fallback 到原 callback（Hermes 原生流式或 None）。
    """
    return _make_hook(
        indent,
        MK_ANSWER_GUARD,
        MK_ANSWER_GUARD_END,
        [
            "_hermes_lark_orig_delta_cb = agent.stream_delta_callback",
            "def _hermes_lark_guarded_delta_cb(text):",
            "    try:",
            "        from hermes_lark_streaming.patch import on_answer_delta",
            (
                "        if text and self._ctx._run_still_current() and on_answer_delta("
                "message_id=self._ctx.event_message_id, chat_id=self._ctx.source.chat_id, text=text):"
            ),
            "            return",
            "    except Exception:",
            "        pass",
            "    if _hermes_lark_orig_delta_cb is not None:",
            "        _hermes_lark_orig_delta_cb(text)",
            "agent.stream_delta_callback = _hermes_lark_guarded_delta_cb",
        ],
    )


def _thinking_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_THINKING,
        MK_THINKING_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_thinking_delta",
            "    if (text and not already_streamed and self._ctx._run_still_current()",
            "            and on_thinking_delta(message_id=self._ctx.event_message_id,",
            "                                  chat_id=self._ctx.source.chat_id, text=text)):",
            "        return",
            "except Exception:",
            "    pass",
        ],
    )


def _reasoning_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_REASONING,
        MK_REASONING_END,
        [
            "def _reasoning_cb(text):",
            "    if text and self._ctx._run_still_current():",
            "        try:",
            "            from hermes_lark_streaming.patch import on_reasoning_delta",
            "            on_reasoning_delta(message_id=self._ctx.event_message_id,",
            "                                 chat_id=self._ctx.source.chat_id, text=text)",
            "        except Exception:",
            "            pass",
            "agent.reasoning_callback = _reasoning_cb",
        ],
    )


def _background_review_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_BACKGROUND_REVIEW,
        MK_BACKGROUND_REVIEW_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_background_review_message",
            "    _lark_bg_review_sender = agent.background_review_callback",
            "    def _lark_bg_review_callback(message):",
            "        _lark_bg_review_deferred = on_background_review_message(",
            "            message_id=self._ctx.event_message_id,",
            "            text=message,",
            "            sender=_lark_bg_review_sender,",
            "        )",
            "        if not _lark_bg_review_deferred and _lark_bg_review_sender is not None:",
            "            _lark_bg_review_sender(message)",
            "    agent.background_review_callback = _lark_bg_review_callback",
            "except Exception:",
            "    pass",
        ],
    )


def _abort_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_ABORT,
        MK_ABORT_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_message_aborted",
            "    on_message_aborted(message_id=event.message_id)",
            "except Exception:",
            "    pass",
        ],
    )


def _interrupt_hook(indent: str) -> str:
    # 旧版（run.py 单文件、_run_agent 闭包）形态：was_interrupted/pending_event/
    # next_message_id/next_source/event_message_id 均在作用域。
    return _make_hook(
        indent,
        MK_INTERRUPT,
        MK_INTERRUPT_END,
        [
            "try:",
            "    if source.platform.value.lower() in ('feishu', 'lark'):",
            "        from hermes_lark_streaming.patch import (",
            "            on_message_aborted, on_message_interrupted, on_message_started,",
            "        )",
            "        _lark_next_message_id = getattr(pending_event, 'message_id', None) or next_message_id",
            "        _lark_next_anchor_id = next_message_id",
            "        if was_interrupted and _lark_next_message_id:",
            "            on_message_interrupted(",
            "                message_id=event_message_id,",
            "                new_message_id=_lark_next_message_id,",
            "                chat_id=source.chat_id,",
            "                anchor_id=_lark_next_anchor_id,",
            "            )",
            "        elif was_interrupted:",
            "            on_message_aborted(message_id=event_message_id)",
            "        elif pending_event is not None and _lark_next_message_id:",
            "            on_message_started(",
            "                message_id=_lark_next_message_id,",
            "                chat_id=getattr(next_source, 'chat_id', source.chat_id),",
            "                anchor_id=_lark_next_anchor_id,",
            "            )",
            "except Exception:",
            "    pass",
        ],
    )


def _interrupt_hook_v2(indent: str) -> str:
    # 新版（run_turn.py GatewayTurnMixin._run_agent_queued_followup）形态：
    # 形参 turn_ctx/result/pending_event/source；anchor 已在 3574 行由
    # next_message_id = self._reply_anchor_for_event(pending_event) 算出。
    # 注入在 typing-restart 注释前（3588），next_message_id/next_source 已就绪。
    return _make_hook(
        indent,
        MK_INTERRUPT,
        MK_INTERRUPT_END,
        [
            "try:",
            "    if source.platform.value.lower() in ('feishu', 'lark'):",
            "        from hermes_lark_streaming.patch import (",
            "            on_message_aborted, on_message_interrupted, on_message_started,",
            "        )",
            "        _lark_next_message_id = next_message_id or getattr(pending_event, 'message_id', None)",
            "        _lark_next_anchor_id = next_message_id",
            "        if result.get('interrupted') and _lark_next_message_id:",
            "            on_message_interrupted(",
            "                message_id=turn_ctx.event_message_id,",
            "                new_message_id=_lark_next_message_id,",
            "                chat_id=source.chat_id,",
            "                anchor_id=_lark_next_anchor_id,",
            "            )",
            "        elif result.get('interrupted'):",
            "            on_message_aborted(message_id=turn_ctx.event_message_id)",
            "        elif pending_event is not None and _lark_next_message_id:",
            "            on_message_started(",
            "                message_id=_lark_next_message_id,",
            "                chat_id=getattr(next_source, 'chat_id', source.chat_id),",
            "                anchor_id=_lark_next_anchor_id,",
            "            )",
            "except Exception:",
            "    pass",
        ],
    )


def _followup_complete_hook_v2(indent: str) -> str:
    # 新版：_run_agent_inner 的 pending 分支（if pending_event or pending:）内、
    # 递归 _run_agent_queued_followup 之前 finalize 当前回合卡片。
    return _make_hook(
        indent,
        MK_FOLLOWUP_COMPLETE,
        MK_FOLLOWUP_COMPLETE_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_queued_followup_boundary",
            "    await on_queued_followup_boundary(message_id=event_message_id, result=result)",
            "except Exception:",
            "    pass",
        ],
    )


def _cron_deliver_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_CRON_DELIVER,
        MK_CRON_DELIVER_END,
        [
            "try:",
            "    if _hermes_lark_cron_target_feishu(target):",
            "        if '_hermes_lark_cron_seen' not in locals():",
            "            _hermes_lark_cron_seen = set()",
            "        _hermes_lark_cron_key = (str(target['chat_id']), cleaned_delivery_content.strip())",
            "        if _hermes_lark_cron_key in _hermes_lark_cron_seen:",
            "            continue",
            "        from hermes_lark_streaming.patch import on_cron_deliver",
            "        if on_cron_deliver(",
            "            chat_id=target['chat_id'],",
            "            content=cleaned_delivery_content.strip(),",
            "            loop=loop,",
            "            task_name=job.get('name', ''),",
            "            run_time=job.get('next_run_at', ''),",
            "            job_id=job.get('id', ''),",
            "        ):",
            "            _hermes_lark_cron_seen.add(_hermes_lark_cron_key)",
            "            delivered = True",
            "            continue",
            "except Exception:",
            "    pass",
        ],
    )


def _bg_deliver_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_BG_DELIVER,
        MK_BG_DELIVER_END,
        [
            "try:",
            "    if source.platform.value.lower() in ('feishu', 'lark') and response:",
            "        from hermes_lark_streaming.patch import on_background_deliver",
            "        _bg_preview = prompt[:60] + ('...' if len(prompt) > 60 else '')",
            "        if await on_background_deliver(",
            "            chat_id=source.chat_id,",
            "            preview=_bg_preview,",
            "            content=text_content,",
            "            reply_to_message_id=event_message_id,",
            "        ):",
            "            text_content = ''",
            "            if not images and not media_files:",
            "                return",
            "except Exception:",
            "    pass",
        ],
    )


def _adapter_init_hook(indent: str) -> str:
    # 在 gateway:startup emit 之后 patch（所有 adapter 已 connect，event_handler 已建）。
    return _make_hook(
        indent,
        MK_ADAPTER_INIT,
        MK_ADAPTER_INIT_END,
        [
            "try:",
            "    from hermes_lark_streaming.clarify import patch_feishu_adapter as _hermes_lark_clarify_patch",
            "    _hermes_lark_clarify_patch(self.adapters)",
            "except Exception:",
            "    pass",
        ],
    )


def _heartbeat_hook(indent: str) -> str:
    # 新版：注入在 _run_agent_notify_long_running 心跳文本组装后、原生 edit/send 前。
    # 作用域有 source / turn_ctx / _heartbeat_text（async 方法内，可 await）。
    # on_heartbeat 返回 True = 卡片已接管 → continue 跳过原生心跳消息循环体；
    # False = 卡片不可用 → 走原逻辑（保底）。
    return _make_hook(
        indent,
        MK_HEARTBEAT,
        MK_HEARTBEAT_END,
        [
            "try:",
            "    if source.platform.value.lower() in ('feishu', 'lark'):",
            "        from hermes_lark_streaming.patch import on_heartbeat",
            "        if on_heartbeat(",
            "            message_id=turn_ctx.event_message_id,",
            "            chat_id=source.chat_id,",
            "            text=_heartbeat_text,",
            "        ):",
            "            continue",
            "except Exception:",
            "    pass",
        ],
    )


def _bg_watcher_hook(indent: str, begin: str, end: str) -> str:
    # 新版两分支都收敛到 _send_watcher_message：finished 文本 / running 进度文本。
    # 通过 message_text 内容区分。handled=True 时跳过原 adapter.send（卡片接管）。
    return _make_hook(
        indent,
        begin,
        end,
        [
            "_hermes_lark_bg_handled = False",
            "try:",
            "    if (platform_name or '').lower() in ('feishu', 'lark'):",
            "        from hermes_lark_streaming.patch import on_bg_watcher_notify",
            "        if await on_bg_watcher_notify(chat_id=chat_id, content=message_text):",
            "            _hermes_lark_bg_handled = True",
            "except Exception:",
            "    pass",
        ],
    )


def _bg_watcher_finished_hook(indent: str) -> str:
    return _bg_watcher_hook(indent, MK_BG_WATCHER_FINISHED, MK_BG_WATCHER_FINISHED_END)


def _bg_watcher_running_hook(indent: str) -> str:
    return _bg_watcher_hook(indent, MK_BG_WATCHER_RUNNING, MK_BG_WATCHER_RUNNING_END)


def _remove_block(content: str, begin: str, end: str) -> str:
    lines = content.splitlines(keepends=True)
    begin_idx = end_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == begin:
            begin_idx = i
        if stripped == end:
            end_idx = i
            break
    if begin_idx is not None and end_idx is not None:
        return "".join(lines[:begin_idx] + lines[end_idx + 1 :])
    return content


def _atomic_write(path: Path, content: str) -> None:
    """原子写入：先写临时文件再 rename，防止崩溃时文件损坏."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, dir=str(path.parent), prefix=".hermes_lark_", mode="w", encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
        shutil.copymode(path, tmp_path)
        os.replace(str(tmp_path), str(path))
    except BaseException:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


class PatcherError(RuntimeError):
    pass


# ════════════════════════════════════════════════════════════════════════════
# 新版注入点定位（多文件）
#
# 每个定位函数返回 (0-indexed 行号, 缩进) 或 None。
# 定位失败 = verify 失败（防位置漂移致重复消息）。
# ════════════════════════════════════════════════════════════════════════════


def _safe_indent(lines: list[str], lineno: int) -> str:
    """获取缩进，跳过空行."""
    for i in range(lineno, -1, -1):
        if 0 <= i < len(lines) and lines[i].strip():
            return lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    for i in range(lineno + 1, len(lines)):
        if lines[i].strip():
            return lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    return ""


def _find_method_first_stmt(tree: ast.Module, lines: list[str], method: str) -> tuple[int, str] | None:
    """定位类方法体第一条真实语句（跳过 docstring）。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == method:
            body = node.body
            start = 0
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                start = 1
            if start < len(body):
                lineno = body[start].lineno - 1
                indent = _safe_indent(lines, lineno)
                return lineno, indent
    return None


def _find_method_last_stmt(tree: ast.Module, lines: list[str], method: str) -> tuple[int, str] | None:
    """定位类方法体最后一条语句之后的位置（注入到方法尾）。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == method:
            if not node.body:
                return None
            last = node.body[-1]
            lineno = (last.end_lineno or last.lineno) - 1
            indent = _safe_indent(lines, lineno)
            return lineno, indent
    return None


def _find_stmt_after(tree: ast.Module, lines: list[str], needle: str) -> tuple[int, str] | None:
    """定位某行字符串之后的插入位（下一行，同一缩进）。AST 兜底用行匹配。"""
    for i, line in enumerate(lines):
        if needle in line:
            indent = _safe_indent(lines, i)
            return i, indent  # 语义：调用方决定插到 i+1（用行级重排）
    return None


def _line_after(lines: list[str], idx: int) -> tuple[int, str] | None:
    if idx + 1 < len(lines):
        return idx + 1, _safe_indent(lines, idx)
    return None


# ── run_turn.py (GatewayTurnMixin) 定位 ──────────────────────────────────────

def _find_turn_start_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    # _handle_message_with_agent 方法体开头（agent 回合真正开始处）。
    return _find_method_first_stmt(tree, lines, "_handle_message_with_agent")


def _find_turn_complete_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    """回合完成 finalize 卡片：_handle_message_with_agent 里 deliver 调用之前。

    选这里而不是 _hmwa_deliver_turn_response 开头，是因为 complete hook 需要
    ``_turn_seconds``/``response``/``agent_result``/``agent_messages``/``event``/``source``
    全在作用域内；且 persist 已发生（库里保留全文），complete 成功后设
    ``agent_result['already_sent']`` 并清 ``response``，deliver 看到 already_sent
    走 streamed 分支不再重复发 body。
    """
    for i, line in enumerate(lines):
        if "return await self._hmwa_deliver_turn_response(" in line:
            indent = _safe_indent(lines, i)
            return i, indent
    return None


def _find_turn_abort_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    """stale 结果丢弃（abort 通知）：调用点 return None 之前（作用域有 event）。

    返回 return None 行，注入用 before（插到该行前 = discard 调用后）。
    """
    for i, line in enumerate(lines):
        if "self._hmwa_discard_stale_result(" in line:
            for j in range(i, min(i + 6, len(lines))):
                if lines[j].strip() == "return None":
                    indent = _safe_indent(lines, j)
                    return j, indent
            indent = _safe_indent(lines, i)
            return i + 1, indent
    return None


def _find_turn_bg_deliver_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    # _run_background_task_inner: extract_images 之后。
    for i, line in enumerate(lines):
        if "images, text_content = adapter.extract_images(response)" in line:
            return _line_after(lines, i)
    # fallback: 方法内 images/media_files/text_content 初始化行后
    for i, line in enumerate(lines):
        if "images, media_files, text_content = [], [], \"\"" in line:
            return _line_after(lines, i)
    return None


def _find_turn_queued_followup_return_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    # _run_agent_queued_followup: return _preserve_queued_followup_history_offset(...) 前。
    for i, line in enumerate(lines):
        if "return _preserve_queued_followup_history_offset(result, followup_result)" in line.strip():
            indent = _safe_indent(lines, i)
            return i, indent
    return None


def _find_turn_followup_boundary_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    """新版 queued followup 边界：_run_agent_inner 中 ``if pending_event or pending:`` 分支内。

    该分支递归 _run_agent_queued_followup 前应 finalize 当前回合卡片。
    返回分支体内 ``return await self._run_agent_queued_followup(`` 行，注入用 before
    （插到该行前 = 分支体内、递归前；缩进即分支体缩进）。
    """
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "if pending_event or pending:":
            for j in range(i + 1, min(i + 5, len(lines))):
                if "return await self._run_agent_queued_followup(" in lines[j]:
                    indent = _safe_indent(lines, j)
                    return j, indent  # before → 插到 j 行前（分支体内）
    return None


def _find_turn_interrupt_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    """新版 interrupt：_run_agent_queued_followup 内 typing-restart 注释前。

    该点 result/pending_event/source/next_message_id/next_source 已全部就绪
    （queued followup 会重开 typing），注入用 before（行前）。
    """
    for i, line in enumerate(lines):
        if "Restart the typing indicator" in line and "outer typing task may be stale" in line:
            indent = _safe_indent(lines, i)
            return i, indent
    # fallback: Restart typing indicator
    for i, line in enumerate(lines):
        if "Restart the typing indicator" in line:
            indent = _safe_indent(lines, i)
            return i, indent
    return None


def _find_turn_heartbeat_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    """新版长回合心跳：_run_agent_notify_long_running 心跳文本组装后、原生 edit/send 前。

    ``_heartbeat_text = (`` 是 4 行多行赋值（f-string），用 AST 找该 Assign 语句
    的 end_lineno，注入用 after（行后 = 原生 try 之前）。
    作用域：source / turn_ctx / _heartbeat_text 全可用（async 方法）。
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_run_agent_notify_long_running":
            for stmt in ast.walk(node):
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == "_heartbeat_text"
                ):
                    lineno = (stmt.end_lineno or stmt.lineno) - 1
                    indent = _safe_indent(lines, lineno)
                    return lineno, indent
    # fallback: 字符串行
    for i, line in enumerate(lines):
        if "_heartbeat_text = (" in line:
            # 往后找收尾 ")"
            for j in range(i, min(i + 10, len(lines))):
                if lines[j].strip() == ")":
                    indent = _safe_indent(lines, j)
                    return j, indent
    return None


# ── run_turn_runner.py (TurnRunner) 定位 ─────────────────────────────────────

def _find_runner_wire_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    """定位 _wire_turn_agent_callbacks 方法尾（回调装配完成后）。

    返回方法体最后一条语句所在行；调用方把各 hook 按倒序插到该行后。
    """
    return _find_method_last_stmt(tree, lines, "_wire_turn_agent_callbacks")


# ── run_inbound.py (GatewayInboundMixin) 定位 ────────────────────────────────

def _find_inbound_normalize_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    """_handle_message 中 event/source 解包之后（normalize 需要 source/event 已就位）。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_handle_message":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Tuple)
                ):
                    names = [getattr(t, "id", None) for t in stmt.targets[0].elts]
                    if names == ["event", "source", "is_internal"]:
                        lineno = stmt.end_lineno or stmt.lineno
                        return lineno, _safe_indent(lines, stmt.lineno - 1)
    # fallback: 行匹配 event, source, is_internal
    for i, line in enumerate(lines):
        if "event, source, is_internal = _admitted" in line or (
            "event, source, is_internal" in line and "=" in line
        ):
            return _line_after(lines, i)
    return None


# ── run_startup.py (GatewayStartupMixin) 定位 ────────────────────────────────

def _find_adapter_init_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    """Locate the gateway:startup emit statement end — insert right after it."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        if call.func.attr != "emit":
            continue
        if not (call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == "gateway:startup"):
            continue
        end = node.end_lineno or call.lineno
        insert_line = end
        if insert_line >= len(lines):
            insert_line = len(lines) - 1
        indent = _safe_indent(lines, insert_line)
        return insert_line, indent
    # fallback 字符串
    for i, line in enumerate(lines):
        if 'hooks.emit("gateway:startup"' in line:
            # 多行调用：在 emit 语句块内找到收尾括号行即可（约 8 行内）。
            for j in range(i, min(i + 8, len(lines))):
                stripped = lines[j].strip()
                if stripped.endswith(")"):
                    return _line_after(lines, j)
            indent = _safe_indent(lines, i)
            return i, indent
    return None


# ── run_notifications.py (GatewayNotificationsMixin) 定位 ────────────────────

def _find_bg_watcher_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    """_send_watcher_message 方法尾（finished/running 两分支都经此发送）。"""
    return _find_method_last_stmt(tree, lines, "_send_watcher_message")


def _guard_line_for_branch(lines: list[str], inject_idx: int) -> int | None:
    """在 inject_idx 之后找要改写的 ``if adapter and chat_id:`` 行索引（旧版专用）。"""
    for j in range(inject_idx, min(inject_idx + 30, len(lines))):
        if lines[j].strip() == "if adapter and chat_id:":
            return j
    return None


def _apply_bg_watcher_guard(lines: list[str], inject_idx: int) -> list[str]:
    """把原 ``if adapter and chat_id:`` 改写为 ``if adapter and chat_id and not _hermes_lark_bg_handled:``."""
    for j in range(inject_idx, min(inject_idx + 30, len(lines))):
        if lines[j].strip() == "if adapter and chat_id:":
            indent = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
            lines[j] = f"{indent}if adapter and chat_id and not _hermes_lark_bg_handled:\n"
            return lines
    return lines


# ════════════════════════════════════════════════════════════════════════════
# Patcher — 多文件目标管理
# ════════════════════════════════════════════════════════════════════════════


class Patcher:
    """管理 AST 注入的安装和移除（Hermes ≥0.21 多文件架构）。"""

    MARKERS: list[tuple[str, str]] = MARKERS

    def __init__(self, run_path: Path | None = None) -> None:
        # 兼容旧签名：run_path 传入时只影响 run.py 目标；默认自动探测形态。
        # 新版按文件收集目标。找不到主文件时仍报错提示。
        self.run_path = run_path or _default_run_path()
        if not (self.run_path.exists() or _default_turn_path().exists()):
            tried = ", ".join(str(r) for r in _code_roots())
            raise PatcherError(
                f"gateway files not found (tried: {tried}). "
                f"Set HERMES_HOME to the dir containing hermes-agent/ and rerun."
            )
        # 形态探测：新版特征 = gateway/run_turn.py 存在且含 class GatewayTurnMixin
        # （旧版单文件 run.py 无 sibling）。用文件存在性而非 run.py 内容，避免
        # facade import/注释里的函数名字样误判。
        self.is_legacy_layout = True
        turn_path = _default_turn_path()
        if turn_path.exists():
            try:
                turn_content = turn_path.read_text(encoding="utf-8")
                if "class GatewayTurnMixin" in turn_content:
                    self.is_legacy_layout = False
            except OSError:
                pass
        self.targets: list[Path] = self._collect_targets()

    def _collect_targets(self) -> list[Path]:
        if self.is_legacy_layout:
            return [self.run_path]
        paths = [
            _default_turn_path(),
            _default_turn_runner_path(),
            _default_inbound_path(),
            _default_startup_path(),
            _default_notif_path(),
        ]
        return [p for p in paths if p.exists()]

    def _all_target_contents(self) -> dict[Path, str]:
        out: dict[Path, str] = {}
        for p in self.targets:
            with contextlib.suppress(OSError):
                out[p] = p.read_text(encoding="utf-8")
        return out

    def is_patched(self) -> bool:
        return any(
            MK_START in content or MK_ANSWER_GUARD in content or MK_CRON_DELIVER in content
            for content in self._all_target_contents().values()
        )

    def is_fully_patched(self) -> bool:
        contents = self._all_target_contents()
        all_text = "\n".join(contents.values()) if contents else ""
        # 按布局取实际注入的 marker 组：HEARTBEAT 只在新版 run_turn.py 注入，
        # legacy 单文件布局不含它（legacy 心跳注入点不存在），不能要求它齐全。
        markers = self.MARKERS
        if self.is_legacy_layout:
            markers = [
                (b, e)
                for b, e in self.MARKERS
                if b not in (MK_HEARTBEAT, MK_HEARTBEAT_END)
            ]
        return all(begin in all_text and end in all_text for begin, end in markers)

    # ── verify ────────────────────────────────────────────────────────────

    def verify_target(self) -> None:
        # 已完整补丁的文件是"产物"：marker 齐全本身就是兼容证据（注入时已对干净
        # 文件验证过锚点）。若在此状态重跑定位器会失败——注入代码（如 followup
        # boundary hook）会插入到两个锚点行之间，破坏 finder 的行间扫描。升级覆盖
        # 文件会清掉 marker → is_fully_patched False → 走完整验证。
        if self.is_fully_patched():
            return
        if self.is_legacy_layout:
            self._verify_legacy()
            return
        self._verify_turn()
        self._verify_runner()
        self._verify_inbound()
        self._verify_startup()
        self._verify_notifications()
        self._verify_cron()

    def _parse(self, path: Path) -> tuple[ast.Module, list[str]]:
        content = path.read_text(encoding="utf-8")
        return ast.parse(content), content.splitlines(keepends=True)

    def _require_found(self, label: str, loc: tuple[int, str] | None) -> None:
        if loc is None:
            raise PatcherError(f"Cannot find {label} injection site — Hermes version may be incompatible")

    def _verify_turn(self) -> None:
        p = _default_turn_path()
        if not p.exists():
            raise PatcherError(f"Cannot find {p} — Hermes version may be incompatible")
        tree, lines = self._parse(p)
        self._require_found("start", _find_turn_start_site(tree, lines))
        self._require_found("complete", _find_turn_complete_site(tree, lines))
        self._require_found("abort", _find_turn_abort_site(tree, lines))
        self._require_found("bg_deliver", _find_turn_bg_deliver_site(tree, lines))
        self._require_found("queued followup result", _find_turn_queued_followup_return_site(tree, lines))
        self._require_found(
            "queued followup boundary", _find_turn_followup_boundary_site(tree, lines)
        )
        self._require_found("interrupt", _find_turn_interrupt_site(tree, lines))
        self._require_found("heartbeat", _find_turn_heartbeat_site(tree, lines))
        # 字符串锚点存在性
        content = "\n".join(lines)
        for needle, label in (
            ("_handle_message_with_agent", "turn handler"),
            ("_hmwa_deliver_turn_response", "turn deliver"),
            ("_run_background_task_inner", "bg task"),
            ("_preserve_queued_followup_history_offset", "followup return"),
            ("_run_agent_queued_followup", "queued followup"),
            ("_run_agent_notify_long_running", "long-running notify"),
        ):
            if needle not in content:
                raise PatcherError(f"Cannot find {label} in run_turn.py — Hermes version may be incompatible")

    def _verify_runner(self) -> None:
        p = _default_turn_runner_path()
        if not p.exists():
            raise PatcherError(f"Cannot find {p} — Hermes version may be incompatible")
        tree, lines = self._parse(p)
        self._require_found("wire_callbacks tail", _find_runner_wire_site(tree, lines))
        content = "\n".join(lines)
        for needle, label in (
            ("def _wire_turn_agent_callbacks", "wire callbacks"),
            ("agent.stream_delta_callback = stream_delta_cb", "stream_delta assign"),
            ("agent.background_review_callback, bg_release = self._make_bg_review_callbacks()", "bg review assign"),
        ):
            if needle not in content:
                raise PatcherError(f"Cannot find {label} in run_turn_runner.py — Hermes version may be incompatible")

    def _verify_inbound(self) -> None:
        p = _default_inbound_path()
        if not p.exists():
            raise PatcherError(f"Cannot find {p} — Hermes version may be incompatible")
        tree, lines = self._parse(p)
        self._require_found("normalize", _find_inbound_normalize_site(tree, lines))
        content = "\n".join(lines)
        if "def _handle_message" not in content:
            raise PatcherError("Cannot find _handle_message in run_inbound.py — Hermes version may be incompatible")

    def _verify_startup(self) -> None:
        p = _default_startup_path()
        if not p.exists():
            raise PatcherError(f"Cannot find {p} — Hermes version may be incompatible")
        tree, lines = self._parse(p)
        self._require_found("adapter_init", _find_adapter_init_site(tree, lines))

    def _verify_notifications(self) -> None:
        p = _default_notif_path()
        if not p.exists():
            raise PatcherError(f"Cannot find {p} — Hermes version may be incompatible")
        tree, lines = self._parse(p)
        self._require_found("bg_watcher", _find_bg_watcher_site(tree, lines))
        content = "\n".join(lines)
        if "def _send_watcher_message" not in content:
            raise PatcherError(
                "Cannot find _send_watcher_message in run_notifications.py — "
                "Hermes version may be incompatible"
            )

    def _verify_cron(self) -> None:
        p = _default_cron_path()
        if not p.exists():
            raise PatcherError(f"Cannot find {p} — Hermes version may be incompatible")
        content = p.read_text(encoding="utf-8")
        for needle, label in (
            ("def _deliver_result", "deliver_result"),
            ("for target in targets:", "delivery loop"),
            ("cleaned_delivery_content", "cleaned_delivery_content"),
        ):
            if needle not in content:
                raise PatcherError(f"Cannot find {label} in scheduler_delivery.py — Hermes version may be incompatible")

    def _verify_legacy(self) -> None:
        content = self.run_path.read_text(encoding="utf-8")
        tree = ast.parse(content)

        handler = _find_method_first_stmt(tree, content.splitlines(keepends=True), "_handle_message_with_agent")
        if handler is None:
            raise PatcherError("Cannot find _handle_message_with_agent in run.py — Hermes version may be incompatible")

        anchor_found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "emit":
                    hooks_obj = func.value
                    if (
                        isinstance(hooks_obj, ast.Attribute)
                        and hooks_obj.attr == "hooks"
                        and (node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "agent:end")
                    ):
                        anchor_found = True
                        break
        if not anchor_found:
            raise PatcherError(
                "Cannot find hooks.emit('agent:end', ...) anchor in run.py — "
                "Hermes version may be incompatible"
            )

        required_callbacks = {"progress_callback": False, "_stream_delta_cb": False, "_interim_assistant_cb": False}
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in required_callbacks:
                required_callbacks[node.name] = True
        missing = [name for name, found in required_callbacks.items() if not found]
        if missing:
            raise PatcherError(
                f"Missing injection targets in run.py: {', '.join(missing)} — "
                "Hermes version may be incompatible"
            )

        for needle, label in (
            ("_already_sent = bool(", "complete"),
            ("Discarding stale agent result", "abort"),
            ('self.hooks.emit("gateway:startup"', "gateway startup emit"),
        ):
            if needle not in content:
                raise PatcherError(f"Cannot find {label} anchor in run.py — Hermes version may be incompatible")

    # ── apply / remove ────────────────────────────────────────────────────

    def apply(self) -> None:
        if self.is_fully_patched():
            return
        # 部分补丁（某些文件已注入、某些没有）先清理再统一重打；清理后 verify
        # 的是干净文件，锚点定位不受残留 marker 影响。
        if self.is_patched() and not self.is_fully_patched():
            self._remove_new_layout() if not self.is_legacy_layout else self._remove_legacy()
        self.verify_target()
        if self.is_legacy_layout:
            self._apply_legacy()
            return
        self._apply_new_layout()

    def remove(self) -> None:
        if self.is_legacy_layout:
            self._remove_legacy()
            return
        self._remove_new_layout()

    def _apply_new_layout(self) -> None:
        for p in self.targets:
            content = p.read_text(encoding="utf-8")
            had_patch = any(begin in content for begin, _ in self.MARKERS)
            if not had_patch:
                backup = p.with_suffix(p.suffix + _BACKUP_SUFFIX)
                if not backup.exists():
                    shutil.copy2(p, backup)
            for begin, end in self.MARKERS:
                content = _remove_block(content, begin, end)
            content = self._inject_target(p, content)
            _atomic_write(p, content)

    def _remove_new_layout(self) -> None:
        for p in self.targets:
            content = p.read_text(encoding="utf-8")
            new_content = content
            for begin, end in self.MARKERS:
                new_content = _remove_block(new_content, begin, end)
            if new_content != content:
                _atomic_write(p, new_content)

    def restore(self) -> None:
        if self.is_legacy_layout:
            backup = self.run_path.with_suffix(self.run_path.suffix + _BACKUP_SUFFIX)
            if not backup.exists():
                raise PatcherError(f"No backup found: {backup}")
            shutil.copy2(backup, self.run_path)
            return
        restored = 0
        for p in self.targets:
            backup = p.with_suffix(p.suffix + _BACKUP_SUFFIX)
            if backup.exists():
                shutil.copy2(backup, p)
                restored += 1
        if restored == 0:
            raise PatcherError("No backup found for any gateway target")

    def _inject_target(self, path: Path, content: str) -> str:
        tree = ast.parse(content)
        lines = content.splitlines(keepends=True)
        # 每文件独立注入。mode='before' 插到 idx 行之前（lines[idx:idx]）；
        # mode='after' 插到 idx 行之后（lines[idx+1:idx+1]）。
        def _insert(idx: int, indent: str, hook: str, mode: str) -> None:
            at = idx if mode == "before" else idx + 1
            lines[at:at] = hook.splitlines(keepends=True)

        name = path.name
        if name == "run_turn.py":
            sites = [
                ("bg_deliver", "after", _find_turn_bg_deliver_site(tree, lines), _bg_deliver_hook),
                ("abort", "before", _find_turn_abort_site(tree, lines), _abort_hook),
                ("complete", "before", _find_turn_complete_site(tree, lines), _complete_hook),
                ("start", "before", _find_turn_start_site(tree, lines), _start_hook),
                (
                    "followup_complete",
                    "before",
                    _find_turn_followup_boundary_site(tree, lines),
                    _followup_complete_hook_v2,
                ),
                ("interrupt", "before", _find_turn_interrupt_site(tree, lines), _interrupt_hook_v2),
                (
                    "followup_result",
                    "before",
                    _find_turn_queued_followup_return_site(tree, lines),
                    _followup_result_hook,
                ),
                ("heartbeat", "after", _find_turn_heartbeat_site(tree, lines), _heartbeat_hook),
            ]
        elif name == "run_turn_runner.py":
            sites = [
                # 5 个流式 hook 收敛到 _wire_turn_agent_callbacks 方法尾（after）。
                ("reasoning", "after", _find_runner_wire_site(tree, lines), _reasoning_hook),
                ("thinking", "after", _find_runner_wire_site(tree, lines), _thinking_hook),
                ("answer_guard", "after", _find_runner_wire_site(tree, lines), _answer_guard_hook),
                ("answer", "after", _find_runner_wire_site(tree, lines), _answer_hook),
                ("tool", "after", _find_runner_wire_site(tree, lines), _tool_hook),
                (
                    "background_review",
                    "after",
                    _find_runner_wire_site(tree, lines),
                    _background_review_hook,
                ),
            ]
        elif name == "run_inbound.py":
            sites = [
                ("normalize", "before", _find_inbound_normalize_site(tree, lines), _feishu_normalize_hook),
            ]
        elif name == "run_startup.py":
            sites = [
                ("adapter_init", "before", _find_adapter_init_site(tree, lines), _adapter_init_hook),
            ]
        elif name == "run_notifications.py":
            sites = [
                ("bg_watcher_running", "after", _find_bg_watcher_site(tree, lines), _bg_watcher_running_hook),
                ("bg_watcher_finished", "after", _find_bg_watcher_site(tree, lines), _bg_watcher_finished_hook),
            ]
        else:
            sites = []

        # 多个 hook 落在同一行时（runner 尾 5 处、notif 尾 2 处），must 保证稳定顺序：
        # 逆序插入（后插的在前，最终顺序与 sites 顺序相反 → 定义时把想先执行的放最后）。
        # runner 期望顺序: tool → answer_guard → thinking → reasoning → background_review；
        # notif 期望顺序: finished → running。
        # 同 idx+同 mode 的多段归为一组、按期望顺序拼接后一次插入，避免逐段插入
        # 时顺序反转/交错。组内顺序 = sites 定义顺序（先定义的先执行）。
        for hook_name, _mode, loc, _fn in sites:
            if loc is None:
                raise PatcherError(
                    f"Cannot locate {hook_name} injection site in {name} — Hermes version may be incompatible"
                )
        groups: dict[tuple[int, str], list[str]] = {}
        group_indent: dict[tuple[int, str], str] = {}
        for _hook_name, mode, loc, fn in sites:
            if loc is None:  # pragma: no cover — 上面已校验
                continue
            idx, indent = loc
            key = (idx, mode)
            groups.setdefault(key, []).append(fn(indent))
            group_indent.setdefault(key, indent)
        # 不同插入点必须从文件尾部往前插（行号才不会因前面插入而漂移）。
        for key in sorted(groups, key=lambda k: k[0], reverse=True):
            idx, mode = key
            # hook 文本自带换行（_make_hook 每行拼 indent+\n），直接拼接不要加 \n，
            # 否则块间多出空行、remove 后残留。
            block = "".join(groups[key])
            _insert(idx, group_indent[key], block, mode)
        return "".join(lines)

    def _apply_legacy(self) -> None:
        content = self.run_path.read_text(encoding="utf-8")
        had_patch = any(begin in content for begin, _ in self.MARKERS)
        if had_patch:
            content = content.replace(
                "if adapter and chat_id and not _hermes_lark_bg_handled:", "if adapter and chat_id:"
            )
            for begin, end in self.MARKERS:
                content = _remove_block(content, begin, end)
        else:
            self._backup()
        content = self._inject_legacy(content)
        _atomic_write(self.run_path, content)

    def _remove_legacy(self) -> None:
        content = self.run_path.read_text(encoding="utf-8")
        if not any(begin in content for begin, _ in self.MARKERS):
            return
        content = content.replace("if adapter and chat_id and not _hermes_lark_bg_handled:", "if adapter and chat_id:")
        for begin, end in self.MARKERS:
            content = _remove_block(content, begin, end)
        _atomic_write(self.run_path, content)

    def _backup(self) -> None:
        backup = self.run_path.with_suffix(self.run_path.suffix + _BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(self.run_path, backup)

    def _inject_legacy(self, content: str) -> str:
        tree = ast.parse(content)
        lines = content.splitlines(keepends=True)

        def _loc(needle: str, after: int = 0) -> tuple[int, str] | None:
            for i, line in enumerate(lines):
                if needle in line:
                    idx = i + after
                    return idx, _safe_indent(lines, idx)
            return None

        hook_defs: list[tuple[str, str, tuple[int, str] | None]] = [
            ("normalize", "normalize", _loc("source = event.source")),
            ("start", "start", _find_method_first_stmt(tree, lines, "_handle_message_with_agent")),
            ("complete", "complete", _loc("_already_sent = bool(")),
            ("abort", "abort", _loc("Discarding stale agent result")),
            ("tool", "tool", _find_method_first_stmt(tree, lines, "progress_callback")),
            ("answer", "answer", _find_method_first_stmt(tree, lines, "_stream_delta_cb")),
            ("answer_guard", "answer_guard", _loc("agent.stream_delta_callback = _stream_delta_cb")),
            ("thinking", "thinking", _find_method_first_stmt(tree, lines, "_interim_assistant_cb")),
            ("reasoning", "reasoning", _loc("agent.reasoning_config = reasoning_config")),
            (
                "background_review",
                "background_review",
                _loc("agent.background_review_callback = _bg_review_send"),
            ),
            ("bg_deliver", "bg_deliver", _loc("images, text_content = adapter.extract_images(response)")),
            ("adapter_init", "adapter_init", _find_adapter_init_site(tree, lines)),
            ("bg_watcher_finished", "bg_watcher_finished", _loc("finished with exit code")),
            ("bg_watcher_running", "bg_watcher_running", _loc("is still running~")),
        ]
        _HOOK_FNS = {
            "normalize": _feishu_normalize_hook,
            "start": _start_hook,
            "complete": _complete_hook,
            "abort": _abort_hook,
            "tool": _tool_hook,
            "answer": _answer_guard_hook,
            "answer_guard": _answer_guard_hook,
            "thinking": _thinking_hook,
            "reasoning": _reasoning_hook,
            "background_review": _background_review_hook,
            "bg_deliver": _bg_deliver_hook,
            "adapter_init": _adapter_init_hook,
            "bg_watcher_finished": _bg_watcher_finished_hook,
            "bg_watcher_running": _bg_watcher_running_hook,
        }
        sites: list[tuple[int, str, str]] = []
        for hook_fn_name, name, loc in hook_defs:
            if loc is None:
                raise PatcherError(f"Cannot locate {name} injection site — Hermes version may be incompatible")
            sites.append((loc[0], loc[1], hook_fn_name))
        sites.sort(key=lambda x: x[0], reverse=True)
        for idx, indent, fn_name in sites:
            hook = _HOOK_FNS[fn_name](indent)
            lines[idx:idx] = hook.splitlines(keepends=True)
        return "".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# CronPatcher — cron/scheduler_delivery.py（0.21 拆出）或旧 scheduler.py
# ════════════════════════════════════════════════════════════════════════════


class CronPatcher:
    """注入 CRON_DELIVER hook 到 cron 的 _deliver_result（0.21+ 在 scheduler_delivery.py）。"""

    def __init__(self, cron_path: Path | None = None) -> None:
        self.cron_path = cron_path or _default_cron_path()
        if not self.cron_path.exists():
            # 回退旧布局（升级前残留的 scheduler.py 单文件形态）
            legacy = _resolve_module_path("cron.scheduler", _code_roots())
            if legacy.exists() and "delivered = False" in legacy.read_text(encoding="utf-8", errors="ignore"):
                self.cron_path = legacy
                self.legacy = True
            else:
                tried = ", ".join(str(r) for r in _code_roots())
                raise PatcherError(
                    f"cron delivery file not found: {self.cron_path} "
                    f"(tried: {tried}). Set HERMES_HOME to the dir containing hermes-agent/ and rerun."
                )
        else:
            self.legacy = False

    def is_patched(self) -> bool:
        return MK_CRON_DELIVER in self.cron_path.read_text(encoding="utf-8")

    def verify_target(self) -> None:
        content = self.cron_path.read_text(encoding="utf-8")
        if "def _deliver_result" not in content:
            raise PatcherError("Cannot find _deliver_result anchor in cron delivery file")
        if "cleaned_delivery_content" not in content:
            raise PatcherError("Cannot find 'cleaned_delivery_content' in cron delivery file")
        if self.legacy:
            if "delivered = False" not in content:
                raise PatcherError("Cannot find 'delivered = False' anchor in scheduler.py")
        else:
            if "for target in targets:" not in content:
                raise PatcherError("Cannot find 'for target in targets:' in scheduler_delivery.py")

    def apply(self) -> None:
        if self.is_patched():
            return
        self.verify_target()
        self._backup()
        lines = self.cron_path.read_text(encoding="utf-8").splitlines(keepends=True)

        inject_idx = None
        if self.legacy:
            for i, line in enumerate(lines):
                if line.strip() == "delivered = False":
                    inject_idx = i
                    break
        else:
            for i, line in enumerate(lines):
                if "for target in targets:" in line:
                    inject_idx = i
                    break
        if inject_idx is None:
            raise PatcherError("Cannot find cron injection anchor")

        indent = _safe_indent(lines, inject_idx)
        if not self.legacy:
            # 新版注入在 for 循环体起点：hook 需要循环体缩进（for 缩进 + 4）。
            indent = indent + "    "
        hook = _cron_deliver_hook(indent)
        # 新版：hook 引用了 _hermes_lark_cron_target_feishu helper —— 需要模块级注入一次。
        if not self.legacy:
            content = "".join(lines)
            if "_hermes_lark_cron_target_feishu" not in content:
                helper = (
                    "\n\ndef _hermes_lark_cron_target_feishu(target) -> bool:\n"
                    "    try:\n"
                    "        plat = target.get('platform')\n"
                    "        if plat is None:\n"
                    "            return False\n"
                    "        val = getattr(plat, 'value', plat)\n"
                    "        return str(val).lower() in ('feishu', 'lark')\n"
                    "    except Exception:\n"
                    "        return False\n"
                )
                content += helper
                lines = content.splitlines(keepends=True)
        lines[inject_idx + 1 : inject_idx + 1] = hook.splitlines(keepends=True)
        _atomic_write(self.cron_path, "".join(lines))

    def remove(self) -> None:
        content = self.cron_path.read_text(encoding="utf-8")
        if MK_CRON_DELIVER not in content:
            return
        content = _remove_block(content, MK_CRON_DELIVER, MK_CRON_DELIVER_END)
        if not self.legacy:
            # 同步移除 apply 追加到文件尾的 platform-helper（精确匹配整段，防误删同名用户代码）。
            helper_tail = (
                "\n\ndef _hermes_lark_cron_target_feishu(target) -> bool:\n"
                "    try:\n"
                "        plat = target.get('platform')\n"
                "        if plat is None:\n"
                "            return False\n"
                "        val = getattr(plat, 'value', plat)\n"
                "        return str(val).lower() in ('feishu', 'lark')\n"
                "    except Exception:\n"
                "        return False\n"
            )
            if helper_tail in content:
                content = content.replace(helper_tail, "")
            # 兜底：单行形态残留（历史版本可能写过不同格式）
            content = re.sub(
                r"\n\ndef _hermes_lark_cron_target_feishu\(target\).*?\n    except Exception:\n        return False\n",
                "",
                content,
                flags=re.DOTALL,
            )
        _atomic_write(self.cron_path, content)

    def restore(self) -> None:
        backup = self.cron_path.with_suffix(self.cron_path.suffix + _BACKUP_SUFFIX)
        if not backup.exists():
            raise PatcherError(f"No backup found: {backup}")
        shutil.copy2(backup, self.cron_path)

    def _backup(self) -> None:
        backup = self.cron_path.with_suffix(self.cron_path.suffix + _BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(self.cron_path, backup)
