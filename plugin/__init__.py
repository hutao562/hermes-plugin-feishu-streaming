"""feishu-streaming-platform 插件入口.

kind: platform 插件：以同名 ``feishu`` 注册平台（registry last-writer-wins，
具体注册压掉 bundled 的 deferred loader），从而整体取代官方 FeishuAdapter；
同时注册插件钩子（reasoning 流观察）。

部署：把本目录放到 ``~/.hermes/plugins/feishu-streaming/``（plugin.yaml +
__init__.py + 其余文件），并在 config.yaml 的 ``plugins.enabled`` 加入
**manifest 名** ``feishu-streaming-platform``（目录名 feishu-streaming 不是
enable 名——填错会静默不加载，2026-10-09 跨机部署实测踩过）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapter import create_scoped_adapter_factory
from .engine import ChatCardEngine

_logger = logging.getLogger("hermes_lark_streaming.plugin")


def _scoped_env(name: str) -> str:
    """profile 作用域感知的凭据读取.

    gateway 规则（#72348/#86905）：multiplex 下 os.environ 恒为 default profile
    的值，secondary profile 的凭据只能经 secret scope 读——所以这里绝不直接
    os.getenv（旧行为是 default 凭据泄漏到 secondary 的通道）。hermes 不可导入
    （本地直跑/单测）时返回空：宁缺勿串。
    """
    try:
        from gateway.platforms._shared import get_scoped_secret
    except ImportError:
        return ""
    return str(get_scoped_secret(name, "") or "")


class _LazyClient:
    """惰性 FeishuClient — 优先从 adapter 绑定的 client 源取（profile 凭据），
    未绑定时回退 scoped env（本地直跑/测试；作用域感知，见 _scoped_env）。"""

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
            app_id=_scoped_env("FEISHU_APP_ID"),
            app_secret=_scoped_env("FEISHU_APP_SECRET"),
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
        # 与内置 feishu 条目对齐的契约面——同名顶替后缺了这些，hermes gateway
        # setup / gateway status / 配置向导对 feishu 整体回归（bundled 条目全有）。
        # 实现直接复用官方模块（延迟解析，官方改了我们跟着对），见 _official_feishu_attr
        is_connected=_feishu_is_connected,
        validate_config=_feishu_is_connected,
        ensure_deps_fn=_feishu_ensure_deps,
        apply_yaml_config_fn=_feishu_apply_yaml_config,
        setup_fn=_feishu_interactive_setup,
    )
    ctx.register_platform(**entry_kwargs)
    _diag_log("platform registered via ctx (scope entry)")
    _fanout_to_profile_scopes(entry_kwargs)

    # reasoning 流观察（off token path）：钩子不带 chat，引擎路由按全局单活跃会话兜底
    if hasattr(ctx, "register_hook"):
        reasoning_hook = _make_reasoning_hook()
        usage_hook = _make_usage_hook()
        ctx.register_hook("on_stream_delta", reasoning_hook)
        # usage 聚合（footer tokens/t/s）：session_id 含 chat_id 路由到所属 engine
        ctx.register_hook("post_api_request", usage_hook)
        # hook 注册表按 profile 分 manager（get_plugin_manager 按 home 缓存，各自
        # 独立 _hooks）——与平台条目分桶同病：这里注册的回调只落在当前 scope，
        # secondary profile 的流线程查自己的 manager 为空 → reasoning 流静默
        # 丢弃（安安侧折叠条消失的根因）。补注册到每个 live profile 的 manager。
        _fanout_hooks_to_profile_scopes({
            "on_stream_delta": reasoning_hook,
            "post_api_request": usage_hook,
        }, manifest=getattr(ctx, "manifest", None))


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
        model_cycle=_model_cycle_config(),
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


# 钩子 fan-out 已注册的回调身份（reload-plugins/重跑幂等；官方 append 无去重）
_FANED_HOOK_IDS: set[int] = set()


def _fanout_hooks_to_profile_scopes(hooks: dict[str, Any], manifest: Any = None) -> None:
    """把 hook 回调补注册到每个 live profile 的 PluginManager（**全程公开面**）.

    manager 按解析到的 home 缓存（_plugin_home_key → get_hermes_home contextvar），
    插件只在 discovery scope 注册一次，secondary profile 的 manager 查不到本插件
    的回调 → 其流线程按自己的 manager 迭代钩子为空（安安侧 reasoning 丢失根因）。
    官方 startup 的 per-profile discover_plugins() 并不会把本插件重复装进 secondary
    manager（生产实证 register() 每进程仅一次）——因此这里显式补注册。catalog 准入
    "no core override" 红线禁止写 hermes 私有字段，全程走公开面：home override
    （hermes_constants 公开 contextvar API）→ get_plugin_manager()（官方：缺席即建
    +缓存）→ PluginContext.register_hook（官方 facade 方法）。
    """
    try:
        from hermes_cli import plugins as plugins_mod
        from hermes_cli.profiles import profiles_to_serve  # type: ignore[import-not-found]
        from hermes_constants import (  # type: ignore[import-not-found]
            hermes_home_key,
            reset_hermes_home_override,
            set_hermes_home_override,
        )
    except Exception as exc:
        _diag_log(f"hook fan-out imports failed: {exc!r}")
        return
    current = hermes_home_key()
    for profile_name, home in profiles_to_serve(multiplex=True):
        key = hermes_home_key(home)
        if key == current or _profile_disabled_plugin(home):
            continue
        try:
            token = set_hermes_home_override(str(home))
            try:
                manager = plugins_mod.get_plugin_manager()
            finally:
                reset_hermes_home_override(token)
            context = plugins_mod.PluginContext(manifest, manager)
            for name, callback in hooks.items():
                if id(callback) in _FANED_HOOK_IDS:
                    continue
                context.register_hook(name, callback)
                _FANED_HOOK_IDS.add(id(callback))
            _diag_log(f"hooks fanned out to profile '{profile_name}' (scope={key})")
        except Exception as exc:
            _diag_log(f"hook fan-out to profile '{profile_name}' FAILED: {exc!r}")


# 跨 engine 钩子路由（multiplex 下每 profile 一个 engine）
_ENGINES: list[ChatCardEngine] = []
_ENGINES_LOCK = threading.Lock()


def _model_cycle_config() -> list[str]:
    """footer 模型切换按钮的轮换清单.

    显式 `streaming.footer.model_cycle: [a, b]` 优先；缺省从同目录 config.yaml
    的 `model.default` + `fallback_providers[].model` 推导（去重保序）。
    """
    explicit = _footer_config("model_cycle")
    if isinstance(explicit, list) and explicit:
        return [str(m).strip() for m in explicit if str(m).strip()]
    try:
        import yaml

        from ._vendor.config import _config_path

        raw = yaml.safe_load(_config_path().read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    models: list[str] = []
    model_cfg = raw.get("model") or {}
    default = str((model_cfg or {}).get("default") or "").strip()
    if default:
        models.append(default)
    for fb in raw.get("fallback_providers") or []:
        name = str((fb or {}).get("model") or "").strip()
        if name and name not in models:
            models.append(name)
    return models


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


# 内置 feishu 注册辅助的延迟解析缓存（函数对象；影子模块的纯函数与真身等价）
_OFFICIAL_HELPERS: dict[str, Any] = {}


def _official_feishu_attr(attr: str) -> Any:
    """官方 feishu adapter 模块里的注册辅助函数（延迟解析，缓存命中后不再查）.

    bundled feishu 是 deferred loader——discover_plugins 时模块多半未加载，
    所以 PlatformEntry 的辅助字段不能在 register() 现场取，只能包一层首调
    解析。解析顺序：sys.modules 里的运行时真身（hermes_plugins.*）→ 源码路径
    回退。这些辅助（is_connected/setup/apply_yaml/ensure_deps）是纯函数或 CLI
    流程，与类身份无关，源码路径的影子模块安全。
    """
    if attr in _OFFICIAL_HELPERS:
        return _OFFICIAL_HELPERS[attr]
    import importlib
    import sys

    mod = next((m for n, m in sys.modules.items()
                if n.startswith("hermes_plugins.") and n.endswith(".adapter")
                and hasattr(m, "FeishuAdapter")), None)
    if mod is None:
        try:
            mod = importlib.import_module("plugins.platforms.feishu.adapter")
        except Exception:
            return None
    fn = getattr(mod, attr, None)
    if callable(fn):
        _OFFICIAL_HELPERS[attr] = fn
        return fn
    return None


def _feishu_is_connected(config: Any) -> bool:
    """复用内置判定：extra 里有 app_id 即视为已连接（gateway status / setup 用）."""
    fn = _official_feishu_attr("_is_connected")
    try:
        return bool(fn(config)) if fn else False
    except Exception:
        return False


def _feishu_ensure_deps() -> bool:
    """ACTIVE 安装器（复用内置 check_feishu_requirements：缺依赖时经 pm 安装）."""
    fn = _official_feishu_attr("check_feishu_requirements")
    try:
        return bool(fn()) if fn else False
    except Exception:
        return False


def _feishu_apply_yaml_config(yaml_cfg: Any, feishu_cfg: Any) -> Any:
    """复用内置 YAML 桥（config.yaml feishu.allow_bots → env/extra，multiplex 安全）."""
    fn = _official_feishu_attr("_apply_yaml_config")
    return fn(yaml_cfg, feishu_cfg) if fn else None


def _feishu_interactive_setup() -> None:
    """复用内置交互配置向导（二维码/手动录入凭据、写 .env、授权与群策略）."""
    fn = _official_feishu_attr("interactive_setup")
    if fn is None:
        raise RuntimeError("官方 feishu adapter 不可导入，无法进入交互配置；"
                           "请手动设置 FEISHU_APP_ID / FEISHU_APP_SECRET")
    fn()


def _make_reasoning_hook() -> Any:
    dropped = {"n": 0}

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
        else:
            # 多会话并发时防串扰丢弃——但要可见（曾静默丢成「折叠条消失」）
            dropped["n"] += 1
            if dropped["n"] == 1 or dropped["n"] % 50 == 0:
                logging.getLogger("gateway.run").info(
                    "[feishu-streaming] reasoning dropped (multi-session): "
                    "engines=%d sessions=%s total_dropped=%d",
                    len(actives), [len(s) for _, s in actives], dropped["n"])

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
    """session key 里的 chat → 拥有该 chat 会话的 engine；无命中回退唯一 engine.

    multiplex（≥2 engine，10-08 晚 fan-out 后 boot 即建双 engine）下 agent.session_id
    是 date_hash 形、不含 chat——单 engine 兜底失效即「footer 只剩时间」回归的根因；
    补「恰好一个 engine 有进行中回合」的兜底（与 on_stream_delta 同一判据，creating
    也算活跃）。并发歧义时放弃，footer 退化为纯时长。
    """
    chat = ChatCardEngine._chat_of_session(session_id)
    if chat:
        owners = [e for e in engines if e.session_for(chat) is not None]
        if len(owners) == 1:
            return owners[0]
    if len(engines) == 1:
        return engines[0]
    actives = [(e, e.open_sessions()) for e in engines]
    actives = [(e, s) for e, s in actives if s]
    if len(actives) == 1 and len(actives[0][1]) == 1:
        return actives[0][0]
    return None


def _make_standalone_sender() -> Any:
    """cron 无网关进程的投递：渲染 cron 卡片后经 FeishuClient 发送.

    返回 dict（``{"success", "message_id"}``）——hermes 消费端对结果做
    ``result.get("error"/"warnings")``，裸 str 会在发送成功后炸成 delivery_failed。
    wrap 信封同 live lane 一样拆掉（⏰ header 用任务名）。
    """
    from ._vendor.cardkit.builder import build_cron_card
    from .adapter import _parse_cron_payload

    async def standalone_send(pconfig: Any, chat_id: str, message: str, *,
                              thread_id: str | None = None,
                              media_files: list | None = None,
                              force_document: bool = False) -> Any:
        task_name, body, _job_id, is_failure = _parse_cron_payload(message)
        card = build_cron_card(
            body, task_name=task_name,
            run_time=datetime.now().astimezone().isoformat(timespec="minutes"),
            template="red" if is_failure else "blue")
        client = _LazyClient()
        card_id = await client.cardkit_create(card)
        msg_id = await client.send_card_to_chat(
            chat_id, {"type": "card", "data": {"card_id": card_id}})
        return {"success": True, "message_id": msg_id}

    return standalone_send
