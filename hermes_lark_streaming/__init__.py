__version__ = "0.12.0"


def _run_self_heal() -> bool:
    """启动时检测并重打 AST hook（hermes 升级覆盖 run.py 后的自动恢复）.

    返回 True 表示磁盘补丁状态与本进程不一致，调用方应触发 gateway restart
    让补丁版重新加载。两种情形返回 True：
      a) run.py 未完整打补丁（升级抹掉了 hook）→ 原地重打成功；
      b) run.py 已打补丁，但本进程启动时间早于 run.py mtime（补丁是后打的）。
    防无限 restart 循环：情形 a 重打后磁盘变完整，下次启动走情形 b 的 mtime
    比较只在「补丁比进程新」时返回 True；restart 后新进程启动时间 > run.py mtime → 不再触发。
    """
    import logging

    log = logging.getLogger("gateway.run")
    try:
        from .patcher import Patcher, PatcherError

        patcher = Patcher()
    except PatcherError as e:
        log.warning("[hermes-lark] self-heal: cannot locate run.py: %s", e)
        return False
    except Exception as e:
        log.warning("[hermes-lark] self-heal init error: %s", e)
        return False

    # 插件可用性检测（必须在 run.py 补丁检测之前）：hermes 升级重建 venv 会丢掉
    # editable 安装，导致 entry_point 消失 → discover_plugins() 扫不到本插件 →
    # 即便 run.py 补丁在、AST hook 注入了 import，gateway 进程也 ImportError（被
    # try/except 吞掉）→ streaming 静默失效。此处发现插件丢失则自动 pip install -e
    # 并触发 restart 让新装的插件被加载。
    try:
        from . import _source

        if not _source.venv_has_plugin():
            log.warning("[hermes-lark] self-heal: plugin missing from venv, re-installing")
            if _source.reinstall_into_venv():
                log.info("[hermes-lark] self-heal: plugin re-installed — restarting to load it")
                return True
            log.warning("[hermes-lark] self-heal: re-install failed — continuing with run.py check")
    except Exception as e:
        log.warning("[hermes-lark] self-heal: plugin-availability check error: %s", e)

    proc_start = _proc_start_time()

    if patcher.is_fully_patched():
        # 磁盘已是完整补丁态。判断当前进程是否加载了补丁版：比较 run.py mtime
        # 与本进程启动时间。若 run.py 在进程启动后被改过 → 补丁是后打的 → restart。
        try:
            run_mtime = patcher.run_path.stat().st_mtime
            if proc_start and run_mtime > proc_start:
                log.warning(
                    "[hermes-lark] run.py patched (%.0f) after this process started (%.0f) "
                    "— hooks NOT loaded by running gateway; restarting to load them",
                    run_mtime,
                    proc_start,
                )
                return True
        except OSError:
            pass
        return False

    # 磁盘未完整打补丁（升级抹掉了 hook）→ 原地重打。
    log.info("[hermes-lark] self-heal: run.py not fully patched, re-applying hooks")
    try:
        patcher.verify_target()
        patcher.apply()
    except Exception as e:
        log.warning(
            "[hermes-lark] self-heal: re-patch failed (hermes may be incompatible): %s", e
        )
        return False
    log.info("[hermes-lark] self-heal: hooks re-applied successfully")

    # 同步 cron hook（失败不阻塞）。
    try:
        from .patcher import CronPatcher

        cron = CronPatcher()
        if not cron.is_patched():
            cron.verify_target()
            cron.apply()
            log.info("[hermes-lark] self-heal: cron hook re-applied")
    except Exception:
        pass

    return True


def _proc_start_time() -> float:
    """本进程的启动时间（epoch 秒）。macOS 用 ps，失败回退 env 标记."""
    import os
    import subprocess

    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            import time

            # ps lstart 格式："Thu Jul 9 20:55:15 2026"
            return time.mktime(time.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
    except Exception:
        pass
    return 0.0


def _maybe_restart_gateway() -> None:
    """自愈后当前进程内存里仍是旧 run.py → 触发 gateway restart 让补丁版重新加载.

    优先用 Hermes 自带的 ``gateway restart``（最可靠，跨 launchd/systemd/手动），
    失败回退 ``launchctl kickstart``。用 detached ``nohup sleep 2 && ...`` 延迟触发，
    让 register() 先返回、本进程优雅退出，再由服务管理器拉起补丁版。
    """
    import logging
    import os
    import shlex
    import shutil
    import subprocess

    from .patcher import hermes_python

    log = logging.getLogger("gateway.run")
    py = hermes_python()
    if py is None or shutil.which("nohup") is None:
        log.warning(
            "[hermes-lark] self-heal re-patched run.py but cannot locate hermes python — "
            "restart gateway manually to load patched hooks"
        )
        return

    # 构造重启命令：优先 hermes gateway restart（hermes_cli），回退 launchctl kickstart。
    py_str = str(py)
    label = os.environ.get("HERMES_LARK_LAUNCHD_LABEL", "ai.hermes.gateway")
    restart_cmd = f'"{py_str}" -m hermes_cli.main gateway restart'
    if shutil.which("launchctl") is not None:
        restart_cmd += f' || launchctl kickstart gui/{os.getuid()}/{label}'
    inner = shlex.quote(f"sleep 2 && {restart_cmd}")
    cmd = f"nohup sh -c {inner} >/dev/null 2>&1 &"
    try:
        subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("[hermes-lark] self-heal: gateway restart scheduled in 2s")
    except Exception as e:
        log.warning("[hermes-lark] self-heal restart-schedule error: %s — restart manually", e)


def register(ctx) -> None:  # type: ignore[no-untyped-def]
    """Hermes 插件入口（hermes_agent.plugins entry point）.

    网关每次启动（含 hermes 升级后自动 restart）都会经 discover_plugins() 调到这里。
    用途：启动时自愈——检测 run.py 的 AST hook 是否被升级抹掉，是则原地重打 +
    触发 restart 让补丁版重新加载（解决「升级后补丁打了但进程没加载」的失效）。

    配置开关 streaming.self_heal（默认 True）关闭后跳过自愈，回退手动 reinstall。
    """
    import logging

    log = logging.getLogger("gateway.run")
    try:
        from .config import Config

        if not Config().self_heal:
            log.debug("[hermes-lark] self-heal disabled by config (streaming.self_heal=false)")
            return
    except Exception:
        pass  # 配置读取失败不阻断自愈

    try:
        if _run_self_heal():
            _maybe_restart_gateway()
    except Exception as e:
        log.warning("[hermes-lark] register() self-heal error: %s", e)
