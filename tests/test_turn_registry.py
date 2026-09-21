"""turn_registry 测试 — 模型速度（t/s）登记与计算."""

from __future__ import annotations

import gc
from collections import deque

from hermes_lark_streaming import turn_registry


class _FakeAgent:
    def __init__(self, lat=None, out=None):
        self._api_latency_history = deque(lat or [], maxlen=10)
        self._api_output_history = deque(out or [], maxlen=10)


def setup_function() -> None:
    turn_registry._refs.clear()


def test_velocity_sum_over_sum() -> None:
    agent = _FakeAgent(lat=[1.0, 3.0], out=[10, 90])
    turn_registry.register(message_id="om_1", chat_id="oc_1", agent=agent)
    # (10 + 90) / (1 + 3) = 25 t/s
    assert turn_registry.velocity(message_id="om_1", chat_id="oc_1") == 25.0


def test_velocity_falls_back_to_chat_key() -> None:
    agent = _FakeAgent(lat=[2.0], out=[40])
    turn_registry.register(message_id="anchor_1", chat_id="oc_1", agent=agent)
    # complete 时拿到的是 event.message_id，与注册键不同 → chat 兜底
    assert turn_registry.velocity(message_id="om_other", chat_id="oc_1") == 20.0


def test_velocity_none_without_data() -> None:
    assert turn_registry.velocity(message_id="om_x", chat_id="oc_x") is None
    agent = _FakeAgent()
    turn_registry.register(message_id="om_2", chat_id="oc_2", agent=agent)
    assert turn_registry.velocity(message_id="om_2", chat_id="oc_2") is None


def test_velocity_rejects_zero_latency_and_absurd() -> None:
    zero = _FakeAgent(lat=[0.0], out=[50])
    turn_registry.register(message_id="om_z", chat_id="oc_z", agent=zero)
    assert turn_registry.velocity(message_id="om_z", chat_id="oc_z") is None

    absurd = _FakeAgent(lat=[1.0], out=[10**9])
    turn_registry.register(message_id="om_a", chat_id="oc_a", agent=absurd)
    assert turn_registry.velocity(message_id="om_a", chat_id="oc_a") is None


def test_velocity_ignores_missing_attrs() -> None:
    class _Bare:
        pass

    bare = _Bare()
    turn_registry.register(message_id="om_b", chat_id="oc_b", agent=bare)
    assert turn_registry.velocity(message_id="om_b", chat_id="oc_b") is None


def test_register_is_weakref() -> None:
    agent = _FakeAgent(lat=[1.0], out=[10])
    turn_registry.register(message_id="om_w", chat_id="oc_w", agent=agent)
    del agent
    gc.collect()
    assert turn_registry.velocity(message_id="om_w", chat_id="oc_w") is None


def test_clear_removes_entries() -> None:
    agent = _FakeAgent(lat=[1.0], out=[10])
    turn_registry.register(message_id="om_c", chat_id="oc_c", agent=agent)
    turn_registry.clear(message_id="om_c", chat_id="oc_c")
    assert turn_registry.velocity(message_id="om_c", chat_id="oc_c") is None
