"""Tool callbacks must never guess a chat or topic before routing is known."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin.engine import ChatCardEngine


def _engine() -> ChatCardEngine:
    client = MagicMock()
    client.cardkit_create = AsyncMock(return_value="card")
    client.reply_card_by_id = AsyncMock(return_value="message")
    client.send_card_to_chat = AsyncMock(return_value="message")
    client.cardkit_batch_update = AsyncMock()
    client.cardkit_stream_element = AsyncMock()
    client.cardkit_close_streaming = AsyncMock()
    client.cardkit_update = AsyncMock()
    return ChatCardEngine(client)


def _tool_names(engine: ChatCardEngine, chat_id: str, thread_id: str | None = None) -> list[str]:
    session = engine.session_for(chat_id, thread_id)
    return ([step["name"] for step in session.tool_tracker.build_display_steps()]
            if session else [])


@pytest.mark.asyncio
async def test_unanchored_tool_before_probe_does_not_create_empty_chat_card() -> None:
    engine = _engine()

    engine.on_tool_start("web_search", "query")

    assert engine._sessions == {}
    engine._client.cardkit_create.assert_not_called()


@pytest.mark.asyncio
async def test_unanchored_tool_is_dropped_when_multiple_topics_are_active() -> None:
    engine = _engine()
    engine.on_turn_started("chat", thread_id="topic-a")
    engine.on_turn_started("chat", thread_id="topic-b")

    engine.on_tool_start("terminal", "ls")

    await asyncio.gather(*(s.card_create_task for s in engine._sessions.values()
                           if s.card_create_task is not None))

    assert _tool_names(engine, "chat", "topic-a") == []
    assert _tool_names(engine, "chat", "topic-b") == []


@pytest.mark.asyncio
async def test_exact_topic_anchor_routes_and_unknown_anchor_never_falls_back() -> None:
    engine = _engine()
    engine.on_turn_started("chat", thread_id="topic-a")
    engine.on_turn_started("chat", thread_id="topic-b")

    engine.on_tool_start("web_search", "topic b", anchor=("chat", "topic-b"))
    engine.on_tool_start("terminal", "stale", anchor=("chat", "missing-topic"))

    await asyncio.gather(*(s.card_create_task for s in engine._sessions.values()
                           if s.card_create_task is not None))

    assert _tool_names(engine, "chat", "topic-a") == []
    assert _tool_names(engine, "chat", "topic-b") == ["web_search"]


@pytest.mark.asyncio
async def test_tool_finish_uses_exact_anchor_when_multiple_topics_are_active() -> None:
    engine = _engine()
    engine.on_turn_started("chat", thread_id="topic-a")
    engine.on_turn_started("chat", thread_id="topic-b")
    engine.on_tool_start("terminal", "a", anchor=("chat", "topic-a"))
    engine.on_tool_start("terminal", "b", anchor=("chat", "topic-b"))

    engine.on_tool_end("terminal", output="a result", anchor=("chat", "topic-a"))

    a = engine.session_for("chat", "topic-a")
    b = engine.session_for("chat", "topic-b")
    assert a is not None and b is not None
    assert [step["status"] for step in a.tool_tracker.build_display_steps()] == ["success"]
    assert [step["status"] for step in b.tool_tracker.build_display_steps()] == ["running"]
