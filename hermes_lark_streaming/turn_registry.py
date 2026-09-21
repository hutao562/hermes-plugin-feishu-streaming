"""回合级 agent 注册表 — 供卡片 footer 计算模型速度（t/s）。

patcher 在 ``_wire_turn_agent_callbacks`` 方法尾（新版布局）把本回合的 agent
以弱引用登记进来；完成态渲染 footer 时从 agent 的滚动历史
（``_api_output_history`` / ``_api_latency_history``，Hermes 维护的 maxlen=10
deque）算出真实生成速度：

    tps = sum(output_tokens) / sum(api 延迟)

与 CLI 状态栏 ``avg_velocity`` 同口径——只算模型生成时间，不含工具等待，
所以比「本回合输出 token / 回合总时长」更能反映模型本身的速度。

按 chat_id 兜底索引：``_wire_turn_agent_callbacks`` 里只有 ``ctx.event_message_id``
（引用消息时是 anchor id），而 complete 钩子拿到的是 ``event.message_id``，
两者可能不一致；同一 chat 同一时刻只有一个活跃回合，故 chat 键足够。
"""

from __future__ import annotations

import threading
import weakref
from typing import Any

_lock = threading.Lock()
# key -> (agent 弱引用, chat_id)
_refs: dict[str, tuple[weakref.ReferenceType[Any], str]] = {}
_MAX_ENTRIES = 200


def _msg_key(message_id: str | None) -> str:
    return (message_id or "").strip()


def _chat_key(chat_id: str | None) -> str:
    return f"chat:{(chat_id or '').strip()}"


def register(
    *,
    message_id: str | None = None,
    chat_id: str | None = None,
    agent: Any = None,
) -> None:
    """登记本回合 agent（弱引用，不阻止 GC）。"""
    if agent is None:
        return
    ref = weakref.ref(agent)
    keys = [k for k in (_msg_key(message_id), _chat_key(chat_id)) if k and k != "chat:"]
    if not keys:
        return
    with _lock:
        if len(_refs) > _MAX_ENTRIES:
            _refs.clear()
        for key in keys:
            _refs[key] = (ref, (chat_id or "").strip())


def velocity(
    *,
    message_id: str | None = None,
    chat_id: str | None = None,
) -> float | None:
    """模型生成速度（tokens/s）。无数据 / 计时异常返回 None。"""
    with _lock:
        entry = _refs.get(_msg_key(message_id)) or _refs.get(_chat_key(chat_id))
    agent = entry[0]() if entry else None
    if agent is None:
        return None
    try:
        lat = list(getattr(agent, "_api_latency_history", None) or [])
        out = list(getattr(agent, "_api_output_history", None) or [])
    except Exception:
        return None
    n = min(len(lat), len(out))
    if n <= 0:
        return None
    total = float(sum(lat[-n:]))
    if total <= 0:
        return None
    tps = float(sum(out[-n:])) / total
    # 与 tui_gateway 相同的护栏：NaN / 负数 / 荒谬 provider 计时一律丢弃。
    if not (0 < tps < 1e6):
        return None
    return tps


def clear(*, message_id: str | None = None, chat_id: str | None = None) -> None:
    """卡片收尾后清理登记（弱引用本身不泄漏，这里只是提前收缩表）。"""
    with _lock:
        for key in (_msg_key(message_id), _chat_key(chat_id)):
            if key and key != "chat:":
                _refs.pop(key, None)
