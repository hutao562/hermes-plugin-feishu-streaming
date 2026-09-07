"""CLI 入口: python -m hermes_lark_streaming [install|uninstall|status|verify]。"""

from __future__ import annotations

import json
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
    print("  install    Apply AST patch to gateway/run.py and cron/scheduler.py")
    print("  uninstall  Remove AST patch")
    print("  restore    Restore from backup")
    print("  status     Show current patch status")
    print("  verify     Verify compatibility without patching")


# 插件名（hermes_agent.plugins entry point key，用于 config.yaml 的 plugins.enabled）。
_PLUGIN_KEY = "hermes-lark-streaming"


def _set_plugin_enabled(enable: bool) -> None:
    """把本插件加入/移出 config.yaml 的 plugins.enabled（JSON 数组字符串行）.

    保持其余 config 原样，只改 plugins 段下的 ``enabled:`` 那一行。启用自愈
    register() 必须把插件列在 plugins.enabled（entry-point 插件默认 opt-in）。
    用缩进感知的块遍历定位 plugins: 顶层段，避免误改其它段的 enabled:。
    """
    cfg_path = hermes_home() / "config.yaml"
    if not cfg_path.exists():
        return
    text = cfg_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    changed = False
    in_plugins = False  # 是否在顶层 plugins: 段内
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key = line.lstrip().split(":", 1)[0].strip()
        # 顶层（indent==0）非 plugins key → 离开 plugins 段
        if indent == 0:
            in_plugins = key == "plugins"
            continue
        if not in_plugins:
            continue
        # plugins 段内的直接子键（indent==2，只匹配第一个 enabled:）
        if indent == 2 and key == "enabled":
            prefix_match = re.match(r"(\s*enabled:\s*)", line)
            if prefix_match is None:
                break
            prefix = prefix_match.group(1)
            items = _parse_enabled(line)
            if enable:
                if _PLUGIN_KEY not in items:
                    items.append(_PLUGIN_KEY)
                    changed = True
            else:
                if _PLUGIN_KEY in items:
                    items = [x for x in items if x != _PLUGIN_KEY]
                    changed = True
            if changed:
                # 写成 YAML flow list（不加外层引号，否则 Hermes 读成字符串）。
                # 用 yaml.safe_dump 保证引号/转义正确（含特殊字符的插件名）。
                import yaml as _yaml

                rendered = _yaml.safe_dump(items, default_flow_style=True).strip()
                lines[i] = f"{prefix}{rendered}\n"
            break

    if changed:
        cfg_path.write_text("".join(lines), encoding="utf-8")


def _parse_enabled(line: str) -> list[str]:
    """解析 plugins.enabled 那一行的值（兼容 YAML flow list、JSON 字符串、旧引号格式）."""
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

    if patcher.is_fully_patched():
        print("Already patched.")
    else:
        print("Verifying target compatibility...")
        try:
            patcher.verify_target()
        except Exception as e:
            print(f"Verification failed: {e}")
            return 1
        print("Target compatible.")

        print("Applying patch...")
        try:
            patcher.apply()
        except Exception as e:
            print(f"Patch failed: {e}")
            return 1
        print("Patch applied successfully.")

    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None and not cron_patcher.is_patched():
        try:
            cron_patcher.verify_target()
            cron_patcher.apply()
            print("Cron hook applied.")
        except Exception as e:
            print(f"Cron hook skipped: {e}")

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

    _uninstall_watchdog()
    try:
        _set_plugin_enabled(False)
        print(f"Plugin disabled in config (plugins.enabled -= {_PLUGIN_KEY}).")
    except Exception as e:
        print(f"Config disable skipped: {e}")

    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None and cron_patcher.is_patched():
        try:
            cron_patcher.remove()
            print("Cron hook removed.")
        except Exception as e:
            print(f"Cron hook remove failed: {e}")

    if not patcher.is_patched():
        print("Not patched.")
        return 0

    print("Removing patch...")
    try:
        patcher.remove()
    except Exception as e:
        print(f"Remove failed: {e}")
        return 1
    print("Patch removed.")
    return 0


def _cmd_restore() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None:
        try:
            cron_patcher.restore()
            print("Cron hook restored.")
        except Exception:
            pass

    print("Restoring from backup...")
    try:
        patcher.restore()
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
        from .patcher import Patcher as _PatcherCls

        content = patcher.run_path.read_text(encoding="utf-8")
        for begin, _end in _PatcherCls.MARKERS:
            found = begin in content
            label = begin.replace("# HERMES_LARK_", "").replace("_BEGIN", "").lower()
            print(f"  {label}: {'installed' if found else 'missing'}")

    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None:
        print(f"Cron hook: {'installed' if cron_patcher.is_patched() else 'not installed'}")

    # Check config
    from .config import Config

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
