"""Feishu clarify 内联单选 — monkey-patch FeishuAdapter.

飞书 adapter 没实现 ``send_clarify``，默认走 base.py 的数字列表 text fallback。
本模块在 gateway ``start()`` 的 adapter connect loop 之前给 ``FeishuAdapter`` 类打
两个补丁：

  * ``send_clarify`` —— 渲染 schema-1.0 单选按钮卡（对标原生 ``send_exec_approval``）。
  * ``_on_card_action_trigger`` —— 包装原方法，在 approval/update-prompt 分支之前
    拦截 ``hermes_clarify_action`` 按钮回调。

按钮点击通过同步 ``CallBackCard`` 立即更新卡片，异步调用
``resolve_gateway_clarify`` 唤醒阻塞中的 agent 线程。「其他」按钮调
``mark_awaiting_text``，下一条非斜杠消息由 gateway 文本拦截（run.py:8407）接手。

关键时序：``_on_card_action_trigger`` 必须在 adapter ``connect()`` **之前** patch
类——lark SDK 在 ``register_p2_card_action_trigger(self._on_card_action_trigger)``
（adapter.py:1639）就捕获了绑定方法引用，事后 patch 实例无效。注入点
``HERMES_LARK_ADAPTER_INIT`` 位于 run.py 的 ``# Initialize and connect each
configured platform`` 注释处（紧接 connect loop 之前）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .config import Config

_logger = logging.getLogger("hermes_lark_streaming")

# clarify_id → {session_key, chat_id, message_id}  (mirror adapter._approval_state)
_CLARIFY_STATE: dict[str, dict[str, str]] = {}

# Sentinel attr stamped on patched methods so patch_feishu_adapter() is idempotent.
_PATCH_MARK = "_hermes_lark_clarify"

# 选项 button label 上限（飞书 button 文本过长会被截断/换行难看）。
_MAX_CHOICE_LABEL = 40
# clarify tool schema 上限是 4 个选项（clarify_tool.MAX_CHOICES）。
_MAX_CHOICES = 4


# =========================================================================
# Choice normalisation — 同 clarify_tool._flatten_choice
# =========================================================================

def _flatten_choice(c: Any) -> str:
    """Coerce a single choice into its user-facing display string.

    LLM 偶尔吐 dict 形 choice（``[{"description": "..."}]``），naive ``str(c)`` 会把
    整个 dict 变成 Python repr 泄漏到按钮 label。这里按 canonical LLM tool-call
    user-facing keys 解包：label → description → text → title。
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


def _normalize_choices(choices: list[Any] | None) -> list[str]:
    clean = [s for s in (_flatten_choice(c) for c in (choices or [])) if s]
    return clean[:_MAX_CHOICES]


def _truncate(text: str, limit: int = _MAX_CHOICE_LABEL) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# =========================================================================
# Card builders (schema 1.0, 同 send_exec_approval / _build_resolved_approval_card)
# =========================================================================

def _build_clarify_card(
    *, question: str, choices: list[str], clarify_id: str
) -> dict[str, Any]:
    """单选按钮卡：选项全文展示在编号列表 + 编号按钮选择.

    飞书 button 的 plain_text 单行不换行、长文本被截断，所以把完整选项放
    markdown 编号列表（支持换行/长文），按钮只标号（① ② ...），点编号即选。
    """
    capped = choices[:_MAX_CHOICES]
    # 编号列表：完整选项文本，markdown 渲染可换行不受 button 截断限制
    list_lines = [f"**{i + 1}.** {choice}" for i, choice in enumerate(capped)]
    options_md = "\n".join(list_lines)

    buttons: list[dict[str, Any]] = []
    for i, choice in enumerate(capped):
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"{i + 1}"},
            "type": "default",
            "value": {
                "hermes_clarify_action": "choice",
                "clarify_id": clarify_id,
                "index": i,
                "text": choice,  # 完整文本，resolve 时用
            },
        })
    buttons.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "✏️ 其他"},
        "type": "default",
        "value": {
            "hermes_clarify_action": "other",
            "clarify_id": clarify_id,
        },
    })

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": str(question or "").strip() or "（无问题文本）"},
        {"tag": "markdown", "content": options_md},
        {"tag": "action", "actions": buttons},
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": "❓ 需要你的输入", "tag": "plain_text"},
            "template": "orange",
        },
        "elements": elements,
    }


