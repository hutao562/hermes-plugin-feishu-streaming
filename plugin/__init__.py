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
import threading
import time
from pathlib import Path
from typing import Any

from .adapter import create_scoped_adapter_factory
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


def _diag_log(message: str) -> None:
    """启动早期 hermes logging 未配置，INFO 会进黑洞——关键生命周期事件落独立文件."""
    logging.getLogger("gateway.run").info("[feishu-streaming] %s", message)
    try:
        path = os.path.join(os.environ.get("HERMES_HOME",
                                           os.path.expanduser("~/.hermes")),
                            "logs", "feishu-streaming-plugin.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {message}\n")
    except Exception:
        pass


def register(ctx: Any) -> None:
    """插件入口 — 由 hermes plugin 系统调用（每进程一次，默认 profile scope）.

    multiplex 网关下 secondary profile（如 family）创建 adapter 时在自己的
    profile scope 查 registry——插件条目默认只落在注册时的 scope 桶里，其它
    profile 查不到就回退官方 bundled adapter（安安没流式卡的根因）。因此注册
    后把同一平台条目补注册到每个 live profile 的 scope。
    """
    _diag_log("register() called")

    entry_kwargs = dict(
        name="feishu",
        label="Feishu / Lark (streaming cards)",
        adapter_factory=create_scoped_adapter_factory(_build_scoped_engine),
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
    ctx.register_platform(**entry_kwargs)
    _diag_log("platform registered via ctx (scope entry)")
    _fanout_to_profile_scopes(entry_kwargs)

    # reasoning 流观察（off token path）：钩子不带 chat，引擎路由按全局单活跃会话兜底
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("on_stream_delta", _make_reasoning_hook())
        # usage 聚合（footer tokens/t/s）：session_id 含 chat_id 路由到所属 engine
        ctx.register_hook("post_api_request", _make_usage_hook())


def _build_scoped_engine() -> tuple[ChatCardEngine, Any]:
    """factory 调用现场（= 目标 profile 的 runtime scope）构建独立 engine.

    footer/header 配置在此读取——HERMES_HOME 此时指向该 profile 目录，
    各 profile 的 streaming.footer/header 段独立生效。engine 登记进
    _ENGINES 供跨 engine 钩子路由。
    """
    client = _LazyClient()
    engine = ChatCardEngine(
        client,
        footer_fields=_footer_config("fields"),
        footer_show_label=_footer_config("show_label", False),
        footer_enabled=_footer_config("enabled", True),
        # header（完成态状态条）与注入模式同源读 streaming.header 段；
        # 漏接会让红卡等 header 依赖特性静默失效
        header_enabled=_header_config(),
    )
    with _ENGINES_LOCK:
        _ENGINES.append(engine)
    _diag_log(f"scoped engine built (home={_current_home()})")
    return engine, client


def _current_home() -> str:
    """当前作用域 home（contextvar 感知）——profile scope 内即该 profile 目录."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME", "?")


def _profile_disabled_plugin(home: Any) -> bool:
    """该 profile 是否把本插件列入 plugins.disabled（全局默认启用的退出开关）."""
    try:
        import yaml

        with open(Path(home) / "config.yaml", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        disabled = ((cfg.get("plugins") or {}).get("disabled")) or []
        return "feishu-streaming-platform" in disabled
    except Exception:
        return False


def _fanout_to_profile_scopes(entry_kwargs: dict[str, Any]) -> None:
    """把平台条目补注册到每个 live profile 的 registry scope 桶.

    全局默认全 profile 启用；profile 在自己 config 的 plugins.disabled 里
    写 feishu-streaming-platform 可退出（回退官方 bundled adapter）。
    """
    try:
        from gateway.platform_registry import PlatformEntry, platform_registry
        from hermes_cli.profiles import profiles_to_serve  # type: ignore[import-not-found]
        from hermes_constants import hermes_home_key  # type: ignore[import-not-found]

        current = hermes_home_key()
        for profile_name, home in profiles_to_serve(multiplex=True):
            key = hermes_home_key(home)
            if key == current:
                continue
            if _profile_disabled_plugin(home):
                _diag_log(f"profile '{profile_name}' disabled this plugin — skip fan-out")
                continue
            platform_registry.register(
                PlatformEntry(source="plugin",
                               plugin_name="feishu-streaming-platform",
                               **entry_kwargs),
                scope=key)
            _diag_log(f"platform fanned out to profile '{profile_name}' (scope={key})")
    except Exception as exc:
        _diag_log(f"profile scope fan-out FAILED: {exc!r}")


# 跨 engine 钩子路由（multiplex 下每 profile 一个 engine）
_ENGINES: list[ChatCardEngine] = []
_ENGINES_LOCK = threading.Lock()


def _footer_config(key: str, default: Any = None) -> Any:
    """读 HERMES_HOME/config.yaml 的 streaming.footer 段（与注入模式同源同形态）."""
    try:
        from ._vendor.config import Config

        footer = Config()._streaming_sec().get("footer", {})
        value = footer.get(key) if isinstance(footer, dict) else None
        if key == "fields" and value and isinstance(value, list) and isinstance(value[0], str):
            value = [value]  # 一维自动包二维（同注入模式 Config.footer_fields）
        return value if value is not None else default
    except Exception:
        return default


def _header_config() -> bool:
    """读 HERMES_HOME/config.yaml 的 streaming.header.enabled（默认 false）."""
    try:
        from ._vendor.config import Config

        return Config().header_enabled
    except Exception:
        return False


def _feishu_deps_present() -> bool:
    """PASSIVE 探针：lark-oapi 可导入即通过（绝不安装）."""
    try:
        import lark_oapi  # noqa: F401

        return True
    except ImportError:
        return False


def _make_reasoning_hook() -> Any:
    def on_stream_delta(**kwargs: Any) -> None:
        if kwargs.get("kind") != "reasoning":
            return
        # 官方 enqueue 参数名是 delta（stream_delivery._enqueue_stream_hook），非 text
        text = kwargs.get("delta") or kwargs.get("text") or ""
        if not text:
            return
        # 钩子不带 chat：全局恰好一个活跃会话时兜底（跨 engine 聚合判定）
        with _ENGINES_LOCK:
            engines = list(_ENGINES)
        actives = [(e, e.streaming_sessions()) for e in engines]
        actives = [(e, s) for e, s in actives if s]
        if len(actives) == 1 and len(actives[0][1]) == 1:
            actives[0][0].on_reasoning("", text)

    return on_stream_delta


def _make_usage_hook() -> Any:
    logged: set[str] = set()

    def on_post_api_request(**kwargs: Any) -> None:
        usage = kwargs.get("usage")
        if not (isinstance(usage, dict)):
            return
        session_id = kwargs.get("session_id") or ""
        with _ENGINES_LOCK:
            engines = list(_ENGINES)
        engine = _route_usage_engine(engines, session_id)
        if engine is None:
            return
        usage = {**usage, "context_length": kwargs.get("context_length"),
                 "started_at": kwargs.get("started_at"),
                 "ended_at": kwargs.get("ended_at")}
        engine.record_usage(session_id, usage, model=kwargs.get("model") or "")
        if session_id not in logged:  # 每回合桶首条打一次（确认钩子活性）
            logged.add(session_id)
            logging.getLogger("gateway.run").info(
                "[feishu-streaming] usage tracking started: session=%s model=%s",
                session_id[:40], kwargs.get("model") or "?")

    return on_post_api_request


def _route_usage_engine(engines: list[ChatCardEngine], session_id: str) -> ChatCardEngine | None:
    """session key 里的 chat → 拥有该 chat 会话的 engine；无命中回退唯一 engine."""
    chat = ChatCardEngine._chat_of_session(session_id)
    if chat:
        owners = [e for e in engines if e.session_for(chat) is not None]
        if len(owners) == 1:
            return owners[0]
    return engines[0] if len(engines) == 1 else None


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
