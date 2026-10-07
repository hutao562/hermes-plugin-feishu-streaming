"""clarify 内联单选按钮卡 — 插件模式版（无 monkey-patch）.

注入模式（hermes_lark_streaming/clarify.py）要 patch 官方类方法 + 替换 SDK
processor.f；插件模式下适配器类是自己的，send_clarify 与卡片回调
_on_card_action_trigger 直接在 Mixin 类定义期覆写——SDK connect() 注册的绑定
方法天然解析到 Mixin 版本。

卡片构建/动作处理逻辑 vendor 自注入模式（已生产验证），依赖的
tools.clarify_gateway 与 lark_oapi 回调模型在 gateway 进程内直接 import。
"""

from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger("gateway.run")

# clarify_id → {session_key, chat_id, message_id}  (mirror adapter._approval_state)
CLARIFY_STATE: dict[str, dict[str, str]] = {}

# 选项 button label 上限（飞书 button 文本过长会被截断/换行难看）。
_MAX_CHOICE_LABEL = 40
# clarify tool schema 上限是 4 个选项（clarify_tool.MAX_CHOICES）。
_MAX_CHOICES = 4


# ── Choice normalisation — 同 clarify_tool._flatten_choice ──

def _flatten_choice(c: Any) -> str:
    """LLM 偶尔吐 dict 形 choice，按 canonical user-facing keys 解包防 repr 泄漏."""
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


def normalize_choices(choices: list[Any] | None) -> list[str]:
    clean = [s for s in (_flatten_choice(c) for c in (choices or [])) if s]
    return clean[:_MAX_CHOICES]


def _truncate(text: str, limit: int = _MAX_CHOICE_LABEL) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── Card builders (schema 1.0, 同官方 approval 卡形态) ──

def build_clarify_card(*, question: str, choices: list[str], clarify_id: str) -> dict[str, Any]:
    """单选按钮卡：选项全文在编号列表 + 编号按钮选择.

    飞书 button 的 plain_text 单行不换行、长文本截断，所以完整选项放 markdown
    编号列表，按钮只标号（1 2 ...），点编号即选。button value 携带完整选项文本
    ——gateway text-intercept 可能提前 resolve 掉 entry，点击时优先用 value text。
    """
    capped = choices[:_MAX_CHOICES]
    options_md = "\n".join(f"**{i + 1}.** {choice}" for i, choice in enumerate(capped))
    buttons: list[dict[str, Any]] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"{i + 1}"},
            "type": "default",
            "value": {
                "hermes_clarify_action": "choice",
                "clarify_id": clarify_id,
                "index": i,
                "text": choice,  # 完整文本，resolve 时用（entry 失活兜底）
            },
        }
        for i, choice in enumerate(capped)
    ]
    buttons.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "✏️ 其他"},
        "type": "default",
        "value": {"hermes_clarify_action": "other", "clarify_id": clarify_id},
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "❓ 需要你的输入", "tag": "plain_text"}, "template": "orange"},
        "elements": [
            {"tag": "markdown", "content": str(question or "").strip() or "（无问题文本）"},
            {"tag": "markdown", "content": options_md},
            {"tag": "action", "actions": buttons},
        ],
    }


