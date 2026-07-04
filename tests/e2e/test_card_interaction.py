"""C1-C5 卡片交互 e2e 测试 — 工具展示/完成态/流式状态色/footer/多工具。"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import PRIVATE_CHAT

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
    """C3: 卡片生命周期 — CardKit 创建（streaming 态）→ complete 收尾。

    状态色（streaming 蓝 / complete 绿）是视觉层，gateway.log 不可见；
    流式 element update 日志用 hermes_lark_streaming logger（不进 gateway.log）。
    可观测的状态转换痕迹：card created（进入 streaming）+ on_completed_wait（收尾）。
    """
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C3] 介绍下你自己，详细点")
    assert wait_for_log(r"CardKit card created", since=start, timeout=30)
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=90)


def test_c4_footer_fields(lark, log_marker, wait_for_log):
    """C4: footer 字段（response ready 含 duration/model）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C4] 几点了")
    assert wait_for_log(r"response ready.*time=.*api_calls=", since=start, timeout=60)


def test_c5_multiple_tools(lark, log_marker, wait_for_log, count_log):
    """C5: 多个工具调用展示（至少 2 次 terminal started）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C5] 先用 terminal 运行 echo step1，再用 terminal 运行 echo step2")
    # 等回复完成（替代固定 sleep，更可靠）
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=60)
    n = count_log(start, r"on_tool_update.*tool=terminal.*status=started")
    assert n >= 2, f"应至少 2 次 terminal started，实际 {n}"
