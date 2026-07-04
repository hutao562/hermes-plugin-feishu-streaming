"""B1-B6 对话流 e2e 测试 — 中断/长文本/reasoning/嵌套中断/跨回合合并。"""

from __future__ import annotations

import time

import pytest

from tests.e2e.conftest import PRIVATE_CHAT

pytestmark = pytest.mark.e2e


def test_b1_interrupt_redirect(lark, log_marker, wait_for_log):
    """B1: 用户发新消息中断前一条 — on_interrupted + 新建 B 卡 + A=ABORTED。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B1-a] 详细介绍一下飞书 CardKit v2 的所有特性，长篇大论")
    # 不等回复，立即发第二条中断
    time.sleep(3)
    lark.send_text(PRIVATE_CHAT, "[e2e B1-b] 停，告诉我现在几点")
    assert wait_for_log(r"on_interrupted.*abort", since=start, timeout=30), "应有中断日志"


def test_b2_long_text_split(lark, log_marker, wait_for_log):
    """B2: 长文本触发 split/rollover。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B2] 写一篇 2000 字关于 AI 发展史的长文")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=120)


def test_b3_reasoning_display(lark, log_marker, wait_for_log):
    """B3: reasoning/thinking 段展示。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B3] 深入思考一下：为什么天空是蓝色的")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_b4_complete_after_interrupt(lark, log_marker, wait_for_log):
    """B4: 中断后完成重定向（complete hook 跳到新 session）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B4-a] 长篇介绍 Python 历史")
    time.sleep(3)
    lark.send_text(PRIVATE_CHAT, "[e2e B4-b] 停，回我 OK")
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=60)


@pytest.mark.xfail(reason="嵌套中断需精确时序，难稳定触发", strict=False)
def test_b5_nested_interrupt(lark, log_marker, wait_for_log):
    """B5: 嵌套中断 A->B->C。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B5-a] 长篇 A")
    time.sleep(3)
    lark.send_text(PRIVATE_CHAT, "[e2e B5-b] 长篇 B")
    time.sleep(3)
    lark.send_text(PRIVATE_CHAT, "[e2e B5-c] 短回 C")
    assert wait_for_log(r"on_interrupted", since=start, timeout=30)


@pytest.mark.xfail(reason="跨回合合并需 background 回合（message_id=None），普通对话不触发", strict=False)
def test_b6_cross_turn_merge(lark, log_marker, wait_for_log):
    """B6: 跨回合合并（background 复用同 chat 卡）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B6] 帮我做个会触发 background 的长任务")
    assert wait_for_log(r"\[cheerwhy-merge\] session reactivated", since=start, timeout=120)
