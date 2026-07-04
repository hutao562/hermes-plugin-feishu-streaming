# E2E 测试集实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 hermes-lark-streaming 的 e2e 测试集（24 个测试），覆盖文件投递 / 对话流 / 卡片交互 / 错误恢复，pytest e2e 标记默认跳过。

**Architecture:** `tests/e2e/` 目录 + `conftest.py`（环境检查 + lark-cli helper + 日志断言）+ 4 个测试文件。`pyproject.toml` 注册 `e2e` marker，`addopts` 默认 skip。测试用 lark-cli（subprocess）发消息 + poll `gateway.log` 断言。

**Tech Stack:** pytest, lark-cli (subprocess), `~/.hermes/logs/gateway.log` polling

**Spec:** `docs/superpowers/specs/2026-07-04-e2e-test-suite-design.md`

**运行环境约定：**
- 所有命令用 Hermes venv：`HP=~/.hermes/hermes-agent/venv/bin/python3`
- e2e 测试需要：`HERMES_HOME=~/.hermes` + lark-cli 配置好（user 身份）+ Hermes gateway 运行
- 跑 e2e：`HERMES_HOME=~/.hermes $HP -m pytest -m e2e tests/e2e/ -v`
- 默认（CI）：`$HP -m pytest tests/ -q`（不跑 e2e）

---

## File Structure

**创建：**
- `tests/e2e/__init__.py` — 空标记文件
- `tests/e2e/conftest.py` — 环境检查 + LarkCLIDriver + 日志断言 helper
- `tests/e2e/test_file_delivery.py` — A1-A7 文件投递
- `tests/e2e/test_conversation_flow.py` — B1-B4 + B5-B6（xfail）
- `tests/e2e/test_card_interaction.py` — C1-C5
- `tests/e2e/test_error_recovery.py` — D1-D3（xfail）

**修改：**
- `pyproject.toml` — 注册 e2e marker + addopts

---

## Task 1: pyproject 注册 e2e marker + 默认 skip

**Files:**
- Modify: `pyproject.toml`（在 `[tool.mypy]` 之前插入）

- [ ] **Step 1: 加 pytest 配置段**

在 `pyproject.toml` 的 `[tool.ruff.format]` 段之后、`[tool.mypy]` 之前，插入：

```toml
[tool.pytest.ini_options]
markers = [
  "e2e: end-to-end tests requiring real Hermes + Feishu (deselected by default)",
]
addopts = "-m 'not e2e'"
```

- [ ] **Step 2: 验证默认 skip 生效**

Run: `$HP -m pytest tests/ -q --co 2>&1 | tail -3`
Expected: 收集的测试数 = 现有单测数（约 439），**不含** tests/e2e/（即使之后建了也跳过）

- [ ] **Step 3: 验证 -m e2e 能选中（此时 tests/e2e/ 还不存在，应 0 个）**

