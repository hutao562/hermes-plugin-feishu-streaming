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
import weakref
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
    # env 启用桥透传（新版 hermes 平台启用依赖；见 _official_feishu_env_bridge）。
    # 仅在官方模块可解析且运行时 PlatformEntry 认识这些字段时附加。
    env_overrides, env_enable_credentials = _official_feishu_env_bridge()
    if (env_overrides or env_enable_credentials) and (
            {"env_overrides", "env_enable_credentials"} <= _platform_entry_field_names()):
        entry_kwargs["env_overrides"] = env_overrides
        entry_kwargs["env_enable_credentials"] = env_enable_credentials or (
            "FEISHU_APP_ID", "FEISHU_APP_SECRET")
    ctx.register_platform(**entry_kwargs)
    _diag_log(f"platform registered via ctx (scope entry, env_bridge={len(env_overrides)} steps)")
    _fanout_to_profile_scopes(entry_kwargs)

    # reasoning 流观察（off token path）：钩子不带 chat，引擎路由按 profile 作用域 +
    # session_id 精确路由（pre_api_request 登记），未路由时按本 profile 单活跃会话兜底
    if hasattr(ctx, "register_hook"):
        hooks = {
            "on_stream_delta": _make_reasoning_hook(),
            "pre_api_request": _make_pre_api_hook(),
            "post_api_request": _make_usage_hook(),
        }
        # register() 会被每个装了本插件的 scope PM 各调一次（v0.21.2 per-profile
        # 拷贝）——同 manager 只挂一份（_HOOKED_MANAGERS 去重），未装拷贝的
        # profile 由 fan-out 兜底
        _register_hooks_to_context(ctx, hooks, getattr(ctx, "_manager", None))
        _fanout_hooks_to_profile_scopes(hooks, manifest=getattr(ctx, "manifest", None))


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
        # 完成卡面板默认展开（streaming.tool_panel_expanded / reasoning_panel_expanded，
        # 未单独配置回退旧键 panel_expanded；此前漏接曾让两键全程不生效）
        tool_panel_expanded=_panel_expanded_config("tool_panel_expanded"),
        reasoning_panel_expanded=_panel_expanded_config("reasoning_panel_expanded"),
    )
    # 构建现场 = 目标 profile 的 runtime scope：记下作用域键，钩子路由按它收敛
    # 到本 profile 的 engine（_scope_engines）
    engine.scope_key = _current_scope_key()
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
        from ._vendor.config import _safe_yaml_load

        cfg = _safe_yaml_load((Path(home) / "config.yaml").read_text(encoding="utf-8"))
        disabled = ((cfg.get("plugins") or {}).get("disabled")) or []
        return "feishu-streaming-platform" in disabled
    except Exception:
        return False


def _fanout_to_profile_scopes(entry_kwargs: dict[str, Any]) -> None:
    """把平台条目补注册到**每个** live profile 的 registry scope 桶（含 current）.

    ctx.register_platform 落桶在 manager.scope_key——multiplex 下 plugin boot
    现场的 manager scope 不保证等于 default home 键（2026-11-17 梦珊机确定性
    复现：j-s 建档后首次全量重启，default 桶空 + user-covers-bundled 又把内置
    feishu 插件挤出 discovery → default profile「No adapter available for
    feishu」，bot 掉线直到下一次重启也不自愈）。因此这里不做 current 跳过：
    所有 live profile 桶无条件补注册（同桶重写为同一 entry，幂等无害）。
    全局默认全 profile 启用；profile 在自己 config 的 plugins.disabled 里
    写 feishu-streaming-platform 可退出（回退官方 bundled adapter）。
    """
    try:
        from gateway.platform_registry import PlatformEntry, platform_registry
        from hermes_cli.profiles import profiles_to_serve  # type: ignore[import-not-found]
        from hermes_constants import hermes_home_key  # type: ignore[import-not-found]

        for profile_name, home in profiles_to_serve(multiplex=True):
            key = hermes_home_key(home)
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


# 已挂上本插件钩子的 PluginManager（弱引用集合：reload 后 manager 重建、旧对象
# 被回收即自动除名——重复 register() 幂等，官方 append 无去重）。v0.21.2 起
# register() 会被**每个装了本插件的 scope PM** 各调一次（per-profile 拷贝部署），
# 按 manager 身份去重取代旧的回调 id 集合（回调 id 会让第二次 register 的
# fan-out 全跳，且挡不住同 manager 重复 append）。
_HOOKED_MANAGERS: weakref.WeakSet[Any] = weakref.WeakSet()


