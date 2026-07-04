"""C1-C5 卡片交互 e2e 测试 — 工具展示/完成态/流式状态色/footer/多工具。"""

from __future__ import annotations

import re
import time

import pytest

from tests.e2e.conftest import GATEWAY_LOG, PRIVATE_CHAT

pytestmark = pytest.mark.e2e


def test_c1_tool_call_display(lark, log_marker, wait_for_log):
    """C1: 工具调用段在卡片展示（terminal started+completed）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C1] 用 terminal 工具运行 echo c1-test")
    assert wait_for_log(r"on_tool_update.*tool=terminal.*status=started", since=start, timeout=30)
    assert wait_for_log(r"on_tool_update.*tool=terminal.*status=completed", since=start, timeout=30)


def test_c2_complete_state(lark, log_marker, wait_for_log):
    """C2: 完成态（state=complete）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C2] 用一句话回我")
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=60)


def test_c3_streaming_then_complete(lark, log_marker, wait_for_log):
    """C3: 状态色 — streaming 阶段有 CardKit stream 元素，complete 后收尾。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C3] 介绍下你自己，详细点")
    assert wait_for_log(r"CardKit (stream|batch update)", since=start, timeout=30)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_c4_footer_fields(lark, log_marker, wait_for_log):
    """C4: footer 字段（response ready 含 duration/model）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C4] 几点了")
    assert wait_for_log(r"response ready.*time=.*api_calls=", since=start, timeout=60)


def test_c5_multiple_tools(lark, log_marker):
    """C5: 多个工具调用展示（至少 2 次 terminal started）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C5] 先用 terminal 运行 echo step1，再用 terminal 运行 echo step2")
    # 等待 agent 处理（工具调用 + 回复）
    time.sleep(20)
    lines = GATEWAY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    tool_starts = sum(
        1
        for line in lines[start:]
        if "on_tool_update" in line and "tool=terminal" in line and "status=started" in line
    )
    assert tool_starts >= 2, f"应至少 2 次 terminal started，实际 {tool_starts}"
