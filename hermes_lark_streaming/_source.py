"""定位插件源码目录 + 检测/重装 venv 中的 editable 安装.

hermes 升级会重建 venv，丢掉本插件的 editable 安装（``__editable__.hermes_lark_streaming*.pth``），
导致 entry_point 消失 → ``discover_plugins()`` 扫不到 → ``register()`` 自愈永不触发。
本模块提供「插件可用性检测 + 自动重装」能力，供三层自愈共用。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

_logger = logging.getLogger("hermes_lark_streaming")

# 写死清华镜像：用户 Clash 代理对 pypi.org/files.pythonhosted.org 转发不通（fake-IP 劫持），
# 清华源走直连可达。改回默认源会在该环境下 SSL 握手超时。
_TSINGHUA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
_DIST_NAME = "hermes-lark-streaming"


def source_dir() -> Path | None:
    """定位插件源码目录（含 ``pyproject.toml`` 的目录）.

    两策略，优先用 importlib.metadata 反查（对 editable 安装最准）：
      1. ``distribution(_DIST_NAME).locate_file('')`` —— editable finder 记录的源码根；
      2. 回退 ``Path(__file__).resolve().parents[1]`` —— editable 安装时本文件就在源码树内，
         ``__file__`` 是 ``<src>/hermes_lark_streaming/_source.py``，上一级即源码根。

    返回的路径会校验含 ``pyproject.toml``，否则视为不可用（返回 None）。
    """
    candidates: list[Path] = []
    try:
        from importlib.metadata import distribution

        dist = distribution(_DIST_NAME)
        located = dist.locate_file("")
        if located is not None:
            candidates.append(Path(str(located)).resolve())
    except Exception:
        pass  # 包未安装（正是要重装的场景）或 importlib 异常

    candidates.append(Path(__file__).resolve().parents[1])

    for cand in candidates:
        if (cand / "pyproject.toml").exists():
            return cand
    return None


def venv_has_plugin(py: Path | None = None) -> bool:
    """插件是否在指定 Python（默认 hermes venv）里可 import.

    用子进程跑 ``find_spec``，**不依赖当前 cwd** —— gateway 进程 cwd 在 ``~/.hermes``，
    直接在本进程 import 会因 ``sys.path[0]=''`` 误判（源码目录恰好是 cwd 时假阳性）。
    子进程 cwd 设为无关目录（``/``）消除该干扰。

    ``py`` 定位失败时（gateway 进程 PATH 里没有 hermes CLI）用 ``sys.executable``
    兜底：self-heal 就在 gateway 进程内跑，它自己就是 hermes venv 的解释器。
    """
    if py is None:
        from .patcher import hermes_python

        py = hermes_python()
    candidates = [py] if py is not None else []
    candidates.append(Path(sys.executable))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        try:
            result = subprocess.run(
                [str(candidate), "-c", "import importlib.util; import sys; "
                                       "sys.exit(0 if importlib.util.find_spec('hermes_lark_streaming') else 1)"],
                capture_output=True,
                cwd="/",  # 中立 cwd，避免 sys.path[0]='' 误判
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False


def reinstall_into_venv(py: Path | None = None) -> bool:
    """``pip install -e <源码目录>`` 到指定 Python（默认 hermes venv），写死清华源.

    返回 True 表示成功。失败只记 WARNING 不抛（调用方降级处理）。
    用 ``--no-build-isolation`` 复用 venv 已装的 setuptools，避免 build 隔离时再去
    pypi 拉 ``setuptools>=61``（同样会撞上代理转发问题）。
    """
    from .patcher import hermes_python

    if py is None:
        py = hermes_python()
    if py is None:
        _logger.warning("[hermes-lark] reinstall: cannot locate hermes python")
        return False

    src = source_dir()
    if src is None:
        _logger.warning("[hermes-lark] reinstall: cannot locate plugin source dir")
        return False

    cmd = [
        str(py), "-m", "pip", "install", "-e", str(src),
        "-i", _TSINGHUA_INDEX,
        "--timeout", "30",
        "--no-build-isolation",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        _logger.warning("[hermes-lark] reinstall: pip install timed out (180s)")
        return False
    except (OSError, subprocess.SubprocessError) as e:
        _logger.warning("[hermes-lark] reinstall: pip invocation failed: %s", e)
        return False

    if result.returncode != 0:
        _logger.warning(
            "[hermes-lark] reinstall: pip install failed (exit %d): %s",
            result.returncode,
            result.stderr.strip()[:500] or result.stdout.strip()[:500],
        )
        return False

    _logger.info("[hermes-lark] reinstall: plugin re-installed into venv from %s", src)
    return True
