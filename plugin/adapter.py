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

import logging
import re
import uuid
from datetime import datetime
from typing import Any, cast

from . import _compat
from ._vendor.cardkit.builder import build_model_picker_card, build_model_switch_ack_card
from .contract import CRON_WRAP_DIVIDER, CRON_WRAP_FOOTER_PREFIX, CRON_WRAP_HEADER, CRON_WRAP_JOBID_LINE
from .engine import ChatCardEngine

_logger = logging.getLogger("hermes_lark_streaming.plugin")

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

        if interim and session is not None:
            # 心跳/进度 → 卡片末尾状态行（不产生真消息）
            engine.on_heartbeat(chat_id, content, thread_id=thread_id)
            return _compat.send_result(success=True,
                                       message_id=f"lark-card:{session.card_msg_id}")

        card_session = engine.latest_session_for_chat(chat_id)  # 含终态 + 任意 thread（bg 通知常在主回合完成后到达）
        if (not interim and card_session is not None and card_session.card_id
                and content.strip()
                and not content.lstrip().startswith(_BUSY_ACK_PREFIXES)
                and not (metadata or {}).get("notify")
                and (metadata or {}).get("thread_id")):
            # background 回合交付 / watcher 通知（特征：无 notify 标记 + thread
            # metadata，与普通 final 的 _mark_notify_metadata 相区分）→ 追加进
            # 该 chat 最近一张卡（跨回合合并；thread_id 是来源标记，合并目标是
            # 最新卡），不再散落纯文本。
            card_msg_id = await engine.append_notice(chat_id, content,
                                                     thread_id=card_session.thread_id)
            if card_msg_id is not None:
                return _compat.send_result(success=True, message_id=card_msg_id)
            # 无可用卡片 → 落回原生文本（append_notice 已带 thread）

        if (session is not None and session.state in ("creating", "streaming") and not interim
                and content and len(content) <= 200
                and content.lstrip().startswith(_BUSY_ACK_PREFIXES)):
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

        if session is not None and session.state == "streaming":
            # 回合终态文本 → 完成卡（官方 transport 在 draft 后仍会真发最终文本，
            # 此处接管渲染；已发送标记由 gateway 流机制去重）
            if session.reply_to is None and reply_to:
                session.reply_to = reply_to
            msg_id = await engine.complete(chat_id, content, thread_id=thread_id)
            if msg_id is not None:
                return _compat.send_result(success=True, message_id=msg_id)
            # 建卡失败 → 落回原生文本
        elif (not interim and content.strip() and reply_to is not None
                and not content.lstrip().startswith(_BUSY_ACK_PREFIXES)):
            # 无流式会话（短回答被 transport 的 _MIN_NEW_MSG_CHARS 吞帧 / 单 tick 直达
            # finalize，draft 让位真发）→ 现场开卡立即完成，保证回合产出卡片形态一致。
            # reply_to 有锚 = 对话回合；无锚通知（watcher 等）保持原生文本。
            # busy-ack（↪/⏳）除外——回合早期的 ack 到达时卡还没建，误开卡会把
            # ack 文本当回答渲染成完成卡。
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
        # 入站登记：followup 拆卡门槛的数据源（drain 消息在 drain 开始时也触发）
        if msg_id != "?" and chat_id:
            self._engine().note_inbound(chat_id, str(msg_id))
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
        """footer 🧠⇄（pick）与选择卡按钮（switch）的统一入口.

        pick：鉴权后由引擎 client 补发模型选择卡（原生 interactive 消息）；
        switch：合成 `/model <target>` 命令事件（官方 synthetic 通道，sender=
        点击者），hermes 原生切换（session 级 override、重启持久），并同步替换
        选择卡为确认卡。完成卡本体原地不变。
        """
        event = getattr(data, "event", None)
        action_kind = str(action_value.get("hermes_model_action") or "")
        target = str(action_value.get("target") or "").strip()
        open_id = str(getattr(getattr(event, "operator", None), "open_id", "") or "")
        chat_id = str(getattr(getattr(event, "context", None), "open_chat_id", "") or "")
        token = str(getattr(event, "token", "") or "")
        if not open_id or not chat_id or (action_kind == "switch" and not target):
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

    async def send_document(self, chat_id: str, file_path: str, *,
                            file_name: str | None = None, **kwargs: Any) -> Any:
        """文档交付：有卡片时上传后 reply 到卡片消息下方（飞书卡片无 file 组件）."""
        engine = self._engine()
        # 文档锚：该 chat 任意 thread 的会话都算，取最近创建那张（latest 语义
        # 与 bg 合并一致——插入序第一张在主聊+话题并存时会挂到旧卡）
        latest = engine.latest_session_for_chat(chat_id)
        card_msg_id = latest.card_msg_id if latest else None
        if card_msg_id:
            try:
                file_key = await self._upload_document_for_card(file_path, file_name)
                if file_key and await self._reply_file_to_card(card_msg_id, file_key,
                                                               file_name or file_path):
                    return _compat.send_result(success=True, message_id=card_msg_id)
            except Exception as e:
                _logger.warning("plugin doc delivery to card failed: %s", e)
        return await super().send_document(chat_id, file_path,  # type: ignore[misc]
                                           file_name=file_name, **kwargs)

    async def _upload_document_for_card(self, file_path: str, file_name: str | None) -> str | None:
        result: str | None = await self.upload_document(  # type: ignore[attr-defined]
            file_path, file_name=file_name)
        return result

    async def _reply_file_to_card(self, card_msg_id: str, file_key: str, file_name: str) -> bool:
        result: bool = await self.reply_file_by_id(  # type: ignore[attr-defined]
            card_msg_id, file_key, file_name)
        return result


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
