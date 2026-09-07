"""E2E 测试 fixtures — 环境检查、lark-cli driver、gateway.log 断言 helper."""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# 现有 chat 常量（从生产日志确认）
PRIVATE_CHAT = "oc_eeba2715144be520aa6a768342c023ae"
TOPIC_CHAT = "oc_d5c00959e919bad9d889ddfa3ff93bd1"
TOPIC_ROOT_MSG = "om_x100b6bb3ec02b4a4b27e0fe38eef710"

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
GATEWAY_LOG = Path(HERMES_HOME) / "logs" / "gateway.log"
AGENT_LOG = Path(HERMES_HOME) / "logs" / "agent.log"
GATEWAY_PID = Path(HERMES_HOME) / "gateway.pid"


def _run_lark(*args: str) -> dict[str, Any]:
    """跑 lark-cli（带 HERMES_HOME），返回 JSON 解析结果。

    lark-cli 成功时 JSON 到 stdout；失败时 JSON 到 stderr（stdout 空，如 unknown flag）。
    不加 --format json：whoami 等命令不支持该 flag（默认就是 JSON）。
    """
    env = {**os.environ, "HERMES_HOME": HERMES_HOME}
    result = subprocess.run(
        ["lark-cli", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )
    import json
    output = result.stdout if result.stdout.strip() else result.stderr
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"ok": False, "error": {"message": f"non-json output: {output[:200]}"}, "raw": output}


class LarkCLIDriver:
    """封装 lark-cli 发消息 / reply / 查消息。"""

    def send_text(self, chat_id: str, text: str) -> str | None:
        data = _run_lark("im", "+messages-send", "--chat-id", chat_id,
                         "--text", text, "--as", "user")
        if not data.get("ok"):
            pytest.fail(f"lark-cli send_text 失败: {data}")
        return data.get("data", {}).get("message_id")

    def reply_in_thread(self, root_msg_id: str, text: str) -> str | None:
        data = _run_lark("im", "+messages-reply", "--message-id", root_msg_id,
                         "--reply-in-thread", "--text", text, "--as", "user")
        if not data.get("ok"):
            pytest.fail(f"lark-cli reply_in_thread 失败: {data}")
        return data.get("data", {}).get("message_id")


@pytest.fixture(autouse=True)
def require_e2e_env(request: pytest.FixtureRequest) -> None:
    """环境就绪检查（仅对 e2e 标记测试生效，非 e2e 直接 return）。"""
    if "e2e" not in [m.name for m in request.node.iter_markers()]:
        return  # 非 e2e 测试，不检查
    if not Path(HERMES_HOME).exists():
        pytest.skip(f"HERMES_HOME 不存在: {HERMES_HOME}")
    whoami = _run_lark("whoami")
    if not whoami.get("available"):
        pytest.skip(f"lark-cli 未就绪: available={whoami.get('available')}")
    if not _gateway_running():
        pytest.skip(f"gateway 没跑（无 {GATEWAY_PID} 且 ps 无 gateway run 进程）")


def _gateway_running() -> bool:
    """gateway 判活：pid 文件优先，缺失时降级 ps 扫描。

    新版 launchd 托管的主 profile gateway 不一定写 gateway.pid（family 写了，
    主没写——是否写取决于外部监督器），所以 pid 文件不存在 ≠ gateway 没跑。
    """
    if GATEWAY_PID.exists():
        return True
    try:
        proc = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return "hermes_cli.main gateway run" in proc.stdout or "gateway run" in proc.stdout


@pytest.fixture
def lark() -> LarkCLIDriver:
    return LarkCLIDriver()


@pytest.fixture
def log_marker() -> Callable[[], int]:
    """返回当前 gateway.log 的行数偏移。"""
    def _marker() -> int:
        if not GATEWAY_LOG.exists():
            return 0
        with GATEWAY_LOG.open("r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    return _marker


@pytest.fixture
def agent_log_marker() -> Callable[[], int]:
    """返回当前 agent.log 的行数偏移（配合 wait_for_agent_log 用）。"""
    def _marker() -> int:
        if not AGENT_LOG.exists():
            return 0
        with AGENT_LOG.open("r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    return _marker


def _read_log_since(offset: int, log_path: Path | None = None) -> str:
    path = log_path or GATEWAY_LOG
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[offset:])


@pytest.fixture
def wait_for_log() -> Callable[..., str]:
    """poll gateway.log 直到出现 pattern（超时秒），返回匹配行；超时 raise AssertionError。"""
    def _wait(pattern: str, since: int, timeout: float = 60.0, interval: float = 1.0) -> str:
        regex = re.compile(pattern)
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = _read_log_since(since)
            for line in last.splitlines():
                if regex.search(line):
                    return line
            time.sleep(interval)
        raise AssertionError(f"超时 {timeout}s 未匹配到日志 pattern={pattern!r}\n最后日志:\n{last[-500:]}")
    return _wait


@pytest.fixture
def wait_for_agent_log() -> Callable[..., str]:
    """poll agent.log 直到出现 pattern（hermes_lark_streaming logger 写 agent.log，
    卡片元素级更新如 CardKit batch/stream element 在此，gateway.log 只有 [cheerwhy-card] 摘要）。"""
    def _wait(pattern: str, since: int, timeout: float = 90.0, interval: float = 1.0) -> str:
        regex = re.compile(pattern)
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = _read_log_since(since, AGENT_LOG)
            for line in last.splitlines():
                if regex.search(line):
                    return line
            time.sleep(interval)
        raise AssertionError(f"超时 {timeout}s 未在 agent.log 匹配到 pattern={pattern!r}\n最后日志:\n{last[-500:]}")
    return _wait


@pytest.fixture
def log_contains() -> Callable[..., bool]:
    """检查 since 之后的日志是否含 pattern（不阻塞）。"""
    def _contains(since: int, pattern: str) -> bool:
        regex = re.compile(pattern)
        return any(regex.search(line) for line in _read_log_since(since).splitlines())
    return _contains


@pytest.fixture
def count_log() -> Callable[..., int]:
    """计数 since 之后匹配 pattern 的日志行数（不阻塞）。"""
    def _count(since: int, pattern: str) -> int:
        regex = re.compile(pattern)
        return sum(1 for line in _read_log_since(since).splitlines() if regex.search(line))
    return _count
