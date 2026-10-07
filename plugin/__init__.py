"""feishu-streaming-platform 插件入口.

kind: platform 插件：以同名 ``feishu`` 注册平台（registry last-writer-wins，
具体注册压掉 bundled 的 deferred loader），从而整体取代官方 FeishuAdapter；
同时注册插件钩子（reasoning 流观察）。

部署：把本目录放到 ``~/.hermes/plugins/feishu-streaming/``（plugin.yaml +
__init__.py + 其余文件），并在 config.yaml 的 ``plugins.enabled`` 加入
``feishu-streaming``；注入模式（hermes-lark-streaming entry-point）需先
``uninstall`` 释放 run.py。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .adapter import create_adapter_factory
from .engine import ChatCardEngine

_logger = logging.getLogger("hermes_lark_streaming.plugin")


class _LazyClient:
    """惰性 FeishuClient — 优先从 adapter 绑定的 client 源取（profile 凭据），
    未绑定时回退 env（本地直跑/测试）。"""

    def __init__(self) -> None:
        self._client: Any = None
        self._source: Any = None  # callable -> FeishuClient | None

    def bind_source(self, source: Any) -> None:
        """factory 实例化 adapter 后注入：source() 返回凭据正确的 FeishuClient."""
        self._source = source
        self._client = None  # 重新解析

    def _build(self) -> Any:
        if self._source is not None:
            self._client = self._source()
            if self._client is not None:
                return self._client
        from ._vendor.feishu import FeishuClient, FeishuClientConfig

        self._client = FeishuClient(FeishuClientConfig(
            app_id=os.environ.get("FEISHU_APP_ID", ""),
            app_secret=os.environ.get("FEISHU_APP_SECRET", ""),
        ))
        return self._client

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if self._client is None:
            self._build()
        return getattr(self._client, name)


def register(ctx: Any) -> None:
    """插件入口 — 由 hermes plugin 系统调用（每 profile 一次）."""
    # 走 gateway.run logger（唯一确认落 gateway.log 的通道；本包 logger 不进日志）
    logging.getLogger("gateway.run").info(
        "[feishu-streaming] register() called — registering streaming feishu platform")
    client = _LazyClient()
    engine = ChatCardEngine(client)

    ctx.register_platform(
        name="feishu",
        label="Feishu / Lark (streaming cards)",
        adapter_factory=create_adapter_factory(engine, client_proxy=client),
        check_fn=_feishu_deps_present,
        required_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        install_hint="Run `hermes setup` to install Feishu support.",
        allowed_users_env="FEISHU_ALLOWED_USERS",
        allow_all_env="FEISHU_ALLOW_ALL_USERS",
        cron_deliver_env_var="FEISHU_HOME_CHANNEL",
        standalone_sender_fn=_make_standalone_sender(),
        max_message_length=8000,
        emoji="🪽",
        allow_update_command=True,
    )
    logging.getLogger("gateway.run").info(
        "[feishu-streaming] platform registered (last-writer-wins over bundled)")

    # reasoning 流观察（off token path）：钩子不带 chat，引擎按单会话兜底路由
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("on_stream_delta", _make_reasoning_hook(engine))


def _feishu_deps_present() -> bool:
    """PASSIVE 探针：lark-oapi 可导入即通过（绝不安装）."""
    try:
        import lark_oapi  # noqa: F401

        return True
    except ImportError:
        return False


def _make_reasoning_hook(engine: ChatCardEngine) -> Any:
    def on_stream_delta(**kwargs: Any) -> None:
        if kwargs.get("kind") != "reasoning":
            return
        text = kwargs.get("text") or ""
        if text:
            engine.on_reasoning("", text)  # chat 未知 → 引擎单会话兜底

    return on_stream_delta


def _make_standalone_sender() -> Any:
    """cron 无网关进程的投递：渲染 cron 卡片后经 FeishuClient 发送."""
    from ._vendor.cardkit.builder import build_cron_card

    async def standalone_send(pconfig: Any, chat_id: str, message: str, *,
                              thread_id: str | None = None,
                              media_files: list | None = None,
                              force_document: bool = False) -> Any:
        card = build_cron_card(message)
        client = _LazyClient()
        card_id = await client.cardkit_create(card)
        return await client.send_card_to_chat(
            chat_id, {"type": "card", "data": {"card_id": card_id}})

    return standalone_send
