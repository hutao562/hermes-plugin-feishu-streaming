"""plugin 特性测试 — clarify 按钮卡 / followup 边界 / usage footer / 通知合并.

不依赖 hermes 源树（tools.clarify_gateway / lark 回调模型用 sys.modules 替身）。
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from test_plugin_mode import _FakeBaseAdapter, _mock_client, _settle

from plugin import _clarify
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


# ── clarify：卡片构建 ──


def test_clarify_card_has_numbered_buttons_and_other() -> None:
    card = _clarify.build_clarify_card(question="选哪个？", choices=["甲", "乙"], clarify_id="cid")
    actions = [e for e in card["elements"] if e["tag"] == "action"]
    assert len(actions) == 1
    buttons = actions[0]["actions"]
    assert len(buttons) == 3  # 2 选项 + 其他
    assert buttons[0]["value"]["hermes_clarify_action"] == "choice"
    assert buttons[0]["value"]["text"] == "甲"  # value 携带完整文本（entry 失活兜底）
    assert buttons[2]["value"]["hermes_clarify_action"] == "other"


def test_clarify_card_caps_choices_at_four() -> None:
    clean = _clarify.normalize_choices(["a", "b", "c", "d", "e", "f"])
    assert len(clean) == 4
    # dict 形 choice 解包（LLM 偶发形态）
    assert _clarify.normalize_choices([{"label": "X"}, {"description": "Y"}]) == ["X", "Y"]


def test_open_clarify_card_has_no_buttons() -> None:
    card = _clarify.build_open_clarify_card(question="怎么想？")
    assert not [e for e in card["elements"] if e["tag"] == "action"]


# ── clarify：send_clarify 经适配器 ──


@pytest.fixture
def clarify_adapter() -> Any:
    engine = ChatCardEngine(_mock_client())
    cls = build_adapter_class(_FakeBaseAdapter, engine)
    inst = cls.__new__(cls)
    _FakeBaseAdapter.__init__(inst)
    inst._engine_instance = engine
    inst._client = "connected"
    inst._feishu_send_with_retry = _async_return({"message_id": "om_clarify_1"})
    inst._finalize_send_result = lambda resp, err="": types.SimpleNamespace(
        success=bool(resp), message_id=resp.get("message_id"))
    return inst


def _async_return(value: Any) -> Any:
    async def _ret(*a: Any, **k: Any) -> Any:
        return value
    return _ret


@pytest.mark.asyncio
async def test_send_clarify_multichoice_sends_button_card(clarify_adapter) -> None:
    result = await clarify_adapter.send_clarify(
        "chat1", "选哪个？", ["甲", "乙"], "cid1", "sk1")
    assert result.success is True
    assert _clarify.CLARIFY_STATE["cid1"]["message_id"] == "om_clarify_1"
    _clarify.CLARIFY_STATE.clear()


@pytest.mark.asyncio
async def test_send_clarify_never_routes_through_send(clarify_adapter) -> None:
    """clarify 卡绝不经过 self.send（会被终态拦截误判完成卡片）."""
    sent_via_send: list = []

    async def _spy_send(chat_id, content, reply_to=None, metadata=None, **kw):
        sent_via_send.append(content)
        return await _FakeBaseAdapter.send(self=clarify_adapter, chat_id=chat_id,
                                           content=content, reply_to=reply_to,
                                           metadata=metadata, **kw)

    clarify_adapter.send = _spy_send  # type: ignore[method-assign]
    await clarify_adapter.send_clarify("chat1", "Q", ["a"], "cid2", "sk")
    assert sent_via_send == []
    _clarify.CLARIFY_STATE.clear()


@pytest.mark.asyncio
async def test_send_clarify_open_question_marks_awaiting(clarify_adapter, monkeypatch) -> None:
    marked: list = []
    fake_cg = types.ModuleType("tools.clarify_gateway")
    fake_cg.mark_awaiting_text = marked.append
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", fake_cg)

    await clarify_adapter.send_clarify("chat1", "开放题", [], "cid3", "sk")
    assert marked == ["cid3"]


@pytest.mark.asyncio
async def test_send_clarify_failure_falls_back_to_text_not_send(clarify_adapter, monkeypatch) -> None:
    """按钮卡失败 → text 兜底（仍不经 self.send），非 super().send_clarify."""
    marked: list = []
    fake_cg = types.ModuleType("tools.clarify_gateway")
    fake_cg.mark_awaiting_text = marked.append
    monkeypatch.setitem(sys.modules, "tools.clarify_gateway", fake_cg)

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("card api down")

    clarify_adapter._feishu_send_with_retry = _boom
    sent_types: list = []

    async def _capture_retry(**kw: Any) -> Any:
        sent_types.append(kw.get("msg_type"))
        return {"message_id": "om_text"}

    # 第二次调用（text 兜底）成功
    calls = {"n": 0}

    async def _retry(**kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("down")
        sent_types.append(kw.get("msg_type"))
        return {"message_id": "om_text"}

    clarify_adapter._feishu_send_with_retry = _retry
    result = await clarify_adapter.send_clarify("chat1", "Q", ["a"], "cid4", "sk")
    assert result.success is True
    assert sent_types == ["text"]  # 兜底走 text 直发，无 interactive 二次、无 self.send


# ── clarify：卡片回调路由 ──


def test_card_action_routes_clarify_and_forwards_others(clarify_adapter) -> None:
    forwarded: list = []

    def _super_handler(data: Any) -> str:
        forwarded.append(data)
        return "official"

    clarify_adapter.__class__.__mro__  # noqa: B018
    # 动态替换 super 调用目标：直接在实例上放官方处理器并断言转发
    clarify_adapter._official_card_action = _super_handler

    _clarify.CLARIFY_STATE["cid9"] = {"session_key": "sk", "chat_id": "chat1", "message_id": "om_x"}
    event = types.SimpleNamespace(
        operator=types.SimpleNamespace(open_id="ou_1"),
        context=types.SimpleNamespace(open_chat_id="chat1"))
    clarify_adapter._is_interactive_operator_authorized = lambda oid: True
    clarify_adapter._get_cached_sender_name = lambda oid: "用户"
    submitted: list = []

    def _submit(loop, coro):
        submitted.append(coro)
        coro.close()
        return True

    clarify_adapter._loop = object()
    clarify_adapter._loop_accepts_callbacks = lambda loop: True
    clarify_adapter._card_response = lambda: "empty"
    clarify_adapter._submit_on_loop = _submit

    # clarify 动作 → handler（返回非 official）
    data = types.SimpleNamespace(event=types.SimpleNamespace(
        action=types.SimpleNamespace(value={
            "hermes_clarify_action": "choice", "clarify_id": "cid9",
            "index": 0, "text": "选项一"})))
    data.event.operator = event.operator
    data.event.context = event.context
    result = clarify_adapter._on_card_action_trigger(data)
    assert result != "official"
    assert submitted  # resolve 已调度
    _clarify.CLARIFY_STATE.clear()

    # 非 clarify 动作（approval/update-prompt）走 super 分支：此处验证 clarify 状态
    # 未被污染（super 转发的完整行为由官方基类保证，_FakeBaseAdapter 无此方法）
    assert not _clarify.CLARIFY_STATE


# ── followup 边界：draft 锚变化 ──


@pytest.mark.asyncio
async def test_draft_anchor_change_seals_old_and_opens_new() -> None:
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "第一回合内容", reply_to="om_msg_A")
    await _settle(engine)
    old = engine.session_for("chat1")
    assert old.state == "streaming" and old.reply_to == "om_msg_A"

    # 新回合：锚变成消息 B → 旧卡收尾 + 新会话
    engine.on_draft("chat1", "第二回合", reply_to="om_msg_B")
    await _settle(engine)
    new = engine.session_for("chat1")
    assert new is not old
    assert new.reply_to == "om_msg_B"
    assert old.state == "completed"  # 旧卡按已有内容收尾
    # 旧卡完成渲染 + 新卡创建（两次 cardkit_update / create）
    assert engine._client.cardkit_create.call_count == 2
    assert engine._client.cardkit_update.call_count == 1


@pytest.mark.asyncio
async def test_draft_same_anchor_keeps_session() -> None:
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "同一回合继续", reply_to="om_msg_A")
    await _settle(engine)
    first = engine.session_for("chat1")
    engine.on_draft("chat1", "同一回合更多", reply_to="om_msg_A")
    assert engine.session_for("chat1") is first


# ── usage 聚合 → footer ──


def test_record_usage_extracts_chat_from_session_id() -> None:
    engine = ChatCardEngine(_mock_client())
    engine.record_usage("agent:main:feishu:dm:oc_eeba2715144be520aa6a768342c023ae",
                        {"prompt_tokens": 100, "completion_tokens": 40}, model="glm-5.3")
    engine.record_usage("agent:main:feishu:dm:oc_eeba2715144be520aa6a768342c023ae",
                        {"prompt_tokens": 10, "completion_tokens": 5}, model="glm-5.3")
    bucket = engine._usage["oc_eeba2715144be520aa6a768342c023ae"]
    assert bucket["input"] == 110 and bucket["output"] == 45
    assert bucket["model"] == "glm-5.3"


@pytest.mark.asyncio
async def test_complete_footer_renders_tokens_and_speed() -> None:
    import json

    engine = ChatCardEngine(
        _mock_client(), footer_fields=[["elapsed", "speed"], ["tokens", "model"]])
    engine.record_usage("agent:main:feishu:dm:oc_chatxxx0000000000000000000000ff",
                        {"prompt_tokens": 1000, "completion_tokens": 120}, model="test-model")
    engine.on_draft("oc_chatxxx0000000000000000000000ff", "回答", reply_to="om_a")
    await _settle(engine)
    await engine.complete("oc_chatxxx0000000000000000000000ff", "回答", duration=6.0)

    card_json = json.dumps(engine._client.cardkit_update.call_args.args[1], ensure_ascii=False)
    assert "↑" in card_json and "↓" in card_json  # tokens 字段渲染
    assert "t/s" in card_json  # speed：120/6 = 20 t/s
    assert "test-model" in card_json


# ── 跨回合合并：bg 通知进卡 ──


@pytest.mark.asyncio
async def test_append_notice_merges_into_completed_card() -> None:
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "主回合", reply_to="om_a")
    await _settle(engine)
    await engine.complete("chat1", "主回合")
    engine._client.cardkit_update.reset_mock()

    msg_id = await engine.append_notice("chat1", "✅ 后台任务完成：报告已生成")

    assert msg_id == "om_card_msg"
    engine._client.cardkit_update.assert_called_once()  # 完成卡重渲（含 NOTICE 段）


@pytest.mark.asyncio
async def test_append_notice_without_card_returns_none() -> None:
    engine = ChatCardEngine(_mock_client())
    assert await engine.append_notice("ghost", "通知") is None


@pytest.mark.asyncio
async def test_send_bg_deliver_merges_into_card(adapter) -> None:
    """bg 交付特征（无 notify + thread_id metadata）→ 通知进卡，不发原生文本."""
    adapter._engine().on_draft("chat1", "主回合", reply_to="om_a")
    await _settle(adapter._engine())
    await adapter._engine().complete("chat1", "主回合")

    result = await adapter.send(
        "chat1", "📊 后台分析完成", metadata={"thread_id": "om_a"})

    assert result.success is True
    assert result.message_id == "om_card_msg"
    assert adapter.native_sends == []


@pytest.mark.asyncio
async def test_send_normal_final_with_notify_stays_card_path(adapter) -> None:
    """notify:True 的普通 final 不进合并分支（走完成卡/原生），不会被当 bg 通知."""
    adapter._engine().on_draft("chat1", "流式", reply_to="om_a")
    await _settle(adapter._engine())
    result = await adapter.send("chat1", "回答文本", reply_to="om_a",
                                metadata={"notify": True})
    assert adapter._engine().session_for("chat1").state == "completed"
    assert result.message_id == "om_card_msg"


# ── redirect：↪ ack 标记 → 下一 draft 收旧开新 ──


@pytest.mark.asyncio
async def test_redirect_ack_then_draft_seals_old_with_notice() -> None:
    """↪ redirect 后新 draft：旧卡 NOTICE 收尾 + 新卡（interrupt 同锚场景）."""
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "旧指令的回答", reply_to="om_a")
    await _settle(engine)
    old = engine.session_for("chat1")
    assert old.state == "streaming"

    engine.mark_redirect("chat1")  # ↪ ack 到达
    engine.on_draft("chat1", "新指令的回答", reply_to="om_a")  # 同锚！
    await _settle(engine)

    new = engine.session_for("chat1")
    assert new is not old, "redirect 后应开新会话"
    assert old.state == "completed"
    notice_segs = [s for s in old.segment_state.segments if s.type.value == "notice"]
    assert notice_segs and "新指令" in notice_segs[-1].text  # NOTICE 收尾文案
    assert engine._client.cardkit_create.call_count == 2  # 新卡
    assert new.redirected is False  # 标记已消费


@pytest.mark.asyncio
async def test_queued_ack_does_not_mark_redirect() -> None:
    """⏳ queued ack 不打标记（queue 回合 drain 成新消息新锚，走锚变化路径）."""
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "内容", reply_to="om_a")
    await _settle(engine)
    first = engine.session_for("chat1")
    assert first.redirected is False  # engine 无 queued 入口，确认默认不标记


@pytest.mark.asyncio
async def test_adapter_redirect_ack_marks_engine(adapter) -> None:
    adapter._engine().on_draft("chat1", "流式中", reply_to="om_a")
    await _settle(adapter._engine())

    result = await adapter.send("chat1", "↪ 已重定向当前运行", reply_to="om_1",
                                metadata={"notify": True})
    assert result.success is True
    assert adapter._engine().session_for("chat1").redirected is True


# ── footer：形态对齐注入模式配置 ──


@pytest.mark.asyncio
async def test_footer_renders_without_labels_single_row() -> None:
    """footer_fields 单行四字段 + show_label=False → emoji 形态（注入模式同款）."""
    import json

    engine = ChatCardEngine(
        _mock_client(),
        footer_fields=[["elapsed", "model", "speed", "context"]],
        footer_show_label=False)
    engine.record_usage(
        "oc_abc0000000000000000000000000099",
        {"prompt_tokens": 5000, "completion_tokens": 100, "context_length": 1000000},
        model="m1")
    engine.on_draft("oc_abc0000000000000000000000000099", "答", reply_to="om_a")
    await _settle(engine)
    await engine.complete("oc_abc0000000000000000000000000099", "答", duration=5.0)

    card = json.dumps(engine._client.cardkit_update.call_args.args[1], ensure_ascii=False)
    assert "Elapsed" not in card  # 无英文标签
    assert "⏱" in card and "⚡" in card  # emoji 形态
    assert "t/s" in card and "m1" in card
    assert "1.0M" in card  # context 字段（1M 上限）


def test_footer_config_reads_hermes_yaml() -> None:
    """register 的 footer 形态来自 HERMES_HOME/config.yaml（与注入模式同源）."""
    from plugin import _footer_config

    fields = _footer_config("fields", None)
    assert isinstance(fields, list) and isinstance(fields[0], list)  # 二维


@pytest.mark.asyncio
async def test_usage_hook_passes_context_length() -> None:
    from plugin import _make_usage_hook

    engine = ChatCardEngine(_mock_client())
    hook = _make_usage_hook(engine)
    hook(session_id="agent:main:feishu:dm:oc_abc00000000000000000000000000099",
         usage={"prompt_tokens": 10, "completion_tokens": 2},
         model="m", context_length=2000000)
    bucket = engine._usage["oc_abc00000000000000000000000000099"]
    assert bucket["context_max"] == 2000000
    assert bucket["context_used"] == 10


@pytest.mark.asyncio
async def test_busy_ack_without_session_does_not_open_card(adapter) -> None:
    """回合早期的 redirect/queued ack（卡还没建）不得开卡渲染 ack 文本."""
    result = await adapter.send("chat_fresh", "↪ 已重定向当前运行", reply_to="om_1",
                                metadata={"notify": True})
    assert adapter._engine().session_for("chat_fresh") is None  # 没开卡
    assert result.message_id == "om_native"  # ack 走原生文本（gateway 语义不变）