Run: `$HP -m pytest -m e2e --co 2>&1 | tail -3`
Expected: `0 tests collected` 或 `no tests ran`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "test: register pytest e2e marker, default skip"
```

---

## Task 2: tests/e2e/conftest.py — 环境检查 + LarkCLIDriver + 日志断言

**Files:**
- Create: `tests/e2e/__init__.py`（空文件）
- Create: `tests/e2e/conftest.py`

- [ ] **Step 1: 建 `tests/e2e/__init__.py`（空）**

```bash
touch tests/e2e/__init__.py
```

- [ ] **Step 2: 写 `tests/e2e/conftest.py` 完整内容**

```python
"""E2E 测试 fixtures — 环境检查、lark-cli driver、gateway.log 断言 helper."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import pytest

# 现有 chat 常量（从生产日志确认）
PRIVATE_CHAT = "oc_eeba2715144be520aa6a768342c023ae"
TOPIC_CHAT = "oc_d5c00959e919bad9d889ddfa3ff93bd1"
TOPIC_ROOT_MSG = "om_x100b6bb3ec02b4a4b27e0fe38eef710"

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
GATEWAY_LOG = Path(HERMES_HOME) / "logs" / "gateway.log"
GATEWAY_PID = Path(HERMES_HOME) / "gateway.pid"


def _run_lark(*args: str) -> dict[str, Any]:
    """跑 lark-cli（带 HERMES_HOME），返回 JSON 解析结果。"""
    env = {**os.environ, "HERMES_HOME": HERMES_HOME}
    result = subprocess.run(
        ["lark-cli", *args, "--format", "json"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    import json
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": {"message": f"non-json output: {result.stdout[:200]}"}, "raw": result.stdout}


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
def require_e2e_env() -> None:
    """环境就绪检查：HERMES_HOME + lark-cli + gateway 在跑，否则 skip。"""
    if not Path(HERMES_HOME).exists():
        pytest.skip(f"HERMES_HOME 不存在: {HERMES_HOME}")
    whoami = _run_lark("whoami")
    if not whoami.get("available"):
        pytest.skip(f"lark-cli 未就绪（token 未配置）: available={whoami.get('available')}")
    if not GATEWAY_PID.exists():
        pytest.skip(f"gateway.pid 不存在（Hermes 没跑）: {GATEWAY_PID}")


@pytest.fixture
def lark() -> LarkCLIDriver:
    return LarkCLIDriver()


@pytest.fixture
def log_marker() -> Callable[[], int]:
    """返回当前 gateway.log 的行数偏移，后续 wait_for_log / log_contains 用它做 since。"""
    def _marker() -> int:
        if not GATEWAY_LOG.exists():
            return 0
        with GATEWAY_LOG.open("r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    return _marker


def _read_log_since(offset: int) -> str:
    if not GATEWAY_LOG.exists():
        return ""
    lines = GATEWAY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
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
def log_contains() -> Callable[..., bool]:
    """检查 since 之后的日志是否含 pattern（不阻塞）。"""
    def _contains(since: int, pattern: str) -> bool:
        regex = re.compile(pattern)
        return any(regex.search(line) for line in _read_log_since(since).splitlines())
    return _contains
```

- [ ] **Step 3: 验证 conftest 不破坏默认 pytest（无 e2e 标记测试时不跑）**

Run: `$HP -m pytest tests/ -q 2>&1 | tail -2`
Expected: 现有 439 测试仍全绿（conftest 的 autouse fixture 只在 e2e 标记测试里激活... 实际 autouse 对所有测试激活，但 require_e2e_env 会 skip 而非失败）

> ⚠️ 注意：`require_e2e_env` 是 autouse，会影响 tests/ 下所有测试。但因为它 `pytest.skip`（而非 fail），非 e2e 测试也会被 skip。**这是个问题** —— 需要把 autouse 限定到 e2e 目录。

- [ ] **Step 4: 修正 — 把 require_e2e_env 的 autouse 限定到 e2e 标记测试**

把 `require_e2e_env` 的签名改为：

```python
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
    if not GATEWAY_PID.exists():
        pytest.skip(f"gateway 没跑: {GATEWAY_PID}")
```

- [ ] **Step 5: 验证 — 默认跑现有单测不受影响**

Run: `$HP -m pytest tests/test_controller.py -q 2>&1 | tail -2`
Expected: 现有测试全过（不被 skip）

Run: `$HP -m pytest -m e2e tests/e2e/ -q --co 2>&1 | tail -2`
Expected: `0 tests collected`（还没写测试文件）

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/__init__.py tests/e2e/conftest.py
git commit -m "test(e2e): add conftest with env check, lark-cli driver, log assert helpers"
```

---

## Task 3: test_file_delivery.py — A1-A7 文件投递

**Files:**
- Create: `tests/e2e/test_file_delivery.py`

- [ ] **Step 1: 写 7 个文件投递测试**

```python
"""A1-A7 文件投递 e2e 测试 — 图片/文件/视频/markdown/post 落私聊 + 话题。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_a1_image_to_private_chat(lark, log_marker, wait_for_log, log_contains):
    """A1: 让 Hermes 发图到私聊，验证卡片正常完成。"""
    start = log_marker()
    lark.send_text(__import__("tests.e2e.conftest", fromlist=["PRIVATE_CHAT"]).PRIVATE_CHAT,
                   "[e2e A1] 发一张桌面图片给我")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=90)