def _build_open_clarify_card(*, question: str) -> dict[str, Any]:
    """开放题（无选项）卡片：纯文本，无按钮."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": "❓ 需要你的输入", "tag": "plain_text"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": str(question or "").strip() or "（无问题文本）"},
            {"tag": "markdown", "content": "_请在对话中回复你的答案_"},
        ],
    }


def _build_resolved_clarify_card(*, choice: str, user_name: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": "✅ 已选择", "tag": "plain_text"},
            "template": "green",
        },
        "elements": [
            {"tag": "markdown", "content": f"✅ **{choice}**\n由 {user_name} 选择"},
        ],
    }


def _build_awaiting_clarify_card(*, user_name: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": "✏️ 等待输入", "tag": "plain_text"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": f"✏️ {user_name} 选择了手动输入。\n请在对话中发送你的答案。",
            },
        ],
    }


# =========================================================================
# Patched send_clarify
# =========================================================================

async def _send_clarify(
    self: Any,
    chat_id: str,
    question: str,
    choices: list[Any] | None,
    clarify_id: str,
    session_key: str,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Render a clarify prompt with inline single-select buttons.

    签名匹配 ``BasePlatformAdapter.send_clarify``（base.py:3088），由 gateway
    ``_clarify_callback_sync`` 以 ``await _status_adapter.send_clarify(...)`` 调用。
    复用 adapter 自身的 ``_feishu_send_with_retry`` + ``_finalize_send_result``，
    保证 reply/thread metadata 处理同原生 approval 一致。
    """
    from gateway.platforms.base import SendResult  # type: ignore[import-not-found]

    if not getattr(self, "_client", None):
        return SendResult(success=False, error="Not connected")

    try:
        clean = _normalize_choices(choices)
        if clean:
            card = _build_clarify_card(
                question=str(question or ""), choices=clean, clarify_id=clarify_id
            )
        else:
            # 开放题：无按钮，flip 进 text-capture 态让 gateway 文本拦截接手。
            from tools.clarify_gateway import mark_awaiting_text
            mark_awaiting_text(clarify_id)
            card = _build_open_clarify_card(question=str(question or ""))

        payload = json.dumps(card, ensure_ascii=False)
        response = await self._feishu_send_with_retry(
            chat_id=chat_id,
            msg_type="interactive",
            payload=payload,
            reply_to=None,
            metadata=metadata,
        )
        result = self._finalize_send_result(response, "send_clarify failed")
        if getattr(result, "success", False):
            _CLARIFY_STATE[clarify_id] = {
                "session_key": session_key or "",
                "chat_id": chat_id,
                "message_id": getattr(result, "message_id", "") or "",
            }
        return result
    except Exception as exc:
        _logger.warning("[clarify] send_clarify failed: %s", exc, exc_info=True)
        return SendResult(success=False, error=str(exc))


# =========================================================================
# Async resolver — 真正唤醒 agent 线程
# =========================================================================

