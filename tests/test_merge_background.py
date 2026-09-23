"""跨回合合并测试：background 回合（message_id=None）复用同 chat 卡片。

验证「一个对话任务全合并一张卡」的核心逻辑：
- on_message_started(message_id=None) → 复用同 chat 最近卡片（终态重激活）
- delta 回调（message_id=None）→ 按 chat_id 找 session 追加
- 回合分隔（begin_new_turn）→ 两回合 answer 不拼在一起
- 用户新消息（om_xxx）→ 新卡片（新对话任务，不复用）
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.streaming.segments import SegmentType
from hermes_lark_streaming.streaming.session import SessionState


def _enable(ctrl: StreamCardController) -> None:
    ctrl._cfg._raw = {
        "streaming": {"enabled": True},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ctrl._loop = loop


def _mock_create_card(ctrl: StreamCardController) -> None:
    """mock _fire_and_forget 避免真实卡片创建（测试手动设 card_id/state）。"""
    patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()).start()


def _seed_streaming_session(ctrl: StreamCardController, message_id: str, chat_id: str):
    """创建一个已就绪的 session（卡片已建 + STREAMING + flush ready）。"""
    ctrl.on_message_started(message_id=message_id, chat_id=chat_id)
    session = ctrl._sessions[message_id]
    session.card_id = "card_" + message_id
    session.card_msg_id = "om_card_" + message_id
    session.state = SessionState.STREAMING
    session.flush.set_card_message_ready(True)
    return session


def test_background_turn_reuses_completed_card() -> None:
    """background 回合复用同 chat 已 COMPLETED 卡片 + 重激活 + 回合分隔。"""
    ctrl = StreamCardController()
    _enable(ctrl)
    _mock_create_card(ctrl)

    chat = "oc_chat_merge_test"

    # --- 第一回合（用户消息 om_msg1）---
    session = _seed_streaming_session(ctrl, "om_msg1", chat)
    assert ctrl._chat_index[chat] == "om_msg1"
    ctrl.on_answer(message_id="om_msg1", chat_id=chat, text="第一回合答案")
    segs = session.segment_state.segments
    assert len(segs) == 1 and segs[0].type == SegmentType.ANSWER

    # 第一回合完成 → 终态
    session.state = SessionState.COMPLETED
    session.flush.mark_completed()

    # --- background 回合（message_id=None）---
    ctrl.on_message_started(message_id=None, chat_id=chat)

    # 断言：复用 session（不新建），reused=True，重激活 STREAMING
    assert len(ctrl._sessions) == 1, "background 回合应复用 session，不新建"
    assert session.reused is True
    assert session.state == SessionState.STREAMING

    # background 回合内容（commentary + answer）—— begin_new_turn 强制新建 segment
    ctrl.on_thinking(message_id=None, chat_id=chat, text="background commentary")
    ctrl.on_answer(message_id=None, chat_id=chat, text="第二回合答案")

    # 断言：两回合 answer 分开（begin_new_turn 让 background 回合新建 answer segment，不拼到第一回合）
    answer_segs = [s for s in session.segment_state.segments if s.type == SegmentType.ANSWER]
    assert len(answer_segs) == 2, "两回合 answer 应各自独立 segment（回合分隔）"
    assert answer_segs[0].text == "第一回合答案"
    # background 回合：commentary（thinking，show_reasoning=False 时拆出的 answer 部分进 answer segment）
    # + 第二回合 answer 都在第二回合 answer segment（回合内同类型追加是 Cheerwhy 的正常行为）
    assert "第二回合答案" in answer_segs[1].text
    assert "第一回合答案" not in answer_segs[1].text, "background 回合 answer 不应拼到第一回合"


def test_background_turn_no_reusable_card_skips() -> None:
    """background 回合（None）无同 chat 可复用卡片时跳过（不创建新卡）。"""
    ctrl = StreamCardController()
    _enable(ctrl)
    _mock_create_card(ctrl)

    ctrl.on_message_started(message_id=None, chat_id="oc_no_prior")
    assert ctrl._sessions == {}, "无可复用卡片时 background 回合应跳过，不新建"


def test_user_new_message_creates_new_card() -> None:
    """用户新消息（om_xxx）创建新卡片（新对话任务），即使同 chat 有 prior session。"""
    ctrl = StreamCardController()
    _enable(ctrl)
    _mock_create_card(ctrl)

    chat = "oc_chat_multi"
    session1 = _seed_streaming_session(ctrl, "om_msg1", chat)
    session1.state = SessionState.COMPLETED
    session1.flush.mark_completed()

    # 用户新消息（om_msg2）—— 应创建新 session（新对话任务）
    ctrl.on_message_started(message_id="om_msg2", chat_id=chat)
    assert "om_msg2" in ctrl._sessions, "用户新消息应创建新卡片"
    assert ctrl._sessions["om_msg2"] is not session1, "新消息应是新 session，不复用"
    assert ctrl._chat_index[chat] == "om_msg2", "chat 索引更新到最新消息"


def test_delta_callback_falls_back_to_chat_id() -> None:
    """delta 回调 message_id=None 时按 chat_id 找 session（含终态重激活）。"""
    ctrl = StreamCardController()
    _enable(ctrl)
    _mock_create_card(ctrl)

    chat = "oc_chat_fallback"
    session = _seed_streaming_session(ctrl, "om_msg1", chat)
    session.state = SessionState.COMPLETED
    session.flush.mark_completed()

    # on_answer 用 message_id=None + chat_id → 应找到 session + 重激活 + 追加
    result = ctrl.on_answer(message_id=None, chat_id=chat, text="bg answer")
    assert result is True
    assert session.reused is True
    assert session.state == SessionState.STREAMING
    assert any(s.type == SegmentType.ANSWER for s in session.segment_state.segments)


def test_reactivate_only_for_completed_state() -> None:
    """重激活只对 COMPLETED 生效；STREAMING 直接复用；其他终态（FAILED/ABORTED）不重激活。"""
    ctrl = StreamCardController()
    _enable(ctrl)

    # STREAMING session：_reactivate_session 返回 True（已活跃），不改 reused
    import asyncio as _a
    loop = _a.new_event_loop()
    from hermes_lark_streaming.streaming.session import CardSession
    s_stream = CardSession("om_s", "oc_s", loop)
    s_stream.state = SessionState.STREAMING
    s_stream.card_id = "c"
    assert ctrl._reactivate_session(s_stream) is True
    assert s_stream.reused is False  # STREAMING 不标记 reused

    # FAILED session：不重激活
    s_fail = CardSession("om_f", "oc_f", loop)
    s_fail.state = SessionState.FAILED
    s_fail.card_id = "c"
    assert ctrl._reactivate_session(s_fail) is False


def test_has_chat_card_gate_states() -> None:
    """has_chat_card：注入侧 delta 门查询——STREAMING/COMPLETED 可接流，FAILED/ABORTED/无卡/无 chat 不可。"""
    ctrl = StreamCardController()
    _enable(ctrl)

    import asyncio as _a
    loop = _a.new_event_loop()
    from hermes_lark_streaming.streaming.session import CardSession

    s_stream = CardSession("om_s", "oc_gate", loop)
    s_stream.state = SessionState.STREAMING
    s_stream.card_id = "c"
    ctrl._sessions["om_s"] = s_stream
    ctrl._chat_index["oc_gate"] = "om_s"

    assert ctrl.has_chat_card("oc_gate") is True  # STREAMING → 可接流

    s_stream.state = SessionState.COMPLETED
    assert ctrl.has_chat_card("oc_gate") is True  # COMPLETED → 可重激活接流

    s_stream.state = SessionState.FAILED
    assert ctrl.has_chat_card("oc_gate") is False  # FAILED → 不可

    s_stream.state = SessionState.ABORTED
    assert ctrl.has_chat_card("oc_gate") is False  # ABORTED → 不可

    assert ctrl.has_chat_card(None) is False  # 无 chat_id → 不可
    assert ctrl.has_chat_card("oc_unknown") is False  # 未知 chat → 不可


def test_bg_watcher_notice_appends_into_card() -> None:
    """bg watcher 非对话通知：同 chat 有卡片 → 重激活 + 追加 NOTICE 段 + 触发重完成."""
    ctrl = StreamCardController()
    _enable(ctrl)
    _mock_create_card(ctrl)

    chat = "oc_chat_notice"
    session = _seed_streaming_session(ctrl, "om_msg1", chat)
    session.state = SessionState.COMPLETED
    session.flush.mark_completed()

    with patch.object(ctrl, "_complete_session") as complete_mock:
        result = asyncio.get_event_loop().run_until_complete(
            ctrl.on_bg_watcher_notify(chat_id=chat, content="💾 Self-improvement review: Skill patched")
        )
    assert result is True
    assert session.state == SessionState.STREAMING  # 已重激活，等重完成
    segs = session.segment_state.segments
    from hermes_lark_streaming.streaming.segments import SegmentType as _ST
    assert segs[-1].type == _ST.NOTICE
    assert "Self-improvement review" in segs[-1].text
    complete_mock.assert_called_once()


def test_bg_watcher_notice_no_card_falls_back_to_background_card() -> None:
    """无同 chat 卡片 → 退化发独立 background card（原行为）."""
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "on_background_deliver", return_value=True) as bg_mock:
        result = asyncio.get_event_loop().run_until_complete(
            ctrl.on_bg_watcher_notify(chat_id="oc_no_card", content="process finished")
        )
    assert result is True
    bg_mock.assert_awaited_once()


def test_document_deliver_replies_file_under_card() -> None:
    """文档交付：上传 + 回复到卡片消息下方，返回 True 让网关跳过原生 send_document."""
    from unittest.mock import AsyncMock, Mock

    ctrl = StreamCardController()
    _enable(ctrl)
    session = _seed_streaming_session(ctrl, "om_msg1", "oc_doc_chat")
    session.state = SessionState.COMPLETED
    session.flush.mark_completed()

    ctrl._initialized = True
    ctrl._client = Mock()
    ctrl._client.upload_document = AsyncMock(return_value="file_v3_xxx")
    ctrl._client.reply_file_by_id = AsyncMock(return_value=True)

    result = asyncio.get_event_loop().run_until_complete(
        ctrl.on_document_deliver(chat_id="oc_doc_chat", file_path="/tmp/report.pdf")
    )
    assert result is True
    ctrl._client.upload_document.assert_awaited_once_with("/tmp/report.pdf")
    ctrl._client.reply_file_by_id.assert_awaited_once_with("om_card_om_msg1", "file_v3_xxx", "report.pdf")


def test_document_deliver_without_card_returns_false() -> None:
    """无可复用卡片（未保留/已被 TTL 清理）→ 返回 False，网关走原生 send_document."""
    ctrl = StreamCardController()
    _enable(ctrl)

    result = asyncio.get_event_loop().run_until_complete(
        ctrl.on_document_deliver(chat_id="oc_unknown", file_path="/tmp/x.pdf")
    )
    assert result is False


def test_busy_ack_updates_heartbeat_status_line() -> None:
    """busy ack：STREAMING 卡片 → 写心跳状态行返回 True；终态/无卡返回 False."""
    ctrl = StreamCardController()
    _enable(ctrl)

    session = _seed_streaming_session(ctrl, "om_msg1", "oc_ack_chat")
    session.heartbeat_enabled = True  # 卡片创建时按 heartbeat_in_card 配置预留
    assert ctrl.on_busy_ack(chat_id="oc_ack_chat", text="⏳ Queued for the next turn") is True
    assert session.heartbeat_text == "⏳ Queued for the next turn"
    assert session.heartbeat_dirty is True

    # 已完成卡片不为 ack 重激活
    session.state = SessionState.COMPLETED
    session.flush.mark_completed()
    assert ctrl.on_busy_ack(chat_id="oc_ack_chat", text="↪ Redirected current run") is False

    # 未预留心跳行的卡片不接管
    _seed_streaming_session(ctrl, "om_msg2", "oc_ack_chat2")
    assert ctrl.on_busy_ack(chat_id="oc_ack_chat2", text="⏳ Queued") is False


def test_redirect_opens_new_card_and_routes_completion() -> None:
    """busy redirect：旧卡 NOTICE 收尾 + _interrupt_map 路由完成信号到新卡."""
    ctrl = StreamCardController()
    _enable(ctrl)
    _mock_create_card(ctrl)

    chat = "oc_redirect_chat"
    old_session = _seed_streaming_session(ctrl, "om_old", chat)
    ctrl.on_answer(message_id="om_old", chat_id=chat, text="被打断前的部分回答")

    with patch.object(ctrl, "_complete_session") as complete_mock:
        ctrl.on_redirect_started(message_id="om_new", chat_id=chat, anchor_id="om_new", session_key="sk")

    # 旧卡：NOTICE 收尾 + 触发完成 + 同步置终态（防尾部 delta 插到 NOTICE 后）
    from hermes_lark_streaming.streaming.segments import SegmentType as _ST
    assert old_session.segment_state.segments[-1].type == _ST.NOTICE
    assert "新卡片" in old_session.segment_state.segments[-1].text
    assert old_session.state == SessionState.COMPLETED
    complete_mock.assert_called_once()

    # 路由：old→new 映射 + chat 索引指向新卡 + 新 session 已建
    assert ctrl._interrupt_map["om_old"] == "om_new"
    assert ctrl._chat_index[chat] == "om_new"
    assert "om_new" in ctrl._sessions

    # 完成信号（带旧 id）路由到新卡：生产中 _complete_session 异步收尾使旧卡终态，
    # 直查跳过 → 走 _interrupt_map；这里手动模拟终态（mock 掉了真实完成）
    old_session.state = SessionState.COMPLETED
    old_session.flush.mark_completed()
    resolved = ctrl._completion_session("om_old", chat)
    assert resolved is not None and resolved.message_id == "om_new"

    # redirect 后流式回调带新 id → 直接命中新卡
    assert ctrl.on_answer(message_id="om_new", chat_id=chat, text="纠正后的回答") is True
    new_session = ctrl._sessions["om_new"]
    assert any(s.type == _ST.ANSWER and "纠正后的回答" in s.text for s in new_session.segment_state.segments)


def test_redirect_skips_when_no_running_card() -> None:
    """无可复用卡片时 redirect 仅开新卡，不注册中断映射."""
    ctrl = StreamCardController()
    _enable(ctrl)
    _mock_create_card(ctrl)

    ctrl.on_redirect_started(message_id="om_new", chat_id="oc_empty", anchor_id="om_new")
    assert "om_new" in ctrl._sessions
    assert not ctrl._interrupt_map


def test_busy_ack_stored_while_card_creating() -> None:
    """redirect ack 在新卡还在 CREATING 时到达 → 暂存心跳文本."""
    ctrl = StreamCardController()
    _enable(ctrl)
    _mock_create_card(ctrl)

    ctrl.on_message_started(message_id="om_new", chat_id="oc_ack_creating")
    session = ctrl._sessions["om_new"]
    assert session.state == SessionState.IDLE  # mock 关闭了建卡协程，停在 IDLE（建卡前）

    assert ctrl.on_busy_ack(chat_id="oc_ack_creating", text="↪ Redirected current run") is True
    assert session.heartbeat_text == "↪ Redirected current run"
    assert session.heartbeat_dirty is True


def test_tooluse_steps_cap_detail() -> None:
    """工具展示步的 detail 截断到 200 字符（面板正文不爆体积）."""
    from hermes_lark_streaming.streaming.tooluse import ToolStatus, ToolUseTracker

    tracker = ToolUseTracker()
    tracker.record_start("terminal", "y" * 800)
    tracker.record_end("terminal", output="done")
    steps = tracker.build_display_steps()
    assert len(steps[0]["detail"]) <= 200
    assert steps[0]["status"] == ToolStatus.SUCCESS.value