def build_open_clarify_card(*, question: str) -> dict[str, Any]:
    """开放题（无选项）卡片：纯文本，无按钮."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "❓ 需要你的输入", "tag": "plain_text"}, "template": "orange"},
        "elements": [
            {"tag": "markdown", "content": str(question or "").strip() or "（无问题文本）"},
            {"tag": "markdown", "content": "_请在对话中回复你的答案_"},
        ],
    }


def build_resolved_clarify_card(*, choice: str, user_name: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "✅ 已选择", "tag": "plain_text"}, "template": "green"},
        "elements": [{"tag": "markdown", "content": f"✅ **{choice}**\n由 {user_name} 选择"}],
    }


def build_awaiting_clarify_card(*, user_name: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "✏️ 等待输入", "tag": "plain_text"}, "template": "blue"},
        "elements": [{
            "tag": "markdown",
            "content": f"✏️ {user_name} 选择了手动输入。\n请在对话中发送你的答案。",
        }],
    }


# ── Async resolver — 唤醒 agent 线程 ──

async def resolve_clarify(
    adapter: Any, clarify_id: str, choice: str,
    *, open_id: str = "", chat_id: str = "",
) -> None:
    state = CLARIFY_STATE.get(clarify_id)
    if not state:
        _logger.debug("[feishu-streaming][clarify] %s already resolved or unknown", clarify_id)
        return
    if not adapter._is_interactive_operator_authorized(open_id):  # type: ignore[attr-defined]
        _logger.warning("[feishu-streaming][clarify] Unauthorized click by %s for %s", open_id or "?", clarify_id)
        return
    expected = str(state.get("chat_id", "") or "")
    if expected and chat_id and expected != chat_id:
        _logger.warning("[feishu-streaming][clarify] %s chat mismatch", clarify_id)
        return
    state = CLARIFY_STATE.pop(clarify_id, None)
    if not state:
        return
    try:
        from tools.clarify_gateway import resolve_gateway_clarify

        if resolve_gateway_clarify(clarify_id, choice):
            _logger.info("[feishu-streaming][clarify] resolved id=%s choice=%s user=%s",
                         clarify_id, _truncate(choice, 30), open_id or "?")
        else:
            _logger.warning("[feishu-streaming][clarify] resolve returned False for %s", clarify_id)
    except Exception as exc:
        _logger.warning("[feishu-streaming][clarify] resolve failed: %s", exc, exc_info=True)


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


# ── Sync card-action handler — 镜像官方 _handle_approval_card_action ──

def handle_clarify_card_action(adapter: Any, *, event: Any, action_value: dict[str, Any]) -> Any:
    """同步处理 clarify 按钮点击：鉴权 → 调度 resolve/awaiting → 返回同步替换卡."""
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
        return _empty()
    if not CLARIFY_STATE.get(clarify_id):
        _logger.debug("[feishu-streaming][clarify] %s already resolved or unknown", clarify_id)
        return _empty()

    operator = getattr(event, "operator", None)
    open_id = str(getattr(operator, "open_id", "") or "")
    if not adapter._is_interactive_operator_authorized(open_id):  # type: ignore[attr-defined]
        _logger.warning("[feishu-streaming][clarify] Unauthorized click by %s", open_id or "?")
        return _empty()

    callback_chat_id = str(getattr(getattr(event, "context", None), "open_chat_id", "") or "")
    expected = str(CLARIFY_STATE.get(clarify_id, {}).get("chat_id", "") or "")
    if callback_chat_id and expected and callback_chat_id != expected:
        _logger.warning("[feishu-streaming][clarify] %s callback chat mismatch", clarify_id)
        return _empty()

    user_name = adapter._get_cached_sender_name(open_id) or open_id  # type: ignore[attr-defined]
    action = str(action_value.get("hermes_clarify_action", "") or "")

    if action == "other":
        # flip 进 text-capture 态；下一条非斜杠消息由 gateway 文本拦截接手
        try:
            from tools.clarify_gateway import mark_awaiting_text

            mark_awaiting_text(clarify_id)
        except Exception as exc:
            _logger.warning("[feishu-streaming][clarify] mark_awaiting_text failed: %s", exc)
        card_data = build_awaiting_clarify_card(user_name=user_name)
    elif action == "choice":
        # 优先 button value 携带的完整文本（entry 可能已被 text-intercept 清掉）
        choice_text = str(action_value.get("text") or "").strip() or _canonical_choice_text(
            clarify_id, action_value.get("index"))
        if not choice_text:
            return _empty()
        if not adapter._submit_on_loop(  # type: ignore[attr-defined]
            adapter._loop,  # type: ignore[attr-defined]
            resolve_clarify(adapter, clarify_id, choice_text,
                            open_id=open_id, chat_id=callback_chat_id),
        ):
            return _empty()
        card_data = build_resolved_clarify_card(choice=choice_text, user_name=user_name)
    else:
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


def card_payload(card: dict[str, Any]) -> str:
    return json.dumps(card, ensure_ascii=False)
