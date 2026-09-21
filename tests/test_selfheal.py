"""Self-heal tests — register() 启动时自愈逻辑（mock Patcher，不碰真实 run.py）.

覆盖场景：
  - run.py 未完整打补丁（升级抹掉）→ verify+apply 重打 → 返回 True（需 restart）
  - run.py 已打补丁但进程启动早于 run.py mtime → 返回 True（补丁后打，需 restart）
  - run.py 已打补丁且进程新于 run.py → 返回 False（无需操作）
  - verify 失败（hermes 改了函数名）→ 降级返回 False（不 crash）
  - register() 在 self_heal=false 时不自愈
  - register() 任何异常都不抛出（绝不阻断网关启动）
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _fake_patcher(is_fully: bool, run_mtime: float, verify_ok: bool = True, apply_ok: bool = True):
    """构造一个 mock Patcher，控制 is_fully_patched / verify_target / apply 行为."""
    p = MagicMock()
    p.is_fully_patched.return_value = is_fully
    run_path = MagicMock()
    run_path.stat.return_value.st_mtime = run_mtime
    p.run_path = run_path
    if not verify_ok:
        p.verify_target.side_effect = RuntimeError("cannot find _handle_message_with_agent")
    if not apply_ok:
        p.apply.side_effect = RuntimeError("inject failed")
    return p


def _patch_patcher(patcher_mock):
    """patch hermes_lark_streaming.__init__ 内的 Patcher/PatcherError 引用."""
    return (
        patch("hermes_lark_streaming.patcher.Patcher", return_value=patcher_mock),
        patch("hermes_lark_streaming.patcher.PatcherError", RuntimeError, create=True),
    )


class TestRunSelfHeal:
    @pytest.fixture(autouse=True)
    def _venv_has_plugin_default(self):
        """默认模拟插件已在 venv（跳过重装段），让原有 run.py 检测逻辑可被单独测。

        需要测「插件丢失」场景的用例自行覆盖此 patch。
        """
        with patch("hermes_lark_streaming._source.venv_has_plugin", return_value=True):
            yield

    def test_unpatched_reapplies_and_signals_restart(self, tmp_path: Path) -> None:
        """run.py 未完整打补丁 → apply 重打 → 返回 True."""
        from hermes_lark_streaming import _run_self_heal

        p = _fake_patcher(is_fully=False, run_mtime=time.time() - 100)
        with patch("hermes_lark_streaming.patcher.Patcher", return_value=p), patch(
            "hermes_lark_streaming.patcher.PatcherError", RuntimeError, create=True
        ):
            result = _run_self_heal()
        assert result is True
        p.verify_target.assert_called_once()
        p.apply.assert_called_once()

    def test_patched_and_process_newer_returns_false(self, tmp_path: Path) -> None:
        """run.py 已打补丁且进程新于 run.py → 无需操作."""
        from hermes_lark_streaming import _run_self_heal

        # run.py mtime 在很早以前，进程刚启动 → 不需 restart
        p = _fake_patcher(is_fully=True, run_mtime=time.time() - 3600)
        with patch("hermes_lark_streaming.patcher.Patcher", return_value=p), patch(
            "hermes_lark_streaming.patcher.PatcherError", RuntimeError, create=True
        ), patch("hermes_lark_streaming._proc_start_time", return_value=time.time()):
            result = _run_self_heal()
        assert result is False
        p.apply.assert_not_called()

    def test_patched_but_run_py_newer_than_process_signals_restart(self) -> None:
        """run.py 已打补丁但 run.py mtime 晚于进程启动 → 补丁是后打的 → 需 restart."""
        from hermes_lark_streaming import _run_self_heal

        now = time.time()
        # 进程在 1 小时前启动，run.py 1 分钟前才被打补丁
        p = _fake_patcher(is_fully=True, run_mtime=now - 60)
        with patch("hermes_lark_streaming.patcher.Patcher", return_value=p), patch(
            "hermes_lark_streaming.patcher.PatcherError", RuntimeError, create=True
        ), patch("hermes_lark_streaming._proc_start_time", return_value=now - 3600):
            result = _run_self_heal()
        assert result is True
        p.apply.assert_not_called()  # 已打补丁，不再重打

    def test_verify_failure_degrades_gracefully(self) -> None:
        """verify_target 失败（hermes 改了函数名）→ 返回 False，不 crash."""
        from hermes_lark_streaming import _run_self_heal

        p = _fake_patcher(is_fully=False, run_mtime=time.time(), verify_ok=False)
        with patch("hermes_lark_streaming.patcher.Patcher", return_value=p), patch(
            "hermes_lark_streaming.patcher.PatcherError", RuntimeError, create=True
        ):
            result = _run_self_heal()
        assert result is False
        p.apply.assert_not_called()

    def test_plugin_missing_reinstalls_and_signals_restart(self) -> None:
        """插件不在 venv（升级重建 venv 丢了）→ reinstall → 返回 True 触发 restart."""
        from hermes_lark_streaming import _run_self_heal

        # run.py 已完整打补丁（隔离插件检测这一层）
        p = _fake_patcher(is_fully=True, run_mtime=time.time() - 3600)
        with patch("hermes_lark_streaming.patcher.Patcher", return_value=p), patch(
            "hermes_lark_streaming.patcher.PatcherError", RuntimeError, create=True
        ), patch(
            "hermes_lark_streaming._source.venv_has_plugin", return_value=False
        ), patch(
            "hermes_lark_streaming._source.reinstall_into_venv", return_value=True
        ) as reinstall:
            result = _run_self_heal()
        assert result is True
        reinstall.assert_called_once()
        p.apply.assert_not_called()  # 没走到 run.py 补丁逻辑

    def test_plugin_missing_reinstall_fails_falls_through_to_run_py_check(self) -> None:
        """插件丢失但 reinstall 失败 → 继续走 run.py 补丁检测（尽力而为）."""
        from hermes_lark_streaming import _run_self_heal

        # run.py 未打补丁 → 即便 reinstall 失败，也应走到 apply 重打
        p = _fake_patcher(is_fully=False, run_mtime=time.time() - 100)
        with patch("hermes_lark_streaming.patcher.Patcher", return_value=p), patch(
            "hermes_lark_streaming.patcher.PatcherError", RuntimeError, create=True
        ), patch(
            "hermes_lark_streaming._source.venv_has_plugin", return_value=False
        ), patch(
            "hermes_lark_streaming._source.reinstall_into_venv", return_value=False
        ):
            result = _run_self_heal()
        assert result is True  # run.py 重打成功 → 触发 restart
        p.apply.assert_called_once()


class TestRegister:
    def test_register_disabled_by_config_does_not_heal(self) -> None:
        """self_heal=false → register() 不调用 _run_self_heal."""
        from hermes_lark_streaming import register

        cfg = MagicMock()
        cfg.self_heal = False
        with patch("hermes_lark_streaming.config.Config", return_value=cfg), patch(
            "hermes_lark_streaming._run_self_heal"
        ) as heal, patch("hermes_lark_streaming._maybe_restart_gateway") as restart:
            register(MagicMock())
        heal.assert_not_called()
        restart.assert_not_called()

    def test_register_enabled_runs_self_heal(self) -> None:
        """self_heal=true → register() 调用 _run_self_heal + 条件 restart."""
        from hermes_lark_streaming import register

        cfg = MagicMock()
        cfg.self_heal = True
        with patch("hermes_lark_streaming.config.Config", return_value=cfg), patch(
            "hermes_lark_streaming._run_self_heal", return_value=True
        ) as heal, patch("hermes_lark_streaming._maybe_restart_gateway") as restart:
            register(MagicMock())
        heal.assert_called_once()
        restart.assert_called_once()

    def test_register_no_restart_when_heal_returns_false(self) -> None:
        """_run_self_heal 返回 False（无需操作）→ 不 restart."""
        from hermes_lark_streaming import register

        cfg = MagicMock()
        cfg.self_heal = True
        with patch("hermes_lark_streaming.config.Config", return_value=cfg), patch(
            "hermes_lark_streaming._run_self_heal", return_value=False
        ), patch("hermes_lark_streaming._maybe_restart_gateway") as restart:
            register(MagicMock())
        restart.assert_not_called()

    def test_register_never_raises(self) -> None:
        """register() 内任何异常都被吞掉（绝不阻断网关启动）."""
        from hermes_lark_streaming import register

        # Config 读取抛异常 → register 仍不应抛
        with patch("hermes_lark_streaming.config.Config", side_effect=RuntimeError("boom")):
            register(MagicMock())  # should not raise

    def test_register_swallows_self_heal_exception(self) -> None:
        """_run_self_heal 抛异常 → register 不抛（降级为不自愈）."""
        from hermes_lark_streaming import register

        cfg = MagicMock()
        cfg.self_heal = True
        with patch("hermes_lark_streaming.config.Config", return_value=cfg), patch(
            "hermes_lark_streaming._run_self_heal", side_effect=RuntimeError("heal boom")
        ):
            register(MagicMock())  # should not raise


class TestProcStartTime:
    def test_proc_start_time_returns_positive_float(self) -> None:
        """_proc_start_time 应返回正数（在 macOS 上用 ps）."""
        from hermes_lark_streaming import _proc_start_time

        ts = _proc_start_time()
        assert isinstance(ts, float)
        assert ts > 0  # ps 解析成功


class TestPluginConfigEnable:
    """plugins.enabled 编辑器：只改 plugins 段的 enabled 行，不动其它段的 enabled."""

    def _setup_config(self, tmp_path: Path, body: str) -> Path:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(body)
        import hermes_lark_streaming.config as cfgmod

        cfgmod.hermes_home = lambda: tmp_path
        import hermes_lark_streaming.__main__ as m

        m.hermes_home = cfgmod.hermes_home
        return cfg

    def test_enable_adds_plugin_key(self, tmp_path: Path) -> None:
        cfg = self._setup_config(
            tmp_path,
            "display:\n  enabled: true\nplugins:\n  enabled: '[\"disk-cleanup\"]'\n  disabled: []\n",
        )
        from hermes_lark_streaming.__main__ import _set_plugin_enabled

        _set_plugin_enabled(True)
        import yaml

        line = next(ln for ln in cfg.read_text().splitlines() if "disk-cleanup" in ln)
        val = yaml.safe_load(line.split(":", 1)[1].strip())
        assert "hermes-lark-streaming" in val

    def test_enable_idempotent_no_duplicate(self, tmp_path: Path) -> None:
        cfg = self._setup_config(
            tmp_path, "plugins:\n  enabled: '[\"hermes-lark-streaming\"]'\n"
        )
        from hermes_lark_streaming.__main__ import _set_plugin_enabled

        _set_plugin_enabled(True)  # already present
        import yaml

        line = next(ln for ln in cfg.read_text().splitlines() if "enabled:" in ln and "hermes" in ln)
        items = yaml.safe_load(line.split(":", 1)[1].strip())
        assert items.count("hermes-lark-streaming") == 1

    def test_disable_removes_plugin_key(self, tmp_path: Path) -> None:
        cfg = self._setup_config(
            tmp_path,
            "plugins:\n  enabled: '[\"disk-cleanup\", \"hermes-lark-streaming\"]'\n",
        )
        from hermes_lark_streaming.__main__ import _set_plugin_enabled

        _set_plugin_enabled(False)
        assert "hermes-lark-streaming" not in cfg.read_text()

    def test_does_not_touch_other_enabled_sections(self, tmp_path: Path) -> None:
        """display.enabled / footer.enabled 等不应被改动."""
        body = (
            "display:\n  enabled: true\n"
            "streaming:\n  footer:\n    enabled: true\n"
            "plugins:\n  enabled: '[\"disk-cleanup\"]'\n"
        )
        cfg = self._setup_config(tmp_path, body)
        from hermes_lark_streaming.__main__ import _set_plugin_enabled

        _set_plugin_enabled(True)
        new = cfg.read_text()
        # 其它 enabled 行原样
        assert "display:\n  enabled: true\n" in new
        assert "    enabled: true\n" in new  # footer.enabled


class TestSourceDir:
    """_source.source_dir / reinstall_into_venv."""

    def test_source_dir_locates_pyproject(self) -> None:
        """source_dir() 返回的路径含 pyproject.toml（真实本包安装位置）."""
        from hermes_lark_streaming._source import source_dir

        src = source_dir()
        assert src is not None
        assert (src / "pyproject.toml").exists()

    def test_source_dir_falls_back_to_file_parent(self, tmp_path: Path) -> None:
        """importlib.metadata 找不到时，回退 Path(__file__).parents[1]."""
        from hermes_lark_streaming import _source

        # distribution() 抛异常 → 走 __file__ 回退（本测试文件就在源码树内）
        with patch("importlib.metadata.distribution", side_effect=RuntimeError("nope")):
            src = _source.source_dir()
        assert src is not None
        assert (src / "pyproject.toml").exists()

    def test_reinstall_uses_tsinghua_mirror(self) -> None:
        """reinstall_into_venv 写死清华源（Clash 对 pypi 转发不通）."""
        from hermes_lark_streaming import _source

        fake_py = Path("/fake/venv/bin/python3")
        captured = MagicMock()
        captured.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch("hermes_lark_streaming.patcher.hermes_python", return_value=fake_py), patch(
            "subprocess.run", captured
        ):
            result = _source.reinstall_into_venv()

        assert result is True
        cmd = captured.call_args.args[0]
        # 命令含清华源 index + --no-build-isolation（避免 build 隔离拉 setuptools）
        assert "-i" in cmd
        assert "https://pypi.tuna.tsinghua.edu.cn/simple" in cmd
        assert "--no-build-isolation" in cmd
        # 目标是源码目录（editable）
        assert "-e" in cmd

    def test_reinstall_returns_false_when_pip_fails(self) -> None:
        """pip 返回非 0 → 返回 False（不抛，调用方降级）."""
        from hermes_lark_streaming import _source

        fake_py = Path("/fake/venv/bin/python3")
        with patch("hermes_lark_streaming.patcher.hermes_python", return_value=fake_py), patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="SSL error"),
        ):
            result = _source.reinstall_into_venv()
        assert result is False


class TestVenvHasPlugin:
    def test_venv_has_plugin_no_python_falls_back_to_sys_executable(self) -> None:
        """hermes python 定位不到 → 用 sys.executable 兜底（self-heal 跑在 gateway 进程内）."""
        from hermes_lark_streaming._source import venv_has_plugin

        with patch("hermes_lark_streaming.patcher.hermes_python", return_value=None):
            # fork 语义：兜底解释器即当前 venv 的 python（editable 安装可见）→ True
            assert venv_has_plugin() is True

    def test_venv_has_plugin_uses_neutral_cwd(self) -> None:
        """子进程 cwd 设为 /，避免 sys.path[0]='' 误判（gateway cwd 干扰）."""
        from hermes_lark_streaming._source import venv_has_plugin

        fake_py = Path("/fake/venv/bin/python3")
        captured = MagicMock()
        captured.return_value = MagicMock(returncode=0, stdout="")
        with patch("hermes_lark_streaming.patcher.hermes_python", return_value=fake_py), patch(
            "subprocess.run", captured
        ):
            venv_has_plugin()
        assert captured.call_args.kwargs.get("cwd") == "/"


class TestWatchdogStatusDetection:
    """watchdog.status() 的 loaded 双检测（list 优先，print 兜底）."""

    def test_status_loaded_via_list_first(self) -> None:
        """launchctl list <label> 返回 0 → loaded=True（不调 print）."""
        from hermes_lark_streaming import watchdog

        def run_side_effect(args, **kwargs):
            if args[1] == "list":  # launchctl list <label>
                return MagicMock(returncode=0, stdout=b"label\n")
            return MagicMock(returncode=113, stdout=b"")  # print 不应被触达

        with patch("shutil.which", return_value="/bin/launchctl"), patch(
            "subprocess.run", side_effect=run_side_effect
        ) as run:
            ws = watchdog.status()
        assert ws.loaded is True
        # 第一个调用应是 list（优先）
        assert run.call_args_list[0].args[0][1] == "list"

    def test_status_falls_back_to_print_when_list_fails(self) -> None:
        """list 返回非 0 但 print 返回 0 → loaded=True（兜底）."""
        from hermes_lark_streaming import watchdog

        def run_side_effect(args, **kwargs):
            if args[1] == "list":
                return MagicMock(returncode=113, stdout=b"")
            return MagicMock(returncode=0, stdout=b"state = not running\n")

        with patch("shutil.which", return_value="/bin/launchctl"), patch(
            "subprocess.run", side_effect=run_side_effect
        ):
            ws = watchdog.status()
        assert ws.loaded is True

    def test_status_not_loaded_when_both_fail(self) -> None:
        """list 和 print 都失败 → loaded=False."""
        from hermes_lark_streaming import watchdog

        with patch("shutil.which", return_value="/bin/launchctl"), patch(
            "subprocess.run", return_value=MagicMock(returncode=113, stdout=b"")
        ):
            ws = watchdog.status()
        assert ws.loaded is False