def _register_hooks_to_context(context: Any, hooks: dict[str, Any], manager: Any) -> None:
    """把钩子挂到 context 所属 manager（已挂过则跳过——多 scope register 幂等）."""
    if manager is not None and manager in _HOOKED_MANAGERS:
        return
    for name, callback in hooks.items():
        context.register_hook(name, callback)
    if manager is not None:
        _HOOKED_MANAGERS.add(manager)


def _fanout_hooks_to_profile_scopes(hooks: dict[str, Any], manifest: Any = None) -> None:
    """把 hook 回调补注册到每个 live profile 的 PluginManager（**全程公开面**）.

    manager 按解析到的 home 缓存（_plugin_home_key → get_hermes_home contextvar），
    插件只在 discovery scope 注册一次，secondary profile 的 manager 查不到本插件
    的回调 → 其流线程按自己的 manager 迭代钩子为空（安安侧 reasoning 丢失根因）。
    v0.21.2 起装了 per-profile 拷贝的 profile 由自己的 discovery 注册（官方路径），
    fan-out 只兜未装拷贝的 profile。catalog 准入 "no core override" 红线禁止写
    hermes 私有字段，全程走公开面：home override（hermes_constants 公开 contextvar
    API）→ get_plugin_manager()（官方：缺席即建+缓存）→ PluginContext.register_hook
    （官方 facade 方法）。
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
            _register_hooks_to_context(context, hooks, manager)
            _diag_log(f"hooks fanned out to profile '{profile_name}' (scope={key})")
        except Exception as exc:
            _diag_log(f"hook fan-out to profile '{profile_name}' FAILED: {exc!r}")


# 跨 engine 钩子路由（multiplex 下每 profile 一个 engine）
_ENGINES: list[ChatCardEngine] = []
_ENGINES_LOCK = threading.Lock()
# hermes session_id（date_hash）→ (engine, chat, thread, 登记时间)。pre_api_request
# 与 usage 钩子在「本 profile 恰好一个进行中回合」窗口登记，reasoning 增量按它
# 精确路由。TTL 防陈旧映射跨会话误路由——pre_api_request 每次 API 调用前续期。
_SESSION_ROUTE: dict[str, tuple[ChatCardEngine, str, str | None, float]] = {}
_SESSION_ROUTE_TTL = 600.0


def _current_scope_key() -> str | None:
    """当前 profile 作用域键（contextvar 感知）；hermes 不可导入时 None."""
    try:
        from hermes_constants import hermes_home_key  # type: ignore[import-not-found]

        return str(hermes_home_key())
    except Exception:
        return None


def _scope_engines() -> list[ChatCardEngine]:
    """当前 profile 作用域内的 engine；无法判定或无命中则返回全部（单测/未标记 engine）.

    钩子回调运行在**发起回合的 contextvars** 里（上游 plugin_stream_hooks 的
    dispatcher 以 context.copy().run 调回调），hermes_home_key() 取到的就是该回合
    的 profile——multiplex 下路由先按 profile 收敛，不再把别的 profile 的进行中
    会话当成并发歧义（2026-11-19~24 两机实测：`reasoning dropped (multi-session)
    engines=2 sessions=[1, 1]` 数百次，另一 engine 只是有个进行中会话）。
    """
    with _ENGINES_LOCK:
        engines = list(_ENGINES)
    key = _current_scope_key()
    if key is not None:
        scoped = [e for e in engines if e.scope_key == key]
        if scoped:
            return scoped
    return engines


def _single_open_session(engines: list[ChatCardEngine]) -> tuple[ChatCardEngine, Any] | None:
    """恰好一个 engine 有恰好一个进行中会话（creating 也算）→ (engine, session)；歧义 None."""
    hit: tuple[ChatCardEngine, Any] | None = None
    for engine in engines:
        opens = engine.open_sessions()
        if not opens:
            continue
        if len(opens) > 1 or hit is not None:
            return None
        hit = (engine, opens[0])
    return hit


def _hook_platform(kwargs: dict[str, Any]) -> str:
    """钩子载荷里的回合来源：pre/post_api_request 的 platform、stream 钩子的 surface."""
    return str(kwargs.get("platform") or kwargs.get("surface") or "").strip().lower()


def _non_feishu_turn(kwargs: dict[str, Any]) -> bool:
    """载荷明确标注了非 feishu 来源（cron / 子代理 / 其它平台）——其 usage 与
    reasoning 不得进飞书卡。缺字段（老版本上游/单测）不拦。"""
    surface = _hook_platform(kwargs)
    return bool(surface) and surface != "feishu"


def _note_session_route(session_id: str, engine: ChatCardEngine, session: Any) -> None:
    if not session_id:
        return
    now = time.time()
    with _ENGINES_LOCK:
        # 顺带清扫过期项（懒清理，防无界增长）
        stale = [k for k, v in _SESSION_ROUTE.items() if now - v[3] > _SESSION_ROUTE_TTL]
        for k in stale:
            _SESSION_ROUTE.pop(k, None)
        if len(_SESSION_ROUTE) > 256:
            _SESSION_ROUTE.clear()
        _SESSION_ROUTE[session_id] = (engine, session.chat_id, session.thread_id, now)


def _bind_session_route(session_id: str) -> tuple[ChatCardEngine, str, str | None] | None:
    """把 hermes session_id 绑到当前 profile 唯一的进行中会话；返回路由三元组.

    「本 profile 恰好一个进行中会话」时（重新）登记——同回合多次 API 调用借此
    续期并跟随 followup/redirect 换出的新会话对象；歧义（同 profile 主聊+话题
    并发）时复用既有有效路由，没有则返回 None——宁可不路由，也不猜。
    """
    session_id = str(session_id or "")
    if not session_id:
        return None
    hit = _single_open_session(_scope_engines())
    if hit is not None:
        _note_session_route(session_id, hit[0], hit[1])
        return hit[0], hit[1].chat_id, hit[1].thread_id
    return _route_by_session(session_id)


def _route_by_session(session_id: str) -> tuple[ChatCardEngine, str, str | None] | None:
    if not session_id:
        return None
    now = time.time()
    with _ENGINES_LOCK:
        hit = _SESSION_ROUTE.get(session_id)
    if hit and now - hit[3] <= _SESSION_ROUTE_TTL:
        return hit[0], hit[1], hit[2]
    return None


def _model_cycle_config() -> list[str]:
    """footer 模型切换按钮的轮换清单.

    显式 `streaming.footer.model_cycle: [a, b]` 优先；缺省从同目录 config.yaml
    的 `model.default` + `fallback_providers[].model` 推导（去重保序）。
    """
    explicit = _footer_config("model_cycle")
    if isinstance(explicit, list) and explicit:
        return [str(m).strip() for m in explicit if str(m).strip()]
    try:
        from ._vendor.config import _config_path, _safe_yaml_load

        raw = _safe_yaml_load(_config_path().read_text(encoding="utf-8"))
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


def _panel_expanded_config(key: str) -> bool:
    """读 streaming.tool_panel_expanded / reasoning_panel_expanded（缺省回退 panel_expanded）."""
    try:
        from ._vendor.config import Config

        return bool(getattr(Config(), key))
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


def _official_feishu_env_bridge() -> tuple[tuple, tuple]:
    """官方 feishu 的 env→config 启用桥（ENV_OVERRIDES 步骤 + 凭据名清单）.

    新版 hermes（2026-10-22 bf0203a 一线）平台启用由条目自带 env 桥驱动：config
    pass 跑 EnvCred 步骤把 .env 凭据写进 platforms.feishu.extra 并在凭据在场时
    启用平台。同名顶替条目不透传 → 凭据门 is_connected（看 extra.app_id）恒
    False → feishu 永不启用、适配器不建，且同名条目还屏蔽 bundled 的 env
    fallback（2026-11-02 升级实测 Gateway running with 1 platform(s)）。
    官方模块不可导入（老版本 hermes）时回退空值——老版本启用不依赖此面。
    """
    try:
        from plugins.platforms.feishu import gateway_env as official_env
    except Exception:
        return (), ()
    return (tuple(getattr(official_env, "ENV_OVERRIDES", ()) or ()),
            tuple(getattr(official_env, "ENV_ENABLE_CREDENTIALS", ()) or ()))


def _platform_entry_field_names() -> set[str]:
    """运行时 PlatformEntry 的字段名集合（跨版本能力探测；不可导入时为空）.

    ctx.register_platform 对未知 kwargs 抛 TypeError——env 桥字段只在运行时
    PlatformEntry 真有这些字段时透传，老版本 hermes 上保持原有条目形状。
    """
    try:
        import dataclasses

        from gateway.platform_registry import PlatformEntry
        return {f.name for f in dataclasses.fields(PlatformEntry)}
    except Exception:
        return set()


def _make_reasoning_hook() -> Any:
    dropped = {"n": 0}

    def on_stream_delta(**kwargs: Any) -> None:
        if kwargs.get("kind") != "reasoning":
            return
        # 官方 enqueue 参数名是 delta（stream_delivery._enqueue_stream_hook），非 text
        text = kwargs.get("delta") or kwargs.get("text") or ""
        if not text:
            return
        if _non_feishu_turn(kwargs):
            # cron/子代理/其它平台回合的思考不进飞书卡（surface=agent.platform）
            return
        # session_id 精确路由（pre_api_request / usage 登记的映射）优先；未登记
        # （同 profile 并发歧义、hook 早于回合建会话）回退「本 profile 恰好一个
        # 进行中会话」兜底——含 creating：建卡窗口内的思考此前被无声丢弃
        session_id = str(kwargs.get("session_id") or "")
        routed = _route_by_session(session_id)
        if routed is not None:
            # strict：命中即只进自己的会话（chat+thread 键），目标不活跃就丢弃
            routed[0].on_reasoning(routed[1], text, strict=True, thread_id=routed[2])
            return
        hit = _single_open_session(_scope_engines())
        if hit is not None:
            hit[0].on_reasoning(hit[1].chat_id, text, thread_id=hit[1].thread_id)
            return
        opens = [(e, e.open_sessions()) for e in _scope_engines()]
        opens = [(e, s) for e, s in opens if s]
        if not opens:
            # 零活跃卡片会话（CLI/cron/其它平台回合，或卡片均已收尾）——没有卡片可丢，
            # 属无损忽略：计入 dropped 只会刷日志（实测非飞书长回合 4000+ 条 INFO 噪音）。
            # 不变量：只有「存在活跃卡片却没路由上」的丢弃才需要可见。
            return
        # 多会话并发时防串扰丢弃——但要可见（曾静默丢成「折叠条消失」）
        dropped["n"] += 1
        if dropped["n"] == 1 or dropped["n"] % 50 == 0:
            logging.getLogger("gateway.run").info(
                "[feishu-streaming] reasoning dropped (multi-session): "
                "engines=%d sessions=%s total_dropped=%d",
                len(opens), [len(s) for _, s in opens], dropped["n"])

    return on_stream_delta


def _make_pre_api_hook() -> Any:
    """pre_api_request：首个 reasoning 增量到达**之前**把 session_id 绑到进行中会话.

    旧实现只在 post_api_request（API 调用结束）登记 + 10 分钟 TTL：用户歇过
    TTL 再发消息，新回合首个 API 调用的思考到达时路由已过期，只能走全局单活跃
    兜底——多 profile 下别的 engine 只要有进行中会话就整体丢弃（2026-11-19~24
    两机 `reasoning dropped (multi-session)` 数百次的主因）。pre_api_request 在
    每次 API 调用前同步触发，此时回合会话已由探针建好。
    """

    def on_pre_api_request(**kwargs: Any) -> None:
        if _non_feishu_turn(kwargs):
            return
        _bind_session_route(kwargs.get("session_id") or "")

    return on_pre_api_request


def _make_usage_hook() -> Any:
    logged: set[str] = set()

    def on_post_api_request(**kwargs: Any) -> None:
        usage = kwargs.get("usage")
        if not (isinstance(usage, dict)):
            return
        if _non_feishu_turn(kwargs):
            # cron/子代理的 usage 不进卡片 footer（否则落 "" 桶，被下一张完成卡误取）
            return
        session_id = kwargs.get("session_id") or ""
        scoped = _scope_engines()
        routed = _bind_session_route(session_id)
        chat_id = routed[1] if routed is not None else ""
        engine = (routed[0] if routed is not None
                  else _route_usage_engine(scoped, session_id))
        if engine is None:
            return
        usage = {**usage, "context_length": kwargs.get("context_length"),
                 "started_at": kwargs.get("started_at"),
                 "ended_at": kwargs.get("ended_at")}
        # 路由命中时按路由的 chat 归桶：date_hash 形 session_id 解析不出 chat，
        # 旧实现一律落 "" 桶，同 profile 两个并发回合的 usage 会混在一起
        engine.record_usage(session_id, usage, model=kwargs.get("model") or "",
                            chat_id=chat_id)
        if session_id not in logged:  # 每回合桶首条打一次（确认钩子活性）
            logged.add(session_id)
            logging.getLogger("gateway.run").info(
                "[feishu-streaming] usage tracking started: session=%s model=%s",
                session_id[:40], kwargs.get("model") or "?")

    return on_post_api_request


def _route_usage_engine(engines: list[ChatCardEngine], session_id: str) -> ChatCardEngine | None:
    """session key 里的 chat → 拥有该 chat 会话的 engine；无命中回退唯一 engine.

    multiplex（≥2 engine）下 agent.session_id 是 date_hash 形、不含 chat——单 engine
    兜底失效即「footer 只剩时间」回归的根因。入参已是**当前 profile 作用域内**的
    engine（_scope_engines）：单 profile 作用域恒为 1 个，直接采用；未标记作用域
    （单测/老 engine）时保留「恰好一个 engine 有进行中回合」的旧兜底。
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
