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
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ._vendor.cardkit.builder import (
    HEARTBEAT_ELEMENT_ID,
    build_complete_card,
    build_streaming_card_v2,
)
from ._vendor.cardkit.markdown import optimize_markdown_style
from ._vendor.streaming.flush import FlushController
from ._vendor.streaming.segment_helper import (
    build_add_segment_action,
    build_reasoning_finalized_action,
    build_tool_update_action,
)
from ._vendor.streaming.segments import Segment, SegmentState, SegmentType
from ._vendor.streaming.text import strip_reasoning_tags
from ._vendor.streaming.tooluse import ToolUseTracker

# 本包 logger 不进 gateway.log（hermes logging 配置问题，见 AGENTS.md）——
# 诊断日志统一走 gateway.run logger。
_logger = logging.getLogger("gateway.run")

# 心跳/interim 文本进卡前的字符上限（防异常长文本撑爆状态行）
_HEARTBEAT_MAX_LEN = 300


@dataclass
class ChatSession:
    """单 chat 的流式卡片会话（一回合一张卡）."""

    chat_id: str
    reply_to: str | None = None  # 回复锚（用户消息 id）；None = 直发 chat
    created_at: float = field(default_factory=time.time)
    state: str = "creating"  # creating → streaming → completed / failed
    card_id: str | None = None
    card_msg_id: str | None = None
    answer_seg: Segment | None = None
    redirected: bool = False  # ↪ redirect ack 已见：下一 draft 收旧开新
    _reasoning_logged: bool = False
    tool_seg: Segment | None = None
    heartbeat_text: str = ""
    sequence: int = 0
    card_create_task: asyncio.Task | None = None
    segment_state: SegmentState = field(default_factory=SegmentState)
    tool_tracker: ToolUseTracker = field(default_factory=ToolUseTracker)
    flush: FlushController | None = None

    completed_at: float = 0.0

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
                 width_mode: str = "default",
                 footer_fields: list[list[str]] | None = None,
                 footer_show_label: bool = False,
                 footer_enabled: bool = True) -> None:
        self._client = client
        self._body_text_size = body_text_size
        self._show_tool_use = show_tool_use
        self._header_enabled = header_enabled
        self._width_mode = width_mode
        # footer 形态与注入模式同源（vendor Config 读 HERMES_HOME/config.yaml
        # 的 streaming.footer 段），两形态渲染一致
        self._footer_fields = footer_fields or [["elapsed", "model", "context"]]
        self._footer_show_label = footer_show_label
        self._footer_enabled = footer_enabled
        self._sessions: dict[str, ChatSession] = {}
        # chat → 回合累计 usage（post_api_request 钩子按 session_id 归组，complete 消费）
        self._usage: dict[str, dict[str, Any]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Any = None

    # ── usage 聚合（post_api_request 钩子 → footer tokens/t/s）──

    @staticmethod
    def _chat_of_session(session_id: str) -> str:
        """agent session key（agent:main:feishu:dm:<chat_id>）→ chat_id；解析失败返 ""."""
        m = re.search(r"oc_[0-9a-f]{16,40}", str(session_id or ""))
        return m.group(0) if m else ""

    def record_usage(self, session_id: str, usage: dict[str, Any] | None, model: str = "") -> None:
        """单次 API 调用的 usage 累加进所属 chat 的回合桶."""
        if not usage:
            return
        chat_id = self._chat_of_session(session_id)
        bucket = self._usage.setdefault(
            chat_id, {"input": 0, "output": 0, "model": "", "context_max": 0,
                      "first_started": None, "last_ended": None})
        # input 取最后值不累加：每轮 API 的 prompt 都含全量历史，累加会虚高
        # 数量级（实测 2000 字回合累出 88 万）；注入模式口径即最后值
        bucket["input"] = max(bucket["input"], int(usage.get("prompt_tokens") or 0))
        bucket["output"] += int(usage.get("completion_tokens")
                                or usage.get("output_tokens") or usage.get("total_tokens") or 0)
        if model:
            bucket["model"] = model
        if usage.get("context_length"):
            bucket["context_max"] = int(usage["context_length"])
            bucket["context_used"] = int(usage.get("prompt_tokens") or 0)
        # API 时间跨度（含排队/工具间隔）：t/s 分母用它，口径对齐注入模式的 _turn_seconds
        if usage.get("started_at"):
            if bucket["first_started"] is None:
                bucket["first_started"] = usage["started_at"]
            bucket["last_ended"] = usage.get("ended_at") or bucket["last_ended"]

    def _pop_usage(self, chat_id: str) -> dict[str, Any] | None:
        bucket = self._usage.pop(chat_id, None) or self._usage.pop("", None)
        if not bucket or not (bucket["input"] or bucket["output"]):
            return None
        return bucket

    def on_turn_started(self, chat_id: str) -> None:
        """回合开始（send_typing 时机）→ 立即建卡.

        带工具的回合 draft 要等工具全部跑完才出现，此前用户什么都看不到
        （注入模式消息进来就建卡）。typing 先行建卡（无锚直发 chat），工具
        事件与正文随后进卡；DM/普通群与注入模式体感一致。
        """
        self._capture_loop()
        existing = self._sessions.get(chat_id)
        if (existing is not None and existing.is_terminal
                and time.time() - existing.completed_at < 5.0):
            # typing 是 2s 心跳循环：回合刚完成后的尾巴调用，不是新回合——
            # 重建会得到一张永挂 loading 的空卡
            return
        existing = self._sessions.get(chat_id)
        if existing is None or existing.is_terminal:
            self._ensure_session(chat_id)
            _logger.info("[feishu-streaming] turn started: chat=%s", chat_id[:12])
        # typing 2s 心跳循环的重复调用：会话健在时静默（此前每 2s 刷一条）

    def mark_redirect(self, chat_id: str) -> None:
        """↪ redirect ack（用户纠正、interrupt 模式）→ 标记当前会话：下一个
        draft 到来时收旧开新（同锚——interrupt 注入新指令不换 event_message_id，
        锚变化检测覆盖不到，此为唯一信号）。"""
        session = self.active_session(chat_id)
        if session is not None:
            session.redirected = True
            _logger.info("[feishu-streaming] redirect marked: chat=%s", chat_id[:12])

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

    def on_draft(self, chat_id: str, content: str, reply_to: str | None = None) -> None:
        """draft 帧（全量快照）— 确保会话与建卡，整段置换 answer 文本.

        由 async 的 send_draft 调用：此处捕获引擎事件循环（后续 format_tool_event
        可能从 agent 工作线程同步到达，需 call_soon_threadsafe 回环）。
        reply_to 是 transport _draft_metadata 带的用户消息锚（首轮记录，后续幂等）。
        """
        self._capture_loop()
        session = self._ensure_session(chat_id)
        if (session.redirected and content and content.strip()
                and (session.state != "creating" or session.tool_tracker.build_display_steps())):
            # redirect 后首个有内容 draft：旧卡 NOTICE 收尾 + 开新卡（同锚）。
            # 等价注入模式 REDIRECT marker（on_redirect_started）语义。
            _logger.info("[feishu-streaming] redirect boundary: sealing card, opening new")
            old_session = session
            self._sessions[chat_id] = ChatSession(chat_id=chat_id, reply_to=session.reply_to)
            session = self._ensure_session(chat_id)
            self._seal_session(old_session, notice="↪ 任务已按新指令重启，结果见下方新卡片")
        if session.state != "creating" and reply_to and session.reply_to and reply_to != session.reply_to:
            # draft 锚变化 = 新回合开始（排队 followup 被 drain，新消息身份）：
            # 旧卡按已有内容收尾（绿色完成态，避免永挂"处理中"），新回合开新卡。
            # 与注入模式的 FOLLOWUP_COMPLETE hook 等价，信号反而更可靠——
            # 锚来自每回合 consumer 的 initial_reply_to_id（即用户消息 id）。
            _logger.info(
                "[feishu-streaming] followup boundary: anchor %s -> %s, sealing old card",
                session.reply_to[:12], reply_to[:12])
            old_session = session
            self._sessions[chat_id] = ChatSession(chat_id=chat_id, reply_to=reply_to)
            session = self._ensure_session(chat_id)  # 新会话补建卡 task
            self._seal_session(old_session)
        if session.reply_to is None and reply_to:
            session.reply_to = reply_to
        if session.answer_seg is None:
            # 空文本走 on_answer_delta：在正确位置（reasoning 之后）新建空 ANSWER 段
            session.segment_state.on_answer_delta("")
            session.answer_seg = session.segment_state.segments[-1]
        # 与注入模式 on_answer 同款防线：<thinking>/<thought> 标签形态的
        # 思考泄漏清洗（模型裸文本碎片两种模式都无法剥，此处只防标签形态）
        session.answer_seg.text = strip_reasoning_tags(content)
        session.answer_seg.dirty = True
        self._schedule(session)

    def on_tool_start(self, tool_name: str, detail: str = "") -> None:
        """format_tool_event(ToolCallChunk) 的落点 — 记录步骤并刷新工具面板.

        工具事件可能先于任何 draft（带工具回合的常态）：无会话时也建卡，
        让工具面板从第一步就滚动在用户眼前。
        """
        session = self._any_active_session()
        if session is None:
            # 工具事件先于 typing 的兜底：在事件循环线程上捕获 loop 建""占位
            # 会话（typing 到达后按 chat 归位）；跨线程无 loop 时丢弃该事件
            try:
                self._capture_loop()
            except RuntimeError:
                return
            session = self._ensure_session("")
        session.tool_tracker.record_start(tool_name, detail)
        if session.tool_seg is None:
            session.segment_state.on_tool_event(len(session.tool_tracker.build_display_steps()))
            session.tool_seg = session.segment_state.segments[-1]
            session.tool_seg.created = False  # 待 flush 建元素
        session.tool_seg.dirty = True
        self._schedule(session)

    def on_tool_end(self, tool_name: str, *, error: str = "", output: str = "") -> None:
        session = self._any_active_session()
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
        if not target._reasoning_logged:
            target._reasoning_logged = True
            _logger.info("[feishu-streaming] reasoning streaming into card: chat=%s",
                         target.chat_id[:12])
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
        session.completed_at = time.time()
        usage = self._pop_usage(chat_id)
        if (usage and usage.get("first_started") and usage.get("last_ended")
                and usage["last_ended"] > usage["first_started"] and duration is None):
            # 真实回合时长 = 首次 API 开始 → 最后一次 API 结束（含工具时间）；
            # 会话时长只覆盖流式尾巴，会让 t/s 虚高一个数量级
            duration = usage["last_ended"] - usage["first_started"]
        if usage:
            _logger.info(
                "[feishu-streaming] footer usage: chat=%s in=%d out=%d model=%s",
                chat_id[:12], usage.get("input", 0), usage.get("output", 0),
                usage.get("model") or model or "?")
        if tokens:
            usage = {"input": tokens.get("input_tokens", 0),
                     "output": tokens.get("output_tokens", 0), "model": model}
        card = build_complete_card(
            segments=session.segment_state.segments,
            all_tool_steps=session.tool_tracker.build_display_steps(),
            footer_data=self._footer_data(session, duration, usage, model),
            footer_fields=self._footer_fields,
            footer_show_label=self._footer_show_label,
            footer_enabled=self._footer_enabled,
            is_error=is_error,
            header_enabled=self._header_enabled,
            body_text_size=self._body_text_size,
            show_tool_use=self._show_tool_use,
            width_mode=self._width_mode,
        )
        try:
            session.sequence += 1
            await self._client.cardkit_close_streaming(session.card_id, sequence=session.sequence)
            session.sequence += 1
            await self._client.cardkit_update(session.card_id, card, sequence=session.sequence)
        except Exception as e:
            _logger.warning("plugin complete card update failed: chat=%s err=%s", chat_id, e)
        return session.card_msg_id

    async def append_notice(self, chat_id: str, text: str) -> str | None:
        """把 background/系统通知追加进该 chat 最近一张卡（跨回合合并）.

        会话可能是 COMPLETED（bg 回合在主回合完成后到达）——追加 NOTICE segment
        后整体重渲完成卡；会话仍活跃则交由 flush 建 NOTICE 元素。
        返回卡片消息 id；无可用卡片返回 None（调用方走原生文本保底）。
        """
        session = self._sessions.get(chat_id)
        if session is None or session.card_id is None:
            return None
        session.segment_state.add_notice(text)
        if session.is_terminal:
            card = build_complete_card(
                segments=session.segment_state.segments,
                all_tool_steps=session.tool_tracker.build_display_steps(),
                footer_data=self._footer_data(session, None, None, ""),
                footer_fields=self._footer_fields,
                footer_show_label=self._footer_show_label,
                footer_enabled=self._footer_enabled,
                header_enabled=self._header_enabled,
                body_text_size=self._body_text_size,
                show_tool_use=self._show_tool_use,
                width_mode=self._width_mode,
            )
            session.sequence += 1
            try:
                await self._client.cardkit_update(session.card_id, card, sequence=session.sequence)
            except Exception as e:
                _logger.warning("[feishu-streaming] append notice update failed: %s", e)
                return None
        else:
            self._schedule(session)
        _logger.info("[feishu-streaming] notice merged into card: chat=%s len=%d",
                     chat_id[:12], len(text))
        return session.card_msg_id

    async def abandon(self, chat_id: str) -> None:
        """流被弃（中断/异常）— 尽力按错误收尾，避免永久转圈卡."""
        session = self.active_session(chat_id)
        if session is None:
            return
        await self.complete(chat_id, session.answer_seg.text if session.answer_seg else "",
                            is_error=True)

    # ── 内部 ──

    def _capture_loop(self) -> None:
        """捕获引擎事件循环（首个 async 入口调用；后续跨线程调度用）."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
            self._thread = threading.current_thread()

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

    def _seal_session(self, session: ChatSession, notice: str | None = None) -> None:
        """旧会话收尾（followup 边界）：按已积累内容渲染完成卡，失败仅记日志."""
        assert self._loop is not None

        async def _seal() -> None:
            if session.card_create_task is not None:
                await session.card_create_task
            if session.state == "failed" or session.card_id is None:
                return
            session.completed_at = time.time()
            if notice:
                session.segment_state.add_notice(notice)
            session.segment_state.finalize_segments(
                len(session.tool_tracker.build_display_steps()))
            session.state = "completed"
            card = build_complete_card(
                segments=session.segment_state.segments,
                all_tool_steps=session.tool_tracker.build_display_steps(),
                footer_data=self._footer_data(session, None, None, ""),
                footer_fields=self._footer_fields,
                footer_show_label=self._footer_show_label,
                footer_enabled=self._footer_enabled,
                # 被打断的卡红色收尾（用户决策）：redirect 旧卡一眼可辨，
                # NOTICE 文案说明结果在新卡。注入模式原为绿色，此处按需变更
                is_aborted=bool(notice),
                header_enabled=self._header_enabled,
                body_text_size=self._body_text_size,
                show_tool_use=self._show_tool_use,
                width_mode=self._width_mode,
            )
            try:
                session.sequence += 1
                await self._client.cardkit_close_streaming(session.card_id, sequence=session.sequence)
                session.sequence += 1
                await self._client.cardkit_update(session.card_id, card, sequence=session.sequence)
            except Exception as e:
                _logger.warning("[feishu-streaming] seal old card failed: %s", e)

        self._loop.create_task(_seal())

    def _any_active_session(self) -> ChatSession | None:
        active = [s for s in self._sessions.values() if not s.is_terminal]
        return active[0] if active else None

    def _resolve_reasoning_target(self, chat_id: str) -> ChatSession | None:
        # 钩子不带 chat：显式 chat 命中优先；否则仅单会话时兜底（多会话并发丢弃防串扰）。
        # 兜底须含 creating（typing 建卡窗口内的 reasoning 也不能丢——长思考回合
        # 的 reasoning 若在建卡期被丢，卡片全程只有 loading）
        session = self.active_session(chat_id)
        if session is not None:
            return session
        active = [s for s in self._sessions.values() if not s.is_terminal]
        return active[0] if len(active) == 1 else None

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
            if session.reply_to:
                # 有锚：卡片落在用户消息下方（话题内即同一线程）
                card_msg_id = await self._client.reply_card_by_id(session.reply_to, card_id)
            else:
                # 无锚：直发 chat（chat_id 不是合法 reply 目标，reply 会 230001）
                card_msg_id = await self._client.send_card_to_chat(
                    session.chat_id, {"type": "card", "data": {"card_id": card_id}})
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
                     usage: dict[str, Any] | None, model: str) -> dict[str, Any] | None:
        data: dict[str, Any] = {
            "duration": duration if duration is not None else time.time() - session.created_at,
        }
        if usage:
            data["input_tokens"] = usage.get("input", 0)
            data["output_tokens"] = usage.get("output", 0)
            if model:
                data["model"] = model
            elif usage.get("model"):
                data["model"] = usage["model"]
            # t/s = 输出 tokens / 回合时长（注入模式同款口径）
            if data["output_tokens"] and data["duration"] > 0:
                data["tps"] = data["output_tokens"] / data["duration"]
            if usage.get("context_max"):
                data["context_max"] = usage["context_max"]
                data["context_used"] = usage.get("context_used", usage.get("input", 0))
        elif model:
            data["model"] = model
        return data
