"""StreamCardController — 流式卡片主控制器（单例）."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future as ConcurrentFuture
from typing import Any

from .config import Config
from .feishu import (
    FeishuClient,
    FeishuClientConfig,
)
from .streaming.controller import StreamingController
from .streaming.segments import SegmentType
from .streaming.session import CardSession, SessionState
from .streaming.text import strip_reasoning_tags

_logger = logging.getLogger("hermes_lark_streaming")
_CARD_CREATION_WAIT_SEC = 10.0


class StreamCardController(StreamingController):
    """流式卡片控制器 — 管理多条消息的卡片生命周期."""

    def __init__(self) -> None:
        self._cfg = Config()
        self._client: FeishuClient | None = None
        self._sessions: dict[str, CardSession] = {}
        self._chat_index: dict[str, str] = {}
        self._interrupt_map: dict[str, str] = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._session_ttl = self._cfg.card_duration_sec
        self._loop: asyncio.AbstractEventLoop | None = None
        self._text_fallback_needed: set[str] = set()
        self._text_fallback_aliases: dict[str, set[str]] = {}

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled and bool(self._cfg.feishu_app_id or self._cfg.env_app_id)

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            app_id = self._cfg.feishu_app_id or self._cfg.env_app_id
            app_secret = self._cfg.feishu_app_secret or self._cfg.env_app_secret
            if not app_id or not app_secret:
                raise RuntimeError("feishu credentials not configured")
            self._client = FeishuClient(
                FeishuClientConfig(
                    app_id=app_id,
                    app_secret=app_secret,
                    base_url=self._cfg.feishu_base_url,
                )
            )
            self._initialized = True

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        """获取事件循环，缓存以便跨线程复用."""
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
            return loop
        except RuntimeError:
            pass
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        return None

    def _get_active_session(self, message_id: str) -> CardSession | None:
        """获取非终态的活跃 session，不存在或已终态返回 None."""
        session = self._sessions.get(message_id)
        if session is None or session.state.is_terminal:
            return None
        return session

    def _find_session_by_chat(self, chat_id: str) -> CardSession | None:
        """按 chat_id 找最近 session（即使已终态，但仍在 _sessions 里）.

        用于 background 回合（message_id=None）复用同 chat 卡片。
        """
        mid = self._chat_index.get(chat_id)
        if mid is None:
            return None
        session = self._sessions.get(mid)
        if session is None or not session.has_card:
            return None
        return session

    def _reactivate_session(self, session: CardSession) -> bool:
        """终态（COMPLETED）session 重激活：重新接受更新（跨回合合并复用卡片）."""
        if session.state == SessionState.STREAMING:
            return True
        if session.state != SessionState.COMPLETED:
            return False
        session.state = SessionState.STREAMING
        session.flush.reset_for_reactivate()
        session.deferred_background_review_closed = False
        if session.segment_state is not None:
            session.segment_state.begin_new_turn()
        session.reused = True
        session.created_at = time.time()  # 续命 TTL
        logging.getLogger("gateway.run").info(
            "[cheerwhy-merge] session reactivated msg=%s (cross-turn merge)", session.message_id[:12])
        return True

    def _resolve_session(
        self, message_id: str | None, chat_id: str | None,
    ) -> CardSession | None:
        """delta 回调统一找 session：message_id 优先，None/找不到时按 chat fallback + 重激活."""
        if message_id:
            session = self._get_active_session(message_id)
            if session is not None:
                return session
        if chat_id:
            session = self._find_session_by_chat(chat_id)
            if session is not None and self._reactivate_session(session):
                return session
        logging.getLogger("gateway.run").info(
            "[cheerwhy-card] delta NO session msg=%r chat=%s → hermes 内置接管（纯文本）",
            message_id, (chat_id or "")[:12])
        return None

    def _fire_and_forget(
        self,
        coro: Coroutine[Any, Any, Any],
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Future[Any] | ConcurrentFuture | None:
        try:
            task = loop.create_task(coro)
            task.add_done_callback(self._on_bg_task_done)
            return task
        except RuntimeError:
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                fut.add_done_callback(self._on_bg_task_done)
                return fut
            except Exception:
                _logger.debug("fire_and_forget failed", exc_info=True)
                return None

    def on_message_started(
        self,
        *,
        message_id: str | None,
        chat_id: str,
        anchor_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """消息处理开始 — 创建会话 + 发占位卡片.

        message_id=None 的回合（hermes background process 完成注入的 synth_event）
        视为同一对话任务的延续：复用同 chat 最近卡片（终态重激活），不新建。
        """
        if not self.enabled:
            return
        # background/内部回合（message_id=None）：复用同 chat 最近卡片
        if not message_id:
            session = self._find_session_by_chat(chat_id)
            _gw = logging.getLogger("gateway.run")
            if session is None:
                _gw.info("[cheerwhy-merge] bg turn NO reusable card, skip chat=%s", chat_id[:12])
                return
            reused = self._reactivate_session(session)
            _gw.info("[cheerwhy-merge] bg turn msg=None chat=%s reuse_msg=%s reactivated=%s",
                     chat_id[:12], session.message_id[:12], reused)
            return
        if message_id in self._sessions:
            return

        self._prune_stale_sessions()

        loop = self._get_loop()
        if loop is None:
            _logger.warning("no event loop available, skipping: msg=%s", message_id[:12])
            return
        session = CardSession(message_id, chat_id, loop)
        self._sessions[message_id] = session
        self._chat_index[chat_id] = message_id  # chat 反向索引（供 background 回合复用）
        if anchor_id and anchor_id != message_id:
            session.anchor_id = anchor_id
            self._sessions[anchor_id] = session
        logging.getLogger("gateway.run").info(
            "[cheerwhy-card] session created msg=%s chat=%s anchor=%s (新卡片)",
            message_id[:12], chat_id[:12], (anchor_id or "")[:12])
        # 话题场景投递诊断：话题回复（anchor != msg）或话题群根消息（thread_id 存在）。
        # 图片/文件投递需带 root_id=anchor 或 receive_id_type=thread_id 才能落进话题
        # （根因常在 Hermes send_image_file 链路不处理 thread_id，这里只做可观测性标记）。
        if (anchor_id and anchor_id != message_id) or thread_id:
            logging.getLogger("gateway.run").info(
                "[cheerwhy-card] 话题场景 msg=%s anchor=%s thread=%s → "
                "图片/文件投递需 root_id=anchor 或 receive_id_type=thread_id",
                message_id[:12], (anchor_id or "")[:12], (thread_id or "")[:12])

        session.create_task = self._fire_and_forget(self._do_create_card(session), loop)

    def _mark_text_fallback_needed(self, session: CardSession) -> None:
        keys = {session.message_id}
        if session.anchor_id:
            keys.add(session.anchor_id)
        self._text_fallback_needed.update(keys)
        for key in keys:
            self._text_fallback_aliases[key] = set(keys)

    def consume_text_fallback(self, message_id: str) -> bool:
        """Return whether gateway should undo already_sent and deliver plain text."""
        if message_id not in self._text_fallback_needed:
            return False
        keys = self._text_fallback_aliases.pop(message_id, {message_id})
        for key in keys:
            self._text_fallback_needed.discard(key)
            self._text_fallback_aliases.pop(key, None)
        return True

    def on_thinking(self, *, message_id: str | None, text: str, chat_id: str | None = None) -> bool:
        """思考内容增量."""
        if not self.enabled:
            return False
        session = self._resolve_session(message_id, chat_id)
        if session is None or session.guard.should_skip("on_thinking"):
            return False

        if session.segment_state is None:
            return False
        return self._on_thinking_segment(session, text)

    def on_reasoning(self, *, message_id: str | None, text: str, chat_id: str | None = None) -> bool:
        """Native model reasoning delta (incremental append)."""
        if not self.enabled:
            return False
        if not self._cfg.show_reasoning:
            return False
        session = self._resolve_session(message_id, chat_id)
        if session is None or session.guard.should_skip("on_reasoning"):
            return False

        if session.segment_state is None:
            return False

        session.segment_state.on_reasoning_delta(text)
        self._schedule_flush(session)
        return True

    def on_tool_update(
        self,
        *,
        message_id: str | None,
        tool_name: str,
        status: str,
        detail: str = "",
        chat_id: str | None = None,
    ) -> bool:
        """工具调用事件."""
        logging.getLogger("gateway.run").info(
            "[cheerwhy-card] on_tool_update 收到 tool=%s status=%s msg=%r chat=%s",
            tool_name, status, message_id, (chat_id or "")[:12])
        if not self.enabled:
            return False
        session = self._resolve_session(message_id, chat_id)
        if session is None or session.guard.should_skip("on_tool_update"):
            return False
        if session.segment_state is None:
            return False

        if status in ("running", "started", "tool.started"):
            session.tool_use.record_start(tool_name, detail)
        else:
            is_error = status in ("error", "failed")
            session.tool_use.record_end(
                tool_name,
                error=detail if is_error else "",
                output="" if is_error else detail,
            )

        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))
        self._schedule_flush(session)
        return True

    def on_answer(self, *, message_id: str | None, text: str, chat_id: str | None = None) -> bool:
        """答案文本增量（流式）."""
        if not self.enabled:
            return False
        session = self._resolve_session(message_id, chat_id)
        if session is None or session.guard.should_skip("on_answer"):
            return False
        if session.segment_state is None:
            return False

        answer_text = strip_reasoning_tags(text)
        if not answer_text:
            return False

        session.segment_state.on_answer_delta(answer_text)
        self._schedule_flush(session)
        return True

    def on_aborted(self, *, message_id: str) -> None:
        """用户 /stop 导致消息被中断."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None:
            return

        session.state = SessionState.ABORTED
        session.flush.mark_completed()
        _logger.info("on_aborted: msg=%s state=ABORTED", message_id[:12])

        self._complete_session(session)

    def on_interrupted(
        self,
        *,
        old_message_id: str,
        new_message_id: str,
        chat_id: str,
        anchor_id: str | None = None,
    ) -> None:
        """用户发送新消息导致前一条消息被中断 — abort A + create B."""
        if not self.enabled:
            return

        old_session = self._get_active_session(old_message_id)
        if old_session is not None:
            old_session.state = SessionState.ABORTED
            old_session.flush.mark_completed()
            _logger.info(
                "on_interrupted: abort old msg=%s",
                old_message_id[:12],
            )
            self._complete_session(old_session)

        if new_message_id not in self._sessions:
            loop = self._get_loop()
            if loop is not None:
                reply_anchor_id = anchor_id if anchor_id and anchor_id != new_message_id else None
                session = CardSession(new_message_id, chat_id, loop)
                session.anchor_id = reply_anchor_id
                self._sessions[new_message_id] = session
                if reply_anchor_id:
                    self._sessions[reply_anchor_id] = session
                _logger.info(
                    "on_interrupted: create new msg=%s chat=%s anchor=%s",
                    new_message_id[:12],
                    chat_id[:12],
                    (reply_anchor_id or new_message_id)[:12],
                )
                session.create_task = self._fire_and_forget(self._do_create_card(session), loop)

        self._interrupt_map[old_message_id] = new_message_id
        for key, val in list(self._interrupt_map.items()):
            if val == old_message_id:
                self._interrupt_map[key] = new_message_id

    async def on_completed_wait(
        self,
        *,
        message_id: str | None,
        answer: str = "",
        duration: float = 0.0,
        model: str = "",
        tokens: dict | None = None,
        context: dict | None = None,
        chat_id: str | None = None,
    ) -> bool:
        """消息处理完成，并等待卡片真正收尾后返回是否已发送."""
        if not self.enabled:
            return False
        session = self._completion_session(message_id, chat_id)
        if session is None:
            logging.getLogger("gateway.run").info(
                "[cheerwhy-card] on_completed_wait msg=%r chat=%s → NO session (fallback)",
                message_id, (chat_id or "")[:12])
            return False
        message_id = session.message_id

        if not await self._wait_for_card_creation(session):
            logging.getLogger("gateway.run").info(
                "[cheerwhy-card] on_completed_wait msg=%s → card creation NOT ready (fallback, 10s 超时)",
                message_id[:12])
            self._mark_text_fallback_needed(session)
            self._cleanup(message_id)
            return False

        if session.state == SessionState.FAILED:
            logging.getLogger("gateway.run").info(
                "[cheerwhy-card] on_completed_wait msg=%s → state=FAILED (fallback)", message_id[:12])
            self._mark_text_fallback_needed(session)
            self._cleanup(message_id)
            return False

        if not session.has_card:
            logging.getLogger("gateway.run").info(
                "[cheerwhy-card] on_completed_wait msg=%s → no card (fallback)", message_id[:12])
            self._mark_text_fallback_needed(session)
            self._cleanup(message_id)
            return False

        logging.getLogger("gateway.run").info(
            "[cheerwhy-card] on_completed_wait msg=%s has_card=%s state=%s → complete",
            message_id[:12], session.has_card, session.state,
        )

        self._apply_completion_payload(
            session=session,
            answer=answer,
            duration=duration,
            model=model,
            tokens=tokens,
            context=context,
        )

        return await self._complete_session_wait(session)

    def on_cron_deliver(
        self,
        *,
        chat_id: str,
        content: str,
        loop: asyncio.AbstractEventLoop,
        task_name: str = "",
        run_time: str = "",
    ) -> bool:
        """Cron 推送 — 包装为静态卡片发送，成功返回 True."""
        if not self.enabled or not content or not chat_id:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._do_cron_deliver(chat_id, content, task_name=task_name, run_time=run_time), loop
        )
        try:
            future.result(timeout=30)
            _logger.info("cron card delivered: chat=%s len=%d", chat_id[:12], len(content))
            return True
        except Exception:
            _logger.warning("cron card delivery failed", exc_info=True)
            return False

    async def on_background_deliver(
        self,
        *,
        chat_id: str,
        preview: str,
        content: str,
        reply_to_message_id: str | None = None,
    ) -> bool:
        """Background 任务完成推送 — 包装为静态卡片发送，成功返回 True."""
        if not self.enabled or not content or not chat_id:
            return False
        try:
            await self._do_background_deliver(
                chat_id,
                preview,
                content,
                reply_to_message_id=reply_to_message_id,
            )
            _logger.info("background card delivered: chat=%s len=%d", chat_id[:12], len(content))
            return True
        except Exception:
            _logger.warning("background card delivery failed", exc_info=True)
            return False

    async def on_bg_watcher_notify(
        self,
        *,
        chat_id: str,
        content: str,
        reply_to_message_id: str | None = None,
    ) -> bool:
        """background watcher 的 text-only 通知 → 合并到活跃 agent 卡片，否则发 background card.

        hermes background watcher（run.py text-only notification）默认 adapter.send 纯文本；
        此方法接管：有活跃 agent session（同 chat）则追加 content 作 answer segment + 重完成
        卡片（一个对话任务一张卡）；否则退化发独立 background 卡片。
        """
        if not self.enabled or not content or not chat_id:
            return False
        session = self._find_session_by_chat(chat_id)
        if session is not None and session.has_card:
            # 有活跃 agent session（agent 已通过 on_tool_update tool=process 跟踪 + 已回复）→
            # watcher 通知冗余，完全过滤（不发卡片不发文本）
            logging.getLogger("gateway.run").info(
                "[cheerwhy-card] bg watcher 通知过滤（agent 已跟踪 process）chat=%s", chat_id[:12])
            return True
        # 无活跃 agent session（/background 命令等，agent 没跟踪）→ 发 background card
        _logger.info("bg watcher 通知无活跃卡片，发 background card chat=%s", chat_id[:12])
        return await self.on_background_deliver(
            chat_id=chat_id,
            preview="background",
            content=content,
            reply_to_message_id=reply_to_message_id,
        )

    def defer_background_review(
        self,
        *,
        message_id: str,
        text: str,
        sender: Callable[[str], Any],
    ) -> bool:
        """暂存 Hermes background review 通知，等卡片收尾后再发送."""
        if not self.enabled or not text or not callable(sender):
            return False
        session = self._get_active_session(message_id)
        if session is None:
            return False
        with session.deferred_background_review_lock:
            if session.deferred_background_review_closed:
                return False
            session.deferred_background_reviews.append((text, sender))
        return True

    def _flush_deferred_background_reviews(self, session: CardSession) -> None:
        lock = getattr(session, "deferred_background_review_lock", None)
        reviews = getattr(session, "deferred_background_reviews", None)
        if lock is None or reviews is None:
            return
        with lock:
            session.deferred_background_review_closed = True
            pending = list(reviews)
            reviews.clear()
        for text, sender in pending:
            try:
                sender(text)
            except Exception:
                _logger.debug("background review sender failed", exc_info=True)

    def _cleanup(self, message_id: str) -> None:
        session = self._sessions.pop(message_id, None)
        if session is None:
            return
        anchor = getattr(session, "anchor_id", None)
        if anchor and self._sessions.get(anchor) is session:
            del self._sessions[anchor]
        stale_keys = [k for k, v in self._interrupt_map.items() if v == message_id]
        for k in stale_keys:
            del self._interrupt_map[k]
        session.flush.mark_completed()
        if session.image_resolver:
            session.image_resolver.cancel_pending()

    def _completion_session(
        self, message_id: str | None, chat_id: str | None = None,
    ) -> CardSession | None:
        # message_id 直查（非终态或 FAILED）
        if message_id:
            session = self._sessions.get(message_id)
            if session is not None and (not session.state.is_terminal or session.state == SessionState.FAILED):
                return session

            redirected_id = self._interrupt_map.pop(message_id, None)
            if redirected_id is not None:
                _logger.info(
                    "on_completed: redirect msg=%s -> msg=%s",
                    message_id[:12],
                    redirected_id[:12],
                )
                redirected = self._sessions.get(redirected_id)
                if redirected is not None and not redirected.state.is_terminal:
                    return redirected

        # background 回合（message_id=None 或直查未果）：按 chat 找复用 session + 重激活
        if chat_id:
            session = self._find_session_by_chat(chat_id)
            if session is not None and self._reactivate_session(session):
                return session
        return None

    async def _wait_for_card_creation(self, session: CardSession) -> bool:
        task = session.create_task
        if task is None:
            return True
        try:
            if isinstance(task, asyncio.Future):
                await asyncio.wait_for(task, timeout=_CARD_CREATION_WAIT_SEC)
            else:
                await asyncio.wait_for(asyncio.wrap_future(task), timeout=_CARD_CREATION_WAIT_SEC)
            return True
        except TimeoutError:
            _logger.warning(
                "card creation timed out: msg=%s timeout=%.1fs",
                session.message_id[:12],
                _CARD_CREATION_WAIT_SEC,
            )
            task.cancel()
            session.mark_failed()
            return False
        except asyncio.CancelledError:
            session.mark_failed()
            return False
        except Exception:
            _logger.debug("card creation task failed", exc_info=True)
            return False

    def _apply_completion_payload(
        self,
        *,
        session: CardSession,
        answer: str,
        duration: float,
        model: str,
        tokens: dict | None,
        context: dict | None,
    ) -> None:
        if answer and session.segment_state:
            final_answer = strip_reasoning_tags(answer)
            # 复用 session（跨回合合并）：begin_new_turn 已强制下个 delta 新建 segment，
            # 直接追加（不拼到第一回合 answer 后）；非复用时仅当尚无 ANSWER 才追加
            if final_answer and (session.reused or not any(
                seg.type == SegmentType.ANSWER for seg in session.segment_state.segments
            )):
                session.segment_state.on_answer_delta(final_answer)

        session.footer = {
            "duration": duration,
            "model": model,
            **({"input_tokens": tokens.get("input_tokens")} if tokens else {}),
            **({"output_tokens": tokens.get("output_tokens")} if tokens else {}),
            **({"context_used": context.get("used_tokens")} if context else {}),
            **({"context_max": context.get("max_tokens")} if context else {}),
        }

    def _complete_session(self, session: CardSession) -> None:
        """异步完成当前流式卡片."""
        session.flush.mark_completed()
        self._fire_and_forget(self._do_complete_card(session), session._loop)

    async def _complete_session_wait(self, session: CardSession) -> bool:
        """完成当前流式卡片，并等待最终 API 结果."""
        session.flush.mark_completed()
        return await self._do_complete_card(session)

    def _prune_stale_sessions(self) -> None:
        now = time.time()
        # 同一 session 注册在 message_id + anchor_id 多个 key 下（见 on_message_started），
        # 按 session 对象去重，避免对同一 session 重复 warning + 重复 _cleanup
        # （历史 bug：话题群 session 双 key 曾导致单次 prune 连打多条相同 warning）。
        seen: set[int] = set()
        stale: list[str] = []
        for mid, s in self._sessions.items():
            if mid is None or id(s) in seen:
                continue
            seen.add(id(s))
            if now - s.created_at > self._session_ttl:
                stale.append(mid)
        for mid in stale:
            _logger.warning("pruning stale session: msg=%s", mid[:12])
            self._cleanup(mid)

    @staticmethod
    def _on_bg_task_done(fut: asyncio.Future[Any] | ConcurrentFuture) -> None:
        try:
            fut.result()
        except asyncio.CancelledError:
            return
        except Exception:
            _logger.warning("background task failed", exc_info=True)


_controller: StreamCardController | None = None
_controller_lock = threading.Lock()


def get_controller() -> StreamCardController:
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = StreamCardController()
        return _controller
