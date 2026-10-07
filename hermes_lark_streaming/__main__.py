"""CLI 入口: python -m hermes_lark_streaming [install|uninstall|status|verify]。"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from .config import hermes_home

if TYPE_CHECKING:
    from .patcher import CronPatcher, Patcher


def main() -> int:
    args = sys.argv[1:]
    if not args:
        _print_usage()
        return 0

    cmd = args[0]
    commands = _commands()
    handler = commands.get(cmd)
    if handler is not None:
        return handler()

    print(f"Unknown command: {cmd}")
    _print_usage()
    return 1


def _commands() -> dict[str, Callable[[], int]]:
    return {
        "install": _cmd_install,
        "uninstall": _cmd_uninstall,
        "restore": _cmd_restore,
        "status": _cmd_status,
        "verify": _cmd_verify,
    }


def _print_usage() -> None:
    print("Usage: python -m hermes_lark_streaming <command>")
    print()
    print("Commands:")
    print("  install    Apply AST hooks to Hermes gateway and cron modules")
    print("  uninstall  Remove AST patch")
    print("  restore    Restore from backup")
    print("  status     Show current patch status")
    print("  verify     Verify compatibility without patching")


# 插件名（hermes_agent.plugins entry point key，用于 config.yaml 的 plugins.enabled）。
_PLUGIN_KEY = "hermes-lark-streaming"


def _set_plugin_enabled(enable: bool) -> None:
    """把本插件加入/移出 config.yaml 的 plugins.enabled。

    保持其余 config 原样，只改 plugins 段下的 ``enabled:``。启用自愈
    register() 必须把插件列在 plugins.enabled（entry-point 插件默认 opt-in）。

    支持两种 YAML 形态（实测踩坑，2026-09-07）：
      1. flow list 单行:  enabled: [a, b]
      2. block list 多行: enabled: 换行 + 每行 "    - item"
    旧实现只解析单行，把 block list 读成 [] 后整体压扁成
    "enabled: [hermes-lark-streaming]"，丢掉原有 5 个插件并可能残留
    顶格 "[hermes-lark-streaming]" 垃圾行（YAML 解析失败 → 全配置失效）。
    """
    cfg_path = hermes_home() / "config.yaml"
    if not cfg_path.exists():
        return
    text = cfg_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    def _indent(s: str) -> int:
        return len(s) - len(s.lstrip())

    in_plugins = False
    enabled_line_idx: int | None = None
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = _indent(line)
        key = line.lstrip().split(":", 1)[0].strip()
        if indent == 0:
            in_plugins = key == "plugins"
            continue
        if not in_plugins:
            continue
        if indent == 2 and key == "enabled":
            enabled_line_idx = i
            break
    if enabled_line_idx is None:
        return

    enabled_indent = _indent(lines[enabled_line_idx])
    # 收集 items：先读 enabled: 行内联值（flow list），再收后续 "- item" 块
    items: list[str] = []
    inline = lines[enabled_line_idx].split(":", 1)[1] if ":" in lines[enabled_line_idx] else ""
    if inline.strip():
        import yaml as _yaml

        parsed_items: list[str] = []
        try:
            parsed = _yaml.safe_load(inline)
            if isinstance(parsed, list):
                parsed_items = [str(x) for x in parsed]
            elif isinstance(parsed, str):
                # 历史格式：单引号包裹的 JSON 字符串行（'["a", "b"]'）→ 剥引号再解析。
                stripped = parsed.strip()
                if stripped.startswith(("[", "{")):
                    try:
                        nested = _yaml.safe_load(stripped)
                        if isinstance(nested, list):
                            parsed_items = [str(x) for x in nested]
                    except _yaml.YAMLError:
                        pass
        except _yaml.YAMLError:
            parsed_items = []
        items = parsed_items
    # 收集 block items：缩进 > enabled 行缩进 且以 "- " 开头
    block_end = enabled_line_idx + 1
    item_indent: int | None = None
    while block_end < len(lines):
        nxt = lines[block_end]
        if not nxt.strip() or nxt.lstrip().startswith("#"):
            block_end += 1
            continue
        nind = _indent(nxt)
        if nind <= enabled_indent:
            break
        if item_indent is None:
            item_indent = nind
        if nxt.strip().startswith("- "):
            raw = nxt.strip()[2:].strip()
            if raw:
                items.append(raw.strip("'\"") if raw.startswith(("'", "\"")) else raw)
        block_end += 1

    changed = False
    if enable:
        if _PLUGIN_KEY not in items:
            items.append(_PLUGIN_KEY)
            changed = True
    else:
        if _PLUGIN_KEY in items:
            items = [x for x in items if x != _PLUGIN_KEY]
            changed = True
    if not changed:
        return

    # 保留原形态：原是 block list 就写 block list，原是 flow list 就写 flow list。
    import yaml as _yaml

    had_block = any(
        _indent(lines[j]) > enabled_indent and lines[j].strip().startswith("- ")
        for j in range(enabled_line_idx + 1, block_end)
        if lines[j].strip()
    )
    prefix = lines[enabled_line_idx][:enabled_indent]
    if had_block or (not inline.strip() and block_end > enabled_line_idx + 1):
        # 重写为 block list：enabled: 空 + 每行 "- item"
        new_block = [f"{prefix}enabled:\n"]
        item_prefix = prefix + "    "
        for it in items:
            # 直接渲染简单字符串；含特殊字符才走 yaml（safe_dump 单值会带
            # 文档结束符 "...\n"，实测 2026-09-07 污染 config 的元凶之一）。
            if _is_simple_yaml_scalar(it):
                new_block.append(f"{item_prefix}- {it}\n")
            else:
                rendered = _yaml.safe_dump([it], default_flow_style=False).strip()
                new_block.append(f"{item_prefix}{rendered}\n")
        # 删除旧 block items 行（含 enabled 行）
        del lines[enabled_line_idx:block_end]
        lines[enabled_line_idx:enabled_line_idx] = new_block
    else:
        rendered = _yaml.safe_dump(items, default_flow_style=True).strip()
        lines[enabled_line_idx] = f"{prefix}enabled: {rendered}\n"
        del lines[enabled_line_idx + 1:block_end]

    cfg_path.write_text("".join(lines), encoding="utf-8")


def _is_simple_yaml_scalar(value: str) -> bool:
    """判断字符串能否安全地无引号写入 YAML block list item（插件名/id 均为简单标识符）。"""
    if not value:
        return False
    # 避免与 YAML 保留字/特殊结构冲突
    if value in {"true", "false", "null", "yes", "no", "on", "off", "~", "None", "True", "False"}:
        return False
    if value.lstrip().startswith(
        ("-", "?", ":", "[", "]", "{", "}", "#", "&", "*", "!", "|", ">", "@", "`", '"', "'", "%")
    ):
        return False
    if ":" in value and not value.startswith("http"):
        return False
    return not any(ch.isspace() for ch in value)


def _parse_enabled(line: str) -> list[str]:
    m = re.match(r"\s*enabled:\s*(\S.*)", line)
    if not m:
        return []
    val = m.group(1).strip()
    import yaml as _yaml

    # 优先 YAML 解析（能处理 flow list [a, b] 和引号 JSON '["a","b"]'）
    try:
        items = _yaml.safe_load(val)
        if isinstance(items, list):
            return [str(x) for x in items]
    except _yaml.YAMLError:
        pass
    # 回退：剥引号后 JSON
    try:
        items = json.loads(val.strip("'\""))
        return [str(x) for x in items] if isinstance(items, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def _install_watchdog() -> None:
    """装 launchd WatchPaths 守护（run.py 变化即自动重打 + 重启）。失败只打印."""
    try:
        from . import watchdog

        if watchdog.install():
            print(f"Watchdog installed ({watchdog.LABEL}) — auto re-patch on run.py change.")
        else:
            print("Watchdog skipped (launchctl unavailable).")
    except Exception as e:
        print(f"Watchdog install skipped: {e}")


def _uninstall_watchdog() -> None:
    try:
        from . import watchdog

        watchdog.uninstall()
        print("Watchdog removed.")
    except Exception as e:
        print(f"Watchdog remove skipped: {e}")


def _check_runtime_hooks_loaded(run_py: Path) -> str | None:
    """检测运行中的网关是否加载了补丁版 run.py.

    补丁打了但运行中的进程是补丁前启动的（升级后补丁是后打的）→ 返回警告串。
    比较最近 ``gateway run`` 进程的启动时间与 run.py mtime。
    """
    import subprocess

    try:
        result = subprocess.run(
            ["pgrep", "-lf", "hermes_cli.main gateway run"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None  # 没有运行中的网关，无从比较
        # 取第一个 PID 的启动时间
        first_pid = result.stdout.splitlines()[0].split()[0]
        ps = subprocess.run(
            ["ps", "-o", "lstart=", "-p", first_pid],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ps.returncode != 0 or not ps.stdout.strip():
            return None
        import time

        proc_start = time.mktime(time.strptime(ps.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
        run_mtime = run_py.stat().st_mtime
        if run_mtime > proc_start:
            return (
                f"  ⚠️  run.py patched ({time.strftime('%H:%M:%S', time.localtime(run_mtime))}) "
                f"AFTER gateway started ({time.strftime('%H:%M:%S', time.localtime(proc_start))})\n"
                f"      hooks patched but NOT loaded by running gateway — restart needed:\n"
                f"      hermes gateway restart"
            )
    except Exception:
        pass
    return None


def _get_patcher() -> Patcher | None:
    from .patcher import Patcher, PatcherError

    try:
        return Patcher()
    except PatcherError as e:
        print(f"Error: {e}")
        return None


def _get_cron_patcher() -> CronPatcher | None:
    from .patcher import CronPatcher, PatcherError

    try:
        return CronPatcher()
    except PatcherError:
        return None


def _cmd_install() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    from .patcher import install_patchers

    patchers: list[Patcher | CronPatcher] = [patcher]
    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None:
        patchers.append(cron_patcher)
    print("Verifying and preparing all gateway/cron hooks before writing...")
    try:
        install_patchers(patchers)
    except Exception as e:
        print(f"Patch failed: {e}")
        return 1
    print("Hooks installed. Restart Hermes Gateway to load the changes.")

    # 启用插件（entry-point 插件 opt-in，否则 register() 自愈不会被调用）
    try:
        _set_plugin_enabled(True)
        print(f"Plugin enabled in config (plugins.enabled += {_PLUGIN_KEY}).")
    except Exception as e:
        print(f"Config enable skipped: {e}")

    _install_watchdog()
    return 0


def _cmd_uninstall() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    from .patcher import _write_changes

    _uninstall_watchdog()
    try:
        _set_plugin_enabled(False)
        print(f"Plugin disabled in config (plugins.enabled -= {_PLUGIN_KEY}).")
    except Exception as e:
        print(f"Config disable skipped: {e}")

    if not patcher.is_patched():
        print("Not patched.")
        return 0

    print("Removing patch...")
    try:
        changes = patcher.prepare_remove()
        cron_patcher = _get_cron_patcher()
        if cron_patcher is not None:
            changes.update(cron_patcher.prepare_remove())
        _write_changes(changes)
    except Exception as e:
        print(f"Remove failed: {e}")
        return 1
    print("Patch removed.")
    return 0


def _cmd_restore() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    from .patcher import _BACKUP_SUFFIX, _write_changes

    print("Restoring from backup...")
    try:
        changes = patcher.prepare_restore()
        cron_patcher = _get_cron_patcher()
        if cron_patcher is not None:
            backup = cron_patcher.cron_path.with_suffix(cron_patcher.cron_path.suffix + _BACKUP_SUFFIX)
            if backup.exists() or cron_patcher.is_patched():
                changes.update(cron_patcher.prepare_restore())
        _write_changes(changes)
    except Exception as e:
        print(f"Restore failed: {e}")
        return 1
    print("Restored.")
    return 0


def _cmd_status() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    patched = patcher.is_patched()
    print(f"Patched: {'yes' if patched else 'no'}")
    print(f"Target:  {patcher.run_path}")
    print("Layout:  split gateway modules")

    # 插件是否在 venv 可 import（升级重建 venv 会丢 editable 安装 —— 即便 patch 打了
    # gateway 进程也 ImportError，streaming 静默失效。这是最关键的诊断项）。
    try:
        from ._source import source_dir, venv_has_plugin

        if venv_has_plugin():
            print("Plugin in venv: yes")
        else:
            src = source_dir()
            hint = f"pip install -e {src}" if src else "pip install -e <plugin source>"
            print(f"Plugin in venv: NO  (gateway will ImportError hooks — run: {hint})")
    except Exception as e:
        print(f"Plugin in venv: unknown ({e})")

    if patched:
        print(f"Fully patched: {'yes' if patcher.is_fully_patched() else 'no'}")
    for path in patcher.target_paths:
        if not path.exists():
            print(f"  {path.name}: MISSING FILE")
            continue
        content = path.read_text(encoding="utf-8")
        labels = [
            begin.replace("# HERMES_LARK_", "").replace("_BEGIN", "").lower()
            for begin, _end in patcher.MARKERS if begin in content
        ]
        print(f"  {path.name}: {', '.join(labels) if labels else 'no hooks'}")

    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None:
        print(f"Cron hook: {'installed' if cron_patcher.is_patched() else 'not installed'}")
        print(f"Cron target: {cron_patcher.cron_path}")
        if cron_patcher.is_patched():
            print(f"Cron fully patched: {'yes' if cron_patcher.is_fully_patched() else 'no'}")

    # Check config
    from .config import Config

    # Since Hermes v0.20.6 the CLI runs in a subprocess without the gateway process's
    # environment, so _get_secret cannot see FEISHU_APP_ID here. Source ~/.hermes/.env
    # manually; setdefault keeps already-exported variables authoritative.
    _env_file = Path.home() / ".hermes" / ".env"
    if _env_file.exists():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

    cfg = Config()
    print(f"Config streaming.enabled: {cfg.enabled}")
    print(f"Config streaming.self_heal: {cfg.self_heal}")
    # 凭据：env 优先，再读 ~/.hermes/.env（status 常在非网关 shell 跑，env 里没有）
    cred_ok = bool(cfg.env_app_id)
    if not cred_ok:
        env_file = hermes_home() / ".env"
        if env_file.exists():
            for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if raw.strip().startswith("FEISHU_APP_ID=") and len(raw.split("=", 1)[1].strip().strip("'\"")) > 0:
                    cred_ok = True
                    break
    print(f"Feishu credentials: {'configured' if cred_ok else 'MISSING'}")

    # 运行时检测：补丁打了但运行中的网关没加载（升级后补丁是后打的）→ 提示 restart
    runtime_warning = _check_runtime_hooks_loaded(patcher.run_path)
    if runtime_warning:
        print(runtime_warning)

    # 守护状态
    try:
        from . import watchdog

        ws = watchdog.status()
        wd_state = "installed" if ws.installed else "not installed"
        if ws.installed:
            wd_state += f", {'loaded' if ws.loaded else 'NOT loaded'}"
        print(f"Watchdog: {wd_state}")
    except Exception:
        pass

    # Python interpreter check
    from .patcher import hermes_install_dir, hermes_python

    expected_py = hermes_python()
    if expected_py is not None:
        print(f"Hermes Python: {expected_py}")
        current = Path(sys.executable).resolve()
        if current != expected_py.resolve():
            print(f"  warning: running under {current}, but Hermes uses {expected_py}")
            print(f"  rerun commands with: {expected_py} -m hermes_lark_streaming ...")

    install_dir = hermes_install_dir()
    if install_dir is not None:
        print(f"Hermes install dir: {install_dir}")
    return 0


def _cmd_verify() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    print(f"Target: {patcher.run_path}")
    print("Layout: split gateway modules")
    for path in patcher.target_paths[1:]:
        print(f"  {path}")
    print("Checking compatibility...")
    try:
        patcher.verify_target()
    except Exception as e:
        print(f"Incompatible: {e}")
        return 1
    print("Compatible.")

    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None:
        print(f"Cron target: {cron_patcher.cron_path}")
        try:
            cron_patcher.verify_target()
        except Exception as e:
            print(f"Cron incompatible: {e}")
            return 1
        print("Cron target compatible.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
