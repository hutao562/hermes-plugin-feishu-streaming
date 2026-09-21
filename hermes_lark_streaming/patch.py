"""AST Patch 注入的 Hook 函数.

这些函数从 gateway/run.py 的注入点被调用.
它们只做一件事：检查配置 → 调用 controller.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any

from .controller import get_controller

_logger = logging.getLogger("hermes_lark_streaming")


def _safe_hook(
    default_return: Any = None,
    log_level: str = "warning",
) -> Callable:
    """统一处理 enabled 检查和异常捕获."""

    def decorator(func: Callable) -> Callable:
        if iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*, message_id: str, **kwargs: Any) -> Any:
                try:
                    ctrl = get_controller()
                    if not ctrl.enabled:
                        return default_return
                    return await func(ctrl=ctrl, message_id=message_id, **kwargs)
                except Exception as exc:
                    getattr(_logger, log_level)("%s error: %s", func.__name__, exc, exc_info=True)
                    return default_return

            return async_wrapper

        @wraps(func)
        def wrapper(*, message_id: str, **kwargs: Any) -> Any:
            try:
                ctrl = get_controller()
                if not ctrl.enabled:
                    return default_return
                return func(ctrl=ctrl, message_id=message_id, **kwargs)
            except Exception as exc:
                getattr(_logger, log_level)("%s error: %s", func.__name__, exc, exc_info=True)
                return default_return

        return wrapper

    return decorator


def on_feishu_normalize(
    *,
    message_id: str,
    source: Any,
    event: Any,
    reply_anchor_id: str | None = None,
) -> None:
    """[注入点 0] _handle_message source 赋值后 — 修正飞书引用消息的虚假 thread_id."""
    try:
        ctrl = get_controller()
        if not ctrl.enabled:
            return

        platform = getattr(getattr(source, "platform", None), "value", "")
        if platform != "feishu":
            return

        raw = getattr(event, "raw_message", None)
        raw_event = raw.get("event") if isinstance(raw, dict) else None
        if raw_event is None:
            raw_event = getattr(raw, "event", None)

        raw_message = None
        if isinstance(raw_event, dict):
            raw_message = raw_event.get("message")
        elif raw_event is not None:
            raw_message = getattr(raw_event, "message", None)
        if raw_message is None and isinstance(raw, dict):
            raw_message = raw.get("message")
        if raw_message is None:
            raw_message = raw

        real_thread_id = None
        if isinstance(raw_message, dict):
            real_thread_id = raw_message.get("thread_id")
        else:
            real_thread_id = getattr(raw_message, "thread_id", None)

        reply_to = getattr(event, "reply_to_message_id", None)
        source_thread_id = getattr(source, "thread_id", None)

        _logger.info(
            "feishu inbound ids: msg=%s anchor=%s source_thread=%s raw_thread=%s reply_to=%s",
            message_id,
            reply_anchor_id,
            source_thread_id,
            real_thread_id,
            reply_to,
        )

        if reply_to and source_thread_id and not real_thread_id:
            source.thread_id = None
            event.source = source
    except Exception as exc:
        _logger.warning("on_feishu_normalize error: %s", exc, exc_info=True)


@_safe_hook()
def on_message_started(
    *,
    ctrl: Any,
    message_id: str | None,
    chat_id: str,
    anchor_id: str | None = None,
    thread_id: str | None = None,
    session_key: str | None = None,
) -> None:
    """[注入点 1] 函数开头 — message.started."""
    ctrl.on_message_started(
        message_id=message_id,
        chat_id=chat_id,
        anchor_id=anchor_id,
        thread_id=thread_id,
        session_key=session_key,
    )


def collect_image_media(agent_messages: Any) -> list[str]:
    """从 agent_messages 提取 image_generate 工具产物的本地图片路径.

    扫 assistant 的 image_generate tool_calls，找对应 tool 结果的 JSON payload，
    提取 host_image/image/agent_visible_image 字段路径（复用 Hermes _collect_media_tags 逻辑，
    见 run.py:1080-1098）。
    """
    import json

    # 只取最后一个 user message 之后的 image_generate（本次回合，避免 history 旧图错误进卡）
    last_user_idx = -1
    for i, msg in enumerate(agent_messages or []):
        if msg.get("role") == "user":
            last_user_idx = i
    if last_user_idx < 0:
        return []
    last_ig_call_id: str | None = None
    for msg in (agent_messages or [])[last_user_idx + 1:]:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                cid = call.get("id") or call.get("call_id")
                name = (call.get("function") or {}).get("name") or call.get("name") or ""
                if cid and name == "image_generate":
                    last_ig_call_id = str(cid)
    if not last_ig_call_id:
        return []

    # 取该 call_id 的产物路径（单张）
    for msg in (agent_messages or []):
        if msg.get("role") not in ("tool", "function"):
            continue
        if str(msg.get("tool_call_id") or msg.get("call_id") or "") != last_ig_call_id:
            continue
        try:
            payload = json.loads(msg.get("content") or "")
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("success"):
            for field in ("host_image", "image", "agent_visible_image"):
                p = payload.get(field)
                if isinstance(p, str) and p:
                    return [p]
    return []


@_safe_hook(default_return=False)
async def on_message_completed_wait(
    *,
    ctrl: Any,
    message_id: str | None,
    answer: str = "",
    is_error: bool = False,
    reconcile_answer: bool = False,
    duration: float = 0.0,
    model: str = "",
    tokens: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    chat_id: str | None = None,
    image_paths: list[str] | None = None,
) -> bool:
    """[注入点 2] return 前 — message.completed，等待卡片完成收尾."""
    return bool(
        await ctrl.on_completed_wait(
            message_id=message_id,
            answer=answer,
            is_error=is_error,
            reconcile_answer=reconcile_answer,
            duration=duration,
            model=model,
            tokens=tokens,
            context=context,
            chat_id=chat_id,
            image_paths=image_paths,
        )
    )


@_safe_hook(default_return=False)
def on_message_needs_text_fallback(*, ctrl: Any, message_id: str) -> bool:
    """Return True once when CardKit failed and gateway must send plain text."""
    return bool(ctrl.consume_text_fallback(message_id))


@_safe_hook(default_return=False)
async def on_queued_followup_boundary(*, ctrl: Any, message_id: str, result: dict[str, Any]) -> bool:
    """Complete the current card before Hermes drains a queued follow-up turn."""
    if not isinstance(result, dict) or result.get("interrupted"):
        return False

    sent = bool(
        await ctrl.on_completed_wait(
            message_id=message_id,
            answer=result.get("final_response") or "",
            is_error=bool(result.get("failed")),
            duration=0.0,
            model=result.get("model", ""),
            tokens={
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
            },
            context={
                "used_tokens": result.get("last_prompt_tokens", 0),
                "max_tokens": result.get("context_length", 0),
            },
        )
    )
    if sent:
        result["response_previewed"] = True
        result["already_sent"] = True
        result["final_response"] = ""
    else:
        ctrl.consume_text_fallback(message_id)
    return sent


@_safe_hook()
def on_queued_followup_result(*, ctrl: Any, message_id: str, followup_result: dict[str, Any]) -> None:
    """Carry the deepest queued follow-up id back to the outer completion hook."""
    if isinstance(followup_result, dict) and message_id:
        followup_result.setdefault("_hermes_lark_completion_id", message_id)


@_safe_hook(default_return=False)
def on_tool_updated(
    *,
    ctrl: Any,
    message_id: str | None,
    tool_name: str,
    status: str,
    detail: str = "",
    chat_id: str | None = None,
) -> bool:
    """[注入点 3] progress_callback — tool.updated."""
    return bool(
        ctrl.on_tool_update(
            message_id=message_id,
            chat_id=chat_id,
            tool_name=tool_name,
            status=status,
            detail=detail,
        )
    )


@_safe_hook(default_return=False, log_level="debug")
def on_answer_delta(*, ctrl: Any, message_id: str | None, text: str, chat_id: str | None = None) -> bool:
    """[注入点 4] _stream_delta_cb — answer.delta."""
    return bool(ctrl.on_answer(message_id=message_id, chat_id=chat_id, text=text))


@_safe_hook(default_return=False, log_level="debug")
def on_thinking_delta(*, ctrl: Any, message_id: str | None, text: str, chat_id: str | None = None) -> bool:
    """[注入点 5] _interim_assistant_cb — thinking.delta."""
    return bool(ctrl.on_thinking(message_id=message_id, chat_id=chat_id, text=text))


@_safe_hook(default_return=False, log_level="debug")
def on_reasoning_delta(*, ctrl: Any, message_id: str | None, text: str, chat_id: str | None = None) -> bool:
    """[注入点 6] reasoning_callback — native model reasoning delta."""
    return bool(ctrl.on_reasoning(message_id=message_id, chat_id=chat_id, text=text))


@_safe_hook(default_return=False, log_level="debug")
def on_heartbeat(*, ctrl: Any, message_id: str | None, text: str, chat_id: str | None = None) -> bool:
    """[注入点 13] Hermes 长回合心跳 → 卡片末尾状态行.

    心跳由 Hermes _run_agent_notify_long_running 周期性产生（默认 180s），
    本函数把文本转给 controller 更新卡片末尾的 heartbeat 元素；返回 True 时
    调用方（注入代码）跳过 Hermes 原生 adapter.send/edit 心跳消息。
    """
    return bool(ctrl.on_heartbeat(message_id=message_id, chat_id=chat_id, text=text))


@_safe_hook(default_return=False, log_level="debug")
def on_background_review_message(
    *,
    ctrl: Any,
    message_id: str,
    text: str,
    sender: Callable[[str], Any],
) -> bool:
    """[注入点 7] background_review_callback — background.review."""
    deferred: bool = ctrl.defer_background_review(message_id=message_id, text=text, sender=sender)
    return deferred


@_safe_hook()
def on_message_aborted(*, ctrl: Any, message_id: str) -> None:
    """[注入点 8] stale return None 前 — message.aborted."""
    ctrl.on_aborted(message_id=message_id)


async def on_session_aborted(*, session_key: str) -> bool:
    """Terminate the active card after Hermes handles a busy-session /stop."""
    try:
        ctrl = get_controller()
        if not ctrl.enabled:
            return False
        return bool(await ctrl.on_session_aborted(session_key=session_key))
    except Exception as exc:
        _logger.warning("on_session_aborted error: %s", exc, exc_info=True)
        return False


@_safe_hook()
def on_message_interrupted(
    *,
    ctrl: Any,
    message_id: str,
    new_message_id: str,
    chat_id: str,
    anchor_id: str | None = None,
    session_key: str | None = None,
) -> None:
    """[注入点 9] interrupt 发生 — message.interrupted."""
    ctrl.on_interrupted(
        old_message_id=message_id,
        new_message_id=new_message_id,
        chat_id=chat_id,
        anchor_id=anchor_id,
        session_key=session_key,
    )


def on_cron_deliver(
    *,
    chat_id: str,
    content: str,
    loop: Any = None,
    task_name: str = "",
    run_time: str = "",
    job_id: str = "",
) -> bool:
    """[注入点 10] cron 推送 — 包装为飞书卡片发送."""
    # 用 cron.scheduler logger（进 agent.log），hermes_lark_streaming logger 不进文件
    _diag = logging.getLogger("cron.scheduler")
    try:
        ctrl = get_controller()
        if not ctrl.enabled:
            _diag.info("[cheerwhy-cron] skip enabled=False chat=%s", chat_id[:12])
            return False
        _diag.info(
            "[cheerwhy-cron] call chat=%s content_len=%d", chat_id[:12], len(content)
        )
        _extra: dict[str, str] = {"job_id": job_id} if job_id else {}
        ok = bool(ctrl.on_cron_deliver(
            chat_id=chat_id, content=content, loop=loop,
            task_name=task_name, run_time=run_time, **_extra,
        ))
        _diag.info("[cheerwhy-cron] result=%s chat=%s", ok, chat_id[:12])
        return ok
    except Exception as exc:
        _diag.warning("[cheerwhy-cron] error: %s", exc, exc_info=True)
        return False


async def on_background_deliver(
    *,
    chat_id: str,
    preview: str,
    content: str,
    reply_to_message_id: str | None = None,
) -> bool:
    """[注入点 11] background 任务完成推送 — 包装为飞书卡片发送."""
    try:
        ctrl = get_controller()
        if not ctrl.enabled:
            return False
        return await ctrl.on_background_deliver(
            chat_id=chat_id,
            preview=preview,
            content=content,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as exc:
        _logger.warning("on_background_deliver error: %s", exc, exc_info=True)
        return False


async def on_bg_watcher_notify(
    *,
    chat_id: str,
    content: str,
    reply_to_message_id: str | None = None,
) -> bool:
    """[注入点 12] background watcher text-only 通知 → 合并到 agent 卡片或发 background card.

    hermes 的 background watcher 完成通知（run.py text-only notification，含 process stdout）
    默认 adapter.send 纯文本；此 hook 接管，让通知进卡片（合并到同 chat 的 agent 卡片，或发独立 background 卡片）。
    """
    try:
        ctrl = get_controller()
        if not ctrl.enabled:
            return False
        return await ctrl.on_bg_watcher_notify(
            chat_id=chat_id,
            content=content,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as exc:
        _logger.warning("on_bg_watcher_notify error: %s", exc, exc_info=True)
        return False


@_safe_hook()
def on_clarify_enter(
    *,
    ctrl: Any,
    message_id: str,
    chat_id: str | None = None,
    session_key: str | None = None,
) -> None:
    """[注入点 12] clarify_callback 进入 — 暂停 flush 保留当前卡。"""
    ctrl.on_clarify_enter(message_id=message_id, chat_id=chat_id, session_key=session_key)


@_safe_hook()
def on_clarify_exit(
    *,
    ctrl: Any,
    message_id: str,
    chat_id: str | None = None,
    session_key: str | None = None,
) -> None:
    """[注入点 12] clarify_callback 退出 — 标记待封卡，等 tool.completed 触发切卡。"""
    ctrl.on_clarify_exit(message_id=message_id, chat_id=chat_id, session_key=session_key)
