"""ChatCardEngine — platform 插件模式的流式卡片引擎.

与注入模式的 StreamCardController（message_id 键、真 delta 追加）不同，这里会话以
**chat** 为键：GatewayStreamConsumer 的 draft 帧（send_draft(chat_id, draft_id, content)）
天然不带 message 身份，且 content 是**全量快照**（transport._draft_push 语义），所以
ANSWER segment 采用「整段置换」而非追加，避免快照重放导致文本重复。

段模型（v1 简化）：REASONING / ANSWER 各至多一个（全回合合并），TOOL 面板首个工具
事件时追加并在原位整体刷新 —— 与 fork 初始卡「工具面板在正文区」的视觉一致。
复用注入模式已验证的底层件：SegmentState / ToolUseTracker / FlushController /
builder（CardKit v2）/ FeishuClient（SDK 封装）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from hermes_lark_streaming.cardkit.builder import (
    HEARTBEAT_ELEMENT_ID,
    build_complete_card,
    build_streaming_card_v2,
)
from hermes_lark_streaming.cardkit.markdown import optimize_markdown_style
from hermes_lark_streaming.streaming.flush import FlushController
from hermes_lark_streaming.streaming.segment_helper import (
    build_add_segment_action,
    build_reasoning_finalized_action,
    build_tool_update_action,
)
from hermes_lark_streaming.streaming.segments import Segment, SegmentState, SegmentType
from hermes_lark_streaming.streaming.tooluse import ToolUseTracker

_logger = logging.getLogger("hermes_lark_streaming.plugin")

# 心跳/interim 文本进卡前的字符上限（防异常长文本撑爆状态行）
_HEARTBEAT_MAX_LEN = 300


@dataclass
class ChatSession:
    """单 chat 的流式卡片会话（一回合一张卡）."""

    chat_id: str
    created_at: float = field(default_factory=time.time)
    state: str = "creating"  # creating → streaming → completed / failed
    card_id: str | None = None
    card_msg_id: str | None = None
    answer_seg: Segment | None = None
    tool_seg: Segment | None = None
    heartbeat_text: str = ""
    sequence: int = 0
    card_create_task: asyncio.Task | None = None
    segment_state: SegmentState = field(default_factory=SegmentState)
    tool_tracker: ToolUseTracker = field(default_factory=ToolUseTracker)
    flush: FlushController | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in ("completed", "failed")


class ChatCardEngine:
    """chat 键流式卡片引擎 — 由 StreamingFeishuAdapter 驱动.

    生命周期：首条 draft 帧触发建卡（后台任务，不阻塞 transport）→ 后续帧整段
    置换 answer 文本 → 回合终态经 :meth:`complete` 渲染完成卡。
    """

    def __init__(self, client: Any, *, body_text_size: str = "normal_v2",
                 show_tool_use: bool = True, header_enabled: bool = False,
                 width_mode: str = "default") -> None:
        self._client = client
        self._body_text_size = body_text_size
        self._show_tool_use = show_tool_use
        self._header_enabled = header_enabled
        self._width_mode = width_mode
        self._sessions: dict[str, ChatSession] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Any = None

    # ── 会话查询 ──

    def session_for(self, chat_id: str) -> ChatSession | None:
        return self._sessions.get(chat_id)

    def active_session(self, chat_id: str) -> ChatSession | None:
        """活跃（非终态）会话 — send() 完成路由的判定入口."""
        session = self._sessions.get(chat_id)
        if session is not None and not session.is_terminal:
            return session
        return None

    def streaming_sessions(self) -> list[ChatSession]:
        return [s for s in self._sessions.values() if s.state == "streaming"]

    def last_card_msg_id(self, chat_id: str) -> str | None:
        """最近一张卡的消息 id（文档交付 reply 锚点用，终态也算）."""
        session = self._sessions.get(chat_id)
        return session.card_msg_id if session else None

    # ── 流式输入 ──

    def on_draft(self, chat_id: str, content: str) -> None:
        """draft 帧（全量快照）— 确保会话与建卡，整段置换 answer 文本.

        由 async 的 send_draft 调用：此处捕获引擎事件循环（后续 format_tool_event
        可能从 agent 工作线程同步到达，需 call_soon_threadsafe 回环）。
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
            self._thread = threading.current_thread()
        session = self._ensure_session(chat_id)
        if session.answer_seg is None:
            # 空文本走 on_answer_delta：在正确位置（reasoning 之后）新建空 ANSWER 段
            session.segment_state.on_answer_delta("")
            session.answer_seg = session.segment_state.segments[-1]
        session.answer_seg.text = content
        session.answer_seg.dirty = True
        self._schedule(session)

    def on_tool_start(self, tool_name: str, detail: str = "") -> None:
        """format_tool_event(ToolCallChunk) 的落点 — 记录步骤并刷新工具面板."""
        session = self._any_streaming_session()
        if session is None:
            return
        session.tool_tracker.record_start(tool_name, detail)
        if session.tool_seg is None:
            session.segment_state.on_tool_event(len(session.tool_tracker.build_display_steps()))
            session.tool_seg = session.segment_state.segments[-1]
            session.tool_seg.created = False  # 待 flush 建元素
        session.tool_seg.dirty = True
        self._schedule(session)

    def on_tool_end(self, tool_name: str, *, error: str = "", output: str = "") -> None:
        session = self._any_streaming_session()
        if session is None:
            return
        session.tool_tracker.record_end(tool_name, error=error, output=output)
        if session.tool_seg is not None:
            session.tool_seg.dirty = True
        self._schedule(session)

    def on_reasoning(self, chat_id: str, text: str) -> None:
        """reasoning 增量 — 插件钩子不带 chat，多会话并发时丢弃（防串扰）."""
        target = self._resolve_reasoning_target(chat_id)
        if target is None:
            return
        target.segment_state.on_reasoning_delta(text)
        self._schedule(target)

    def on_heartbeat(self, chat_id: str, text: str) -> None:
        """心跳/interim 文本 → 卡片末尾状态行."""
        session = self.active_session(chat_id)
        if session is None:
            return
        session.heartbeat_text = text[:_HEARTBEAT_MAX_LEN]
        self._schedule(session)

    # ── 完成 ──

    async def complete(self, chat_id: str, final_text: str, *, is_error: bool = False,
                       duration: float | None = None, tokens: dict[str, int] | None = None,
                       model: str = "") -> str | None:
        """回合终态：渲染完成卡（含 footer），返回卡片消息 id."""
        session = self.active_session(chat_id)
        if session is None:
            return None
        if session.card_create_task is not None:
            await session.card_create_task
        if session.flush is not None:
            session.flush.mark_completed()
        if session.state == "failed" or session.card_id is None:
            return session.card_msg_id  # 建卡已失败：交回原生文本保底
        if final_text and session.answer_seg is not None:
            session.answer_seg.text = final_text
            session.answer_seg.dirty = False  # 完成卡整体重渲，不再单独流式
        session.segment_state.finalize_segments(
            len(session.tool_tracker.build_display_steps()))
        session.state = "failed" if is_error else "completed"
        card = build_complete_card(
            segments=session.segment_state.segments,
            all_tool_steps=session.tool_tracker.build_display_steps(),
            footer_data=self._footer_data(session, duration, tokens, model),
            is_error=is_error,
            header_enabled=self._header_enabled,
            body_text_size=self._body_text_size,
            show_tool_use=self._show_tool_use,
            width_mode=self._width_mode,
        )
        session.sequence += 1
        try:
            await self._client.cardkit_close_streaming(session.card_id, sequence=session.sequence)
            await self._client.cardkit_update(session.card_id, card, sequence=session.sequence)
        except Exception as e:
            _logger.warning("plugin complete card update failed: chat=%s err=%s", chat_id, e)
        return session.card_msg_id

    async def abandon(self, chat_id: str) -> None:
        """流被弃（中断/异常）— 尽力按错误收尾，避免永久转圈卡."""
        session = self.active_session(chat_id)
        if session is None:
            return
        await self.complete(chat_id, session.answer_seg.text if session.answer_seg else "",
                            is_error=True)

    # ── 内部 ──

    def _ensure_session(self, chat_id: str) -> ChatSession:
        session = self._sessions.get(chat_id)
        if session is None or session.is_terminal:
            # 旧卡（终态）保留在聊天里，新回合开新会话
            session = ChatSession(chat_id=chat_id)
            self._sessions[chat_id] = session
        if session.state == "creating" and session.card_create_task is None:
            assert self._loop is not None
            if session.flush is None:
                session.flush = FlushController(loop=self._loop)
            session.card_create_task = self._loop.create_task(self._do_create_card(session))
        return session

    def _any_streaming_session(self) -> ChatSession | None:
        streaming = self.streaming_sessions()
        return streaming[0] if streaming else None

    def _resolve_reasoning_target(self, chat_id: str) -> ChatSession | None:
        # 钩子不带 chat：显式 chat 命中优先；否则仅单会话时兜底（多会话并发丢弃防串扰）
        session = self.active_session(chat_id)
        if session is not None:
            return session
        streaming = self.streaming_sessions()
        return streaming[0] if len(streaming) == 1 else None

    def _schedule(self, session: ChatSession) -> None:
        if session.state == "creating" or session.flush is None or self._loop is None:
            return  # 建卡完成后的首次 flush 会带上全部已暂存内容
        callback = lambda s=session: self._do_flush(s)  # noqa: E731
        if threading.current_thread() is self._thread:
            session.flush.schedule_update(callback)
        else:
            self._loop.call_soon_threadsafe(session.flush.schedule_update, callback)

    async def _do_create_card(self, session: ChatSession) -> None:
        card = build_streaming_card_v2(
            show_tool_use=False,
            show_reasoning=False,
            show_streaming_element=False,
            header_enabled=self._header_enabled,
            text_size=self._body_text_size,
            heartbeat_enabled=True,
            width_mode=self._width_mode,
        )
        try:
            card_id = await self._client.cardkit_create(card)
            card_msg_id = await self._client.reply_card_by_id(session.chat_id, card_id)
        except Exception as e:
            _logger.warning("plugin card create failed: chat=%s err=%s", session.chat_id, e)
            session.state = "failed"
            return
        session.card_id = card_id
        session.card_msg_id = card_msg_id
        session.state = "streaming"
        if session.flush is not None:
            session.flush.set_card_message_ready(True)
        self._schedule(session)

    async def _do_flush(self, session: ChatSession) -> None:
        """幂等 flush：新 segment 建元素 + 脏文本流式 + 工具面板/心跳更新."""
        if session.is_terminal or not session.card_id:
            return
        segments = session.segment_state.segments
        all_steps = session.tool_tracker.build_display_steps()
        actions: list[dict[str, Any]] = []
        for seg in segments:
            if not seg.created:
                actions.append(build_add_segment_action(
                    seg, all_steps, text_size=self._body_text_size))
                seg.created = True
            elif seg.type == SegmentType.TOOL and seg.dirty:
                actions.append(build_tool_update_action(element_id=seg.el_id, steps=all_steps))
                seg.dirty = False
            elif (seg.type == SegmentType.REASONING and seg.elapsed_ms > 0
                  and not seg.reasoning_finalized):
                actions.append(build_reasoning_finalized_action(seg))
                seg.reasoning_finalized = True
        if actions:
            session.sequence += 1
            try:
                await self._client.cardkit_batch_update(
                    session.card_id, actions, sequence=session.sequence)
            except Exception as e:
                _logger.warning("plugin batch update failed: chat=%s err=%s",
                                session.chat_id, e)
                return
        # 脏文本流式（answer / reasoning）
        for seg in segments:
            if not seg.created or not seg.dirty:
                continue
            content = optimize_markdown_style(seg.text) or " "
            session.sequence += 1
            try:
                await self._client.cardkit_stream_element(
                    session.card_id, seg.text_el_id or seg.el_id, content,
                    sequence=session.sequence)
                seg.dirty = False
            except Exception as e:
                _logger.debug("plugin stream element failed: el=%s err=%s", seg.el_id, e)
        # 心跳状态行
        if session.heartbeat_text:
            session.sequence += 1
            try:
                await self._client.cardkit_stream_element(
                    session.card_id, HEARTBEAT_ELEMENT_ID,
                    optimize_markdown_style(session.heartbeat_text) or " ",
                    sequence=session.sequence)
                session.heartbeat_text = ""
            except Exception as e:
                _logger.debug("plugin heartbeat update failed: %s", e)

    def _footer_data(self, session: ChatSession, duration: float | None,
                     tokens: dict[str, int] | None, model: str) -> dict[str, Any] | None:
        data: dict[str, Any] = {
            "duration": duration if duration is not None else time.time() - session.created_at,
        }
        if tokens:
            data["input_tokens"] = tokens.get("input_tokens", 0)
            data["output_tokens"] = tokens.get("output_tokens", 0)
        if model:
            data["model"] = model
        return data
