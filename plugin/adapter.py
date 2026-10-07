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
  → 卡片心跳行；其余走原生。

官方基类在 gateway 进程内以 ``plugins.platforms.feishu.adapter`` 可导入；为让本包
在无 hermes 源树的环境（CI/单测）也可导入，基类经工厂延迟绑定。
"""

from __future__ import annotations

import logging
from typing import Any, cast

from . import _compat
from .engine import ChatCardEngine

_logger = logging.getLogger("hermes_lark_streaming.plugin")

# busy ack（redirect/queue 确认文本）的前缀特征 — 无官方语义标记时的启发式。
# 文案来自 hermes locales（gateway.progress.redirected_head / queued_head）。
_BUSY_ACK_PREFIXES = ("↪", "⏳")


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
        logging.getLogger("gateway.run").info(
            "[feishu-streaming] supports_draft_streaming probe -> True (chat=%s)", chat_id)
        return True

    async def send_draft(self, chat_id: str, draft_id: int, content: str,
                         metadata: dict[str, Any] | None = None) -> Any:
        logging.getLogger("gateway.run").info(
            "[feishu-streaming] send_draft chat=%s draft_id=%s len=%d", chat_id, draft_id, len(content))
        self._engine().on_draft(chat_id, content)
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
        self._engine().on_tool_start(event.tool_name, detail[:200])
        return None

    # ── 出站拦截 ──

    async def send(self, chat_id: str, content: str, reply_to: str | None = None,
                   metadata: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        logging.getLogger("gateway.run").info(
            "[feishu-streaming] send chat=%s len=%d interim=%s", chat_id, len(content),
            bool((metadata or {}).get("_interim_send")))
        engine = self._engine()
        interim = bool((metadata or {}).get("_interim_send"))
        session = engine.active_session(chat_id)

        if interim and session is not None:
            # 心跳/进度 → 卡片末尾状态行（不产生真消息）
            engine.on_heartbeat(chat_id, content)
            return _compat.send_result(success=True,
                                       message_id=f"lark-card:{session.card_msg_id}")

        if (session is not None and session.state == "streaming" and not interim
                and content and len(content) <= 200
                and content.lstrip().startswith(_BUSY_ACK_PREFIXES)):
            # busy ack（↪ Redirected / ⏳ Queued）→ 心跳行；回合完成时随完成卡消失
            engine.on_heartbeat(chat_id, content)
            return _compat.send_result(success=True,
                                       message_id=f"lark-card:{session.card_msg_id}")

        if session is not None and session.state == "streaming":
            # 回合终态文本 → 完成卡（官方 transport 在 draft 后仍会真发最终文本，
            # 此处接管渲染；已发送标记由 gateway 流机制去重）
            msg_id = await engine.complete(chat_id, content)
            if msg_id is not None:
                return _compat.send_result(success=True, message_id=msg_id)
            # 建卡失败 → 落回原生文本
        elif not interim and content.strip() and reply_to is not None:
            # 无流式会话（短回答被 transport 的 _MIN_NEW_MSG_CHARS 吞帧 / 单 tick 直达
            # finalize，draft 让位真发）→ 现场开卡立即完成，保证回合产出卡片形态一致。
            # reply_to 有锚 = 对话回合；无锚通知（watcher 等）保持原生文本。
            engine.on_draft(chat_id, content)
            msg_id = await engine.complete(chat_id, content)
            if msg_id is not None:
                return _compat.send_result(success=True, message_id=msg_id)

        return await super().send(chat_id, content, reply_to=reply_to,  # type: ignore[misc]
                                  metadata=metadata, **kwargs)

    async def edit_message(self, chat_id: str, message_id: str, content: str,
                           *, finalize: bool = False, **kwargs: Any) -> Any:
        # 心跳首条被截获时返回合成 id，后续 edit 打回卡片心跳行
        if message_id.startswith("lark-card:"):
            card_msg_id = message_id.split(":", 1)[1]
            session = self._engine().session_for(chat_id)
            if session is not None and session.card_msg_id == card_msg_id:
                self._engine().on_heartbeat(chat_id, content)
                return _compat.send_result(success=True, message_id=message_id)
        return await super().edit_message(chat_id, message_id, content,  # type: ignore[misc]
                                          finalize=finalize, **kwargs)

    async def send_document(self, chat_id: str, file_path: str, *,
                            file_name: str | None = None, **kwargs: Any) -> Any:
        """文档交付：有卡片时上传后 reply 到卡片消息下方（飞书卡片无 file 组件）."""
        engine = self._engine()
        card_msg_id = engine.last_card_msg_id(chat_id)
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


def create_adapter_factory(engine: ChatCardEngine) -> Any:
    """adapter_factory(PlatformConfig)：延迟导入官方基类 + 合成子类."""

    def factory(config: Any) -> Any:
        base = _import_base_adapter()
        cls = build_adapter_class(base, engine)
        logging.getLogger("gateway.run").info(
            "[feishu-streaming] adapter factory: base=%s -> StreamingFeishuAdapter",
            base.__module__)
        return cls(config)

    return factory