async def _resolve_clarify(
    adapter: Any,
    clarify_id: str,
    choice: str,
    *,
    open_id: str = "",
    chat_id: str = "",
) -> None:
    """Pop clarify state and unblock the waiting agent thread."""
    state = _CLARIFY_STATE.get(clarify_id)
    if not state:
        _logger.debug("[clarify] %s already resolved or unknown", clarify_id)
        return
    if not adapter._is_interactive_operator_authorized(open_id):
        _logger.warning(
            "[clarify] Unauthorized clarify click by %s for %s",
            open_id or "<unknown>", clarify_id,
        )
        return
    expected_chat_id = str(state.get("chat_id", "") or "")
    if expected_chat_id and chat_id and expected_chat_id != chat_id:
        _logger.warning(
            "[clarify] %s chat mismatch (expected=%s, got=%s)",
            clarify_id, expected_chat_id, chat_id,
        )
        return
    state = _CLARIFY_STATE.pop(clarify_id, None)
    if not state:
        _logger.debug("[clarify] %s already resolved while validating callback", clarify_id)
        return
    try:
        from tools.clarify_gateway import resolve_gateway_clarify
        resolved = resolve_gateway_clarify(clarify_id, choice)
        if resolved:
            _logger.info(
                "[clarify] resolved id=%s choice=%s user=%s",
                clarify_id, _truncate(choice, 30), open_id or "?",
            )
        else:
            _logger.warning("[clarify] resolve_gateway_clarify returned False for %s", clarify_id)
    except Exception as exc:
        _logger.warning("[clarify] resolve_gateway_clarify failed: %s", exc, exc_info=True)


# =========================================================================
# Sync card-action handler — 镜像 _handle_approval_card_action
# =========================================================================

def _handle_clarify_card_action(
    adapter: Any,
    *,
    event: Any,
    action_value: dict[str, Any],
) -> Any:
    """同步处理 clarify 按钮点击：鉴权 → 调度 resolve/awaiting → 返回同步卡."""
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            CallBackCard,
            P2CardActionTriggerResponse,
        )
    except Exception:
        CallBackCard = None  # type: ignore[assignment]
        P2CardActionTriggerResponse = None  # type: ignore[assignment]

    def _empty() -> Any:
        return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None

    clarify_id = action_value.get("clarify_id")
    if not clarify_id:
        _logger.debug("[clarify] Card action missing clarify_id, ignoring")
        return _empty()
    state = _CLARIFY_STATE.get(clarify_id)
    if not state:
        _logger.debug("[clarify] %s already resolved or unknown", clarify_id)
        return _empty()

    operator = getattr(event, "operator", None)
    open_id = str(getattr(operator, "open_id", "") or "")
    if not adapter._is_interactive_operator_authorized(open_id):
        _logger.warning("[clarify] Unauthorized clarify click by %s", open_id or "<unknown>")
        return _empty()

    callback_chat_id = str(getattr(getattr(event, "context", None), "open_chat_id", "") or "")
    expected_chat_id = str(state.get("chat_id", "") or "")
    if callback_chat_id and expected_chat_id and callback_chat_id != expected_chat_id:
        _logger.warning(
            "[clarify] %s callback chat mismatch (expected=%s, got=%s)",
            clarify_id, expected_chat_id, callback_chat_id,
        )
        return _empty()

    user_name = adapter._get_cached_sender_name(open_id) or open_id
    action = str(action_value.get("hermes_clarify_action", "") or "")

    if action == "other":
        # flip 进 text-capture 态；下一条非斜杠消息由 gateway 文本拦截接手。
        try:
            from tools.clarify_gateway import mark_awaiting_text
            mark_awaiting_text(clarify_id)
        except Exception as exc:
            _logger.warning("[clarify] mark_awaiting_text failed: %s", exc, exc_info=True)
        card_data = _build_awaiting_clarify_card(user_name=user_name)
    elif action == "choice":
        # 优先用 button value 里携带的完整文本（entry 可能已被 text-intercept 清掉）；
        # entry round-trip 仅作 fallback（理论上更权威，但依赖 entry 存活）。
        choice_text = str(action_value.get("text") or "").strip()
        if not choice_text:
            choice_text = _canonical_choice_text(clarify_id, action_value.get("index"))
        if not choice_text:
            _logger.warning(
                "[clarify] %s choice has no text (index=%r), ignoring",
                clarify_id, action_value.get("index"),
            )
            return _empty()
        if not adapter._submit_on_loop(
            adapter._loop,
            _resolve_clarify(
                adapter, clarify_id, choice_text,
                open_id=open_id, chat_id=callback_chat_id,
            ),
        ):
            return _empty()
        card_data = _build_resolved_clarify_card(choice=choice_text, user_name=user_name)
    else:
        _logger.debug("[clarify] Unknown clarify action=%r", action)
        return _empty()

    if P2CardActionTriggerResponse is None:
        return None
    response = P2CardActionTriggerResponse()
    if CallBackCard is not None:
        card = CallBackCard()
        card.type = "raw"
        card.data = card_data
        response.card = card
    return response


