"""飞书 Open API 客户端 — 基于 lark-oapi SDK."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    BatchUpdateCardRequest,
    BatchUpdateCardRequestBody,
    Card,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .config import DEFAULT_DOMAIN

_FILE_TYPE_BY_EXT: dict[str, str] = {
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".csv": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
    ".mp4": "mp4",
    ".mov": "mp4",
    ".opus": "opus",
    ".ogg": "opus",
}


_logger = logging.getLogger("hermes_lark_streaming")

_OPEN_APIS_SUFFIX = "/open-apis"

CARDKIT_GATEWAY_TIMEOUT = 2200
CARDKIT_INTERNAL_ERROR = 1663
CARDKIT_SERVER_INTERNAL_ERROR = 300000
CARDKIT_TRANSIENT_ERROR_CODES = frozenset(
    {
        CARDKIT_GATEWAY_TIMEOUT,
        CARDKIT_INTERNAL_ERROR,
        CARDKIT_SERVER_INTERNAL_ERROR,
    }
)
_TRANSIENT_RETRY_DELAYS_SEC = (0.15, 0.5, 1.0)


def _sanitize_message(msg: str) -> str:
    """从错误消息中移除 token 和 secret."""
    msg = re.sub(r'(tenant_access_token["\s:=]+)([A-Za-z0-9_-]{10,})', r"\1***", msg)
    msg = re.sub(r'(app_secret["\s:=]+)([A-Za-z0-9]{10,})', r"\1***", msg)
    msg = re.sub(r"(Bearer\s+)([A-Za-z0-9_-]{10,})", r"\1***", msg)
    return msg


class FeishuAPIError(RuntimeError):
    """飞书 API 错误，携带 API 错误码."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code

    def extract_sub_code(self) -> int | None:
        """从 msg 字符串中提取子错误码.

        格式: "Failed to create card content, ext=ErrCode: 11310; ..."
        """
        m = re.search(r"ErrCode:\s*(\d+)", str(self))
        if m:
            return int(m.group(1))
        return None


CARDKIT_RATE_LIMITED = 230020  # 频控
CARDKIT_CONTENT_FAILED = 230099  # 卡片内容创建失败（通用码，需检查子错误）
CARDKIT_ELEMENT_LIMIT = 11310  # 子码: 卡片元素数量超限
CARDKIT_STREAMING_CLOSED = 300309  # 卡片流式模式已关闭
CARD_OVER_SIZE = 200860  # 卡片超过飞书体积上限（工具长输出堆爆 JSON，元素数还没到阈值）
MSG_NOT_FOUND = 1000023  # 消息不存在/已删除


@dataclass(frozen=True)
class FeishuClientConfig:
    app_id: str
    app_secret: str
    base_url: str = DEFAULT_DOMAIN

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or not self.app_id.strip():
            raise ValueError("app_id is required")
        if not isinstance(self.app_secret, str) or not self.app_secret.strip():
            raise ValueError("app_secret is required")