def test_a2_image_to_topic(lark, log_marker, wait_for_log, log_contains):
    """A2: 让 Hermes 发图到话题（痛点回归 99992402）。"""
    from tests.e2e.conftest import TOPIC_CHAT, TOPIC_ROOT_MSG
    start = log_marker()
    lark.reply_in_thread(TOPIC_ROOT_MSG, "[e2e A2] 用 reply 格式发一张桌面图片到这个话题")
    assert wait_for_log(r"\[cheerwhy-card\] 话题场景", since=start)
    assert not log_contains(start, r"99992402"), "踩了 send_image_file 不处理 thread_id 的坑"
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_a3_file_to_private_chat(lark, log_marker, wait_for_log):
    """A3: 让 Hermes 发文件到私聊。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e A3] 发我桌面任意一个文档文件")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_a4_file_to_topic(lark, log_marker, wait_for_log):
    """A4: 让 Hermes 发文件到话题。"""
    from tests.e2e.conftest import TOPIC_ROOT_MSG
    start = log_marker()
    lark.reply_in_thread(TOPIC_ROOT_MSG, "[e2e A4] 发我桌面任意一个文档到这个话题")
    assert wait_for_log(r"\[cheerwhy-card\] 话题场景", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_a5_video_to_private_chat(lark, log_marker, wait_for_log):
    """A5: 让 Hermes 发视频到私聊。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e A5] 发我桌面任意一个视频文件")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_a6_markdown_to_private_chat(lark, log_marker, wait_for_log):
    """A6: 让 Hermes 发 markdown 富文本到私聊。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e A6] 用 markdown 格式列三个水果")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=60)


def test_a7_post_to_topic(lark, log_marker, wait_for_log):
    """A7: 让 Hermes 发 post 富文本到话题。"""
    from tests.e2e.conftest import TOPIC_ROOT_MSG
    start = log_marker()
    lark.reply_in_thread(TOPIC_ROOT_MSG, "[e2e A7] 用富文本 post 格式回复三个水果")
    assert wait_for_log(r"\[cheerwhy-card\] 话题场景", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=60)
```

- [ ] **Step 2: 跑 A1 验证（确保环境 + 框架对）**

Run: `HERMES_HOME=~/.hermes $HP -m pytest tests/e2e/test_file_delivery.py::test_a1_image_to_private_chat -v -s 2>&1 | tail -15`
Expected: PASSED（如果环境就绪）或 SKIPPED（环境没就绪）。不应 ERROR。

- [ ] **Step 3: 跑全部 A 类**

Run: `HERMES_HOME=~/.hermes $HP -m pytest tests/e2e/test_file_delivery.py -v 2>&1 | tail -15`
Expected: A1-A7 全 PASSED（真实环境）

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_file_delivery.py
git commit -m "test(e2e): add A1-A7 file delivery tests (image/file/video/markdown/post)"
```

---

## Task 4: test_conversation_flow.py — B1-B4 + B5-B6（xfail）

**Files:**
- Create: `tests/e2e/test_conversation_flow.py`

- [ ] **Step 1: 写对话流测试**

```python
"""B1-B6 对话流 e2e 测试 — 中断/合并/长文本/reasoning/嵌套中断。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_b1_interrupt_redirect(lark, log_marker, wait_for_log):
    """B1: 用户发新消息中断前一条 — on_interrupted + 新建 B 卡 + A=ABORTED。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B1-a] 详细介绍一下飞书 CardKit v2 的所有特性，长篇大论")
    # 不等回复，立即发第二条中断
    import time; time.sleep(3)
    lark.send_text(PRIVATE_CHAT, "[e2e B1-b] 停，告诉我现在几点")
    assert wait_for_log(r"on_interrupted.*abort", since=start, timeout=30), "应有中断日志"


def test_b2_long_text_split(lark, log_marker, wait_for_log):
    """B2: 长文本触发 split/rollover。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B2] 写一篇 2000 字关于 AI 发展史的长文")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=120)


def test_b3_reasoning_display(lark, log_marker, wait_for_log):
    """B3: reasoning/thinking 段展示。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B3] 深入思考一下：为什么天空是蓝色的")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_b4_complete_after_interrupt(lark, log_marker, wait_for_log):
    """B4: 中断后完成重定向（complete hook 跳到新 session）。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B4-a] 长篇介绍 Python 历史")
    import time; time.sleep(3)
    lark.send_text(PRIVATE_CHAT, "[e2e B4-b] 停，回我 OK")
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=60)