def _canonical_choice_text(clarify_id: str, index: Any) -> str:
    """从 clarify_gateway entry 拿 canonical 选项文本（round-trip 防截断）."""
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return ""
    if idx < 0:
        return ""
    try:
        from tools.clarify_gateway import _entries
        entry = _entries.get(clarify_id)
    except Exception:
        return ""
    if entry is None or not getattr(entry, "choices", None):
        return ""
    choices = entry.choices
    if idx >= len(choices):
        return ""
    return str(choices[idx]).strip()


# =========================================================================
# Public entry — 由 AST 注入点（gateway:startup emit 之后）调用一次
# =========================================================================

def patch_feishu_adapter(adapters: Any) -> None:
    """Patch FeishuAdapter.send_clarify + card-action processor (idempotent).

    在 ``gateway:startup`` emit 之后调用（所有 adapter 已 connect，
    ``event_handler`` 已建）。此时：

      * ``send_clarify`` 是普通类属性查找，覆盖类即可（运行时实例生效）。
      * ``_on_card_action_trigger`` 已被 lark SDK 在 ``connect()`` 时存进
        ``event_handler._callback_processor_map["p2.card.action.trigger"].f``
        （绑定方法快照）—— 直接替换该 processor 的 ``.f``，绕过快照。

    ``adapters`` 是 gateway runner 的 ``self.adapters`` dict（Platform → 实例）。
    """
    try:
        if not Config().clarify_inline:
            return
    except Exception:
        _logger.warning(
            "[clarify] failed to read clarify_inline config; defaulting to enabled"
        )

    FeishuAdapter = _find_feishu_adapter_class()
    if FeishuAdapter is None:
        _logger.info("[clarify] FeishuAdapter class not found; skipping clarify patch")
        return

    # 1) patch send_clarify 类方法（运行时实例通过类属性查找命中）
    send_patched = False
    try:
        current_send = getattr(FeishuAdapter, "send_clarify", None)
        if not getattr(current_send, _PATCH_MARK, False):
            setattr(_send_clarify, _PATCH_MARK, True)
            FeishuAdapter.send_clarify = _send_clarify  # type: ignore[assignment]
            send_patched = True
    except Exception:
        _logger.warning("[clarify] send_clarify patch failed", exc_info=True)

    # 2) 替换每个 feishu 实例的 card-action processor.f
    instances_patched = _patch_card_action_processors(adapters, FeishuAdapter)

    if send_patched or instances_patched:
        _logger.info(
            "[clarify] patched: send_clarify=%s card_action_instances=%d module=%s",
            send_patched, instances_patched, getattr(FeishuAdapter, "__module__", "?"),
        )
    elif not send_patched:
        _logger.debug("[clarify] already patched (send_clarify + processors)")


def _patch_card_action_processors(adapters: Any, feishu_cls: type) -> int:
    """Replace ``processor.f`` on each feishu adapter's card-action handler.

    SDK 在 ``connect()`` 时把 ``adapter._on_card_action_trigger``（绑定方法）
    快照进 ``event_handler._callback_processor_map["p2.card.action.trigger"].f``。
    覆盖类属性已无法影响这个快照 —— 直接换 processor 的 ``.f`` 指向我们的
    wrapper，wrapper 闭包持有原 ``adapter`` 以调回原逻辑。
    """
    count = 0
    for adapter in _iter_feishu_adapters(adapters, feishu_cls):
        try:
            handler = getattr(adapter, "_event_handler", None)
            if handler is None:
                continue
            proc_map = getattr(handler, "_callback_processor_map", None)
            if not isinstance(proc_map, dict):
                continue
            processor = proc_map.get("p2.card.action.trigger")
            if processor is None:
                continue
            if getattr(processor.f, _PATCH_MARK, False) is True:
                continue  # 已替换
            original_fn = processor.f  # 原绑定方法
            processor.f = _make_card_action_wrapper(adapter, original_fn)
            count += 1
        except Exception:
            _logger.warning(
                "[clarify] failed to patch card-action processor", exc_info=True
            )
    return count


