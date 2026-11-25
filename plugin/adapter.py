"""StreamingFeishuAdapter — kind: platform 插件的飞书流式适配器.

子类化官方 ``FeishuAdapter``（bundled feishu-platform），把 draft-streaming 契约
映射到 CardKit v2 卡片：

- ``supports_draft_streaming → True`` + ``send_draft``：GatewayStreamConsumer 的
  draft 帧（全量快照）→ ChatCardEngine 的 chat 键卡片；
- ``draft_stream_is_message = True``：流即消息 —— 工具边界（segment break finalize）
  不会封卡发真消息，一回合一张卡；
- ``format_tool_event``：记录结构化 ToolCallChunk 后返回 None「吃掉」文本行，
  工具面板由引擎按结构化状态渲染（官方契约明确允许）；
- ``send``：回合终态文本命中活跃卡会话 → 渲染完成卡；interim（_interim_send）
  → 卡片心跳行；bg 交付（thread_id metadata）→ NOTICE 并进最近卡；cron 结果
  （job_id metadata、无锚）→ ⏰ cron 卡片；其余走原生。

官方基类在 gateway 进程内以 ``plugins.platforms.feishu.adapter`` 可导入；为让本包
在无 hermes 源树的环境（CI/单测）也可导入，基类经工厂延迟绑定。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any, cast

from . import _compat
from ._vendor.cardkit.builder import (
    build_model_admin_card,
    build_model_admin_done_card,
    build_model_picker_card,
    build_model_switch_ack_card,
)
from ._vendor.paths import local_file_name
from .contract import CRON_WRAP_DIVIDER, CRON_WRAP_FOOTER_PREFIX, CRON_WRAP_HEADER, CRON_WRAP_JOBID_LINE
from .engine import ChatCardEngine

_logger = logging.getLogger("hermes_lark_streaming.plugin")
# 诊断日志统一走 gateway.run（插件 logger 的 info 级不进 gateway.log——
# 媒体投递成功/回退必须可观测，2026-11-17 自测实证其 info 被丢弃）
_gw = logging.getLogger("gateway.run")

# busy ack（redirect/queue 确认文本）的前缀特征 — 无官方语义标记时的启发式。
# 文案来自 hermes locales（gateway.progress.redirected_head / queued_head）。
# busy/interrupt 系统通知前缀：↪ redirect、⏳ queued、⚡ interrupting、
# ⚠️/♻️ 网关生命周期（正在关闭/已上线）——这些文本进心跳行或原生文本，
# 不渲染成卡、不并进完成卡（gateway.progress.* / gateway lifecycle locale）
_BUSY_ACK_PREFIXES = ("↪", "⏳", "⚡", "⚠️", "♻️")
_draft_log_state: dict[int, int] = {}

# cron 结果 wrap 信封（cron.wrap_response）——常量与锚点 needle 同源于
# plugin/contract.py（上游改文案 → 每日 hermes-check 锚点报警）；解析范式同
# gateway/platforms/yuanbao.py 的 strip_cron_wrapper。不匹配时原样降级
# （无 header 卡片 / 原生文本），不会坏。

# 失败通知形状（cron 失败 header 判定）：⚠️ Cron '...' failed: 是
# cron/scheduler_failure_copy.py 全部文案的公共前缀（该模块 pinned revision
# 尚未抽出，未锚——改版只降级为蓝卡）；**Status:** script failed 是脚本门形状。
_CRON_FAILURE_RE = re.compile(r"Cron '[^']{0,120}' failed: |\*\*Status:\*\* [^\n]{0,60}(?:failed|error)")

# ── 飞书任务（todo）转发识别（v0.22.1）──
# 任务从 Tasks 应用转发进会话 = msg_type=todo。官方 normalize_feishu_message 不认
# todo → 归一化为空文本 → 「Ignoring empty text message」丢弃，bot 对转发完全失明
# （2026-11-25 话题群实测：转发任务零反应，用户追问「你看不到我转发的卡片吗」）。
# content 形状（2026-11-25 抓包实证）：{"task_id": uuid, "summary": {"title": "",
# "content": [[{"tag": "text", "text": "…"}]]}, "due_time": "epoch-ms 字符串"} ——
# summary 是 post 形嵌套数组，title 常为空，可见文本在 content。上游若原生支持
# todo，此覆写可整体退场。
_TODO_PREFIX = "[飞书任务]"


def _todo_task_text(raw_content: Any) -> tuple[str, dict[str, Any]] | None:
    """todo 消息 content → ``(入站文本, metadata)``；非 todo JSON / 空对象返回 None.

    文本形态：``[飞书任务] <标题>（截止 %Y-%m-%d %H:%M）``——标题优先，缺失时
    拼 content 里的 text run；截止时间按本机时区格式化。
    """
    try:
        payload = json.loads(raw_content) if isinstance(raw_content, str) else (raw_content or {})
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    title = str(summary.get("title") or "").strip()
    lines: list[str] = []
    for row in summary.get("content") or []:
        if not isinstance(row, list):
            continue
        for item in row:
            if isinstance(item, dict) and str(item.get("tag") or "") == "text":
                run = str(item.get("text") or "").strip()
                if run:
                    lines.append(run)
    metadata: dict[str, Any] = {}
    task_id = str(payload.get("task_id") or "").strip()
    if task_id:
        metadata["task_id"] = task_id
    due_text = ""
    due = payload.get("due_time")
    if due:
        try:
            ts = int(due) / 1000.0
            if ts > 0:
                due_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                metadata["due_time"] = int(due)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    body = title or " ".join(lines) or "（未携带标题）"
    text = f"{_TODO_PREFIX} {body}"
    if due_text:
        text += f"（截止 {due_text}）"
    return text, metadata


def _official_adapter_module() -> Any:
    """官方 feishu adapter 模块（经 FeishuAdapter 类反查；无源树环境返回 None）."""
    import sys

    try:
        cls = _import_base_adapter()
    except Exception:
        return None
    return sys.modules.get(cls.__module__)


def _normalized_todo_message(text: str, metadata: dict[str, Any]) -> Any:
    """构造 todo 的归一化结果：官方 dataclass 优先，无源树环境 duck-type 兜底."""
    mod = _official_adapter_module()
    cls = getattr(mod, "FeishuNormalizedMessage", None) if mod is not None else None
    if cls is not None:
        try:
            return cls(raw_type="todo", text_content=text, relation_kind="todo",
                       metadata=metadata)
        except Exception:
            pass
    from types import SimpleNamespace

    return SimpleNamespace(raw_type="todo", text_content=text, preferred_message_type="text",
                           image_keys=[], media_refs=[], mentions=[],
                           relation_kind="todo", metadata=metadata)


def _looks_like_cron_failure(body: str) -> bool:
    """cron 正文是否失败通知（红 header 用；误判只影响配色，不影响投递）."""
    return _CRON_FAILURE_RE.search(body) is not None


def _parse_cron_payload(content: str) -> tuple[str, str, str, bool]:
    """拆 cron wrap 信封 → ``(task_name, 卡片正文, job_id, is_failure)``.

    正文 = 原始产出 + 管理提示尾（含 job_id，保留原生文本的可追溯性）；形状不
    匹配（wrap_response=false / 上游改版）时原 content 整体作为正文返回。
    """
    if not content.startswith(CRON_WRAP_HEADER):
        return "", content, "", _looks_like_cron_failure(content)
    divider_pos = content.find(CRON_WRAP_DIVIDER)
    footer_pos = content.rfind(CRON_WRAP_FOOTER_PREFIX)
    if (divider_pos < 0 or footer_pos < 0 or footer_pos <= divider_pos
            or CRON_WRAP_JOBID_LINE not in content[:divider_pos]):
        return "", content, "", _looks_like_cron_failure(content)
    head = content[len(CRON_WRAP_HEADER):divider_pos].split("\n")
    task_name = head[0].strip()
    job_id = ""
    for line in head[1:]:
        line = line.strip()
        if line.startswith(CRON_WRAP_JOBID_LINE) and line.endswith(")"):
            job_id = line[len(CRON_WRAP_JOBID_LINE):-1]
            break
    payload = content[divider_pos + len(CRON_WRAP_DIVIDER):footer_pos].strip()
    body = payload or content
    hint = content[footer_pos:].lstrip()
    if hint:
        body += "\n\n" + hint
        if job_id:
            body += f" (job_id: {job_id})"
    elif job_id:
        body += f"\n\n(job_id: {job_id})"
    return task_name, body, job_id, _looks_like_cron_failure(payload)


def _import_base_adapter() -> type[Any]:
    """定位官方 FeishuAdapter.

    优先 hermes_plugins.*（plugin loader 的 slug 派生名，运行时真身）；源码路径
    ``plugins.platforms.feishu.adapter`` 作回退（子类化两可——影子类陷阱只影响
    对既有实例的 monkey-patch，不影响工厂自建实例）。
    """
    import sys

    for name, module in list(sys.modules.items()):
        if (name.startswith("hermes_plugins.") and name.endswith(".adapter")
                and hasattr(module, "FeishuAdapter")):
            cls: type[Any] = module.FeishuAdapter
            return cls
    from plugins.platforms.feishu.adapter import FeishuAdapter

    return cast("type[Any]", FeishuAdapter)


class StreamingFeishuMixin:
    """draft-streaming → CardKit v2 的全部覆写。经 :func:`build_adapter_class` 与
    官方基类合成；运行期成员（engine 等）由工厂注入。"""

    # CardKit 流式卡按 chat 路由，与 chat_type 无关
    draft_stream_is_message: bool = True

    def _engine(self) -> ChatCardEngine:
        engine: ChatCardEngine = self._engine_instance  # type: ignore[attr-defined]
        return engine

    def _normalize(self, message_type: Any, raw_content: Any, mentions: Any) -> Any:
        """官方归一化入口的 todo 补课：飞书任务转发 → 可读文本入站（v0.22.1）.

        官方 normalize_feishu_message 不认 todo（空文本 → 「Ignoring empty text
        message」丢弃）。其余类型原样走官方通道。
        """
        if str(message_type or "").strip().lower() == "todo":
            parsed = _todo_task_text(raw_content)
            if parsed is not None:
                return _normalized_todo_message(*parsed)
        return super()._normalize(message_type, raw_content, mentions)  # type: ignore[misc]

    # ── draft-streaming 契约 ──

    def supports_draft_streaming(self, chat_type: str | None = None,
                                 metadata: dict[str, Any] | None = None,
                                 chat_id: str | None = None) -> bool:
        # 探针每回合开始必调（transport 选择）且带 chat_id——飞书无 typing API
        # （send_typing 不被调），这里是"回合开始"最可靠信号：立即建卡，带工具
        # 回合的工具面板从第一步就可见（对齐注入模式 on_message_started 体感）。
        if chat_id:
            # 话题回合：metadata 带 thread_id（hermes dm:oc_x:omt_y 会话键）——
            # 卡片会话必须同粒度隔离，否则话题与主聊互相串写
            thread = (metadata or {}).get("thread_id")
            self._engine().on_turn_started(chat_id, thread_id=thread)
            # 当回合锚（format_tool_event 落在本 adapter 上但事件本身不带 chat）
            self._turn_anchor = (chat_id, thread)
        return True

    async def send_draft(self, chat_id: str, draft_id: int, content: str,
                         metadata: dict[str, Any] | None = None) -> Any:
        if draft_id not in _draft_log_state or len(content) - _draft_log_state[draft_id] > 1500:
            _draft_log_state[draft_id] = len(content)
            logging.getLogger("gateway.run").info(
                "[feishu-streaming] draft chat=%s len=%d", chat_id[:12], len(content))
        reply_to = (metadata or {}).get("reply_to_message_id")
        thread = (metadata or {}).get("thread_id")
        self._turn_anchor = (chat_id, thread)
        self._engine().on_draft(chat_id, content, reply_to=reply_to,
                                thread_id=thread)
        return _compat.send_result(success=True, message_id=None)

    # ── 结构化流事件 ──

    def format_tool_event(self, event: Any, *, mode: str = "all",
                          preview_max_len: int = 40) -> str | None:
        """记录结构化工具事件后返回 None 吃掉文本行（不污染 draft 快照）."""
        if not _compat.is_tool_chunk(event):
            return None
        detail = event.preview or ""
        if isinstance(event.args, dict) and event.args:
            first_val = next(iter(event.args.values()))
            if not detail and first_val is not None:
                detail = str(first_val)
        self._engine().on_tool_start(event.tool_name, detail[:200],
                                     anchor=getattr(self, "_turn_anchor", None))
        return None

    # ── 出站拦截 ──

    async def send(self, chat_id: str, content: str, reply_to: str | None = None,
                   metadata: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        logging.getLogger("gateway.run").info(
            "[feishu-streaming] send chat=%s len=%d interim=%s", chat_id, len(content),
            bool((metadata or {}).get("_interim_send")))
        engine = self._engine()
        interim = bool((metadata or {}).get("_interim_send"))
        thread_id = (metadata or {}).get("thread_id")
        session = engine.active_session(chat_id, thread_id)
        is_ack = bool(content) and content.lstrip().startswith(_BUSY_ACK_PREFIXES)

        if interim and session is not None:
            # 心跳/进度 → 卡片末尾状态行（不产生真消息）。卡尚未建好（card_msg_id
            # 未落）时返回无 id 成功——"lark-card:None" 会被上游拿去原生 edit
            # （2026-11-23 j-s 实证：SSLError + 垃圾 url /im/v1/messages/lark-card%3ANone）
            engine.on_heartbeat(chat_id, content, thread_id=thread_id)
            return _compat.send_result(
                success=True,
                message_id=(f"lark-card:{session.card_msg_id}"
                            if session.card_msg_id else None))

        if (not interim and thread_id and content.strip() and not is_ack
                and not (metadata or {}).get("notify")
                and (session is None or not session.has_draft)):
            # 话题内的后台交付 / watcher 通知（特征：无 notify 标记 + thread_id
            # metadata）→ 追加进**同一话题**最近一张卡。只认同话题：话题群里每个
            # 话题是独立会话，并进「chat 里最新那张卡」会把 A 话题的内容写进 B
            # 话题的卡。本话题的活跃会话已有 draft = 那是本回合自己的终态文本
            # （has_draft），交下方完成分支——并入它自己的卡会让卡永不完成。
            target = engine.latest_card_session(chat_id, thread_id)
            if target is not None:
                card_msg_id = await engine.append_notice(
                    chat_id, content, thread_id=target.thread_id, session=target)
                if card_msg_id is not None:
                    return _compat.send_result(success=True, message_id=card_msg_id)
            # 同话题没有可合并的卡 → 下方在话题内开新卡（官方无锚话题直发恒 99992402）

        if (session is not None and session.state in ("creating", "streaming") and not interim
                and is_ack and len(content) <= 200):
            # busy ack → 心跳行；回合完成时随完成卡消失。
            # ↪ redirect（用户纠正、interrupt 注入新指令）→ 即刻收旧开新（hermes
            # redirect 是同回合改锚续跑，draft 锚不变，等锚变化/首条 draft 都
            # 接不到）。含 creating：ack 可早于建卡完成，此时也不能漏标记。
            if content.lstrip().startswith("↪"):
                # ack 的 reply_to = 用户纠正消息 id（hermes 锚到新消息）→ 新卡 reply 引用它
                engine.mark_redirect(chat_id, anchor=reply_to, thread_id=thread_id)
                session = engine.active_session(chat_id, thread_id)  # mark_redirect 可能已顶替会话
            engine.on_heartbeat(chat_id, content, thread_id=thread_id)
            # 新卡尚在建（card_msg_id 未落）时返回无 id 的成功——ack 无后续 edit，
            # 合成 "lark-card:None" 会让后续 edit 打到原生链路上
            return _compat.send_result(
                success=True,
                message_id=(f"lark-card:{session.card_msg_id}"
                            if session is not None and session.card_msg_id else None))

        if (not interim and reply_to is None and content.strip()
                and not is_ack
                and not (metadata or {}).get("thread_id")
                and not (metadata or {}).get("notify")
                and not (metadata or {}).get("job_id")
                and (session is None or not session.has_draft)):
            # 无锚通知类 send（watch/process 注入直投：无 reply 锚、无 thread、
            # 无 notify、无 job_id）——绝不当终态完成渲染。此前它会命中下方
            # streaming 完成分支，把当时唯一活跃的卡 complete 成通知文本：
            # redirect 刚拆开的新卡（零 draft）21 秒即被劫持完成，真回答被迫
            # 再开一张卡、前半程纯 loading（2026-11-04 梦珊机 2/2 实测）。
            # 与 bg 交付同语义：NOTICE 合并进该 chat 最近一张卡（活跃卡由
            # flush 渲 NOTICE 段，流式不受影响）；无可用卡片落回原生文本。
            # has_draft 防误伤：自己 draft 过的无锚 final（v0.19.3 无探针
            # 通知回合秒回场景）仍走下方完成分支正常收卡。
            card_msg_id = await engine.append_notice(chat_id, content, thread_id=None)
            if card_msg_id is not None:
                return _compat.send_result(success=True, message_id=card_msg_id)
            # 无可用卡片 → 落到底部原生文本

        if session is not None and session.state in ("creating", "streaming"):
            # 回合终态文本 → 完成卡（官方 transport 在 draft 后仍会真发最终文本，
            # 此处接管渲染；已发送标记由 gateway 流机制去重）
            if session.state == "creating":
                # final send 撞进建卡窗口：无探针的后台通知回合整段秒回，
                # draft→final 间隔短于 cardkit create，此前 creating 不命中
                # 本分支、无锚又不命中下方兜底 → 卡建好后永停 loading（2026-10-19
                # 实测）。on_draft 幂等：补记锚、启动推迟的建卡任务；complete
                # 内部 await 建卡完成后收尾。
                engine.on_draft(chat_id, content, reply_to=reply_to, thread_id=thread_id)
                if session.card_create_task is None and thread_id:
                    # 话题会话等不到 reply 锚（hermes 合成入站：后台进程通知注入、
                    # 被打断回合的收尾文本——没有 message id）→ 补话题内锚再建卡。
                    # 此前卡停在 creating → complete 置 failed → 官方 thread_id
                    # 直发 99992402，回答整条丢失（2026-11-19~24 两机 3 条投递
                    # 义务失败）
                    session.thread_anchor = await engine.resolve_thread_anchor(
                        chat_id, thread_id)
                    engine.on_draft(chat_id, content, reply_to=reply_to, thread_id=thread_id)
            if session.reply_to is None and reply_to:
                session.reply_to = reply_to
            msg_id = await engine.complete(chat_id, content, thread_id=thread_id)
            if msg_id is not None:
                return _compat.send_result(success=True, message_id=msg_id)
            # 建卡失败 → 落回原生文本（底部补话题锚）
        elif (not interim and content.strip() and not is_ack
                and (reply_to is not None
                     or (thread_id and not (metadata or {}).get("notify")))):
            # 无流式会话（短回答被 transport 的 _MIN_NEW_MSG_CHARS 吞帧 / 单 tick 直达
            # finalize，draft 让位真发）→ 现场开卡立即完成，保证回合产出卡片形态一致。
            # reply_to 有锚 = 对话回合；话题内无锚（后台交付找不到可合并的卡）→
            # 话题内锚开卡。notify 通知与无锚非话题 send（watcher 等）保持原生文本。
            # busy-ack（↪/⏳）除外——回合早期的 ack 到达时卡还没建，误开卡会把
            # ack 文本当回答渲染成完成卡。
            if reply_to is None:
                reply_to = await engine.resolve_thread_anchor(chat_id, thread_id)
            if reply_to is not None:
                engine.on_draft(chat_id, content, reply_to=reply_to, thread_id=thread_id)
                msg_id = await engine.complete(chat_id, content, thread_id=thread_id)
                if msg_id is not None:
                    return _compat.send_result(success=True, message_id=msg_id)
        elif (not interim and (metadata or {}).get("job_id")
                and content.strip() and reply_to is None):
            # cron 结果投递（scheduler live lane 特征：metadata={job_id, notify}，
            # 无 thread_id 无 reply 锚——router 对 feishu 不传 reply_to）→ ⏰ cron
            # 卡片直发 chat，替代「Cronjob Response:」原生富文本；失败通知
            # （⚠️ Cron...failed / **Status:**...failed）红 header。解析/发送失败
            # 落回原生文本。带 thread_id 的 bg 交付在上面合并分支已被接走，不相交。
            task_name, body, cron_job_id, is_failure = _parse_cron_payload(content)
            msg_id = await engine.send_cron_card(
                chat_id, body, task_name=task_name, job_id=cron_job_id,
                run_time=datetime.now().astimezone().isoformat(timespec="minutes"),
                template="red" if is_failure else "blue")
            if msg_id is not None:
                return _compat.send_result(success=True, message_id=msg_id)

        if (reply_to is None and thread_id
                and not (metadata or {}).get("reply_to_message_id")):
            # 原生兜底的话题锚：官方 adapter 无 reply 锚时用 receive_id_type=
            # "thread_id" 直发，实测恒 99992402 field validation failed（话题里
            # 送达的原生消息无一例外都是带 reply 的）。上游给了 reply_to_message_id
            # 时沿用它的。
            reply_to = await engine.resolve_thread_anchor(chat_id, thread_id)
        return await super().send(chat_id, content, reply_to=reply_to,  # type: ignore[misc]
                                  metadata=metadata, **kwargs)

    async def edit_message(self, chat_id: str, message_id: str, content: str,
                           *, finalize: bool = False, **kwargs: Any) -> Any:
        # 心跳首条被截获时返回合成 id，后续 edit 打回卡片心跳行
        if message_id.startswith("lark-card:"):
            card_msg_id = message_id.split(":", 1)[1]
            for session in self._engine()._sessions.values():
                if session.card_msg_id == card_msg_id:
                    self._engine().on_heartbeat(session.chat_id, content,
                                                thread_id=session.thread_id)
                    return _compat.send_result(success=True, message_id=message_id)
        if (finalize and message_id and not message_id.startswith("lark-card:")
                and str(content or "").strip()):
            # stale-finalize / transformed-final 的编辑目标是我们返回的卡片消息
            # id（真 om_ id）：官方 edit 走原生文本编辑 API 对卡片必 230054，
            # 上游随即降级 normal final send——同一回答以原生文本双发
            # （2026-10-17 12:24 实测）。重定向为完成卡重渲，成功即上游置
            # already_sent 不再补发；失败落回官方 edit（行为同旧版）。
            engine = self._engine()
            card_session = engine.session_by_card_msg_id(message_id)
            if card_session is not None and await engine.reconcile_card(card_session, content):
                return _compat.send_result(success=True, message_id=message_id)
        return await super().edit_message(chat_id, message_id, content,  # type: ignore[misc]
                                          finalize=finalize, **kwargs)

    async def send_typing(self, chat_id: str, metadata: dict[str, Any] | None = None) -> Any:
        """回合开始的 typing 指示 → 立即建卡（带工具回合的 draft 要等工具跑完，
        此前用户什么都看不到）。官方实现是 no-op，此处附加建卡后转调。"""
        self._engine().on_turn_started(chat_id, thread_id=(metadata or {}).get("thread_id"))
        return await super().send_typing(chat_id, metadata)  # type: ignore[misc]

    # ── processing 生命周期（Typing 徽章）观测 — 官方实现加/删用户消息上的
    # reaction；埋点进 gateway.log 是因为徽章失败只落 DEBUG，出问题静默无痕 ──

    async def on_processing_start(self, event: Any) -> None:
        super_method = getattr(super(), "on_processing_start", None)
        if callable(super_method):
            await super_method(event)
        msg_id = getattr(event, "message_id", "") or "?"
        chat_id = getattr(getattr(event, "source", None), "chat_id", "") or ""
        # 入站登记：followup 拆卡门槛的数据源（drain 消息在 drain 开始时也触发）；
        # 话题内的入站同时是话题锚（无锚回合建卡/回退的兜底目标）
        if msg_id != "?" and chat_id:
            self._engine().note_inbound(
                chat_id, str(msg_id),
                thread_id=getattr(getattr(event, "source", None), "thread_id", None))
        logging.getLogger("gateway.run").info(
            "[feishu-streaming] processing_start msg=%s typing_badge=%s",
            msg_id[:16],
            msg_id in getattr(self, "_pending_processing_reactions", {}))

    async def on_processing_complete(self, event: Any, outcome: Any = None) -> None:
        msg_id = getattr(event, "message_id", "") or "?"
        logging.getLogger("gateway.run").info(
            "[feishu-streaming] processing_complete msg=%s outcome=%s",
            msg_id[:16], getattr(outcome, "name", outcome))
        super_method = getattr(super(), "on_processing_complete", None)
        if callable(super_method):
            await super_method(event, outcome)
        # /stop、/new、/reset 等 adapter 路由的取消 → 立即红卡收尾。上游语义
        # （base.py `_expected_cancelled_tasks`）：仅 adapter 自己路由的取消才
        # 是 CANCELLED——redirect 不产生、drain 正常路径是 SUCCESS、失败路径走
        # FAILURE 且有自己的错误文本 send 兜底（不在此收卡，防双发）
        if getattr(outcome, "name", outcome) == "CANCELLED":
            source = getattr(event, "source", None)
            chat_id = getattr(source, "chat_id", "") or ""
            if chat_id:
                session = await self._engine().abandon(
                    chat_id, thread_id=getattr(source, "thread_id", None))
                if session is not None and session.card_id is None:
                    # 无卡可渲（话题延迟建卡 + 零 draft 就被 /stop）→ 原生文本
                    # 反馈，reply 到回合入站锚保证落在线程内（否则用户侧静默）
                    anchor = (getattr(session, "turn_inbound", None)
                              or session.reply_to
                              or getattr(session, "thread_anchor", None))
                    thread_id = getattr(source, "thread_id", None)
                    if anchor is None and thread_id:
                        # 合成入站/被打断的回合没有入站锚 → 话题内锚；否则原生直发
                        # 会落到群顶层（话题群里 = 新建一个话题）
                        anchor = await self._engine().resolve_thread_anchor(chat_id, thread_id)
                    try:
                        import json as _json

                        await self._feishu_send_with_retry(  # type: ignore[attr-defined]
                            chat_id=chat_id, msg_type="text",
                            payload=_json.dumps({"text": "⏹ 本回合已中断"},
                                                ensure_ascii=False),
                            reply_to=anchor, metadata=None)
                    except Exception as exc:
                        logging.getLogger("gateway.run").warning(
                            "[feishu-streaming] cancel text fallback failed: %s", exc)
        elif getattr(outcome, "name", outcome) == "SUCCESS":
            # 回合正常结束的收尾兜底：卡从未建（无 draft/无 final send，如模型只回
            # MEDIA 行）→ 清理幽灵会话，否则永停 creating，污染 usage/reasoning 的
            # 「回合进行中」判据；卡已建却全程无正文 → 收尾完成卡（否则永停「处理中」）
            source = getattr(event, "source", None)
            chat_id = getattr(source, "chat_id", "") or ""
            if chat_id:
                engine = self._engine()
                thread_id = getattr(source, "thread_id", None)
                session = engine.active_session(chat_id, thread_id)
                if (session is not None and session.turn_inbound and msg_id != "?"
                        and msg_id != session.turn_inbound):
                    # 不是本回合自己的入站消息：↪ redirect ack / 排队消息的回执也各发
                    # 一次 SUCCESS，那时本回合仍在进行，什么都不收
                    return
                await engine.on_turn_ended(chat_id, thread_id=thread_id)

    # ── clarify 内联单选（类定义期覆写；SDK connect() 注册的绑定方法即本版本）──

    async def send_clarify(
        self, chat_id: str, question: str, choices: Any, clarify_id: str,
        session_key: str, metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Clarify prompt：多选 → 编号按钮卡（schema 1.0）；开放题 → 无按钮卡 + text-capture.

        官方契约（base.send_clarify docstring）：choice 按钮必须经
        resolve_gateway_clarify 回调 resolve；「其他」调 mark_awaiting_text。
        全程走 _feishu_send_with_retry（与官方 approval 卡同链路），绝不经过
        self.send——send 拦截会把 streaming 卡误判为回合终态。
        """
        from . import _clarify

        if not getattr(self, "_client", None):
            return _compat.send_result(success=False, error="Not connected")
        try:
            clean = _clarify.normalize_choices(choices)
            if clean:
                card = _clarify.build_clarify_card(
                    question=str(question or ""), choices=clean, clarify_id=clarify_id)
            else:
                from tools.clarify_gateway import mark_awaiting_text  # type: ignore[import-not-found]

                mark_awaiting_text(clarify_id)
                card = _clarify.build_open_clarify_card(question=str(question or ""))
            response = await self._feishu_send_with_retry(  # type: ignore[attr-defined]
                chat_id=chat_id, msg_type="interactive",
                payload=_clarify.card_payload(card), reply_to=None, metadata=metadata)
            result = self._finalize_send_result(response, "send_clarify failed")  # type: ignore[attr-defined]
            if getattr(result, "success", False):
                _clarify.CLARIFY_STATE[clarify_id] = {
                    "session_key": session_key or "",
                    "chat_id": chat_id,
                    "message_id": getattr(result, "message_id", "") or "",
                }
                logging.getLogger("gateway.run").info(
                    "[feishu-streaming][clarify] card sent id=%s choices=%d chat=%s",
                    clarify_id[:12], len(clean), chat_id[:12])
            return result
        except Exception as exc:
            logging.getLogger("gateway.run").warning(
                "[feishu-streaming][clarify] card send failed, falling back to text: %s", exc)
            # 回退 text 版：不能调 super().send_clarify——它的默认实现走 self.send，
            # 会被 send() 拦截误判为回合终态（把 streaming 卡完成掉）。直发 text。
            try:
                from tools.clarify_gateway import mark_awaiting_text  # type: ignore[import-not-found]

                mark_awaiting_text(clarify_id)
                import json as _json

                response = await self._feishu_send_with_retry(  # type: ignore[attr-defined]
                    chat_id=chat_id, msg_type="text",
                    payload=_json.dumps({"text": str(question or "")}, ensure_ascii=False),
                    reply_to=None, metadata=metadata)
                return self._finalize_send_result(response, "send_clarify text fallback failed")  # type: ignore[attr-defined]
            except Exception as exc2:
                return _compat.send_result(success=False, error=str(exc2))

    def _on_card_action_trigger(self, data: Any) -> Any:
        """SDK 卡片回调 wrapper：clarify / 模型切换按钮 → 插件处理；其余转发官方.

        注入模式必须事后替换 SDK processor.f（绑定方法已快照）；插件模式下本方法
        在类定义期覆写，SDK connect() 拿到的绑定方法就是本版本——零 monkey-patch。
        """
        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        action_value = getattr(action, "value", {}) or {}
        # v2 behaviors 按钮的 value 在 action.behaviors[*]（action.value 为空）
        if not action_value:
            for behavior in getattr(action, "behaviors", None) or []:
                bv = getattr(behavior, "value", None)
                if isinstance(bv, dict) and (bv.get("hermes_model_action")
                                             or bv.get("hermes_clarify_action")):
                    action_value = bv
                    break
        # footer 模型下拉（select_static）：选中项在 action.option/value，形态依
        # 赖端上实现——宽容提取，提取不到记日志（首次点击校准用）
        if str(getattr(action, "tag", "") or "").startswith("select"):
            picked = self._extract_selected_model(action)  # type: ignore[attr-defined]
            logging.getLogger("gateway.run").info(
                "[feishu-streaming] model select picked=%s raw_tag=%s", picked,
                str(getattr(action, "tag", "")))
            if picked:
                loop = self._loop  # type: ignore[attr-defined]
                if not self._loop_accepts_callbacks(loop):  # type: ignore[attr-defined]
                    return self._card_response()  # type: ignore[attr-defined]
                # 历史下拉卡（0.17.2）：选中即 switch
                return self._handle_model_switch_action(
                    data=data, action_value={"hermes_model_action": "switch",
                                             "target": picked})
            return self._card_response()  # type: ignore[attr-defined]
        if isinstance(action_value, dict) and action_value.get("hermes_clarify_action"):
            loop = self._loop  # type: ignore[attr-defined]
            if not self._loop_accepts_callbacks(loop):  # type: ignore[attr-defined]
                return self._card_response()  # type: ignore[attr-defined]
            from . import _clarify

            return _clarify.handle_clarify_card_action(
                self, event=event, action_value=action_value)
        if isinstance(action_value, dict) and action_value.get("hermes_model_action"):
            loop = self._loop  # type: ignore[attr-defined]
            if not self._loop_accepts_callbacks(loop):  # type: ignore[attr-defined]
                return self._card_response()  # type: ignore[attr-defined]
            return self._handle_model_switch_action(
                data=data, action_value=action_value)
        return super()._on_card_action_trigger(data)  # type: ignore[misc]

    @staticmethod
    def _extract_selected_model(action: Any) -> str:
        """从 select_static 回调动作里提取选中的模型名（端上形态不一，宽容取值）."""
        option = getattr(action, "option", None)
        candidates = (getattr(option, "value", None), option,
                      getattr(action, "value", None),
                      getattr(action, "input_value", None))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, dict):
                v = candidate.get("value") or candidate.get("model")
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

    def _handle_model_switch_action(self, *, data: Any, action_value: dict[str, Any]) -> Any:
        """footer 🧠⇄（pick）/ 选择卡（switch）/ 管理卡（admin/toggle/done）统一入口.

        pick：鉴权后由引擎 client 补发模型选择卡（原生 interactive 消息）；
        switch：合成 `/model <target>` 命令事件（官方 synthetic 通道，sender=
        点击者），hermes 原生切换（session 级 override、重启持久），并同步替换
        选择卡为确认卡。admin：补发常用模型管理卡；toggle：改常用清单并整卡
        原地刷新（_card_response 替换）；admin_done：收尾确认卡。完成卡本体原地不变。
        """
        event = getattr(data, "event", None)
        action_kind = str(action_value.get("hermes_model_action") or "")
        target = str(action_value.get("target") or "").strip()
        open_id = str(getattr(getattr(event, "operator", None), "open_id", "") or "")
        chat_id = str(getattr(getattr(event, "context", None), "open_chat_id", "") or "")
        token = str(getattr(event, "token", "") or "")
        if not open_id or not chat_id or (action_kind in ("switch", "toggle") and not target):
            return self._card_response()  # type: ignore[attr-defined]
        # 官方同款 token 去重：防双击重复派发
        if token and self._is_card_action_duplicate(token):  # type: ignore[attr-defined]
            return self._card_response()  # type: ignore[attr-defined]
        if not self._is_interactive_operator_authorized(open_id):  # type: ignore[attr-defined]
            logging.getLogger("gateway.run").warning(
                "[feishu-streaming] model switch unauthorized: %s", open_id[:16])
            return self._card_response()  # type: ignore[attr-defined]

        if action_kind == "pick":
            submitted = self._submit_on_loop(  # type: ignore[attr-defined]
                self._loop,  # type: ignore[attr-defined]
                self._send_model_picker(chat_id=chat_id))
            logging.getLogger("gateway.run").info(
                "[feishu-streaming] model picker requested: chat=%s submitted=%s",
                chat_id[:12], submitted)
            return self._card_response()  # type: ignore[attr-defined]

        if action_kind == "admin":
            submitted = self._submit_on_loop(  # type: ignore[attr-defined]
                self._loop,  # type: ignore[attr-defined]
                self._send_model_admin_card(chat_id=chat_id))
            logging.getLogger("gateway.run").info(
                "[feishu-streaming] model admin requested: chat=%s submitted=%s",
                chat_id[:12], submitted)
            return self._card_response()  # type: ignore[attr-defined]

        if action_kind == "toggle":
            engine = self._engine()  # type: ignore[attr-defined]
            favorites, notice = engine.toggle_model_favorite(target)
            card = self._rebuild_admin_card(chat_id, favorites, notice)
            logging.getLogger("gateway.run").info(
                "[feishu-streaming] model favorite toggled: target=%s count=%d chat=%s",
                target[:40], len(favorites), chat_id[:12])
            return self._card_response(card_data=card)  # type: ignore[attr-defined]

        if action_kind == "admin_done":
            engine = self._engine()  # type: ignore[attr-defined]
            return self._card_response(  # type: ignore[attr-defined]
                card_data=build_model_admin_done_card(engine._load_model_favorites()))

        if action_kind == "switch":
            submitted = self._submit_on_loop(  # type: ignore[attr-defined]
                self._loop,  # type: ignore[attr-defined]
                self._dispatch_model_switch(chat_id=chat_id, open_id=open_id,
                                            target=target, raw=data, message_id=token))
            logging.getLogger("gateway.run").info(
                "[feishu-streaming] model switch click: target=%s chat=%s submitted=%s",
                target, chat_id[:12], submitted)
            # 同步替换选择卡为确认卡（clarify resolved 同款模式）
            return self._card_response(  # type: ignore[attr-defined]
                card_data=build_model_switch_ack_card(target))
        return self._card_response()  # type: ignore[attr-defined]

    async def _send_model_picker(self, chat_id: str) -> None:
        data = self._engine().model_picker_data(chat_id)  # type: ignore[attr-defined]
        if not data:
            logging.getLogger("gateway.run").warning(
                "[feishu-streaming] model picker unavailable (cycle <2)")
            return
        card = build_model_picker_card(data["current"], data["models"])
        await self._engine()._client.send_card_to_chat(  # type: ignore[attr-defined]
            chat_id, card)

    async def _send_model_admin_card(self, chat_id: str) -> None:
        """补发常用模型管理卡（⚙ 入口）；候选池获取失败回退选择卡."""
        engine = self._engine()  # type: ignore[attr-defined]
        data = engine.model_picker_data(chat_id) or {"current": "", "models": []}
        providers = await engine.model_candidates()
        if not providers:
            # 候选池拿不到（hermes provider 目录异常）——选择卡可用性不受影响
            logging.getLogger("gateway.run").warning(
                "[feishu-streaming] model admin candidates empty, fallback to picker")
            await engine._client.send_card_to_chat(
                chat_id, build_model_picker_card(data["current"], data["models"]))
            return
        await engine._client.send_card_to_chat(
            chat_id, build_model_admin_card(
                data["current"], engine._load_model_favorites(), providers))

    def _rebuild_admin_card(self, chat_id: str, favorites: list[str],
                            notice: str) -> dict[str, Any]:
        """toggle 后原地刷新管理卡：复用 60s 候选池缓存，无网请求."""
        engine = self._engine()  # type: ignore[attr-defined]
        current = engine._chat_models.get(chat_id) or engine._last_model
        providers = engine._candidates_cache[1] if engine._candidates_cache else []
        return build_model_admin_card(current, favorites, providers, notice=notice)

    async def _dispatch_model_switch(self, *, chat_id: str, open_id: str,
                                     target: str, raw: Any, message_id: str) -> None:
        from types import SimpleNamespace

        try:
            from gateway.platforms.event import MessageType
        except ImportError:
            MessageType = None  # type: ignore[assignment]
        kwargs: dict[str, Any] = {}
        if MessageType is not None:
            kwargs["message_type"] = MessageType.COMMAND
        # chat_type 必须按真实会话解析：/model 的 override 键从 source.chat_type
        # 派生（dm:oc_xxx / group:oc_xxx）——官方按钮处理器硬编码 "group"，DM 里
        # 点按钮会把 override 写进 group 键，真实 dm 会话读不到（2026-10-09 实测）。
        # 注意用 raw_type（飞书原始形态 p2p/group）：_resolve_source_chat_type 的
        # 回退分支字面比较 "p2p"，映射后的 "dm" 会被再次错位成 group。
        try:
            chat_info = await self.get_chat_info(chat_id)  # type: ignore[attr-defined]
        except Exception:
            chat_info = {}
        chat_type = str((chat_info or {}).get("raw_type") or "") or "p2p"
        await self._dispatch_synthetic_event(  # type: ignore[attr-defined]
            text=f"/model {target}",
            chat_id=chat_id,
            sender_id=SimpleNamespace(open_id=open_id, user_id=None, union_id=None),
            event_chat_type=chat_type, raw_message=raw,
            message_id=message_id or str(uuid.uuid4()),
            **kwargs,
        )

    async def retire_clarify_card(self, clarify_id: str, notice: Any = None) -> None:
        """官方钩子：clarify 未点击而终结（超时/重置/自由文本取代）时清理状态.

        卡面残留可接受（后续点击因 state miss 被忽略）；官方 interactive 消息
        更新链路（edit_message）只支持 text/post，不值得为此另起 update API。
        """
        from . import _clarify

        state = _clarify.CLARIFY_STATE.pop(clarify_id, None)
        if state:
            logging.getLogger("gateway.run").info(
                "[feishu-streaming][clarify] retired id=%s (no click)", clarify_id[:12])

    @staticmethod
    def _media_route(metadata: Any) -> tuple[str | None, str | None]:
        """媒体 metadata 的路由信息：(thread_id, reply_to_message_id)."""
        if not isinstance(metadata, dict):
            return None, None
        return (str(metadata.get("thread_id") or "") or None,
                str(metadata.get("reply_to_message_id") or "") or None)

    def _media_session(self, chat_id: str, metadata: Any) -> Any:
        """媒体交付（文件/图片）的锚会话：同话题最近一张**有卡**的会话优先.

        话题群回合的卡在线程里——run_notifications 的 post-stream 媒体交付带
        thread_id metadata（2026-11-17 梦珊机实测：忽略它会把话题回合的文件
        挂到 chat 级最近卡/顶层直发，落群顶层）。交付发生在 complete 之后，
        且常拖到用户已发下一条消息之后：那时新回合的会话已经顶替了刚完成的
        卡会话——被顶替的终态会话留在引擎的 _recent 里，仍是落点（2026-11-19
        梦珊 j-s：顶替后 3 个文件 card=no 走原生，话题内无锚直发必失败）。
        metadata 的 reply_to_message_id 命中某会话的锚时优先它——文件挂回产出
        它的那一回合的卡，而不是新回合刚建的卡。thread 未命中退 chat 级。
        """
        engine = self._engine()
        thread_id, hint = self._media_route(metadata)
        session = engine.latest_card_session(chat_id, thread_id, anchor_hint=hint)
        if session is not None:
            return session
        # 没有任何卡：退回该话题/该 chat 的会话（拿 reply 锚 / 话题锚做回复目标）
        if thread_id:
            return engine.session_for(chat_id, thread_id)
        return engine.latest_session_for_chat(chat_id)

    def _media_reply_anchor(self, session: Any, fallback: str | None) -> str | None:
        """媒体投递的回复锚：卡消息 → 会话回复锚 → 话题内已知锚 → 回合原始入站消息 → 调用方锚.

        话题回合的卡可能根本没建（延迟建卡 + 模型只回 MEDIA 行），官方原生
        send_image_file 顶层直发必 99992402（图片丢失）。此时退回话题内锚或
        reply 到回合入站消息（turn_inbound）：同样落在话题线程里，且走 vendor
        reply_*_by_id 不吃官方那条坏路径。
        """
        if session is None:
            return fallback
        return (session.card_msg_id or session.reply_to
                or getattr(session, "thread_anchor", None)
                or getattr(session, "turn_inbound", None) or fallback)

    async def _media_fallback_anchor(self, chat_id: str, metadata: Any) -> str | None:
        """会话链解不出回复锚、且目标是话题 → 查话题内锚（已知锚或话题最新消息）."""
        thread_id, _ = self._media_route(metadata)
        if not thread_id:
            return None
        return await self._engine().resolve_thread_anchor(chat_id, thread_id)

    @staticmethod
    def _note_media_delivered(session: Any) -> None:
        count = getattr(session, "media_delivered", None)
        if isinstance(count, int):
            session.media_delivered = count + 1

    @staticmethod
    def _media_duplicate(kind: str, target: str, message_id: str, path: str) -> Any:
        _gw.info("[feishu-streaming] media duplicate skipped: kind=%s target=%s name=%s",
                 kind, target[:22], local_file_name(path)[:80])
        return _compat.send_result(success=True, message_id=message_id)

    async def _reply_media_to_anchor(self, client: Any, anchor: str, *,
                                     image_path: str | None = None,
                                     file_path: str | None = None,
                                     file_name: str | None = None) -> bool:
        """无卡时的媒体投递：上传后 reply 到锚消息（图片/文档二选一）."""
        if image_path is not None:
            image_key = await client.upload_image_file(image_path)
            if not image_key:
                return False
            return bool(await client.reply_image_by_id(anchor, image_key))
        if file_path is not None:
            file_key = await client.upload_document(file_path, file_name=file_name)
            if not file_key:
                return False
            return bool(await client.reply_file_by_id(
                anchor, file_key, local_file_name(file_path, file_name)))
        return False

    async def send_document(self, chat_id: str, file_path: str, *,
                            caption: str | None = None, file_name: str | None = None,
                            reply_to: str | None = None, metadata: Any = None,
                            **kwargs: Any) -> Any:
        """文档交付：有卡片时上传后 reply 到卡片消息下方（飞书卡片无 file 组件）.

        上传/回复一律走引擎 client（vendor FeishuClient）——官方 adapter 无
        upload_document/reply_file_by_id（v0.20.0 前调 self.* 必 AttributeError，
        测试替身曾伪造该方法掩盖断裂）。caption 走官方回退（file 消息无 caption
        载体，官方路径可渲染）。
        """
        engine = self._engine()
        session = self._media_session(chat_id, metadata)
        card_msg_id = getattr(session, "card_msg_id", None) if session else None
        if card_msg_id and not caption:
            target = f"card:{card_msg_id}"
            if engine.media_already_delivered(target, file_path):
                return self._media_duplicate("document", target, card_msg_id, file_path)
            engine.reserve_media_delivery(target, file_path)
            try:
                client = engine.client
                file_key = await client.upload_document(file_path, file_name=file_name)
                if file_key:
                    name = local_file_name(file_path, file_name)
                    if await client.reply_file_by_id(card_msg_id, file_key, name):
                        _gw.info(
                            "[feishu-streaming] media delivered to card: kind=document "
                            "thread=%s card=%s name=%s",
                            (getattr(session, "thread_id", None) or "-")[:16],
                            str(card_msg_id)[:16], name)
                        self._note_media_delivered(session)
                        return _compat.send_result(success=True, message_id=card_msg_id)
            except Exception as e:
                _gw.warning("[feishu-streaming] media card delivery failed (document): %s", e)
            engine.release_media_delivery(target, file_path)
        fallback_anchor = self._media_reply_anchor(session, reply_to)
        if fallback_anchor is None:
            fallback_anchor = await self._media_fallback_anchor(chat_id, metadata)
        if fallback_anchor and not caption:
            # 无卡（话题延迟建卡 + 媒体早于首 draft）→ reply 到锚消息，仍在线程内
            target = f"anchor:{fallback_anchor}"
            if engine.media_already_delivered(target, file_path):
                return self._media_duplicate("document", target, fallback_anchor, file_path)
            engine.reserve_media_delivery(target, file_path)
            try:
                if await self._reply_media_to_anchor(
                        engine.client, fallback_anchor,
                        file_path=file_path, file_name=file_name):
                    _gw.info(
                        "[feishu-streaming] media delivered to anchor: kind=document anchor=%s",
                        str(fallback_anchor)[:16])
                    self._note_media_delivered(session)
                    return _compat.send_result(success=True, message_id=fallback_anchor)
            except Exception as e:
                _gw.warning("[feishu-streaming] media anchor delivery failed (document): %s", e)
            engine.release_media_delivery(target, file_path)
        _gw.info(
            "[feishu-streaming] media fallback to native: kind=document card=%s caption=%s reply_to=%s",
            "yes" if card_msg_id else "no", "yes" if caption else "no",
            str(fallback_anchor)[:16] if fallback_anchor else "-")
        return await super().send_document(chat_id, file_path,  # type: ignore[misc]
                                           caption=caption, file_name=file_name,
                                           reply_to=fallback_anchor,
                                           metadata=metadata, **kwargs)

    async def send_image_file(self, chat_id: str, image_path: str, *,
                              caption: str | None = None, reply_to: str | None = None,
                              metadata: Any = None, **kwargs: Any) -> Any:
        """本地图片交付：上传后以 image 消息 reply 到卡片（话题内锚定）.

        base.send_multiple_images 对 file:// 已 url2pathname 解码后路由到此
        （alt 作 caption）；官方 send_image_file 顶层直发无线程锚且曾在
        Windows 实测 99992402——覆写后图片随回合落进话题线程。
        """
        engine = self._engine()
        session = self._media_session(chat_id, metadata)
        card_msg_id = getattr(session, "card_msg_id", None) if session else None
        if card_msg_id and not caption:
            target = f"card:{card_msg_id}"
            if engine.media_already_delivered(target, image_path):
                return self._media_duplicate("image", target, card_msg_id, image_path)
            engine.reserve_media_delivery(target, image_path)
            try:
                client = engine.client
                image_key = await client.upload_image_file(image_path)
                if image_key and await client.reply_image_by_id(card_msg_id, image_key):
                    _gw.info(
                        "[feishu-streaming] media delivered to card: kind=image "
                        "thread=%s card=%s",
                        (getattr(session, "thread_id", None) or "-")[:16],
                        str(card_msg_id)[:16])
                    self._note_media_delivered(session)
                    return _compat.send_result(success=True, message_id=card_msg_id)
            except Exception as e:
                _gw.warning("[feishu-streaming] media card delivery failed (image): %s", e)
            engine.release_media_delivery(target, image_path)
        fallback_anchor = self._media_reply_anchor(session, reply_to)
        if fallback_anchor is None:
            fallback_anchor = await self._media_fallback_anchor(chat_id, metadata)
        if fallback_anchor and not caption:
            target = f"anchor:{fallback_anchor}"
            if engine.media_already_delivered(target, image_path):
                return self._media_duplicate("image", target, fallback_anchor, image_path)
            engine.reserve_media_delivery(target, image_path)
            try:
                if await self._reply_media_to_anchor(
                        engine.client, fallback_anchor, image_path=image_path):
                    _gw.info(
                        "[feishu-streaming] media delivered to anchor: kind=image anchor=%s",
                        str(fallback_anchor)[:16])
                    self._note_media_delivered(session)
                    return _compat.send_result(success=True, message_id=fallback_anchor)
            except Exception as e:
                _gw.warning("[feishu-streaming] media anchor delivery failed (image): %s", e)
            engine.release_media_delivery(target, image_path)
        _gw.info(
            "[feishu-streaming] media fallback to native: kind=image card=%s caption=%s reply_to=%s",
            "yes" if card_msg_id else "no", "yes" if caption else "no",
            str(fallback_anchor)[:16] if fallback_anchor else "-")
        return await super().send_image_file(chat_id, image_path,  # type: ignore[misc]
                                             caption=caption,
                                             reply_to=fallback_anchor,
                                             metadata=metadata, **kwargs)

    async def _media_anchor_for_native(self, chat_id: str, metadata: Any,
                                       reply_to: str | None, kind: str) -> str | None:
        """语音/视频/动图的线程锚（官方实现接受 reply_to，注入即可落线程）."""
        session = self._media_session(chat_id, metadata)
        anchor = self._media_reply_anchor(session, reply_to)
        if anchor is None:
            anchor = await self._media_fallback_anchor(chat_id, metadata)
        _gw.info("[feishu-streaming] media native send: kind=%s reply_to=%s",
                 kind, str(anchor)[:16] if anchor else "-")
        return anchor

    async def send_voice(self, chat_id: str, audio_path: str, *,
                         caption: str | None = None, reply_to: str | None = None,
                         metadata: Any = None, **kwargs: Any) -> Any:
        """语音消息：注入线程锚，话题回合不落群顶层（官方负责转码/上传）."""
        anchor = await self._media_anchor_for_native(chat_id, metadata, reply_to, "voice")
        return await super().send_voice(chat_id, audio_path,  # type: ignore[misc]
                                        caption=caption, reply_to=anchor,
                                        metadata=metadata, **kwargs)

    async def send_video(self, chat_id: str, video_path: str, *,
                         caption: str | None = None, reply_to: str | None = None,
                         metadata: Any = None, **kwargs: Any) -> Any:
        """视频消息：同上（官方负责上传封面，这里只补线程锚）."""
        anchor = await self._media_anchor_for_native(chat_id, metadata, reply_to, "video")
        return await super().send_video(chat_id, video_path,  # type: ignore[misc]
                                        caption=caption, reply_to=anchor,
                                        metadata=metadata, **kwargs)

    async def send_animation(self, chat_id: str, animation_url: str, *,
                             caption: str | None = None, reply_to: str | None = None,
                             metadata: Any = None, **kwargs: Any) -> Any:
        """GIF/动图：同上（官方降级为文件发送，锚定同样保持线程内）."""
        anchor = await self._media_anchor_for_native(chat_id, metadata, reply_to, "animation")
        return await super().send_animation(chat_id, animation_url,  # type: ignore[misc]
                                            caption=caption, reply_to=anchor,
                                            metadata=metadata, **kwargs)