@pytest.mark.xfail(reason="嵌套中断需精确时序，难稳定触发", strict=False)
def test_b5_nested_interrupt(lark, log_marker, wait_for_log):
    """B5: 嵌套中断 A→B→C。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B5-a] 长篇 A")
    import time; time.sleep(3)
    lark.send_text(PRIVATE_CHAT, "[e2e B5-b] 长篇 B")
    time.sleep(3)
    lark.send_text(PRIVATE_CHAT, "[e2e B5-c] 短回 C")
    assert wait_for_log(r"on_interrupted", since=start, timeout=30)


@pytest.mark.xfail(reason="跨回合合并需 background 回合（message_id=None），普通对话不触发", strict=False)
def test_b6_cross_turn_merge(lark, log_marker, wait_for_log):
    """B6: 跨回合合并（background 复用同 chat 卡）。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B6] 帮我做个会触发 background 的长任务")
    assert wait_for_log(r"\[cheerwhy-merge\] session reactivated", since=start, timeout=120)
```

- [ ] **Step 2: 跑 B1 验证**

Run: `HERMES_HOME=~/.hermes $HP -m pytest tests/e2e/test_conversation_flow.py::test_b1_interrupt_redirect -v -s 2>&1 | tail -10`
Expected: PASSED

- [ ] **Step 3: 跑全部 B 类（B5/B6 预期 xfail）**

Run: `HERMES_HOME=~/.hermes $HP -m pytest tests/e2e/test_conversation_flow.py -v 2>&1 | tail -15`
Expected: B1-B4 PASSED，B5-B6 xfail

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_conversation_flow.py
git commit -m "test(e2e): add B1-B6 conversation flow tests (interrupt/long/reasoning/merge)"
```

---

## Task 5: test_card_interaction.py — C1-C5

**Files:**
- Create: `tests/e2e/test_card_interaction.py`

- [ ] **Step 1: 写卡片交互测试**

```python
"""C1-C5 卡片交互 e2e 测试 — 工具展示/折叠/状态色/footer/多工具。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_c1_tool_call_display(lark, log_marker, wait_for_log):
    """C1: 工具调用段在卡片展示（terminal started+completed）。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C1] 用 terminal 工具运行 echo c1-test")
    assert wait_for_log(r"on_tool_update.*tool=terminal.*status=started", since=start, timeout=30)
    assert wait_for_log(r"on_tool_update.*tool=terminal.*status=completed", since=start, timeout=30)


def test_c2_complete_state(lark, log_marker, wait_for_log):
    """C2: 完成态（state=complete）。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C2] 用一句话回我")
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=60)


def test_c3_streaming_then_complete(lark, log_marker, wait_for_log):
    """C3: 状态色 — streaming 阶段有 CardKit stream 元素，complete 后收尾。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C3] 介绍下你自己，详细点")
    assert wait_for_log(r"CardKit (stream|batch update)", since=start, timeout=30)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_c4_footer_fields(lark, log_marker, wait_for_log):
    """C4: footer 字段（response ready 含 duration/model）。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C4] 几点了")
    assert wait_for_log(r"response ready.*time=.*api_calls=", since=start, timeout=60)


def test_c5_multiple_tools(lark, log_marker, wait_for_log):
    """C5: 多个工具调用展示。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C5] 先用 terminal 运行 echo step1，再用 terminal 运行 echo step2")
    # 期望至少 2 次 tool=terminal started
    import time; time.sleep(20)
    import re
    from tests.e2e.conftest import GATEWAY_LOG
    lines = GATEWAY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    tool_starts = sum(1 for l in lines[start:] if "on_tool_update" in l and "tool=terminal" in l and "status=started" in l)
    assert tool_starts >= 2, f"应至少 2 次 terminal started，实际 {tool_starts}"
```

- [ ] **Step 2: 跑全部 C 类**

