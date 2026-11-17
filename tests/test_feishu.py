"""Feishu client transient-error behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from plugin._vendor.feishu import FeishuAPIError, FeishuClient, local_file_name, local_file_path


class _Resp:
    def __init__(self, *, ok: bool, code: int = 0, msg: str = "", data: object | None = None) -> None:
        self._ok = ok
        self.code = code
        self.msg = msg
        self.data = data

    def success(self) -> bool:
        return self._ok


def _client_with(**methods: AsyncMock) -> FeishuClient:
    client = FeishuClient.__new__(FeishuClient)
    client._client = SimpleNamespace(  # type: ignore[attr-defined]
        cardkit=SimpleNamespace(
            v1=SimpleNamespace(
                card=SimpleNamespace(
                    acreate=methods.get("card_create", AsyncMock()),
                    aupdate=methods.get("card_update", AsyncMock()),
                    abatch_update=methods.get("batch_update", AsyncMock()),
                    asettings=methods.get("settings", AsyncMock()),
                ),
                card_element=SimpleNamespace(content=methods.get("card_element_content", AsyncMock())),
            ),
        ),
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(
                    acreate=methods.get("create_message", AsyncMock()),
                    areply=methods.get("reply", AsyncMock()),
                ),
            ),
        ),
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "message"),
    [
        (2200, "Gateway timeout. Please try again later."),
        (300000, "Server Internal Error"),
    ],
    ids=["gateway-timeout", "server-internal-error"],
)
async def test_cardkit_create_retries_transient_errors_once(code: int, message: str) -> None:
    create = AsyncMock(
        side_effect=[
            _Resp(ok=False, code=code, msg=message),
            _Resp(ok=True, data=SimpleNamespace(card_id="card-ok")),
        ]
    )
    client = _client_with(card_create=create)

    assert await client.cardkit_create({"schema": "2.0"}) == "card-ok"
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_cardkit_batch_update_retries_internal_error() -> None:
    batch_update = AsyncMock(
        side_effect=[
            _Resp(ok=False, code=1663, msg="internal error"),
            _Resp(ok=True),
        ]
    )
    client = _client_with(batch_update=batch_update)

    await client.cardkit_batch_update("card", [{"action": "add"}], sequence=7)

    assert batch_update.await_count == 2
    first_request = batch_update.await_args_list[0].args[0]
    second_request = batch_update.await_args_list[1].args[0]
    assert second_request.card_id == first_request.card_id
    assert second_request.request_body.sequence == first_request.request_body.sequence


@pytest.mark.asyncio
async def test_reply_card_by_id_retries_gateway_timeout_once() -> None:
    reply = AsyncMock(
        side_effect=[
            _Resp(ok=False, code=2200, msg="Gateway timeout. Please try again later."),
            _Resp(ok=True, data=SimpleNamespace(message_id="msg-ok")),
        ]
    )
    client = _client_with(reply=reply)

    assert await client.reply_card_by_id("anchor", "card") == "msg-ok"
    assert reply.await_count == 2
    first_request = reply.await_args_list[0].args[0]
    second_request = reply.await_args_list[1].args[0]
    assert first_request.request_body.uuid
    assert second_request.request_body.uuid == first_request.request_body.uuid


@pytest.mark.asyncio
async def test_send_card_to_chat_reuses_uuid_across_retries() -> None:
    create = AsyncMock(
        side_effect=[
            _Resp(ok=False, code=2200, msg="Gateway timeout. Please try again later."),
            _Resp(ok=True, data=SimpleNamespace(message_id="msg-ok")),
        ]
    )
    client = _client_with(create_message=create)

    assert await client.send_card_to_chat("chat", {"schema": "2.0"}) == "msg-ok"
    assert create.await_count == 2
    first_request = create.await_args_list[0].args[0]
    second_request = create.await_args_list[1].args[0]
    assert first_request.request_body.uuid
    assert second_request.request_body.uuid == first_request.request_body.uuid


@pytest.mark.asyncio
async def test_cardkit_create_does_not_retry_non_transient_error() -> None:
    create = AsyncMock(side_effect=[_Resp(ok=False, code=230099, msg="content failed")])
    client = _client_with(card_create=create)

    with pytest.raises(FeishuAPIError):
        await client.cardkit_create({"schema": "2.0"})

    assert create.await_count == 1


# ── 媒体交付路径（话题锚定 / file:// 解码）──


def test_local_file_path_decodes_quoted_file_url() -> None:
    """run_notifications 以 quote(file://) 传本地媒体——Windows 盘符/含空格路径必被编码."""
    assert local_file_path("file://C%3A%5CUsers%5Chermes%5Ca.jpg") == "C:\\Users\\hermes\\a.jpg"
    assert local_file_path("file:///tmp/my%20report.pdf") == "/tmp/my report.pdf"
    # 裸路径不解码（显式指定文件名的 quote 语义只属于 file:// 契约）
    assert local_file_path("/tmp/裸路径 100%.pdf") == "/tmp/裸路径 100%.pdf"


def test_local_file_name_prefers_override() -> None:
    assert local_file_name("file:///tmp/a%20b.pdf", "展示名.pdf") == "展示名.pdf"
    assert local_file_name("file:///tmp/a%20b.pdf") == "a b.pdf"


@pytest.mark.asyncio
async def test_upload_document_decodes_quoted_file_url(tmp_path) -> None:
    target = tmp_path / "我的 报告.pdf"
    target.write_bytes(b"%PDF-1.4")
    client = FeishuClient.__new__(FeishuClient)
    client._upload_file_bytes = AsyncMock(return_value="fk_9")  # type: ignore[method-assign]

    key = await client.upload_document(f"file://{quote(str(target))}")

    assert key == "fk_9"
    data, name, ftype = client._upload_file_bytes.await_args.args  # type: ignore[attr-defined]
    assert data == b"%PDF-1.4" and name == "我的 报告.pdf" and ftype == "pdf"


@pytest.mark.asyncio
async def test_reply_image_by_id_returns_message_id_flag() -> None:
    reply = AsyncMock(return_value=_Resp(ok=True, data=SimpleNamespace(message_id="om_img_reply")))
    client = _client_with(reply=reply)

    assert await client.reply_image_by_id("om_card", "img_key") is True
    reply.assert_awaited_once()
    failed = _client_with(reply=AsyncMock(return_value=_Resp(ok=True, data=None)))
    assert await failed.reply_image_by_id("om_card", "img_key") is False
