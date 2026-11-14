"""Delivery must retain topic identity even when another topic has a newer card."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_plugin_mode import _FakeBaseAdapter, _mock_client

from plugin import _compat
from plugin.adapter import build_adapter_class
from plugin.engine import ChatCardEngine, ChatSession


def make_adapter():
    engine = ChatCardEngine(_mock_client())
    adapter = build_adapter_class(_FakeBaseAdapter, engine)()
    for topic, created in [(None, 1), ("omt_a", 2), ("omt_b", 3)]:
        suffix = topic or "main"
        session = ChatSession(
            chat_id="chat", thread_id=topic, reply_to=f"om_user_{suffix}",
            state="completed", created_at=created, completed_at=created,
            card_id=f"card_{suffix}", card_msg_id=f"om_card_{suffix}")
        engine._sessions[engine._skey("chat", topic)] = session
    return adapter, engine


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", [None, "omt_a", "omt_b"])
async def test_notice_does_not_pick_newest_card(topic):
    adapter, engine = make_adapter()
    result = await adapter.send("chat", "background complete", metadata={"thread_id": topic})
    assert result.success
    updated = engine._client.cardkit_update.call_args.args[0]
    assert updated == f"card_{topic or 'main'}"
    assert not adapter.native_sends


@pytest.mark.asyncio
async def test_unknown_topic_falls_back_with_original_route():
    adapter, engine = make_adapter()
    metadata = {"thread_id": "omt_missing"}
    await adapter.send("chat", "background complete", metadata=metadata)
    engine._client.cardkit_update.assert_not_called()
    assert adapter.native_sends == [("chat", "background complete", None, metadata)]


@pytest.mark.asyncio
async def test_known_legacy_message_anchor_resolves_only_its_card():
    adapter, engine = make_adapter()
    await adapter.send("chat", "background complete", metadata={"thread_id": "om_user_omt_a"})
    assert engine._client.cardkit_update.call_args.args[0] == "card_omt_a"


@pytest.mark.asyncio
async def test_regular_topic_final_completes_instead_of_becoming_notice():
    adapter, engine = make_adapter()
    engine.session_for("chat", "omt_a").state = "streaming"
    engine.complete = AsyncMock(return_value="om_card_omt_a")
    engine.append_notice = AsyncMock()
    await adapter.send("chat", "final answer", reply_to="om_user_omt_a",
                       metadata={"thread_id": "omt_a", "notify": True})
    engine.complete.assert_awaited_once_with("chat", "final answer", thread_id="omt_a")
    engine.append_notice.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_notice_without_card_falls_back_without_completing_active_turn():
    adapter, engine = make_adapter()
    active = ChatSession(
        chat_id="chat", thread_id="omt_a", reply_to="om_user_omt_a",
        state="streaming", created_at=4,
    )
    engine._sessions[engine._skey("chat", "omt_a")] = active
    engine.complete = AsyncMock(return_value="unexpected")
    engine.append_notice = AsyncMock(return_value=None)

    metadata = {"thread_id": "omt_a"}
    result = await adapter.send("chat", "background complete", metadata=metadata)

    assert result.success
    engine.complete.assert_not_awaited()
    assert adapter.native_sends == [("chat", "background complete", None, metadata)]


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", [None, "omt_a", "omt_b", "omt_missing"])
async def test_documents_use_only_the_requested_topic(topic):
    adapter, _engine = make_adapter()
    adapter._reply_file_to_card = AsyncMock(return_value=True)
    await adapter.send_document("chat", "/tmp/report.pdf", metadata={"thread_id": topic})
    if topic == "omt_missing":
        adapter._reply_file_to_card.assert_not_awaited()
        assert adapter.native_docs
    else:
        assert adapter._reply_file_to_card.call_args.args[0] == f"om_card_{topic or 'main'}"
        assert not adapter.native_docs


@pytest.mark.asyncio
async def test_synthetic_status_id_cannot_update_card_in_another_chat():
    adapter, engine = make_adapter()
    await adapter.edit_message("other-chat", "lark-card:om_card_omt_a", "working")
    assert not engine.session_for("chat", "omt_a").heartbeat_text


@pytest.mark.asyncio
async def test_inbound_before_probe_opens_loading_card_in_exact_topic():
    engine = ChatCardEngine(_mock_client())
    engine.note_inbound("chat", "om_user_a", thread_id="omt_a")
    engine.note_inbound("chat", "om_user_b", thread_id="omt_b")
    engine.on_turn_started("chat", thread_id="omt_b")
    engine.on_turn_started("chat", thread_id="omt_a")
    a, b = engine.session_for("chat", "omt_a"), engine.session_for("chat", "omt_b")
    await asyncio.gather(a.card_create_task, b.card_create_task)
    assert a.reply_to == a.turn_inbound == "om_user_a"
    assert b.reply_to == b.turn_inbound == "om_user_b"
    anchors = {call.args[0] for call in engine._client.reply_card_by_id.call_args_list}
    assert anchors == {"om_user_a", "om_user_b"}
    engine._client.send_card_to_chat.assert_not_called()


@pytest.mark.asyncio
async def test_tool_routes_are_task_local_with_two_topics(monkeypatch):
    adapter, engine = make_adapter()
    engine.on_turn_started = lambda *args, **kwargs: None
    engine.on_tool_start = lambda *args, **kwargs: received.append(kwargs["anchor"])
    monkeypatch.setattr(_compat, "is_tool_chunk", lambda _: True)
    received = []
    ready, done = asyncio.Event(), asyncio.Event()

    async def topic_a():
        adapter.supports_draft_streaming(chat_id="chat", metadata={"thread_id": "omt_a"})
        ready.set()
        await done.wait()
        adapter.format_tool_event(SimpleNamespace(tool_name="a", preview="", args={}))

    async def topic_b():
        await ready.wait()
        adapter.supports_draft_streaming(chat_id="chat", metadata={"thread_id": "omt_b"})
        adapter.format_tool_event(SimpleNamespace(tool_name="b", preview="", args={}))
        done.set()

    await asyncio.gather(topic_a(), topic_b())
    assert received == [("chat", "omt_b"), ("chat", "omt_a")]


@pytest.mark.asyncio
async def test_unmarked_tool_progress_does_not_seal_topic_card():
    engine = ChatCardEngine(_mock_client())
    adapter = build_adapter_class(_FakeBaseAdapter, engine)()
    engine.on_turn_started("chat", thread_id="omt_topic")
    engine.note_inbound("chat", "om_user", thread_id="omt_topic")
    session = engine.active_session("chat", "omt_topic")
    assert session is not None
    await session.card_create_task

    engine.on_tool_start("terminal", "printf topic-routing-ok",
                         anchor=("chat", "omt_topic"))
    progress = "💻 terminal\n```\nprintf topic-routing-ok\n```"
    result = await adapter.send(
        "chat", progress, reply_to="om_user", metadata={"thread_id": "omt_topic"},
    )

    assert result.success
    assert engine.session_for("chat", "omt_topic") is session
    assert session.state == "streaming"
    assert session.heartbeat_text == progress
    engine._client.cardkit_close_streaming.assert_not_called()

    result = await adapter.send(
        "chat", "话题路由验收通过", reply_to="om_user",
        metadata={"thread_id": "omt_topic", "notify": True},
    )

    assert result.message_id == "om_card_msg"
    assert session.state == "completed"
    engine._client.send_card_to_chat.assert_not_called()
    engine._client.reply_card_by_id.assert_awaited_once()


@pytest.mark.asyncio
async def test_choice_picker_confirmation_with_synthetic_inbound_is_final():
    """Card-action commands use a synthetic c-* token, not a tool-progress om_* turn."""
    engine = ChatCardEngine(_mock_client())
    adapter = build_adapter_class(_FakeBaseAdapter, engine)()
    engine.on_turn_started("chat", thread_id="omt_topic")
    engine.note_inbound("chat", "c-choice-token", thread_id="omt_topic")
    session = engine.active_session("chat", "omt_topic")
    assert session is not None
    await session.card_create_task

    result = await adapter.send(
        "chat", "🧠 ✓ 推理强度已设置为 `medium`",
        metadata={"thread_id": "omt_topic", "reply_to_message_id": "om_choice_root"},
    )

    assert result.success
    assert result.message_id == "om_card_msg"
    assert session.state == "completed"
    engine._client.cardkit_close_streaming.assert_awaited_once()
    engine._client.send_card_to_chat.assert_not_called()


@pytest.mark.asyncio
async def test_typed_command_confirmation_on_real_inbound_is_final():
    engine = ChatCardEngine(_mock_client())
    adapter = build_adapter_class(_FakeBaseAdapter, engine)()
    engine.on_turn_started("chat", thread_id="omt_topic")
    engine.note_inbound("chat", "om_command", thread_id="omt_topic")
    session = engine.active_session("chat", "omt_topic")
    assert session is not None
    await session.card_create_task

    result = await adapter.send(
        "chat", "🧠 ✓ 推理强度已设置为 `medium`",
        metadata={"thread_id": "omt_topic", "reply_to_message_id": "om_command"},
    )

    assert result.success
    assert session.state == "completed"
    engine._client.cardkit_close_streaming.assert_awaited_once()
