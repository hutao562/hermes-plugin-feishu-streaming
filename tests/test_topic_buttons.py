"""Model-picker callback routing for Feishu topic threads."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from test_plugin_mode import _FakeBaseAdapter, _mock_client

from plugin.adapter import build_adapter_class
from plugin.engine import ChatCardEngine


@pytest.fixture
def adapter() -> Any:
    engine = ChatCardEngine(_mock_client())
    cls = build_adapter_class(_FakeBaseAdapter, engine)
    inst = cls.__new__(cls)
    _FakeBaseAdapter.__init__(inst)
    inst._engine_instance = engine
    return inst


def _topic_card(adapter: Any, *, chat_id: str = "chat1", thread_id: str = "omt_topic",
                message_id: str = "om_stream_topic") -> None:
    session = adapter._engine()._ensure_session(chat_id, thread_id)
    session.card_msg_id = message_id


def _action_data(message_id: str, *, chat_id: str = "chat1") -> Any:
    return SimpleNamespace(event=SimpleNamespace(
        operator=SimpleNamespace(open_id="ou_admin"),
        context=SimpleNamespace(open_chat_id=chat_id, open_message_id=message_id),
        token="token-1",
    ))


def test_model_route_comes_from_clicked_card_and_checks_chat(adapter: Any) -> None:
    _topic_card(adapter)
    route = adapter._resolve_model_card_route("chat1", "om_stream_topic")
    assert route == ("chat1", "omt_topic", "om_stream_topic")
    assert adapter._resolve_model_card_route("another-chat", "om_stream_topic") is None
    assert adapter._resolve_model_card_route("chat1", "om_unknown") is None


@pytest.mark.asyncio
async def test_picker_replies_in_topic_and_remembers_its_route(adapter: Any) -> None:
    adapter._engine().model_picker_data = lambda chat_id: {
        "current": "model-a", "models": ["model-a", "model-b"]}

    await adapter._send_model_picker(
        "chat1", thread_id="omt_topic", reply_to_message_id="om_stream_topic")

    adapter._engine()._client.send_card_to_chat.assert_awaited_once()
    args = adapter._engine()._client.send_card_to_chat.await_args
    assert args.args[0] == "chat1"
    assert args.kwargs["reply_to_message_id"] == "om_stream_topic"
    assert adapter._resolve_model_card_route("chat1", "om_card_msg") == (
        "chat1", "omt_topic", "om_stream_topic")


@pytest.mark.asyncio
async def test_switch_dispatch_preserves_verified_topic_through_guards(
    adapter: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, Any]] = []
    handled: list[Any] = []

    event_mod = types.ModuleType("gateway.platforms.event")

    @dataclass(frozen=True)
    class MessageEvent:
        text: str
        message_type: Any
        source: Any
        raw_message: Any
        message_id: str
        channel_prompt: str | None
        timestamp: Any

    event_mod.MessageEvent = MessageEvent
    event_mod.MessageType = SimpleNamespace(COMMAND="command")
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.platforms", types.ModuleType("gateway.platforms"))
    monkeypatch.setitem(sys.modules, "gateway.platforms.event", event_mod)

    async def sender_profile(sender_id: Any) -> dict[str, str]:
        assert sender_id.open_id == "ou_admin"
        return {"user_id": "u1", "user_name": "Admin", "user_id_alt": "ou_admin"}

    async def chat_info(chat_id: str) -> dict[str, str]:
        return {"raw_type": "group", "name": "group"}

    def build_source(**kwargs: Any) -> Any:
        built.append(kwargs)
        return SimpleNamespace(**kwargs)

    async def handle_guards(event: Any) -> None:
        handled.append(event)

    adapter._resolve_sender_profile = sender_profile
    adapter.get_chat_info = chat_info
    adapter._resolve_source_chat_type = lambda **kwargs: "group"
    adapter.build_source = build_source
    adapter._resolve_channel_prompt = lambda chat_id: "prompt"
    adapter._handle_message_with_guards = handle_guards

    await adapter._dispatch_model_switch(
        chat_id="chat1", open_id="ou_admin", target="model-b", raw=object(),
        message_id="token", thread_id="omt_topic")

    assert built[0]["thread_id"] == "omt_topic"
    assert len(handled) == 1
    assert handled[0].source.thread_id == "omt_topic"
    assert handled[0].source.chat_id == "chat1"
    assert handled[0].text == "/model model-b"
    assert handled[0].message_id == "token"


@pytest.mark.asyncio
async def test_switch_action_passes_card_verified_topic_not_action_value(adapter: Any) -> None:
    _topic_card(adapter)
    scheduled: list[Any] = []
    dispatched: list[dict[str, Any]] = []

    async def dispatch(**kwargs: Any) -> None:
        dispatched.append(kwargs)

    adapter._loop = object()
    adapter._card_response = lambda *a, **k: "ack"
    adapter._is_interactive_operator_authorized = lambda open_id: True
    adapter._is_card_action_duplicate = lambda token: False
    adapter._submit_on_loop = lambda loop, coro: (scheduled.append(coro), True)[1]
    adapter._dispatch_model_switch = dispatch

    response = adapter._handle_model_switch_action(
        data=_action_data("om_stream_topic"),
        action_value={"hermes_model_action": "switch", "target": "model-b",
                      "thread_id": "omt_forged"})

    assert response != "ignored"
    assert len(scheduled) == 1
    await scheduled[0]
    assert dispatched[0]["thread_id"] == "omt_topic"
    assert dispatched[0]["chat_id"] == "chat1"


def test_unrecognized_card_callback_is_dropped_without_action_value_fallback(adapter: Any) -> None:
    scheduled: list[Any] = []
    adapter._card_response = lambda *a, **k: "ignored"
    adapter._is_interactive_operator_authorized = lambda open_id: True
    adapter._is_card_action_duplicate = lambda token: False
    adapter._submit_on_loop = lambda loop, coro: (scheduled.append(coro), True)[1]
    adapter._loop = object()

    response = adapter._handle_model_switch_action(
        data=_action_data("om_unknown"),
        action_value={"hermes_model_action": "switch", "target": "model-b",
                      "thread_id": "omt_forged"})

    assert response == "ignored"
    assert scheduled == []
