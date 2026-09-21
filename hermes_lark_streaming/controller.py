"""StreamCardController — 流式卡片主控制器（单例）."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import Future as ConcurrentFuture
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import turn_registry
from .config import Config, hermes_home
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
_ZOMBIE_SCAN_SEC = 30.0  # 僵尸卡守护扫描间隔
# 非终态 session 无任何流式活动的判定阈值。
# 120→300（2026-08-06 修误杀：长回合 API 思考 2 分钟无流式事件被误判僵尸）
_ZOMBIE_IDLE_SEC = 300.0


class StreamCardController(StreamingController):
    """流式卡片控制器 — 管理多条消息的卡片生命周期."""

    def __init__(self, profile_home: Path | None = None) -> None:
        self._profile_home = (profile_home or hermes_home()).resolve()
        self._cfg = Config(self._profile_home)
        self._client: FeishuClient | None = None
        self._sessions: dict[str, CardSession] = {}
        self._chat_index: dict[str, str] = {}
        self._session_keys: dict[str, CardSession] = {}
        self._interrupt_map: dict[str, str] = {}
        self._initialized = False
        self._init_lock = threading.Lock()
        self._session_ttl = self._cfg.card_duration_sec
        self._zombie_started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._text_fallback_needed: set[str] = set()
        self._text_fallback_aliases: dict[str, set[str]] = {}
        self._unscoped_enabled: bool | None = None

    @property
    def enabled(self) -> bool:
        unscoped = self._needs_fallback_scope()
        if unscoped and self._unscoped_enabled is not None:
            return self._unscoped_enabled
        with self._credential_scope():
            enabled = self._cfg.enabled and bool(self._cfg.feishu_app_id or self._cfg.env_app_id)
        if unscoped and enabled:
            self._unscoped_enabled = True
        return enabled

    @staticmethod
    def _needs_fallback_scope() -> bool:
        try:
            from agent.secret_scope import current_secret_scope, is_multiplex_active  # type: ignore[import-not-found]
        except ImportError:
            return False
        return is_multiplex_active() and current_secret_scope() is None

    @contextmanager
    def _credential_scope(self) -> Iterator[None]:
        try:
            from agent.secret_scope import (  # type: ignore[import-not-found]
                build_profile_secret_scope,
                current_secret_scope,
                is_multiplex_active,
                reset_secret_scope,
                set_secret_scope,
            )
        except ImportError:
            yield
            return
        if not is_multiplex_active() or current_secret_scope() is not None:
            yield
            return
        token = set_secret_scope(build_profile_secret_scope(self._profile_home))
        try:
            yield
        finally:
            reset_secret_scope(token)

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self._credential_scope():
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
            self._start_zombie_guard()

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

    def has_chat_card(self, chat_id: str | None) -> bool:
        """同 chat 是否存在可接流的卡片（STREAMING 或可重激活的 COMPLETED）.

        供注入侧 delta 前置判断：busy redirect 会把 inbound_message_id 切到新消息，
        原回合卡片按 message_id 查不到时按 chat 兜底命中，避免内容走 Hermes 纯文本。
        无副作用——真正的重激活仍由 _resolve_session 在首条 delta 时完成。
        """
        if not chat_id:
            return False
        session = self._find_session_by_chat(chat_id)
        if session is None:
            return False
        return session.state in (SessionState.STREAMING, SessionState.COMPLETED)

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
        session_key: str | None = None,
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
        session.session_key = session_key
        self._sessions[message_id] = session
        self._chat_index[chat_id] = message_id  # chat 反向索引（供 background 回合复用）
        if session_key:
            self._session_keys[session_key] = session
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
        session.last_activity_at = time.time()
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

        session.last_activity_at = time.time()
        session.segment_state.on_reasoning_delta(text)
        self._schedule_flush(session)
        return True

    def on_heartbeat(self, *, message_id: str | None, text: str, chat_id: str | None = None) -> bool:
        """Hermes 长回合心跳（⏳ Working — N min ...）→ 卡片末尾状态行。

        返回 True = 已接管（文本进卡片元素，调用方应跳过 Hermes 原生心跳消息）；
        False = 未接管（无卡片/心跳进卡关闭/卡片已终态），调用方走原生保底。
        心跳频率极低（Hermes 默认 180s 一次），走 flush 同一条更新链即可。
        """
        if not self.enabled:
            return False
        if not self._cfg.heartbeat_in_card:
            return False
        if not text:
            return False
        session = self._resolve_session(message_id, chat_id)
        if session is None or session.guard.should_skip("on_heartbeat"):
            return False
        # 心跳只在回合 running 期有意义；终态/无卡场景退回原生。
        if session.state.is_terminal:
            return False
        # 配置层开关（卡创建时同步读 heartbeat_in_card → session.heartbeat_enabled）；
        # 若关闭则完全不接管（Hermes 原生心跳保底）。
        if not self._cfg.heartbeat_in_card:
            return False
        # 卡片创建中：文本暂存 + dirty，卡建好后的 flush 一并推（_do_create_card
        # 完成后会 schedule flush）。若卡创建失败 fallback，session 进 FAILED →
        # 上面 is_terminal 拦掉，Hermes 原生心跳保底。
        session.last_activity_at = time.time()
        session.heartbeat_text = text
        session.heartbeat_dirty = True
        if session.has_card:
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

        session.last_activity_at = time.time()
        if status in ("running", "started", "tool.started"):
            session.tool_use.record_start(tool_name, detail)
        else:
            is_error = status in ("error", "failed")
            session.tool_use.record_end(
                tool_name,
                error=detail if is_error else "",
                output="" if is_error else detail,
            )
            # clarify 工具 ended 且有待封卡标志 → 封旧卡 + 建新卡
            if session.clarify_pending_split:
                if tool_name and tool_name.strip().lower() == "clarify":
                    session.clarify_pending_split = False
                    self._schedule_clarify_split(session)
                else:
                    # pending 标志残留但工具名不匹配（如上游改名）——记录以便排查，
                    # 避免切卡静默失效。
                    _logger.warning(
                        "clarify_pending_split set but tool '%s' did not match, msg=%s",
                        tool_name,
                        session.message_id[:12],
                    )

        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))
        self._schedule_flush(session)
        return True

    def _schedule_clarify_split(self, session: CardSession) -> None:
        """封旧卡 + 建新卡（clarify 工具 ended 后触发）。"""
        loop = self._get_loop()
        if loop is None:
            return
        future: ConcurrentFuture | None = None
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._do_clarify_split(session), loop,
            )
            # 预算 = 卡片创建等待 + seal/建卡余量
            future.result(timeout=_CARD_CREATION_WAIT_SEC * 2 + 30)
        except Exception:
            _logger.warning(
                "clarify_split failed or timed out, msg=%s",
                session.message_id[:12],
                exc_info=True,
            )
            # 取消仍在运行的切卡协程，避免与后续 flush 重叠。
            if future is not None and not future.done():
                future.cancel()

    def on_answer(self, *, message_id: str | None, text: str, chat_id: str | None = None) -> bool:
        """答案文本增量（流式）."""
        if not self.enabled:
            return False
        session = self._resolve_session(message_id, chat_id)
        if session is None or session.guard.should_skip("on_answer"):
            return False
        if session.segment_state is None:
            return False

        session.last_activity_at = time.time()
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

    async def on_session_aborted(self, *, session_key: str) -> bool:
        """Terminate the active card bound to a Hermes session key."""
        if not self.enabled or not session_key:
            return False
        session = self._session_keys.pop(session_key, None)
        if session is None or session.state.is_terminal:
            return False

        session.state = SessionState.ABORTED
        session.flush.mark_completed()
        _logger.info("on_session_aborted: msg=%s state=ABORTED", session.message_id[:12])

        return await self._complete_session_after_creation(session)

    def on_interrupted(
        self,
        *,
        old_message_id: str,
        new_message_id: str,
        chat_id: str,
        anchor_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """用户发送新消息导致前一条消息被中断 — abort A + create B."""
        if not self.enabled:
            return

        old_session = self._get_active_session(old_message_id)
        session_key = session_key or (old_session.session_key if old_session is not None else None)
        if old_session is not None:
            old_session.state = SessionState.ABORTED
            old_session.flush.mark_completed()
            _logger.info(
                "on_interrupted: abort old msg=%s",
                old_message_id[:12],
            )
            self._complete_session(old_session)

        existing = self._sessions.get(new_message_id)
        if existing is None or existing.state.is_terminal:
            loop = self._get_loop()
            if loop is not None:
                reply_anchor_id = anchor_id if anchor_id and anchor_id != new_message_id else None
                session = CardSession(new_message_id, chat_id, loop)
                session.anchor_id = reply_anchor_id
                session.session_key = session_key
                self._sessions[new_message_id] = session
                if session_key:
                    self._session_keys[session_key] = session
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

    def on_clarify_enter(
        self,
        *,
        message_id: str,
        chat_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """clarify 进入：仅暂停 flush，等 tool.completed 再封卡（避免工具状态卡在 running）。"""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None or session.state != SessionState.STREAMING:
            return
        session.state = SessionState.CLARIFY_PAUSED  # 暂停 flush，保留当前卡

    def on_clarify_exit(
        self,
        *,
        message_id: str,
        chat_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """clarify 退出：标记待封卡，恢复 STREAMING，等 tool.completed 触发切卡。"""
        if not self.enabled:
            return
        session = self._sessions.get(message_id)
        if session is None or session.state != SessionState.CLARIFY_PAUSED:
            return  # 已被 interrupt 接管 / enter 未生效
        session.clarify_pending_split = True
        session.state = SessionState.STREAMING  # 让 tool.completed 能更新卡片

    async def on_completed_wait(
        self,
        *,
        message_id: str | None,
        answer: str = "",
        is_error: bool = False,
        reconcile_answer: bool = False,
        duration: float = 0.0,
        model: str = "",
        tokens: dict | None = None,
        context: dict | None = None,
        chat_id: str | None = None,
        image_paths: list[str] | None = None,
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
            if session.has_card:
                _logger.info("on_completed_wait: msg=%s card creation not ready but card exists", message_id[:12])
            else:
                logging.getLogger("gateway.run").info(
                    "[cheerwhy-card] on_completed_wait msg=%s → card creation NOT ready (fallback, 10s 超时)",
                    message_id[:12])
                self._mark_text_fallback_needed(session)
            self._cleanup_session(session)
            return False

        if session.state == SessionState.FAILED:
            if session.has_card:
                _logger.info("on_completed_wait: msg=%s state=FAILED but card exists", message_id[:12])
            else:
                logging.getLogger("gateway.run").info(
                    "[cheerwhy-card] on_completed_wait msg=%s → state=FAILED (fallback)", message_id[:12])
                self._mark_text_fallback_needed(session)
            self._cleanup_session(session)
            return False

        if not session.has_card:
            logging.getLogger("gateway.run").info(
                "[cheerwhy-card] on_completed_wait msg=%s → no card (fallback)", message_id[:12])
            self._mark_text_fallback_needed(session)
            self._cleanup_session(session)
            return False

        logging.getLogger("gateway.run").info(
            "[cheerwhy-card] on_completed_wait msg=%s has_card=%s state=%s → complete",
            message_id[:12], session.has_card, session.state,
        )

        self._apply_completion_payload(
            session=session,
            answer=answer,
            reconcile_answer=reconcile_answer,
            duration=duration,
            model=model,
            tokens=tokens,
            context=context,
        )
        if is_error:
            session.mark_failed()

        # image_generate 产物图：上传 img_key + 存 session，等上传完才 complete
        if image_paths:
            await self._attach_images_to_session(session, image_paths)

        return await self._complete_session_wait(session)

    async def _attach_images_to_session(
        self, session: CardSession, image_paths: list[str]
    ) -> None:
        """上传 image_generate 产物图到飞书，img_key 存 session.image_keys 供卡片渲染."""
        if self._client is None:
            return
        for path in image_paths:
            try:
                img_key = await self._client.upload_image_file(path)
                if img_key:
                    session.image_keys.append(img_key)
                    logging.getLogger("gateway.run").info(
                        "[cheerwhy-card] image_generate 产物上传 msg=%s path=%s -> %s",
                        session.message_id[:12], path[-40:], img_key)
            except Exception:
                _logger.debug("image upload failed for %s", path, exc_info=True)

    def on_cron_deliver(
        self,
        *,
        chat_id: str,
        content: str,
        loop: asyncio.AbstractEventLoop | None,
        task_name: str = "",
        run_time: str = "",
        job_id: str = "",
    ) -> bool:
        """Return True when card delivery owns the text, including an uncertain timeout."""
        # 用 gateway.run logger（进 gateway.log），hermes_lark_streaming logger 不进文件
        _diag = logging.getLogger("gateway.run")
        if not self.enabled or not content or not chat_id:
            _diag.info(
                "[cheerwhy-cron] ctrl skip enabled=%s content_len=%d chat=%s",
                self.enabled, len(content), chat_id[:12] if chat_id else "?",
            )
            return False
        coroutine = self._do_cron_deliver(
            chat_id, content, task_name=task_name, run_time=run_time, job_id=job_id
        )
        try:
            if loop is not None and loop.is_running() and not loop.is_closed():
                try:
                    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                except Exception:
                    coroutine.close()
                    raise
                try:
                    future.result(timeout=30)
                except TimeoutError:
                    if future.done():
                        # Distinguish a completed send's own TimeoutError from our wait budget.
                        future.result()
                    else:
                        # Cancellation cannot retract an accepted remote send. Keep ownership
                        # rather than racing the still-running card with a native text fallback.
                        future.add_done_callback(self._on_bg_task_done)
                        _diag.warning("cron card delivery pending after timeout: chat=%s", chat_id[:12])
                        return True
            else:
                asyncio.run(coroutine)
            _diag.info("cron card delivered: chat=%s len=%d", chat_id[:12], len(content))
            return True
        except Exception:
            _diag.warning(
                "[cheerwhy-cron] delivery failed chat=%s", chat_id[:12], exc_info=True
            )
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
        session_key = getattr(session, "session_key", None)
        if session_key and self._session_keys.get(session_key) is session:
            del self._session_keys[session_key]
        stale_keys = [k for k, v in self._interrupt_map.items() if v == message_id]
        for k in stale_keys:
            del self._interrupt_map[k]
        session.flush.mark_completed()
        if session.image_resolver:
            session.image_resolver.cancel_pending()

    def _cleanup_session(self, session: CardSession) -> None:
        if self._sessions.get(session.message_id) is session:
            self._sessions.pop(session.message_id, None)
        anchor = session.anchor_id
        if anchor and self._sessions.get(anchor) is session:
            del self._sessions[anchor]
        session_key = session.session_key
        if session_key and self._session_keys.get(session_key) is session:
            del self._session_keys[session_key]
        stale_keys = [key for key, value in self._interrupt_map.items() if value == session.message_id]
        for key in stale_keys:
            del self._interrupt_map[key]
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
        reconcile_answer: bool = False,
    ) -> None:
        if answer and session.segment_state:
            final_answer = strip_reasoning_tags(answer)
            latest_answer = next(
                (seg for seg in reversed(session.active_segments()) if seg.type == SegmentType.ANSWER),
                None,
            )
            # 复用 session（跨回合合并）：begin_new_turn 已强制下个 delta 新建 segment，
            # 直接追加（不拼到第一回合 answer 后）；非复用时仅当尚无 ANSWER 才追加
            # ⚠️ 2026-08-01 修复: 话题/thread 模式下 session.reused 恒为 True, 短路掉去重保护,
            #    导致流式已建 ANSWER 段后 complete 又无条件追加一遍 → 卡片 answer 重复渲染。
            #    追加前检查已有 ANSWER 段是否已包含 final_answer, 包含则跳过。
            already_appended = any(
                seg.type == SegmentType.ANSWER
                and seg.text
                and final_answer in seg.text
                for seg in session.segment_state.segments
            )
            if final_answer and reconcile_answer:
                if latest_answer is not None and final_answer.startswith(latest_answer.text):
                    suffix = final_answer[len(latest_answer.text):]
                    if suffix:
                        latest_answer.text += suffix
                        latest_answer.dirty = True
                else:
                    # Keep useful partial output and separate the authoritative final notice.
                    separator = "\n\n" if session.segment_state.segments and (
                        session.segment_state.segments[-1].type == SegmentType.ANSWER
                    ) else ""
                    session.segment_state.on_answer_delta(separator + final_answer)
            elif final_answer and not already_appended and (session.reused or not any(
                seg.type == SegmentType.ANSWER for seg in session.segment_state.segments
            )):
                session.segment_state.on_answer_delta(final_answer)

        # 模型速度（t/s）：从本回合 agent 的滚动历史算（与 CLI 状态栏同口径）
        _tps = turn_registry.velocity(message_id=session.message_id, chat_id=session.chat_id)
        turn_registry.clear(message_id=session.message_id, chat_id=session.chat_id)
        session.footer = {
            "duration": duration,
            "model": model,
            **({"input_tokens": tokens.get("input_tokens")} if tokens else {}),
            **({"output_tokens": tokens.get("output_tokens")} if tokens else {}),
            **({"context_used": context.get("used_tokens")} if context else {}),
            **({"context_max": context.get("max_tokens")} if context else {}),
            **({"tps": _tps} if _tps else {}),
        }

    def _complete_session(self, session: CardSession) -> None:
        """异步完成当前流式卡片."""
        session.flush.mark_completed()
        self._fire_and_forget(self._complete_session_after_creation(session), session._loop)

    async def _complete_session_after_creation(self, session: CardSession) -> bool:
        if not await self._wait_for_card_creation(session):
            self._cleanup_session(session)
            return False
        return await self._complete_session_wait(session)

    async def _complete_session_wait(self, session: CardSession) -> bool:
        """完成当前流式卡片，并等待最终 API 结果."""
        session.flush.mark_completed()
        return await self._do_complete_card(session)

    def _start_zombie_guard(self) -> None:
        """启动僵尸卡守护（幂等）：回合结束但 complete 信号丢失时，
        强制终止长期无活动的 streaming 卡片，避免永远转省略号。"""
        if self._zombie_started:
            return
        self._zombie_started = True
        loop = self._get_loop()
        if loop is None:
            return
        self._fire_and_forget(self._zombie_guard_loop(), loop)
        _logger.info("[cheerwhy-zombie] zombie guard started")

    async def _zombie_guard_loop(self) -> None:
        """每 30s 扫描非终态 session：超过 _ZOMBIE_IDLE_SEC 无任何流式活动
        （tool/reasoning/thinking/answer 均无），视为回合已结束但 complete 未送达
        （Hermes Relay finalization failed 场景），强制终止卡片。"""
        while True:
            try:
                await asyncio.sleep(_ZOMBIE_SCAN_SEC)
                now = time.time()
                for mid, session in list(self._sessions.items()):
                    if session.state.is_terminal:
                        continue
                    if session.state == SessionState.IDLE:
                        continue
                    last = getattr(session, "last_activity_at", None) or session.created_at
                    idle = now - last
                    if idle <= _ZOMBIE_IDLE_SEC:
                        continue
                    _logger.warning(
                        "[cheerwhy-zombie] force-abort stuck card msg=%s state=%s idle=%.0fs card_id=%s",
                        mid[:12], session.state.value, idle,
                        getattr(session, "card_id", None),
                    )
                    session.state = SessionState.ABORTED
                    session.flush.mark_completed()
                    self._complete_session(session)
            except asyncio.CancelledError:
                return
            except Exception:
                _logger.warning("[cheerwhy-zombie] scan error", exc_info=True)

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
            # ⚠️ 2026-08-02 修复：只清「终态」stale session。之前不管状态直接清，
            # 长回合（>card_ttl_sec，如 23 分钟的多轮工具回合）的活跃 session 被误杀 →
            # 后续 delta/complete 全部 NO session → 流式卡片永远停在运行中（孤儿卡闪省略号）。
            # 活跃 session 应由 complete/abort 流程自行收尾，TTL 只负责回收已完成残留。
            if now - s.created_at > self._session_ttl and s.state.is_terminal:
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


_controllers: dict[str, StreamCardController] = {}
_controller_lock = threading.Lock()


def get_controller() -> StreamCardController:
    profile_home = hermes_home().resolve()
    key = str(profile_home)
    with _controller_lock:
        controller = _controllers.get(key)
        if controller is None:
            controller = StreamCardController(profile_home)
            _controllers[key] = controller
        return controller