def _make_card_action_wrapper(adapter: Any, original: Callable[..., Any]) -> Callable[..., Any]:
    """Build a replacement for ``processor.f`` that intercepts clarify buttons.

    ``original`` 是 SDK 快照的原绑定方法（``adapter._on_card_action_trigger``）。
    非 clarify 按钮直接转发给 original，保持 approval/update-prompt 等行为不变。
    """

    def wrapper(data: Any) -> Any:
        try:
            event = getattr(data, "event", None)
            action = getattr(event, "action", None)
            value = getattr(action, "value", {}) or {}
            if isinstance(value, dict) and value.get("hermes_clarify_action"):
                loop = adapter._loop
                if not adapter._loop_accepts_callbacks(loop):
                    _logger.warning("[clarify] Dropping card action before adapter loop ready")
                    from lark_oapi.event.callback.model.p2_card_action_trigger import (
                        P2CardActionTriggerResponse,
                    )
                    return (
                        P2CardActionTriggerResponse()
                        if P2CardActionTriggerResponse else None
                    )
                return _handle_clarify_card_action(adapter, event=event, action_value=value)
        except Exception:
            _logger.warning("[clarify] card-action intercept failed", exc_info=True)
        return original(data)

    setattr(wrapper, _PATCH_MARK, True)
    return wrapper


def _iter_feishu_adapters(adapters: Any, feishu_cls: type) -> list[Any]:
    """Yield feishu adapter instances from the gateway's adapters mapping."""
    result: list[Any] = []
    if not adapters:
        _logger.warning("[clarify] adapters is empty/None")
        return result
    try:
        values = list(adapters.values()) if hasattr(adapters, "values") else list(adapters)
    except TypeError:
        _logger.warning("[clarify] adapters not iterable: %r", type(adapters))
        return result
    for value in values:
        # isinstance may fail if feishu_cls is a stale shadow — also match by
        # class name + presence of _on_card_action_trigger as a fallback.
        is_match = isinstance(value, feishu_cls)
        if not is_match:
            cls = type(value)
            if cls.__name__ == "FeishuAdapter" and hasattr(cls, "_on_card_action_trigger"):
                is_match = True
        if is_match:
            result.append(value)
    if not result:
        _logger.warning(
            "[clarify] no feishu adapter found in %d values (types=%s, looking for %s)",
            len(values),
            [type(v).__name__ for v in values],
            feishu_cls.__name__,
        )
    return result


def _find_feishu_adapter_class() -> Any:
    """运行时从 sys.modules 扫描真正的 FeishuAdapter 类.

    hermes plugin loader 把 ``plugins/platforms/feishu`` 加载成
    ``hermes_plugins.feishu_platform``（slug 派生），和源码 import 路径不同。
    扫 sys.modules 里所有名为 ``FeishuAdapter`` 且有 ``_on_card_action_trigger``
    的类，命中真身。找不到则回退直接 import（开发/测试环境）。
    """
    import sys

    candidates: list[Any] = []
    # Snapshot names first — getattr on a module may trigger lazy imports that
    # mutate sys.modules mid-iteration.
    names = [n for n in list(sys.modules) if "feishu" in n.lower() and "adapter" in n.lower()]
    for name in names:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            cls = getattr(mod, "FeishuAdapter", None)
        except Exception:
            continue
        if isinstance(cls, type) and hasattr(cls, "_on_card_action_trigger"):
            candidates.append(cls)

    if candidates:
        for cls in candidates:
            if "hermes_plugins" in getattr(cls, "__module__", ""):
                return cls
        return candidates[0]

    try:
        from plugins.platforms.feishu.adapter import FeishuAdapter  # type: ignore[import-not-found]
        return FeishuAdapter
    except Exception:
        return None