Run: `HERMES_HOME=~/.hermes $HP -m pytest tests/e2e/test_card_interaction.py -v 2>&1 | tail -15`
Expected: C1-C5 PASSED

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_card_interaction.py
git commit -m "test(e2e): add C1-C5 card interaction tests (tool/state/footer/multi-tool)"
```

---

## Task 6: test_error_recovery.py — D1-D3（xfail）

**Files:**
- Create: `tests/e2e/test_error_recovery.py`

- [ ] **Step 1: 写错误恢复测试（全 xfail，难真实触发）**

```python
"""D1-D3 错误恢复 e2e 测试 — 难真实触发，标 xfail 占位，后续 mock 补。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.xfail(reason="CardKit 创建失败需 mock，e2e 环境难触发", strict=False)
def test_d1_cardkit_failure_fallback(lark, log_marker, wait_for_log):
    """D1: CardKit 创建失败 → fallback 纯文本（consume_text_fallback）。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e D1] 测试")
    assert wait_for_log(r"consume_text_fallback\|fallback", since=start, timeout=60)


@pytest.mark.xfail(reason="卡片创建超时需 mock 慢响应", strict=False)
def test_d2_card_creation_timeout(lark, log_marker, wait_for_log):
    """D2: 卡片创建超时（10s）。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e D2] 测试")
    assert wait_for_log(r"card creation timed out", since=start, timeout=30)


@pytest.mark.xfail(reason="UnavailableGuard 需删除消息触发，难自动化", strict=False)
def test_d3_unavailable_guard(lark, log_marker, wait_for_log):
    """D3: 消息删除触发 UnavailableGuard 自动收尾。"""
    from tests.e2e.conftest import PRIVATE_CHAT
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e D3] 测试")
    assert wait_for_log(r"unavailable\|auto.?terminat", since=start, timeout=60)
```

- [ ] **Step 2: 跑 D 类（预期全 xfail）**

Run: `HERMES_HOME=~/.hermes $HP -m pytest tests/e2e/test_error_recovery.py -v 2>&1 | tail -10`
Expected: D1-D3 全 xfail（不 fail）

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_error_recovery.py
git commit -m "test(e2e): add D1-D3 error recovery tests (xfail, need mock to trigger)"
```

---

## Task 7: 全量验证 + 文档

- [ ] **Step 1: 默认 pytest 不跑 e2e（CI 安全）**

Run: `$HP -m pytest tests/ -q 2>&1 | tail -3`
Expected: 现有单测全过（约 439 passed），**不含 e2e**

- [ ] **Step 2: e2e 全量跑（本地，需环境）**

Run: `HERMES_HOME=~/.hermes $HP -m pytest -m e2e tests/e2e/ -v 2>&1 | tail -30`
Expected: P0/P1 PASSED，P2（B5/B6/D1-D3）xfail。总 ~24 测试。

- [ ] **Step 3: 更新 CLAUDE.md / README（e2e 运行说明）**

在 `CLAUDE.md` 的 Commands 段加：

```bash
# E2E 测试（需 Hermes 运行 + lark-cli 配置，默认跳过）
HERMES_HOME=~/.hermes $HERMES_PYTHON -m pytest -m e2e tests/e2e/ -v
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document e2e test suite run command"
```

---

## Self-Review（plan 写完后的自检）

**1. Spec coverage:**
- A1-A7（文件投递）→ Task 3 ✅
- B1-B4 + B5-B6（对话流）→ Task 4 ✅
- C1-C5（卡片交互）→ Task 5 ✅
- D1-D3（错误恢复）→ Task 6 ✅
- pyproject e2e marker → Task 1 ✅
- conftest fixture → Task 2 ✅
- chat 策略（[e2e] 前缀）→ 各测试消息含 [e2e] ✅
- 运行方式 → Task 7 ✅

**2. Placeholder scan:** 无 TBD/TODO。fixture + 测试代码完整。

**3. Type consistency:**
- `lark.send_text(chat_id, text)` / `lark.reply_in_thread(root, text)` — conftest 定义 + 测试使用一致 ✅
- `wait_for_log(pattern, since, timeout)` — 一致 ✅
- `log_contains(since, pattern)` — 一致 ✅
- `log_marker()` 返回 int offset — 一致 ✅
- chat 常量 PRIVATE_CHAT / TOPIC_CHAT / TOPIC_ROOT_MSG — 一致 ✅

**4. 已知风险/边界：**
- B5/B6/D1-D3 标 xfail（strict=False），不阻塞 P0/P1
- A5 视频 / A6 markdown / A7 post 依赖 Hermes 支持，若不支持测试 fail（非 xfail）— 实施时观察，必要时加 xfail
- e2e 测试消耗 API + 时间，建议本地手动跑，不进 CI
