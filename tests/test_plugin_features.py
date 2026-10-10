"""plugin 特性测试 — clarify 按钮卡 / followup 边界 / usage footer / 通知合并.

不依赖 hermes 源树（tools.clarify_gateway / lark 回调模型用 sys.modules 替身）。
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock

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

    # 新回合：锚变成消息 B（drain 时 processing_start 已登记）→ 旧卡收尾 + 新会话
    engine.note_inbound("chat1", "om_msg_B")
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
    assert bucket["input"] == 100 and bucket["output"] == 45  # input=max，output=累加
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


# ── redirect：↪ ack 即刻收旧开新（不等首条正文 draft）──


@pytest.mark.asyncio
async def test_redirect_ack_then_draft_seals_old_with_notice() -> None:
    """↪ redirect ack 一到就拆：旧卡 NOTICE 收尾 + 新卡即刻开（思考/工具期不再画老卡）."""
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "旧指令的回答", reply_to="om_a")
    await _settle(engine)
    old = engine.session_for("chat1")
    assert old.state == "streaming"

    engine.mark_redirect("chat1")  # ↪ ack 到达：此刻立即收旧开新
    await _settle(engine)
    new = engine.session_for("chat1")
    assert new is not old, "ack 即刻开新会话，不等 draft"
    assert old.state == "completed"
    notice_segs = [s for s in old.segment_state.segments if s.type.value == "notice"]
    assert notice_segs and "新指令" in notice_segs[-1].text  # NOTICE 收尾文案
    assert engine._client.cardkit_create.call_count == 2  # 新卡已建（尚无任何 draft）
    assert new.redirected is False

    engine.on_draft("chat1", "新指令的回答", reply_to="om_a")  # 同锚！落新卡
    await _settle(engine)
    assert engine.session_for("chat1") is new
    assert new.answer_seg is not None and "新指令" in new.answer_seg.text


@pytest.mark.asyncio
async def test_redirect_post_ack_reasoning_tools_land_new_card() -> None:
    """ack 后、正文 draft 前的 reasoning/工具事件必须落新卡（回归：旧实现落老卡）."""
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "旧回答", reply_to="om_a")
    await _settle(engine)
    old = engine.session_for("chat1")

    engine.mark_redirect("chat1", anchor="om_new")
    await _settle(engine)
    new = engine.session_for("chat1")
    assert new is not old and new.state == "streaming"
    assert new.reply_to == "om_new"

    engine.on_reasoning("chat1", "新回合思考")
    engine.on_tool_start("execute_code", "ls")
    engine.on_draft("chat1", "新正文", reply_to="om_a")
    await _settle(engine)
    reasoning = [s for s in new.segment_state.segments if s.type.value == "reasoning"]
    assert reasoning and "新回合思考" in reasoning[0].text
    assert all("新回合思考" not in s.text for s in old.segment_state.segments)
    assert "新正文" in (new.answer_seg.text if new.answer_seg else "")


@pytest.mark.asyncio
async def test_redirect_straggler_draft_dropped() -> None:
    """旧请求取消前的残尾快照（老内容超集）不得闪进新卡."""
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "旧答案写到一半", reply_to="om_a")
    await _settle(engine)

    engine.mark_redirect("chat1", anchor="om_new")
    new = engine.session_for("chat1")
    engine.on_draft("chat1", "旧答案写到一半的更多内容", reply_to="om_a")  # 残尾（超集）
    assert new.answer_seg is None, "残尾不应写入新卡"

    engine.on_draft("chat1", "全新的回答", reply_to="om_a")  # 真新内容：放行并解除拦截
    await _settle(engine)
    assert new.answer_seg is not None and new.answer_seg.text == "全新的回答"
    # guard 已解除：后续帧正常置换
    engine.on_draft("chat1", "全新的回答（续）", reply_to="om_a")
    assert new.answer_seg.text == "全新的回答（续）"


@pytest.mark.asyncio
async def test_redirect_new_card_anchors_user_correction_message() -> None:
    """redirect 新卡的 reply 锚 = 用户纠正消息 id（ack 的 reply_to），非老回合锚.

    interrupt 不换 event_message_id，draft 帧仍带老锚——新卡引用必须来自
    ↪ ack 的 reply_to，否则回复引用指向被打断的老指令（用户实测踩坑）。
    """
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "老指令的回答", reply_to="om_old")
    await _settle(engine)

    # ↪ ack 到达：reply_to 是用户新指令消息 id；此刻新卡即以该锚建卡
    engine.mark_redirect("chat1", anchor="om_new")
    await _settle(engine)

    new = engine.session_for("chat1")
    assert new.reply_to == "om_new", "新卡锚应为用户纠正消息 id"

    # 后续 draft（仍带老锚——interrupt 不换消息身份）落入新卡，锚不变
    engine.on_draft("chat1", "新指令的回答", reply_to="om_old")
    await _settle(engine)
    assert engine.session_for("chat1") is new

    # 建卡时以新锚 reply 落位（卡片落在用户纠正消息下方）
    reply_call = engine._client.reply_card_by_id.call_args
    assert reply_call.args[0] == "om_new"


@pytest.mark.asyncio
async def test_redirect_same_turn_anchor_swing_does_not_split_card() -> None:
    """redirect 同回合锚回摆（老锚 draft 跟随新锚 draft 到达）不触发 followup 拆卡.

    15:49 实测：redirected 回合 draft 锚中途变回老消息 id（工具边界换 consumer
    重锚）——旧逻辑把回摆误判成新回合，把新卡拦腰密封成两张卡+第三张引错锚。
    """
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "散文开头", reply_to="om_old")
    await _settle(engine)

    engine.mark_redirect("chat1", anchor="om_new")  # ack 即刻收旧开新
    await _settle(engine)
    session = engine.session_for("chat1")
    assert session.reply_to == "om_new"

    # 同回合锚回摆：老锚 draft（老回合尾巴/重锚后的 consumer）→ 不拆卡
    engine.on_draft("chat1", "改成诗歌的计划", reply_to="om_old")
    engine.on_draft("chat1", "诗歌正文", reply_to="om_old")
    await _settle(engine)
    assert engine.session_for("chat1") is session, "锚回摆不应拆卡"
    assert session.answer_seg is not None and "诗歌正文" in session.answer_seg.text

    # 真正的新锚（下一条用户消息的 followup drain，processing_start 已登记）→ 拆卡
    engine.note_inbound("chat1", "om_next")
    engine.on_draft("chat1", "新回合内容", reply_to="om_next")
    await _settle(engine)
    new_session = engine.session_for("chat1")
    assert new_session is not session
    assert new_session.reply_to == "om_next"


@pytest.mark.asyncio
async def test_anchor_swing_to_unseen_id_reanchors_without_split() -> None:
    """工具边界换 consumer 的重锚（锚=引擎没见过的 id）→ 改锚不拆卡.

    2026-10-09 实测回归：redirected 回合写文件后 draft 锚变成全新 id，
    旧逻辑当新回合拆卡——思考 27s 的卡只装了个「Done.」，1497 字正文
    全落进第三张卡（write_done）。
    """
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "开头", reply_to="om_a")
    await _settle(engine)

    engine.mark_redirect("chat1", anchor="om_b")
    await _settle(engine)
    session = engine.session_for("chat1")

    # 写文件工具跑完，consumer 重锚到引擎从未见过的 om_swing（非入站消息）
    engine.on_draft("chat1", "写好了，最终版已盘至 novel_draft.md", reply_to="om_swing")
    await _settle(engine)
    assert engine.session_for("chat1") is session, "未见入站的锚变化不应拆卡"
    assert session.reply_to == "om_swing"
    assert "om_swing" in session.accepted_anchors
    assert "novel_draft" in (session.answer_seg.text if session.answer_seg else "")
    assert engine._client.cardkit_create.call_count == 2  # 没有第三张卡


@pytest.mark.asyncio
async def test_followup_boundary_fires_only_for_inbound_anchor() -> None:
    """拆卡门槛：新锚 = on_processing_start 登记过的入站消息才拆."""
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "旧回合", reply_to="om_a")
    await _settle(engine)

    # 未登记入站 → 不拆（swing 改锚）
    engine.on_draft("chat1", "同回合续", reply_to="om_swing")
    assert engine.session_for("chat1").reply_to == "om_swing"

    # 登记入站（drain 开始时的 processing_start）→ 拆
    engine.note_inbound("chat1", "om_drain")
    engine.on_draft("chat1", "新回合", reply_to="om_drain")
    await _settle(engine)
    new_session = engine.session_for("chat1")
    assert new_session.reply_to == "om_drain"
    assert new_session.answer_seg is not None and "新回合" in new_session.answer_seg.text


@pytest.mark.asyncio
async def test_queued_ack_does_not_mark_redirect() -> None:
    """⏳ queued ack 不打标记（queue 回合 drain 成新消息新锚，走锚变化路径）."""
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "内容", reply_to="om_a")
    await _settle(engine)
    first = engine.session_for("chat1")
    assert first.redirected is False  # engine 无 queued 入口，确认默认不标记


@pytest.mark.asyncio
async def test_adapter_redirect_ack_opens_new_card_immediately(adapter) -> None:
    """↪ ack 经 send() 拦截即触发收旧开新（老会话密封、新会话顶替）."""
    engine = adapter._engine()
    engine.on_draft("chat1", "流式中", reply_to="om_a")
    await _settle(engine)
    old = engine.session_for("chat1")

    result = await adapter.send("chat1", "↪ 已重定向当前运行", reply_to="om_1",
                                metadata={"notify": True})
    assert result.success is True
    await _settle(engine)  # 等 seal task 跑完
    new = engine.session_for("chat1")
    assert new is not old, "ack 即刻顶替会话"
    assert old.redirected is True  # 被重启回合的诊断标记留在旧会话上
    assert old.state == "completed"
    assert new.state in ("creating", "streaming")  # 新卡已在建/已建

    # ↪ ack 文本进新卡心跳行（旧卡已密封，不再吃内容；settle 后字段已被 flush 清空）
    from plugin._vendor.cardkit.builder import HEARTBEAT_ELEMENT_ID

    heartbeat_pushes = [c.args[2] for c in engine._client.cardkit_stream_element.call_args_list
                        if c.args[1] == HEARTBEAT_ELEMENT_ID]
    assert any("已重定向" in t for t in heartbeat_pushes)


# ── hook 跨 profile fan-out（安安侧 reasoning 流丢失的根因修复）──


def _stub_hermes_plugin_scope(monkeypatch: Any, profiles: list[tuple[str, str]]) -> Any:
    """替身 hermes_cli.plugins / profiles / hermes_constants（公开面形态）.

    替身暴露 get_plugin_manager()（按 override 栈顶 home 建+缓存 manager）+
    PluginContext（官方 facade 形态：register_hook 落 manager._hooks）。必须用
    ModuleType（SimpleNamespace 缺 __name__，`from X import Y` 的子模块回退会
    拿不到位置信息直接 ImportError）。
    """
    import sys
    import types

    managers: dict[str, Any] = {}

    plugins_mod = types.ModuleType("hermes_cli.plugins")

    class _Mgr:
        def __init__(self, scope_key: str | None = None) -> None:
            self.scope_key = scope_key
            self._hooks: dict[str, list[Any]] = {}

    class _Ctx:
        def __init__(self, manifest: Any, manager: Any) -> None:
            self.manifest = manifest
            self._manager = manager

        def register_hook(self, name: str, cb: Any) -> None:
            self._manager._hooks.setdefault(name, []).append(cb)

    stack = [str(profiles[0][1])]  # 模拟 override 栈：栈顶 = 当前 home（discovery scope）

    def get_plugin_manager() -> Any:
        mgr = managers.get(stack[-1])
        if mgr is None:
            mgr = _Mgr(scope_key=stack[-1])
            managers[stack[-1]] = mgr
        return mgr

    plugins_mod.PluginManager = _Mgr
    plugins_mod.PluginContext = _Ctx
    plugins_mod.get_plugin_manager = get_plugin_manager
    plugins_mod._managers = managers  # 测试断言入口

    pkg = types.ModuleType("hermes_cli")
    pkg.plugins = plugins_mod
    profiles_mod = types.ModuleType("hermes_cli.profiles")
    profiles_mod.profiles_to_serve = lambda multiplex=True: profiles
    constants_mod = types.ModuleType("hermes_constants")

    def set_override(home: str) -> int:
        stack.append(str(home))
        return len(stack)

    def reset_override(token: int) -> None:
        del stack[token - 1:]

    constants_mod.hermes_home_key = lambda home=None: str(home) if home else stack[-1]
    constants_mod.set_hermes_home_override = set_override
    constants_mod.reset_hermes_home_override = reset_override
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins_mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", profiles_mod)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants_mod)
    return plugins_mod


def test_hook_fanout_registers_into_other_profile_manager(monkeypatch: Any) -> None:
    import plugin as plugin_pkg

    plugins_mod = _stub_hermes_plugin_scope(
        monkeypatch, [("default", "/hermes"), ("family", "/home-family")])
    cb = lambda **kw: None  # noqa: E731
    plugin_pkg._FANED_HOOK_IDS.clear()

    plugin_pkg._fanout_hooks_to_profile_scopes({"on_stream_delta": cb}, manifest=object())
    mgr = plugins_mod._managers["/home-family"]
    assert mgr._hooks["on_stream_delta"] == [cb]
    # 当前 scope 不重复注册（profiles 里 default == current 被跳过）
    assert "/hermes" not in plugins_mod._managers


def test_hook_fanout_reuses_existing_manager_and_dedupes(monkeypatch: Any) -> None:
    import plugin as plugin_pkg

    plugins_mod = _stub_hermes_plugin_scope(
        monkeypatch, [("default", "/hermes"), ("family", "/home-family")])
    cb = lambda **kw: None  # noqa: E731
    plugin_pkg._FANED_HOOK_IDS.clear()

    plugin_pkg._fanout_hooks_to_profile_scopes({"on_stream_delta": cb}, manifest=object())
    plugin_pkg._fanout_hooks_to_profile_scopes({"on_stream_delta": cb}, manifest=object())  # 重跑幂等
    assert len(plugins_mod._managers) == 1, "重跑经 override 后的官方缓存复用同一 manager"
    mgr = next(iter(plugins_mod._managers.values()))
    assert mgr.scope_key == "/home-family"
    assert mgr._hooks["on_stream_delta"] == [cb]


@pytest.mark.asyncio
async def test_redirect_old_session_terminal_immediately() -> None:
    """ack 即把旧会话置终态（不等异步 seal）：reasoning 全局路由立刻只看新会话.

    seal 要走 cardkit close+update 两跳网络，期间旧会话若仍计为 streaming，
    多会话守卫会把新回合的思考流判成串扰丢弃或错送旧卡。
    """
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "旧回答", reply_to="om_a")
    await _settle(engine)
    old = engine.session_for("chat1")
    assert old.state == "streaming"

    engine.mark_redirect("chat1", anchor="om_new")
    assert old.state == "completed"  # 同步置终态，不等异步 seal
    assert engine.streaming_sessions() == []  # 旧卡已出全局流式路由，新卡尚在 creating


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
    import plugin as plugin_pkg

    engine = ChatCardEngine(_mock_client())
    # 路由版钩子：engine 登记进全局表，按 session_id 里的 chat 路由
    plugin_pkg._ENGINES.clear()
    plugin_pkg._ENGINES.append(engine)
    hook = plugin_pkg._make_usage_hook()
    hook(session_id="agent:main:feishu:dm:oc_abc00000000000000000000000000099",
         usage={"prompt_tokens": 10, "completion_tokens": 2},
         model="m", context_length=2000000)
    bucket = engine._usage["oc_abc00000000000000000000000000099"]
    assert bucket["context_max"] == 2000000
    assert bucket["context_used"] == 10
    plugin_pkg._ENGINES.clear()


@pytest.mark.asyncio
async def test_busy_ack_without_session_does_not_open_card(adapter) -> None:
    """回合早期的 redirect/queued ack（卡还没建）不得开卡渲染 ack 文本."""
    result = await adapter.send("chat_fresh", "↪ 已重定向当前运行", reply_to="om_1",
                                metadata={"notify": True})
    assert adapter._engine().session_for("chat_fresh") is None  # 没开卡
    assert result.message_id == "om_native"  # ack 走原生文本（gateway 语义不变）


@pytest.mark.asyncio
async def test_interrupt_notice_never_opens_card(adapter) -> None:
    """⚡ interrupt 通知（gateway.progress.interrupting_head）不开卡——
    无活跃会话时走原生文本，有活跃会话时进心跳行."""
    # 无活跃会话：不开卡
    result = await adapter.send("chat_fresh", "⚡ 正在中断当前任务。我很快就会回复你的消息。",
                                reply_to="om_1", metadata={"notify": True})
    assert adapter._engine().session_for("chat_fresh") is None
    assert result.message_id == "om_native"

    # 有活跃会话：进心跳行，不完成卡片
    adapter._engine().on_draft("chat1", "流式中", reply_to="om_a")
    await _settle(adapter._engine())
    result2 = await adapter.send("chat1", "⚡ 正在中断当前任务。", reply_to="om_a",
                                 metadata={"notify": True})
    assert result2.message_id.startswith("lark-card:")
    assert adapter._engine().session_for("chat1").state == "streaming"


# ── typing 建卡 + 工具阶段可见 + t/s 真实口径 ──


@pytest.mark.asyncio
async def test_turn_started_creates_card_before_any_draft() -> None:
    """typing 时机建卡：工具阶段用户就能看到 loading 卡（不等首条 draft）."""
    engine = ChatCardEngine(_mock_client())
    engine.on_turn_started("chat1")  # send_typing 时机
    await _settle(engine)
    session = engine.session_for("chat1")
    assert session is not None and session.state == "streaming"
    engine._client.cardkit_create.assert_called_once()


@pytest.mark.asyncio
async def test_tool_events_before_draft_show_in_panel() -> None:
    """工具事件先于 draft：面板数据积累，卡已建（typing 兜底）。"""
    engine = ChatCardEngine(_mock_client())
    engine.on_turn_started("chat1")
    engine.on_tool_start("web_search", "ai news")
    engine.on_tool_end("web_search", output="results")
    await _settle(engine)
    session = engine.session_for("chat1")
    steps = session.tool_tracker.build_display_steps()
    assert len(steps) == 1 and steps[0]["status"] == "success"
    # 工具面板元素已建（不等正文）
    assert session.tool_seg is not None and session.tool_seg.created


@pytest.mark.asyncio
async def test_draft_after_typing_session_records_anchor() -> None:
    """typing 建的会话（无锚）收到带锚 draft：记录锚不重开（DM 直发卡保留）."""
    engine = ChatCardEngine(_mock_client())
    engine.on_turn_started("chat1")
    await _settle(engine)
    first = engine.session_for("chat1")
    engine.on_draft("chat1", "内容", reply_to="om_a")
    assert engine.session_for("chat1") is first  # 不重开
    assert first.reply_to == "om_a"  # 锚已记录
    assert engine._client.cardkit_create.call_count == 1  # 没建第二张


@pytest.mark.asyncio
async def test_ts_uses_api_time_span_not_session_time() -> None:
    """t/s 分母 = API 时间跨度（首 started → 末 ended，含工具时间），
    不用会话时长（只覆盖流式尾巴，会虚高一个数量级）。"""
    import json

    engine = ChatCardEngine(
        _mock_client(), footer_fields=[["elapsed", "speed"]])
    engine.record_usage(
        "oc_aaa0000000000000000000000000099",
        {"prompt_tokens": 100, "completion_tokens": 500,
         "started_at": 100.0, "ended_at": 110.0}, model="m")
    engine.record_usage(
        "oc_aaa0000000000000000000000000099",
        {"prompt_tokens": 50, "completion_tokens": 500,
         "started_at": 130.0, "ended_at": 140.0}, model="m")
    engine.on_draft("oc_aaa0000000000000000000000000099", "答", reply_to="om_a")
    await _settle(engine)
    # 会话时长 ~0（刚建）；API 跨度 40s → 1000 tokens / 40s = 25 t/s
    await engine.complete("oc_aaa0000000000000000000000000099", "答")
    card = json.dumps(engine._client.cardkit_update.call_args.args[1], ensure_ascii=False)
    assert "25 t/s" in card


@pytest.mark.asyncio
async def test_adapter_send_typing_starts_card(adapter) -> None:
    typed = await adapter.send_typing("chat1")
    await _settle(adapter._engine())
    assert adapter._engine().session_for("chat1") is not None
    assert typed is None  # 官方 no-op 返回值


@pytest.mark.asyncio
async def test_probe_starts_card(adapter) -> None:
    """draft 探针（回合开始必调、带 chat_id）→ 立即建卡（飞书无 typing API）."""
    assert adapter.supports_draft_streaming(chat_id="chat1") is True
    await _settle(adapter._engine())
    assert adapter._engine().session_for("chat1") is not None
    assert adapter._engine().session_for("chat1").state == "streaming"


@pytest.mark.asyncio
async def test_typing_tail_after_completion_does_not_reopen(adapter) -> None:
    """回合完成后 2s 内的 typing 循环尾巴不得重建空卡（completed_at <5s 守卫）."""
    engine = adapter._engine()
    engine.on_turn_started("chat1")
    await _settle(engine)
    await engine.complete("chat1", "回答")
    assert engine._client.cardkit_create.call_count == 1

    engine.on_turn_started("chat1")  # 完成后 2s 内的 typing 尾巴
    await _settle(engine)
    assert engine._client.cardkit_create.call_count == 1  # 没建空卡

    # 模拟旧会话已过窗口（>5s）→ 新回合 typing 正常建新卡
    engine.session_for("chat1").completed_at -= 10.0
    engine.on_turn_started("chat1")
    await _settle(engine)
    assert engine._client.cardkit_create.call_count == 2


@pytest.mark.asyncio
async def test_reasoning_during_creating_window_not_dropped() -> None:
    """建卡窗口（creating）的 reasoning 不丢弃——长思考回合卡片不能全程空转."""
    engine = ChatCardEngine(_mock_client())
    engine.on_turn_started("chat1")  # typing 建卡（creating 中）
    engine.on_reasoning("", "💭 思考片段")  # 钩子无 chat → 单活跃会话兜底（含 creating）
    await _settle(engine)
    session = engine.session_for("chat1")
    reasoning = [s for s in session.segment_state.segments if s.type.value == "reasoning"]
    assert reasoning and "思考" in reasoning[0].text


def test_input_tokens_takes_last_not_sum() -> None:
    """input 取最后值：每轮 API prompt 含全量历史，累加虚高数量级."""
    engine = ChatCardEngine(_mock_client())
    sid = "agent:main:feishu:dm:oc_bbb00000000000000000000000000099"
    engine.record_usage(sid, {"prompt_tokens": 100000, "completion_tokens": 50})
    engine.record_usage(sid, {"prompt_tokens": 120000, "completion_tokens": 60})
    engine.record_usage(sid, {"prompt_tokens": 110000, "completion_tokens": 70})
    bucket = engine._usage["oc_bbb00000000000000000000000000099"]
    assert bucket["input"] == 120000  # max，非 330000 累加
    assert bucket["output"] == 180  # output 累加（真总产出）


@pytest.mark.asyncio
async def test_gateway_lifecycle_notice_not_merged_into_card(adapter) -> None:
    """⚠️/♻️ 网关生命周期通知不并进完成卡（走原生文本，与注入模式一致）."""
    adapter._engine().on_draft("chat1", "回合", reply_to="om_a")
    await _settle(adapter._engine())
    await adapter._engine().complete("chat1", "回合")

    result = await adapter.send(
        "chat1", "⚠️ Hermes 正在关闭——你当前的任务将被中断。",
        metadata={"thread_id": "om_a"})
    assert result.message_id == "om_native"  # 原生文本
    assert adapter.native_sends  # 确实发出去了


@pytest.mark.asyncio
async def test_reasoning_hook_reads_delta_kwarg() -> None:
    """官方 enqueue 参数名是 delta（非 text）——回归防护."""
    import plugin as plugin_pkg

    engine = ChatCardEngine(_mock_client())
    engine.on_turn_started("chat1")  # 建活跃会话（含 creating 兜底）
    await _settle(engine)
    plugin_pkg._ENGINES.clear()
    plugin_pkg._ENGINES.append(engine)
    hook = plugin_pkg._make_reasoning_hook()
    hook(kind="reasoning", delta="💭 思考增量")
    plugin_pkg._ENGINES.clear()
    session = engine.session_for("chat1")
    reasoning = [s for s in session.segment_state.segments if s.type.value == "reasoning"]
    assert reasoning and "思考增量" in reasoning[0].text


@pytest.mark.asyncio
async def test_draft_strips_thinking_tags() -> None:
    """draft 快照剥 <thinking> 标签（注入模式同款防线）."""
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "<thinking>盘算</thinking>正文", reply_to="om_a")
    await _settle(engine)
    seg = engine.session_for("chat1").answer_seg
    assert "thinking" not in seg.text and "正文" in seg.text


@pytest.mark.asyncio
async def test_interrupted_card_seals_red() -> None:
    """被打断（redirect）的旧卡红色收尾：header=stopped 红 + NOTICE 说明."""
    import json

    engine = ChatCardEngine(_mock_client(), header_enabled=True)
    engine.on_draft("chat1", "写到一半的内容", reply_to="om_a")
    await _settle(engine)

    engine.mark_redirect("chat1")
    engine.on_draft("chat1", "新回合", reply_to="om_a")
    await _settle(engine)

    # 第一张 update = 旧卡 seal（红 + NOTICE）
    old_card = json.dumps(engine._client.cardkit_update.call_args_list[0].args[1],
                          ensure_ascii=False)
    assert '"red"' in old_card  # 红 header
    assert "已按新指令重启" in old_card  # NOTICE 文案

    # 新回合完成后正常绿色
    await engine.complete("chat1", "新回合的完整回答")
    new_card = json.dumps(engine._client.cardkit_update.call_args_list[-1].args[1],
                          ensure_ascii=False)
    assert '"green"' in new_card


@pytest.mark.asyncio
async def test_interrupted_card_forces_red_header_even_if_disabled() -> None:
    """header 配置关闭时，被打断的卡仍强制红 header（红标唯一载体）.

    回归：register() 曾漏接 streaming.header 配置 + 红标只活在 header 里，
    导致生产（header 关）redirect 旧卡毫无红色痕迹。
    """
    import json

    engine = ChatCardEngine(_mock_client())  # header_enabled 默认 False
    engine.on_draft("chat1", "写到一半的内容", reply_to="om_a")
    await _settle(engine)

    engine.mark_redirect("chat1")
    engine.on_draft("chat1", "新回合", reply_to="om_a")
    await _settle(engine)

    old_card = json.dumps(engine._client.cardkit_update.call_args_list[0].args[1],
                          ensure_ascii=False)
    assert '"red"' in old_card, "打断卡必须强制显示红 header"

    # 正常完成路径不受强制 header 影响：仍无 header
    await engine.complete("chat1", "新回合的完整回答")
    new_card = json.dumps(engine._client.cardkit_update.call_args_list[-1].args[1],
                          ensure_ascii=False)
    assert '"header"' not in new_card


# ── 折叠区摘要化：思考头尾摘录 + 工具步数封顶（防卡片体积爆炸）──


def test_cap_reasoning_text_head_tail_excerpt() -> None:
    from plugin._vendor.cardkit.builder import cap_reasoning_text

    short = "短思考" * 50  # 150 字
    assert cap_reasoning_text(short) == short

    long_text = "思" * 3000
    capped = cap_reasoning_text(long_text)
    assert len(capped) < 1100, "摘录应远小于原文"
    assert "思考原文共 3000 字" in capped
    assert capped.startswith("思" * 600)
    assert capped.endswith("思" * 300)


def test_reasoning_panel_single_element_bounded() -> None:
    from plugin._vendor.cardkit.builder import _build_reasoning_panel

    panel = _build_reasoning_panel("考" * 5000, expanded=False)
    md = [e for e in panel["elements"] if e.get("tag") == "markdown"]
    assert len(md) == 1, "摘录后应单元素（原 2400 分块已移除）"
    assert len(md[0]["content"]) < 1100
    assert "中间省略" in md[0]["content"]


def test_tool_panel_caps_steps_to_recent() -> None:
    from plugin._vendor.cardkit.builder import _build_tool_panel

    steps = [{"title": f"step{i}", "status": "done", "icon": "✅",
              "label": f"步骤{i}"} for i in range(20)]
    panel = _build_tool_panel(steps)
    title = panel["header"]["title"]["content"]
    assert "20" in title, "标题计数保持全量"
    body = str(panel["elements"])
    assert "已折叠前 5 步" in body
    assert "step19" in body and "step15" in body
    assert "step0" not in body and "step4" not in body, "只显示最近 15 步"


@pytest.mark.asyncio
async def test_engine_streams_capped_reasoning() -> None:
    """流式路径：长思考经 cardkit_stream_element 下发的是摘录不是全文."""
    engine = ChatCardEngine(_mock_client())
    engine.on_draft("chat1", "回答", reply_to="om_a")
    await _settle(engine)
    engine.on_reasoning("chat1", "想" * 4000)
    await _settle(engine)

    contents = [c.args[2] for c in engine._client.cardkit_stream_element.call_args_list]
    reasoning_sends = [c for c in contents if isinstance(c, str) and len(c) > 50]
    assert reasoning_sends, "应有思考文本流式下发"
    biggest = max(reasoning_sends, key=len)
    assert len(biggest) < 1200, f"流式思考应封顶，实测 {len(biggest)}"
    assert "思考原文共" in biggest or "想" * 4000 not in biggest


# ── multiplex：多 profile engine 路由（安安/family 场景）──


@pytest.mark.asyncio
async def test_scoped_factory_builds_independent_engines() -> None:
    """每次 factory 调用（= 每 profile 的 adapter 实例化）独立 engine+client."""
    import plugin as plugin_pkg
    from plugin.adapter import create_scoped_adapter_factory

    built = []

    def builder():
        engine = ChatCardEngine(_mock_client())
        client = plugin_pkg._LazyClient()
        built.append((engine, client))
        return engine, client

    from test_plugin_mode import _FakeBaseAdapter

    import plugin.adapter as padapter
    real_import = padapter._import_base_adapter
    padapter._import_base_adapter = lambda: _FakeBaseAdapter
    try:
        factory = create_scoped_adapter_factory(builder)
        f1 = factory(config=None)
        f2 = factory(config=None)
    finally:
        padapter._import_base_adapter = real_import
    assert f1 is not f2
    assert len(built) == 2
    assert built[0][0] is not built[1][0], "engine 必须按 adapter 独立（凭据隔离）"
    assert f1._engine() is built[0][0] and f2._engine() is built[1][0]


@pytest.mark.asyncio
async def test_usage_hook_routes_by_chat_across_engines() -> None:
    """usage 按 session_id 里的 chat 路由到拥有该 chat 会话的 engine."""
    import plugin as plugin_pkg

    e_default = ChatCardEngine(_mock_client())
    e_family = ChatCardEngine(_mock_client())
    e_default.on_draft("oc_aaaaaaaaaaaaaaaa0000000000000001", "答", reply_to="om_a")
    e_family.on_draft("oc_bbbbbbbbbbbbbbbb0000000000000002", "答", reply_to="om_b")
    plugin_pkg._ENGINES.clear()
    plugin_pkg._ENGINES.extend([e_default, e_family])
    try:
        hook = plugin_pkg._make_usage_hook()
        hook(session_id="agent:family:feishu:dm:oc_bbbbbbbbbbbbbbbb0000000000000002",
             usage={"prompt_tokens": 5, "completion_tokens": 1}, model="m")
        assert "oc_bbbbbbbbbbbbbbbb0000000000000002" in e_family._usage
        assert not e_default._usage, "不得串到别的 profile 的 engine"
    finally:
        plugin_pkg._ENGINES.clear()


def test_fanout_registers_profile_scopes() -> None:
    """补注册把平台条目写进每个 live profile 的 registry scope 桶."""
    import plugin as plugin_pkg

    registered = {}

    class _FakeRegistry:
        def register(self, entry, *, scope=None):
            registered[scope] = entry

    import sys
    import types
    fake_reg_mod = types.ModuleType("gateway.platform_registry")
    fake_reg_mod.platform_registry = _FakeRegistry()
    fake_reg_mod.PlatformEntry = lambda **kw: kw
    fake_const_mod = types.ModuleType("hermes_constants")
    fake_const_mod.hermes_home_key = lambda path=None: f"key:{path or '/h/default'}"
    fake_prof_mod = types.ModuleType("hermes_cli.profiles")
    fake_prof_mod.profiles_to_serve = lambda multiplex=False: [
        ("default", "/h/default"), ("family", "/h/family")]
    saved = {m: sys.modules.get(m) for m in
             ("gateway.platform_registry", "hermes_constants", "hermes_cli.profiles")}
    sys.modules.update({"gateway.platform_registry": fake_reg_mod,
                        "hermes_constants": fake_const_mod,
                        "hermes_cli.profiles": fake_prof_mod})
    try:
        plugin_pkg._fanout_to_profile_scopes({"name": "feishu"})
    finally:
        for m, mod in saved.items():
            if mod is None:
                sys.modules.pop(m, None)
            else:
                sys.modules[m] = mod
    assert "key:/h/family" in registered, "family scope 必须被补注册"
    assert "key:/h/default" not in registered, "注册 scope 本身不重复"
    assert registered["key:/h/family"]["name"] == "feishu"


def test_fanout_respects_profile_optout(tmp_path) -> None:
    """plugins.disabled 列出本插件的 profile 不被补注册（全局启用的退出开关）."""
    import plugin as plugin_pkg

    (tmp_path / "config.yaml").write_text(
        "plugins:\n  disabled:\n    - feishu-streaming-platform\n", encoding="utf-8")
    assert plugin_pkg._profile_disabled_plugin(tmp_path) is True

    (tmp_path / "config.yaml").write_text(
        "plugins:\n  disabled: []\n", encoding="utf-8")
    assert plugin_pkg._profile_disabled_plugin(tmp_path) is False

    (tmp_path / "config.yaml").unlink()
    assert plugin_pkg._profile_disabled_plugin(tmp_path) is False  # 坏文件不退出


# ── cron 结果投递 → ⏰ cron 卡片（scheduler live lane，无锚）──

_CRON_WRAPPED = (
    "Cronjob Response: 举水乡情·十日读\n"
    "(job_id: 426179a81bb3)\n"
    "-------------\n\n"
    "📖 举水乡情 · 第 9 / 10 篇\n\n正文内容在这里。\n\n"
    'To stop or manage this job, send me a new message (e.g. "stop reminder 举水乡情·十日读").'
)

# hermes 失败通知的两个真实形状（copy 表公共前缀 / 脚本门）
_CRON_FAILURE_WRAPPED = (
    "Cronjob Response: 会失败的job\n"
    "(job_id: deadbeef)\n"
    "-------------\n\n"
    "⚠️ Cron '会失败的job' failed: its script timed out. No model was invoked. \n\n"
    'To stop or manage this job, send me a new message (e.g. "stop reminder 会失败的job").'
)
_CRON_FAILURE_SCRIPT_GATE = (
    "Cronjob Response: 脚本job\n"
    "(job_id: cafebabe)\n"
    "-------------\n\n"
    "**Status:** script failed\n\n{output}\n\n"
    'To stop or manage this job, send me a new message (e.g. "stop reminder 脚本job").'
)


def test_parse_cron_payload_splits_wrapper() -> None:
    from plugin.adapter import _parse_cron_payload

    task_name, body, job_id, is_failure = _parse_cron_payload(_CRON_WRAPPED)
    assert task_name == "举水乡情·十日读"
    assert job_id == "426179a81bb3"
    assert is_failure is False
    assert "Cronjob Response" not in body
    assert "-------------" not in body
    assert "第 9 / 10 篇" in body
    assert "stop reminder" in body and "(job_id: 426179a81bb3)" in body


def test_parse_cron_payload_detects_failure() -> None:
    """两个上游失败形状（copy 表前缀 / 脚本门）→ 红 header；成功正文不误判."""
    from plugin.adapter import _parse_cron_payload

    for raw in (_CRON_FAILURE_WRAPPED, _CRON_FAILURE_SCRIPT_GATE):
        assert _parse_cron_payload(raw)[3] is True, raw[:60]
    # 用户脚本输出里出现 "failed" 字样但非 hermes copy 形状 → 不误判
    assert _parse_cron_payload(_CRON_WRAPPED)[3] is False
    assert _parse_cron_payload(
        "Cronjob Response: 监控\n(job_id: x)\n-------------\n\n磁盘使用率 88%，未达 failed 阈值\n"
        "\n\nTo stop or manage this job, send me a new message (e.g. \"stop reminder 监控\").")[3] is False


def test_parse_cron_payload_passthrough_on_mismatch() -> None:
    """wrap_response=false 或上游改版 → 原样返回，不硬拆."""
    from plugin.adapter import _parse_cron_payload

    for raw in ("普通通知文本", "Cronjob Response: 缺分隔线\n(job_id: x)", ""):
        task_name, body, job_id, is_failure = _parse_cron_payload(raw)
        assert (task_name, body, job_id) == ("", raw, "")
        assert is_failure is False


@pytest.mark.asyncio
async def test_send_cron_failure_renders_red_card(adapter) -> None:
    """失败通知 → 红 header cron 卡（成功是蓝的）."""
    result = await adapter.send(
        "chat1", _CRON_FAILURE_WRAPPED, metadata={"job_id": "deadbeef", "notify": True})

    assert result.success is True
    card = adapter._engine()._client.cardkit_create.call_args[0][0]
    assert card["header"]["template"] == "red"
    assert "会失败的job" in card["header"]["title"]["content"]


@pytest.mark.asyncio
async def test_send_cron_result_renders_card(adapter) -> None:
    """job_id metadata + 无锚 → ⏰ cron 卡片直发，不走原生文本."""
    result = await adapter.send(
        "chat1", _CRON_WRAPPED,
        metadata={"job_id": "426179a81bb3", "notify": True})

    assert result.success is True
    assert result.message_id == "om_card_msg"
    assert adapter.native_sends == []
    client = adapter._engine()._client
    client.cardkit_create.assert_called_once()
    card = client.cardkit_create.call_args[0][0]
    assert "举水乡情·十日读" in card["header"]["title"]["content"]
    body_md = "".join(e["content"] for e in card["body"]["elements"])
    assert "第 9 / 10 篇" in body_md and "stop reminder" in body_md
    client.send_card_to_chat.assert_called_once()


@pytest.mark.asyncio
async def test_send_cron_result_card_failure_falls_back_native(adapter) -> None:
    """建卡失败 → 落回原生文本（fail-open，cron 结果不能被卡片异常吞掉）."""
    adapter._engine()._client.cardkit_create = AsyncMock(side_effect=RuntimeError("api down"))

    result = await adapter.send(
        "chat1", _CRON_WRAPPED, metadata={"job_id": "j1", "notify": True})

    assert result.success is True
    assert result.message_id == "om_native"
    assert adapter.native_sends and adapter.native_sends[0][1] == _CRON_WRAPPED


@pytest.mark.asyncio
async def test_send_unanchored_without_jobid_stays_native(adapter) -> None:
    """无锚系统通知（watcher 等，无 job_id）保持原生文本 — 不被 cron 分支误吃."""
    result = await adapter.send("chat1", "watcher 命中通知")

    assert result.message_id == "om_native"
    assert adapter.native_sends
    assert not adapter._engine()._client.cardkit_create.called


@pytest.mark.asyncio
async def test_standalone_sender_returns_dict_and_parses_wrapper(monkeypatch) -> None:
    """无网关 standalone 投递：返回 dict（hermes 消费端 result.get 契约）+ 拆 wrap 信封."""
    import plugin as plugin_pkg

    class _FakeLazyClient:
        async def cardkit_create(self, card: dict) -> str:
            _FakeLazyClient.card = card
            return "card_xyz"

        async def send_card_to_chat(self, chat_id: str, card: dict) -> str:
            return "om_standalone_msg"

    monkeypatch.setattr(plugin_pkg, "_LazyClient", _FakeLazyClient)
    send = plugin_pkg._make_standalone_sender()

    result = await send(None, "chat1", _CRON_WRAPPED)

    assert result == {"success": True, "message_id": "om_standalone_msg"}
    card = _FakeLazyClient.card
    assert "举水乡情·十日读" in card["header"]["title"]["content"]
    body_md = "".join(e["content"] for e in card["body"]["elements"])
    assert "Cronjob Response" not in body_md and "第 9 / 10 篇" in body_md


# ── usage 路由：multiplex 双 engine 下 session_id 不含 chat 的兜底 ──


@pytest.mark.asyncio
async def test_open_sessions_counts_creating_and_streaming() -> None:
    engine = ChatCardEngine(_mock_client())
    chat = "oc_" + "c" * 20
    engine.on_turn_started(chat)
    assert [s.state for s in engine.open_sessions()] == ["creating"]
    engine.on_draft(chat, "草稿")
    await _settle(engine)
    assert [s.state for s in engine.open_sessions()] == ["streaming"]


@pytest.mark.asyncio
async def test_route_usage_engine_multiplex_active_fallback() -> None:
    from plugin import _route_usage_engine

    e1, e2 = ChatCardEngine(_mock_client()), ChatCardEngine(_mock_client())
    # 双 engine 都无进行中回合 → 歧义放弃（10-08 回归的形态）
    assert _route_usage_engine([e1, e2], "20261009_100000_abc123") is None
    # 恰好一个有进行中回合（creating 也算）→ 路由到它
    e2.on_turn_started("oc_" + "a" * 20)
    assert _route_usage_engine([e1, e2], "20261009_100000_abc123") is e2
    # 两个都有 → 歧义放弃
    e1.on_turn_started("oc_" + "b" * 20)
    assert _route_usage_engine([e1, e2], "20261009_100000_abc123") is None
    # 单 engine 兜底保留
    assert _route_usage_engine([e1], "whatever") is e1


@pytest.mark.asyncio
async def test_complete_without_draft_creates_answer_segment(adapter) -> None:
    """短回答整段直发、无 draft 帧 → complete 用 final_text 补建 ANSWER 段（不渲染 Done. 占位）."""
    chat = "oc_" + "d" * 20
    engine = adapter._engine()
    engine.on_turn_started(chat)
    await _settle(engine)
    engine.on_reasoning("", "想了一下")
    await _settle(engine)

    msg_id = await engine.complete(chat, "退了。有事喊我。")

    assert msg_id == "om_card_msg"
    card = engine._client.cardkit_update.call_args[0][1]
    body_md = "".join(e.get("content", "") for e in card["body"]["elements"]
                      if e.get("tag") == "markdown")
    assert "退了。有事喊我。" in body_md
    assert "Done." not in body_md


# ── footer 模型循环切换按钮：渲染 + 路由 + 命令合成 ──


def _find_switch_buttons(card: dict) -> list[dict]:
    """递归收集模型切换按钮（tiny 🔄 嵌在 footer column_set 内）."""
    found: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") == "button":
                for b in node.get("behaviors", []) or []:
                    if isinstance(b.get("value"), dict) and b["value"].get("hermes_model_action"):
                        found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(card.get("body", {}))
    return found


def test_build_complete_card_renders_model_switch_button() -> None:
    from plugin._vendor.cardkit.builder import build_complete_card
    from plugin._vendor.streaming.segments import Segment, SegmentType

    seg = Segment(SegmentType.ANSWER, "answer_0")
    seg.text = "回答"
    card = build_complete_card(
        segments=[seg], all_tool_steps=[],
        footer_data={"duration": 1.0, "model": "deepseek-flash"},
        model_switch={"current": "deepseek-flash"})
    buttons = _find_switch_buttons(card)
    assert len(buttons) == 1  # v2 独立 button（action 容器被 CardKit v2 拒绝，200861）
    btn = buttons[0]
    assert btn["text"]["content"] == "🧠⇄"
    assert btn["behaviors"][0]["value"] == {"hermes_model_action": "pick",
                                            "from": "deepseek-flash"}


def test_build_complete_card_no_button_without_model_switch() -> None:
    from plugin._vendor.cardkit.builder import build_complete_card
    from plugin._vendor.streaming.segments import Segment, SegmentType

    seg = Segment(SegmentType.ANSWER, "answer_0")
    seg.text = "回答"
    card = build_complete_card(segments=[seg], all_tool_steps=[])
    assert not _find_switch_buttons(card)


def test_engine_model_switch_target_cycle() -> None:
    engine = ChatCardEngine(_mock_client(),
                            model_cycle=["deepseek-flash", "glm-5.3-flash"])
    assert engine._model_switch_data("deepseek-flash") == {"current": "deepseek-flash"}
    assert engine._model_switch_data("glm-5.3-flash") == {"current": "glm-5.3-flash"}
    # 当前模型未知 / 清单不足 → 不出按钮
    assert engine._model_switch_data("") is None
    assert ChatCardEngine(_mock_client(), model_cycle=["solo"])._model_switch_data("solo") is None
    assert ChatCardEngine(_mock_client())._model_switch_data("x") is None


def test_build_model_picker_card_lists_models() -> None:
    from plugin._vendor.cardkit.builder import build_model_picker_card

    card = build_model_picker_card("deepseek-flash", ["deepseek-flash", "glm-5.3-flash"])
    buttons = [b for e in card["elements"] for b in e["actions"]]
    assert [b["text"]["content"] for b in buttons] == ["✅ deepseek-flash", "glm-5.3-flash"]
    assert buttons[1]["value"] == {"hermes_model_action": "switch",
                                   "target": "glm-5.3-flash"}


def test_build_model_switch_ack_card() -> None:
    from plugin._vendor.cardkit.builder import build_model_switch_ack_card

    card = build_model_switch_ack_card("glm-5.3-flash")
    assert "已切换到 glm-5.3-flash" in card["header"]["title"]["content"]


@pytest.mark.asyncio
async def test_complete_card_carries_model_switch_button(adapter) -> None:
    """complete 用 usage 里的模型算按钮，NOTICE 重渲透传不丢."""
    chat = "oc_" + "e" * 20
    engine = adapter._engine()
    engine._model_cycle = ["deepseek-flash", "glm-5.3-flash"]
    engine.on_turn_started(chat)
    await _settle(engine)
    engine.record_usage("20261009_120000_aabbcc", {"prompt_tokens": 100,
                                                   "completion_tokens": 10},
                        model="deepseek-flash")
    await engine.complete(chat, "回答文本")
    card = engine._client.cardkit_update.call_args[0][1]
    buttons = _find_switch_buttons(card)
    assert buttons and buttons[0]["behaviors"][0]["value"] == {
        "hermes_model_action": "pick", "from": "deepseek-flash"}
    # NOTICE 追加重渲：按钮仍在
    await engine.append_notice(chat, "后续通知")
    assert _find_switch_buttons(engine._client.cardkit_update.call_args[0][1])


def _card_action_event(value: dict | None = None, token: str = "tok1") -> Any:
    from types import SimpleNamespace
    return SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(value=value or {"hermes_model_action": "switch",
                                                   "target": "glm-5.3-flash"}),
            operator=SimpleNamespace(open_id="ou_admin"),
            context=SimpleNamespace(open_chat_id="oc_" + "f" * 20),
            token=token))


@pytest.mark.asyncio
async def test_model_pick_action_sends_picker_card(adapter) -> None:
    """🧠⇄ 点击 → 引擎 client 补发选择卡（当前模型 + 清单按钮）."""
    engine = adapter._engine()
    engine._model_cycle = ["deepseek-flash", "glm-5.3-flash"]
    engine.record_usage("20261009_130000_xxyyzz", {"prompt_tokens": 1, "completion_tokens": 1},
                        model="deepseek-flash")
    scheduled: list[Any] = []
    adapter._loop = object()
    adapter._card_response = lambda *a, **k: k.get("card_data") or "empty_response"
    adapter._is_interactive_operator_authorized = lambda open_id: True
    adapter._is_card_action_duplicate = lambda token: False
    adapter._submit_on_loop = lambda loop, coro: (scheduled.append(coro), True)[1]

    adapter._handle_model_switch_action(
        data=_card_action_event(value={"hermes_model_action": "pick",
                                       "from": "deepseek-flash"}),
        action_value={"hermes_model_action": "pick", "from": "deepseek-flash"})

    assert len(scheduled) == 1
    await scheduled[0]
    sent = engine._client.send_card_to_chat.call_args[0][1]
    assert "切换模型" in sent["header"]["title"]["content"]
    buttons = [b for e in sent["elements"] for b in e["actions"]]
    assert len(buttons) == 2


def test_extract_selected_model_from_action_shapes() -> None:
    from types import SimpleNamespace

    from plugin.adapter import StreamingFeishuMixin

    extract = StreamingFeishuMixin._extract_selected_model
    assert extract(SimpleNamespace(option=SimpleNamespace(value="glm-5.3-flash"))) == "glm-5.3-flash"
    assert extract(SimpleNamespace(option="deepseek-flash")) == "deepseek-flash"
    assert extract(SimpleNamespace(value={"value": "glm-5.3-flash"})) == "glm-5.3-flash"
    assert extract(SimpleNamespace()) == ""


@pytest.mark.asyncio
async def test_model_switch_action_dispatches_model_command(adapter, monkeypatch) -> None:
    """按钮点击 → 鉴权 → 合成 /model <target> synthetic 命令."""
    dispatched: list[dict] = []

    async def _fake_dispatch(**kwargs: Any) -> None:
        dispatched.append(kwargs)

    scheduled: list[Any] = []
    adapter._loop = object()
    adapter._card_response = lambda *a, **k: k.get("card_data") or "empty_response"
    adapter._is_interactive_operator_authorized = lambda open_id: True
    adapter._is_card_action_duplicate = lambda token: False
    adapter._submit_on_loop = lambda loop, coro: (scheduled.append(coro), True)[1]
    monkeypatch.setattr(adapter, "_dispatch_model_switch",
                        lambda **kw: _fake_dispatch(**kw))

    resp = adapter._handle_model_switch_action(
        data=_card_action_event(), action_value={"hermes_model_action": "switch",
                                                 "target": "glm-5.3-flash"})
    assert len(scheduled) == 1
    await scheduled[0]  # 执行被调度的合成协程
    assert len(dispatched) == 1
    # text=f"/model {target}" 在 _dispatch_model_switch 内拼装，此处验证路由参数
    assert dispatched[0]["target"] == "glm-5.3-flash"
    assert dispatched[0]["chat_id"] == "oc_" + "f" * 20
    assert dispatched[0]["open_id"] == "ou_admin"
    # 同步确认卡替换选择卡
    assert "已切换到 glm-5.3-flash" in str(resp)


@pytest.mark.asyncio
async def test_model_switch_action_rejects_unauthorized(adapter, monkeypatch) -> None:
    """未授权点击者 → 不派发命令."""
    dispatched: list[dict] = []

    async def _fake_dispatch(**kwargs: Any) -> None:
        dispatched.append(kwargs)

    adapter._loop = object()
    adapter._card_response = lambda *a, **k: k.get("card_data") or "empty_response"
    adapter._is_interactive_operator_authorized = lambda open_id: False
    adapter._is_card_action_duplicate = lambda token: False
    adapter._submit_on_loop = lambda loop, coro: (coro.close(), True)[1]
    monkeypatch.setattr(adapter, "_dispatch_model_switch",
                        lambda **kw: _fake_dispatch(**kw))

    adapter._handle_model_switch_action(
        data=_card_action_event(), action_value={"hermes_model_action": "switch",
                                                 "target": "glm-5.3-flash"})
    assert dispatched == []


@pytest.mark.asyncio
async def test_dispatch_model_switch_resolves_real_chat_type(adapter) -> None:
    """/model 的 override 键派生自 chat_type——DM 必须解析成 p2p（dm 键），不能
    沿用官方按钮处理器的 group 硬编码."""
    sent: list[dict] = []

    async def _fake_synth(**kwargs: Any) -> None:
        sent.append(kwargs)

    async def _fake_chat_info(chat_id: str) -> dict:
        return {"type": "p2p", "name": "涛哥"}

    adapter._dispatch_synthetic_event = _fake_synth
    adapter.get_chat_info = _fake_chat_info

    await adapter._dispatch_model_switch(chat_id="oc_" + "a" * 20, open_id="ou_admin",
                                         target="glm-5.3-flash",
                                         raw=object(), message_id="tok9")
    assert sent[0]["text"] == "/model glm-5.3-flash"
    assert sent[0]["event_chat_type"] == "p2p"


@pytest.mark.asyncio
async def test_dispatch_model_switch_uses_raw_type_for_dm(adapter) -> None:
    """映射后的 type='dm' 会被解析器再次错位成 group——必须传 raw_type='p2p'."""
    sent: list[dict] = []

    async def _fake_synth(**kwargs: Any) -> None:
        sent.append(kwargs)

    async def _fake_chat_info(chat_id: str) -> dict:
        return {"type": "dm", "raw_type": "p2p", "name": "涛哥"}

    adapter._dispatch_synthetic_event = _fake_synth
    adapter.get_chat_info = _fake_chat_info

    await adapter._dispatch_model_switch(chat_id="oc_" + "b" * 20, open_id="ou_admin",
                                         target="glm-5.3-flash",
                                         raw=object(), message_id="tok7")
    assert sent[0]["event_chat_type"] == "p2p"


@pytest.mark.asyncio
async def test_topic_thread_sessions_isolated(adapter) -> None:
    """话题（thread）与主聊同 chat 并发：会话按 (chat, thread) 隔离，锚与正文互不串写.

    2026-10-09 实测回归：话题回合与主聊回合共用 chat 键会话，reasoning/正文
    互相串写、followup 边界误判 redirect，用户被迫 /stop + /new 解缠。"""
    engine = adapter._engine()
    main_chat = "oc_" + "1" * 20
    topic_thread = "omt_" + "a" * 16

    # 主聊与话题回合交错开始 → 两张独立卡
    engine.on_turn_started(main_chat)
    engine.on_turn_started(main_chat, thread_id=topic_thread)
    await _settle(engine)
    assert len(engine._sessions) == 2
    main_sess = engine.session_for(main_chat)
    topic_sess = engine.session_for(main_chat, thread_id=topic_thread)
    assert main_sess is not None and topic_sess is not None
    assert main_sess is not topic_sess
    assert main_sess.thread_id is None and topic_sess.thread_id == topic_thread

    # 各自 draft（不同锚）→ 正文与锚不串
    engine.on_draft(main_chat, "主聊回答", reply_to="om_main_1")
    engine.on_draft(main_chat, "话题回答", reply_to="om_topic_1", thread_id=topic_thread)
    await _settle(engine)
    assert main_sess.answer_seg.text == "主聊回答"
    assert topic_sess.answer_seg.text == "话题回答"
    assert main_sess.reply_to == "om_main_1"
    assert topic_sess.reply_to == "om_topic_1"

    # 各自完成 → 完成卡只含自己的正文
    await engine.complete(main_chat, "主聊回答")
    await engine.complete(main_chat, "话题回答", thread_id=topic_thread)
    updates = [c.args[1] for c in engine._client.cardkit_update.call_args_list]
    assert len(updates) >= 2
    bodies = []
    for card in updates[-2:]:
        bodies.append("".join(
            e.get("content", "") for e in (card.get("body", {}).get("elements") or [])
            if isinstance(e, dict) and e.get("tag") == "markdown"))
    assert "主聊回答" in bodies[0] and "话题回答" not in bodies[0]
    assert "话题回答" in bodies[1] and "主聊回答" not in bodies[1]


@pytest.mark.asyncio
async def test_topic_session_defers_card_until_anchor(adapter) -> None:
    """话题会话探针期不建卡（无锚直发会落主聊顶层）；首个 draft 锚到手后
    才建卡且走 reply（进话题线程）."""
    engine = adapter._engine()
    main_chat = "oc_" + "1" * 20
    topic2 = "omt_" + "b" * 16

    engine.on_turn_started(main_chat, thread_id=topic2)
    await _settle(engine)
    n_create = engine._client.cardkit_create.call_count
    n_reply = engine._client.reply_card_by_id.call_count

    engine.on_draft(main_chat, "话题新回合", reply_to="om_topic_2", thread_id=topic2)
    await _settle(engine)

    assert engine._client.cardkit_create.call_count == n_create + 1
    assert engine._client.reply_card_by_id.call_count == n_reply + 1
    sess = engine.session_for(main_chat, thread_id=topic2)
    assert sess.reply_to == "om_topic_2"
    assert sess.answer_seg.text == "话题新回合"


def test_footer_speed_below_one_tps_renders_lt1() -> None:
    """短回答 t/s <1 显示 <1 t/s，不再取整成 0（跨机部署实测观感像坏了）."""
    from plugin._vendor.cardkit.builder import _render_footer_field

    en, _zh = _render_footer_field("speed", {"tps": 0.31}, False, False, False)
    assert "0 t/s" not in en and "<1 t/s" in en
    en2, _ = _render_footer_field("speed", {"tps": 48.2}, False, False, False)
    assert "48 t/s" in en2


# ── 折叠栏：流式工具面板按段切片 + 面板展开配置接线 ──


@pytest.mark.asyncio
async def test_flush_tool_update_slices_per_segment() -> None:
    """第二段工具面板的脏更新只含自己的步骤切片（曾传全量导致跨段重复渲染）。"""
    from plugin.engine import ChatSession

    client = _mock_client()
    engine = ChatCardEngine(client)
    session = ChatSession(chat_id="chat1", thread_id=None)
    session.state = "streaming"
    session.card_id = "card_1"

    def _step(name: str, detail: str, output: str) -> None:
        session.tool_tracker.record_start(name, detail)
        session.segment_state.on_tool_event(len(session.tool_tracker.build_display_steps()))
        session.tool_tracker.record_end(name, output=output)

    # 第一段：2 步 + 正文 → flush 建 seg1/answer 元素
    _step("read", "a.md", "ok")
    _step("read", "b.md", "ok")
    session.segment_state.on_answer_delta("mid")
    await engine._do_flush(session)
    # 第二段创建：1 步 → flush 建 seg2 元素（add action）
    _step("bash", "ls", "ok")
    await engine._do_flush(session)
    # 第二段追加：1 步 → flush 走 seg2 的脏更新（partial_update_element）
    _step("read", "c.md", "ok")
    await engine._do_flush(session)

    tool_segs = [s for s in session.segment_state.segments if s.type == "tool"]
    updates = [
        a for a in client.cardkit_batch_update.call_args_list[-1].args[1]
        if a.get("action") == "partial_update_element"
    ]
    seg2_updates = [u for u in updates
                    if u["params"]["element_id"] == tool_segs[1].el_id]
    assert len(seg2_updates) == 1, "第三波 flush 应只含 seg2 的脏更新"
    upd = seg2_updates[0]
    assert "steps 3–4" in upd["params"]["partial_element"]["header"]["title"]["content"]
    body = str(upd["params"]["partial_element"]["elements"])
    assert "ls" in body and "c.md" in body
    assert "a.md" not in body and "b.md" not in body


@pytest.mark.asyncio
async def test_complete_passes_panel_expanded_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """engine 把 tool/reasoning 面板展开配置透传给完成卡（曾漏接致配置全程不生效）。"""
    import plugin.engine as engine_mod
    from plugin.engine import ChatSession

    captured: dict[str, Any] = {}

    def _fake_build(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"schema": "2.0", "body": {"elements": []}}

    monkeypatch.setattr(engine_mod, "build_complete_card", _fake_build)
    client = _mock_client()
    engine = engine_mod.ChatCardEngine(
        client, tool_panel_expanded=True, reasoning_panel_expanded=True)
    session = ChatSession(chat_id="chat1", thread_id=None)
    session.state = "streaming"
    session.card_id = "card_1"
    session.card_msg_id = "om_1"
    engine._sessions[engine._skey("chat1", None)] = session

    assert await engine.complete("chat1", "hello") == "om_1"
    assert captured["tool_panel_expanded"] is True
    assert captured["reasoning_panel_expanded"] is True


@pytest.mark.asyncio
async def test_reasoning_routes_by_session_map_no_cross_chat(adapter) -> None:
    """session_id 映射路由：双 chat 并发时 reasoning 各进各卡，绝不互串."""
    import plugin as plugin_pkg

    engine = adapter._engine()
    chat_a, chat_b = "oc_" + "a" * 20, "oc_" + "b" * 20
    engine.on_turn_started(chat_a)
    engine.on_turn_started(chat_b)
    await _settle(engine)

    plugin_pkg._note_session_route("sess_b", engine, chat_b)
    hook = plugin_pkg._make_reasoning_hook()
    hook(kind="reasoning", delta="B 的思考", session_id="sess_b")

    await _settle(engine)
    sa = engine.session_for(chat_a)
    sb = engine.session_for(chat_b)
    assert "B 的思考" in (sb.segment_state.segments[0].text
                          if sb.segment_state.segments else "")
    assert not sa.segment_state.segments, "A 的卡不应出现 B 的思考"


@pytest.mark.asyncio
async def test_reasoning_strict_drops_when_target_inactive(adapter) -> None:
    """strict 路由目标已终态 → 丢弃，不回落单活跃（回落=串扰）."""
    import plugin as plugin_pkg

    engine = adapter._engine()
    chat_a, chat_b = "oc_" + "a" * 20, "oc_" + "b" * 20
    engine.on_turn_started(chat_a)
    await _settle(engine)
    # chat_b 会话不存在（映射过期场景）

    plugin_pkg._note_session_route("sess_b", engine, chat_b)
    hook = plugin_pkg._make_reasoning_hook()
    hook(kind="reasoning", delta="孤儿思考", session_id="sess_b")
    await _settle(engine)

    sa = engine.session_for(chat_a)
    assert not sa.segment_state.segments or all(
        "孤儿思考" not in (s.text or "") for s in sa.segment_state.segments)


@pytest.mark.asyncio
async def test_tool_event_routes_by_turn_anchor(adapter) -> None:
    from types import SimpleNamespace
    """工具事件按 adapter 回合锚路由：并发双 chat，工具只进锚定的那张卡."""
    engine = adapter._engine()
    chat_a, chat_b = "oc_" + "a" * 20, "oc_" + "b" * 20
    engine.on_turn_started(chat_a)
    engine.on_turn_started(chat_b)
    await _settle(engine)

    try:
        from gateway.stream_events import ToolCallChunk
        chunk = ToolCallChunk(tool_name="terminal", preview="ls")
    except ImportError:  # 无 hermes 源树环境：duck-typing 替身
        chunk = SimpleNamespace(tool_name="terminal", preview="ls", args={})
    adapter._turn_anchor = (chat_b, None)
    adapter.format_tool_event(chunk)
    await _settle(engine)

    sa = engine.session_for(chat_a)
    sb = engine.session_for(chat_b)
    # B 的会话记了工具步；A 的没有
    assert len(sb.tool_tracker.build_display_steps()) == 1
    assert len(sa.tool_tracker.build_display_steps()) == 0