def build_adapter_class(base_cls: type, engine: ChatCardEngine) -> type[Any]:
    """合成 StreamingFeishuAdapter 并绑定引擎实例（每 profile 一个）."""
    namespace: dict[str, Any] = {"_engine_instance": engine}

    def _engine(self: Any) -> ChatCardEngine:
        found: ChatCardEngine = self._engine_instance
        return found

    namespace["_engine"] = _engine
    return cast("type[Any]", type("StreamingFeishuAdapter", (StreamingFeishuMixin, base_cls), namespace))


def create_adapter_factory(engine: ChatCardEngine, client_proxy: Any = None) -> Any:
    """adapter_factory(PlatformConfig)：延迟导入官方基类 + 合成子类.

    client_proxy：引擎的 _LazyClient。实例化后绑定官方 adapter 的 lark client
    （profile 作用域凭据），引擎所有 cardkit 调用走同一凭据通道。
    """

    def factory(config: Any) -> Any:
        base = _import_base_adapter()
        cls = build_adapter_class(base, engine)
        logging.getLogger("gateway.run").info(
            "[feishu-streaming] adapter factory: base=%s -> StreamingFeishuAdapter",
            base.__module__)
        adapter = cls(config)
        if client_proxy is not None:
            def _source() -> Any:
                from ._vendor.feishu import FeishuClient

                lark_client = getattr(adapter, "_client", None)
                if lark_client is None and hasattr(adapter, "_prepare_client"):
                    adapter._prepare_client()
                    lark_client = getattr(adapter, "_client", None)
                if lark_client is None:
                    return None  # _LazyClient 回退 env
                logging.getLogger("gateway.run").info(
                    "[feishu-streaming] engine client bound to adapter lark client")
                return FeishuClient.from_lark_client(lark_client)

            client_proxy.bind_source(_source)
        return adapter

    return factory


def create_scoped_adapter_factory(engine_builder: Any) -> Any:
    """multiplex 版 factory：每次实例化（每 profile 的 adapter）独立 engine.

    平台注册条目按 plugin scope 分桶——secondary profile（如 family）创建
    adapter 时在同一 registry 条目上调用 factory，此处运行于该 profile 的
    _profile_runtime_scope 内（HERMES_HOME 指向其目录）。共享单 engine 会把
    client_proxy 重绑到最后一个 adapter 的凭据上（跨 app 串扰），且 footer/
    header 配置必须按各 profile 自己的 config.yaml 解析——所以 engine 与
    client 均按调用现场构建。engine_builder() -> (engine, client_proxy)，
    并自行登记进 __init__._ENGINES 供钩子路由。
    """

    def factory(config: Any) -> Any:
        engine, client_proxy = engine_builder()
        return create_adapter_factory(engine, client_proxy=client_proxy)(config)

    return factory
