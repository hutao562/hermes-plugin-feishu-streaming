"""plugin 模式测试 — ChatCardEngine + StreamingFeishuMixin + manifest/register.

不依赖 hermes 源树（_compat 兜底），FeishuClient 以 AsyncMock 替身注入。
生产里引擎入口都在事件循环内（send_draft / format_tool_event 跨线程），
故测试统一以 asyncio 驱动。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from plugin import _compat
from plugin.adapter import build_adapter_class
from plugin.engine import ChatCardEngine

PLUGIN_DIR = Path(__file__).parent.parent / "plugin"


# ── 替身 ──


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.cardkit_create = AsyncMock(return_value="card_abc")
    client.reply_card_by_id = AsyncMock(return_value="om_card_msg")
    client.cardkit_batch_update = AsyncMock()
    client.cardkit_stream_element = AsyncMock()
    client.cardkit_close_streaming = AsyncMock()
    client.cardkit_update = AsyncMock()
    client.send_card_to_chat = AsyncMock(return_value="om_card_msg")
    return client


async def _settle(engine: ChatCardEngine, *, flush_delay: bool = True) -> None:
    """跑完建卡 task + flush timer（BATCH_AFTER_GAP_MS=0.3s 延迟）."""
    for _ in range(3):
        await asyncio.sleep(0.35)  # 先睡过 FlushController 的长间隔批延迟窗口
        for _ in range(20):
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task() and not t.done()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.sleep(0)


def _streamed_texts(client: MagicMock, el_id: str) -> list[str]:
    return [c.args[2] for c in client.cardkit_stream_element.call_args_list if c.args[1] == el_id]


@pytest.fixture
def client() -> MagicMock:
    return _mock_client()


@pytest.fixture
def engine(client: MagicMock) -> ChatCardEngine:
    return ChatCardEngine(client)


# ── engine：draft 驱动 ──


@pytest.mark.asyncio
async def test_draft_creates_card_and_streams_answer(engine: ChatCardEngine) -> None:
    engine.on_draft("chat1", "你好")
    await _settle(engine)

    session = engine.session_for("chat1")
    assert session is not None and session.state == "streaming"
    assert session.card_id == "card_abc"
    assert session.card_msg_id == "om_card_msg"
    engine._client.cardkit_create.assert_called_once()
    assert session.answer_seg is not None
    assert "你好" in _streamed_texts(engine._client, session.answer_seg.el_id)


@pytest.mark.asyncio
async def test_snapshot_replace_does_not_duplicate(engine: ChatCardEngine) -> None:
    engine.on_draft("chat1", "hello ")
    await _settle(engine)
    engine.on_draft("chat1", "hello world")  # 全量快照（扩展）
    await _settle(engine)

    session = engine.session_for("chat1")
    assert session is not None and session.answer_seg is not None
    assert session.answer_seg.text == "hello world"  # 置换而非追加
    texts = _streamed_texts(engine._client, session.answer_seg.el_id)
    assert texts[-1] == "hello world"


@pytest.mark.asyncio
async def test_new_turn_after_completion_opens_new_session(engine: ChatCardEngine) -> None:
    engine.on_draft("chat1", "第一回合")
    await _settle(engine)
    await engine.complete("chat1", "第一回合")
    assert engine.session_for("chat1").state == "completed"

    engine.on_draft("chat1", "第二回合")
    await _settle(engine)
    # 新回合开新卡（旧卡留在聊天里）
    assert engine._client.cardkit_create.call_count == 2
    assert engine.session_for("chat1").state == "streaming"


@pytest.mark.asyncio
async def test_tool_lifecycle_updates_tracker_and_panel(engine: ChatCardEngine) -> None:
    engine.on_draft("chat1", "查一下")
    await _settle(engine)
    engine._client.cardkit_batch_update.reset_mock()

    engine.on_tool_start("web_search", "hermes")
    engine.on_tool_end("web_search", output="result")
    await _settle(engine)

    session = engine.session_for("chat1")
    steps = session.tool_tracker.build_display_steps()
    assert len(steps) == 1 and steps[0]["status"] == "success"
    assert session.tool_seg is not None and session.tool_seg.created
    assert engine._client.cardkit_batch_update.called


@pytest.mark.asyncio
async def test_heartbeat_goes_to_heartbeat_element(engine: ChatCardEngine) -> None:
    from hermes_lark_streaming.cardkit.builder import HEARTBEAT_ELEMENT_ID

    engine.on_draft("chat1", "工作中")
    await _settle(engine)
    engine._client.cardkit_stream_element.reset_mock()

    engine.on_heartbeat("chat1", "⏳ Working — 2 min")
    await _settle(engine)

    texts = _streamed_texts(engine._client, HEARTBEAT_ELEMENT_ID)
    assert texts and "Working" in texts[-1]


@pytest.mark.asyncio
async def test_complete_renders_final_card(engine: ChatCardEngine) -> None:
    engine.on_draft("chat1", "答案")
    await _settle(engine)
    msg_id = await engine.complete("chat1", "最终答案")

    assert msg_id == "om_card_msg"
    engine._client.cardkit_close_streaming.assert_called_once()
    engine._client.cardkit_update.assert_called_once()
    assert engine.session_for("chat1").state == "completed"


@pytest.mark.asyncio
async def test_complete_without_card_returns_none(engine: ChatCardEngine) -> None:
    assert await engine.complete("ghost", "text") is None


@pytest.mark.asyncio
async def test_draft_with_reply_anchor_replies_to_user_message(engine: ChatCardEngine) -> None:
    """draft 帧带 metadata 锚（transport _draft_metadata 注入）→ 卡片 reply 到用户消息."""
    engine.on_draft("chat1", "内容", reply_to="om_user_msg")
    await _settle(engine)

    session = engine.session_for("chat1")
    assert session.reply_to == "om_user_msg"
    engine._client.reply_card_by_id.assert_called_once_with("om_user_msg", session.card_id)
    assert not engine._client.send_card_to_chat.called


@pytest.mark.asyncio
async def test_draft_without_anchor_sends_to_chat(engine: ChatCardEngine) -> None:
    """无锚 draft → 直发 chat（chat_id 不是合法 reply 目标，reply 会 230001）."""
    engine.on_draft("chat1", "内容")  # 无 reply_to
    await _settle(engine)

    session = engine.session_for("chat1")
    assert session.reply_to is None
    engine._client.send_card_to_chat.assert_called_once()
    assert not engine._client.reply_card_by_id.called


@pytest.mark.asyncio
async def test_send_draft_passes_metadata_anchor(adapter) -> None:
    result = await adapter.send_draft(
        "chat1", 1, "内容", metadata={"reply_to_message_id": "om_user_msg"})
    await _settle(adapter._engine())

    assert result.success is True
    assert adapter._engine().session_for("chat1").reply_to == "om_user_msg"
    adapter._engine()._client.reply_card_by_id.assert_called_once()


@pytest.mark.asyncio
async def test_reasoning_single_session_routing(engine: ChatCardEngine) -> None:
    engine.on_draft("chat1", "thinking body")
    await _settle(engine)
    engine.on_reasoning("", "💭 思考内容")  # chat 未知 → 单会话兜底
    await _settle(engine)

    session = engine.session_for("chat1")
    reasoning_segs = [s for s in session.segment_state.segments if s.type.value == "reasoning"]
    assert reasoning_segs and "思考" in reasoning_segs[0].text


# ── adapter：draft 契约与出站拦截 ──


class _FakeBaseAdapter:
    """最小基类替身 — 记录原生调用."""

    def __init__(self, config: Any = None) -> None:
        self.native_sends: list[tuple] = []
        self.native_edits: list[tuple] = []
        self.native_docs: list[tuple] = []

    async def send(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
        self.native_sends.append((chat_id, content, reply_to, metadata))
        return _compat.send_result(success=True, message_id="om_native")

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, **kwargs):
        self.native_edits.append((chat_id, message_id, content, finalize))
        return _compat.send_result(success=True, message_id=message_id)

    async def send_document(self, chat_id, file_path, *, file_name=None, **kwargs):
        self.native_docs.append((chat_id, file_path))
        return _compat.send_result(success=True, message_id="om_doc")

    async def upload_document(self, file_path, *, file_name=None):
        return "file_key_1"

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def reply_file_by_id(self, message_id, file_key, file_name):
        return True


@pytest.fixture
def adapter(client: MagicMock) -> Any:
    engine = ChatCardEngine(client)
    cls = build_adapter_class(_FakeBaseAdapter, engine)
    inst = cls.__new__(cls)  # 跳过真实 FeishuAdapter.__init__（依赖平台配置）
    _FakeBaseAdapter.__init__(inst)  # 显式初始化替身状态（native_sends 等）
    inst._engine_instance = engine
    return inst


@pytest.mark.asyncio
async def test_draft_streaming_contract(adapter) -> None:
    assert adapter.supports_draft_streaming() is True
    assert adapter.draft_stream_is_message is True


@pytest.mark.asyncio
async def test_adapter_send_draft_feeds_engine(adapter) -> None:
    result = await adapter.send_draft("chat1", 1, "内容")
    await _settle(adapter._engine())

    assert result.success is True
    assert adapter._engine().session_for("chat1") is not None


@pytest.mark.asyncio
async def test_format_tool_event_eats_line_and_records(adapter) -> None:
    chunk = SimpleNamespace(tool_name="web_search", preview="query", args={"q": "hermes"})
    assert adapter.format_tool_event(chunk, mode="all") is None
    adapter._engine().on_draft("chat1", "searching")
    await _settle(adapter._engine())
    adapter._engine().on_tool_start("web_search", "query")
    steps = adapter._engine().session_for("chat1").tool_tracker.build_display_steps()
    assert steps and steps[0]["name"] == "web_search"


@pytest.mark.asyncio
async def test_send_interim_routes_to_heartbeat_not_native(adapter) -> None:
    adapter._engine().on_draft("chat1", "流式内容")
    await _settle(adapter._engine())

    result = await adapter.send("chat1", "⏳ Working — 1 min", metadata={"_interim_send": True})

    assert result.success is True
    assert result.message_id.startswith("lark-card:")
    assert adapter._engine().session_for("chat1").state == "streaming"  # 不算完成
    assert adapter.native_sends == []


@pytest.mark.asyncio
async def test_send_busy_ack_prefix_routes_to_heartbeat(adapter) -> None:
    adapter._engine().on_draft("chat1", "流式内容")
    await _settle(adapter._engine())

    result = await adapter.send("chat1", "↪ Redirected current run",
                                reply_to="om_1", metadata={"notify": True})

    assert result.success is True
    # ↪ ack 即刻收旧开新：ack 文本进新卡心跳行；新卡尚在建，无消息 id 可回
    assert result.message_id is None
    assert "Redirected" in adapter._engine().session_for("chat1").heartbeat_text
    assert adapter.native_sends == []


@pytest.mark.asyncio
async def test_send_final_text_completes_card(adapter) -> None:
    adapter._engine().on_draft("chat1", "部分")
    await _settle(adapter._engine())

    result = await adapter.send("chat1", "部分 + 最终答案")

    assert result.success is True
    assert result.message_id == "om_card_msg"  # 完成卡的消息 id
    assert adapter._engine().session_for("chat1").state == "completed"
    adapter._engine()._client.cardkit_update.assert_called_once()
    assert adapter.native_sends == []  # 终态被完成卡接管，不走原生


@pytest.mark.asyncio
async def test_send_without_active_session_falls_to_native(adapter) -> None:
    result = await adapter.send("ghost_chat", "普通消息")
    assert result.message_id == "om_native"
    assert adapter.native_sends[0][1] == "普通消息"


@pytest.mark.asyncio
async def test_send_conversation_final_without_session_opens_card(adapter) -> None:
    """有 reply 锚的终态文本（draft 帧被 MIN_CHARS 吞掉时）→ 现场开卡即完成."""
    result = await adapter.send("chat_fresh", "短回答", reply_to="om_anchor",
                                metadata={"notify": True})

    assert result.success is True
    assert result.message_id == "om_card_msg"  # 卡片消息 id，非原生文本
    assert adapter.native_sends == []
    assert adapter._engine().session_for("chat_fresh").state == "completed"


@pytest.mark.asyncio
async def test_send_unanchored_notice_stays_native(adapter) -> None:
    """无锚通知（bg watcher 等）保持原生文本，不开卡."""
    result = await adapter.send("ghost_chat", "Background task finished", metadata=None)
    assert result.message_id == "om_native"
    assert adapter._engine().session_for("ghost_chat") is None


@pytest.mark.asyncio
async def test_edit_synthetic_card_id_routes_to_heartbeat(adapter) -> None:
    adapter._engine().on_draft("chat1", "流式内容")
    await _settle(adapter._engine())
    session = adapter._engine().session_for("chat1")

    result = await adapter.edit_message(
        "chat1", f"lark-card:{session.card_msg_id}", "⏳ Working — 3 min")

    assert result.success is True
    assert result.message_id.startswith("lark-card:")


@pytest.mark.asyncio
async def test_send_document_replies_to_card(adapter) -> None:
    adapter._engine().on_draft("chat1", "给你文件")
    await _settle(adapter._engine())
    msg_id = await adapter._engine().complete("chat1", "给你文件")

    result = await adapter.send_document("chat1", "/tmp/report.pdf")

    assert result.success is True and result.message_id == msg_id
    assert adapter.native_docs == []  # 卡片投递成功，不走原生 send_document


@pytest.mark.asyncio
async def test_send_document_without_card_falls_to_native(adapter) -> None:
    await adapter.send_document("ghost_chat", "/tmp/x.pdf")
    assert adapter.native_docs


# ── manifest 与 register ──


def test_plugin_manifest_is_platform_kind() -> None:
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["kind"] == "platform"
    # manifest 名只须唯一且带 -platform 后缀；平台名由 register_platform(name="feishu") 决定
    assert manifest["name"].endswith("-platform")
    assert "FEISHU_APP_ID" in [e["name"] for e in manifest["requires_env"]]


def test_register_replaces_bundled_feishu() -> None:
    from plugin import register

    ctx = MagicMock()
    register(ctx)

    assert ctx.register_platform.called
    kwargs = ctx.register_platform.call_args.kwargs
    # 关键：平台名必须是 "feishu" 才能以 last-writer-wins 顶替 bundled feishu-platform
    assert kwargs["name"] == "feishu"
    assert callable(kwargs["adapter_factory"])
    assert kwargs["cron_deliver_env_var"] == "FEISHU_HOME_CHANNEL"
    assert kwargs["standalone_sender_fn"] is not None
    # reasoning 观察钩子已注册
    hook_calls = [c for c in ctx.register_hook.call_args_list if c.args[0] == "on_stream_delta"]
    assert hook_calls


def test_factory_binds_engine_client_to_adapter_lark_client(monkeypatch) -> None:
    """factory 实例化后把官方 adapter 的 lark client 绑给引擎（profile 凭据）."""
    import plugin.adapter as adapter_mod

    class _FakeBaseWithClient(_FakeBaseAdapter):
        def __init__(self, config=None):
            self._client = "lark-sdk-client"

    monkeypatch.setattr(adapter_mod, "_import_base_adapter", lambda: _FakeBaseWithClient)
    bound: dict = {}
    proxy = SimpleNamespace(bind_source=lambda src: bound.__setitem__("source", src))
    engine = ChatCardEngine(_mock_client())

    factory = adapter_mod.create_adapter_factory(engine, client_proxy=proxy)
    adapter = factory(SimpleNamespace())

    assert isinstance(adapter, _FakeBaseWithClient)
    assert "source" in bound
    client = bound["source"]()
    # FeishuClient 包装器且内持官方 lark client
    assert getattr(client, "_client", None) == "lark-sdk-client"


def test_register_hook_routes_reasoning_only() -> None:
    from plugin import register

    ctx = MagicMock()
    register(ctx)
    hook = next(c.args[1] for c in ctx.register_hook.call_args_list
                if c.args[0] == "on_stream_delta")

    hook(kind="text", text="answer chunk")  # text kind → 不进引擎
    hook(kind="reasoning", text="💭 思考")  # 无活跃会话 → 静默丢弃