class FeishuClient:
    """飞书 REST API 封装 — 基于 lark-oapi SDK.

    SDK 自动管理 tenant_access_token 的获取和刷新.
    """

    def __init__(self, config: FeishuClientConfig) -> None:
        self.config = config
        domain = config.base_url.strip().rstrip("/").removesuffix(_OPEN_APIS_SUFFIX) or DEFAULT_DOMAIN
        builder = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .domain(domain)
        )
        self._client = builder.build()

    @classmethod
    def from_lark_client(cls, lark_client: Any) -> FeishuClient:
        """用现成的 lark SDK client 包装（platform 插件复用官方 adapter 的
        profile 凭据 client，避免自行读 env 拿到空凭据）."""
        obj = cls.__new__(cls)
        obj.config = None  # type: ignore[assignment,attr-defined]
        obj._client = lark_client
        return obj

    @staticmethod
    def _check(response: Any, operation: str) -> None:
        """检查 SDK 响应，失败时抛出 FeishuAPIError."""
        if not response.success():
            code = response.code or 0
            msg = response.msg or ""
            raise FeishuAPIError(
                _sanitize_message(f"{operation}: code={code}, msg={msg}"),
                code,
            )

    @staticmethod
    def _dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False)

    async def _checked_call(self, operation: str, call: Any) -> Any:
        """Run a Feishu SDK call and retry transient CardKit/Lark server errors."""
        attempts = len(_TRANSIENT_RETRY_DELAYS_SEC) + 1
        last_error: FeishuAPIError | None = None
        for attempt in range(attempts):
            resp = await call()
            try:
                self._check(resp, operation)
                return resp
            except FeishuAPIError as exc:
                last_error = exc
                if exc.code not in CARDKIT_TRANSIENT_ERROR_CODES or attempt >= attempts - 1:
                    raise
                delay = _TRANSIENT_RETRY_DELAYS_SEC[attempt]
                _logger.warning(
                    "%s transient Feishu API error code=%s, retrying attempt=%d/%d delay=%.2fs",
                    operation,
                    exc.code,
                    attempt + 2,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def send_card_to_chat(
        self,
        chat_id: str,
        card: dict[str, Any],
        *,
        reply_to_message_id: str | None = None,
    ) -> str:
        """发送独立卡片到聊天（非回复），返回 message_id."""
        request_uuid = uuid.uuid4().hex
        if reply_to_message_id:
            request = (
                ReplyMessageRequest.builder()
                .message_id(reply_to_message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(self._dumps(card))
                    .uuid(request_uuid)
                    .build()
                )
                .build()
            )
            resp = await self._checked_call(
                "send_card_to_chat",
                lambda: self._client.im.v1.message.areply(request),
            )
        else:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(self._dumps(card))
                    .uuid(request_uuid)
                    .build()
                )
                .build()
            )
            resp = await self._checked_call(
                "send_card_to_chat",
                lambda: self._client.im.v1.message.acreate(request),
            )
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("send_card_to_chat: response missing message_id")

    async def reply_card_by_id(self, message_id: str, card_id: str) -> str:
        """通过 card_id 回复 CardKit 卡片消息，返回 message_id."""
        request_uuid = uuid.uuid4().hex
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(self._dumps({"type": "card", "data": {"card_id": card_id}}))
                .uuid(request_uuid)
                .build()
            )
            .build()
        )
        resp = await self._checked_call(
            "reply_card_by_id",
            lambda: self._client.im.v1.message.areply(request),
        )
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("reply_card_by_id: response missing message_id")

    async def cardkit_create(self, card: dict[str, Any]) -> str:
        """创建 CardKit 实体，返回 card_id."""
        request = (
            CreateCardRequest.builder()
            .request_body(CreateCardRequestBody.builder().type("card_json").data(self._dumps(card)).build())
            .build()
        )
        resp = await self._checked_call(
            "cardkit_create",
            lambda: self._client.cardkit.v1.card.acreate(request),
        )
        if resp.data and resp.data.card_id:
            return str(resp.data.card_id)
        raise FeishuAPIError("cardkit_create: response missing card_id")

    async def cardkit_stream_element(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int = 0,
    ) -> None:
        """流式更新卡片内指定 element 的内容（打字机效果）."""
        body_builder = ContentCardElementRequestBody.builder().content(content)
        body_builder = body_builder.sequence(sequence)
        request = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(element_id)
            .request_body(body_builder.build())
            .build()
        )
        await self._checked_call(
            "cardkit_stream_element",
            lambda: asyncio.to_thread(self._client.cardkit.v1.card_element.content, request),
        )

    async def cardkit_update(
        self,
        card_id: str,
        card: dict[str, Any],
        sequence: int = 0,
    ) -> None:
        """全量更新 CardKit 卡片."""
        body_builder = UpdateCardRequestBody.builder().card(
            Card.builder().type("card_json").data(self._dumps(card)).build()
        )
        body_builder = body_builder.sequence(sequence)
        request = UpdateCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
        await self._checked_call(
            "cardkit_update",
            lambda: self._client.cardkit.v1.card.aupdate(request),
        )

    async def cardkit_batch_update(
        self,
        card_id: str,
        actions: list[dict[str, Any]],
        *,
        sequence: int = 0,
    ) -> None:
        """局部更新 CardKit 卡片（增删改组件）."""
        body_builder = BatchUpdateCardRequestBody.builder().sequence(sequence).actions(self._dumps(actions))
        request = BatchUpdateCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
        await self._checked_call(
            "cardkit_batch_update",
            lambda: self._client.cardkit.v1.card.abatch_update(request),
        )

    async def cardkit_close_streaming(self, card_id: str, sequence: int = 0) -> None:
        """关闭 CardKit 卡片的流式模式."""
        body_builder = SettingsCardRequestBody.builder().settings(self._dumps({"streaming_mode": False}))
        body_builder = body_builder.sequence(sequence)
        request = SettingsCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
        await self._checked_call(
            "cardkit_close_streaming",
            lambda: self._client.cardkit.v1.card.asettings(request),
        )

    async def upload_image(self, image_url: str) -> str | None:
        """下载远程图片并上传到飞书，返回 img_key."""
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None,
                self._download_image,
                image_url,
            )
        except Exception:
            _logger.debug("image upload failed for %s", image_url, exc_info=True)
            return None

        if data is None:
            return None

        file = io.BytesIO(data)
        request = (
            CreateImageRequest.builder()
            .request_body(CreateImageRequestBody.builder().image_type("message").image(file).build())
            .build()
        )
        resp = await self._client.im.v1.image.acreate(request)
        if resp.success() and resp.data and resp.data.image_key:
            return str(resp.data.image_key)
        return None

    async def upload_image_file(self, image_path: str) -> str | None:
        """上传本地图片文件到飞书，返回 img_key（支持 file:// 前缀 / 裸路径）."""
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, self._read_image_file, image_path)
        except Exception:
            _logger.debug("image file upload failed for %s", image_path, exc_info=True)
            return None

        if data is None:
            return None

        file = io.BytesIO(data)
        request = (
            CreateImageRequest.builder()
            .request_body(CreateImageRequestBody.builder().image_type("message").image(file).build())
            .build()
        )
        resp = await self._client.im.v1.image.acreate(request)
        if resp.success() and resp.data and resp.data.image_key:
            return str(resp.data.image_key)
        return None

    @staticmethod
    def _read_image_file(path: str) -> bytes | None:
        """读取本地图片文件（在线程池中运行）."""
        try:
            p = path[7:] if path.startswith("file://") else path
            with open(p, "rb") as f:
                return bytes(f.read())
        except OSError:
            _logger.debug("image file read failed: %s", path)
            return None

    @staticmethod
    def _download_image(url: str, timeout: int = 15) -> bytes | None:
        """同步下载图片（在线程池中运行）."""
        try:
            req = Request(url, headers={"User-Agent": "hermes-lark-streaming/1.0"})
            with urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                return bytes(resp.read())
        except (URLError, OSError):
            _logger.debug("image download failed: %s", url)
            return None
    async def _upload_file_bytes(self, data: bytes, file_name: str, file_type: str) -> str | None:
        """上传文件字节到飞书 IM，返回 file_key（失败返回 None，不抛异常）."""
        file = io.BytesIO(data)
        builder = CreateFileRequestBody.builder().file_type(file_type).file_name(file_name).file(file)
        request = CreateFileRequest.builder().request_body(builder.build()).build()
        resp = await self._client.im.v1.file.acreate(request)
        if resp.success() and resp.data and getattr(resp.data, "file_key", None):
            return str(resp.data.file_key)
        _logger.warning(
            "file upload failed: code=%s msg=%s file=%s",
            getattr(resp, "code", None), getattr(resp, "msg", None), file_name,
        )
        return None

    async def upload_document(self, file_path: str, *, file_name: str | None = None) -> str | None:
        """上传本地文档/文件到飞书，返回 file_key（支持 file:// 前缀 / 裸路径）.

        file_type 按扩展名映射（pdf/doc/xls/ppt/mp4/opus），未知扩展名用 stream。
        """
        p = file_path[7:] if file_path.startswith("file://") else file_path
        name = file_name or os.path.basename(p)
        ext = os.path.splitext(name)[1].lower()
        file_type = _FILE_TYPE_BY_EXT.get(ext, "stream")
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, self._read_image_file, p)
        except Exception:
            _logger.debug("document upload failed for %s", file_path, exc_info=True)
            return None
        if not data:
            return None
        return await self._upload_file_bytes(data, name, file_type)

    async def reply_file_by_id(self, message_id: str, file_key: str, file_name: str) -> bool:
        """以 file 消息回复指定消息（卡片消息），文件落在卡片正下方的会话链里.

        内容字段用 file_key（与 FeishuAdapter._send_uploaded_file_message 的
        key_payload 一致——官方文档写 file_id，实测 230001 内容校验失败）。
        """
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("file")
                .content(self._dumps({"file_key": file_key}))
                .build()
            )
            .build()
        )
        resp = await self._checked_call(
            "reply_file_by_id",
            lambda: self._client.im.v1.message.areply(request),
        )
        return bool(resp.data and resp.data.message_id)
