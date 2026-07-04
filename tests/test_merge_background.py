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
