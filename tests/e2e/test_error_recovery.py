"""D1-D3 错误恢复 e2e 测试 — 难真实触发，标 xfail 占位，后续 mock 补。"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import PRIVATE_CHAT

pytestmark = pytest.mark.e2e


@pytest.mark.xfail(reason="CardKit 创建失败需 mock，e2e 环境难触发", strict=False)
def test_d1_cardkit_failure_fallback(lark, log_marker, wait_for_log):
    """D1: CardKit 创建失败 -> fallback 纯文本（consume_text_fallback）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e D1] 测试")
    assert wait_for_log(r"consume_text_fallback|fallback", since=start, timeout=20)


@pytest.mark.xfail(reason="卡片创建超时需 mock 慢响应", strict=False)
def test_d2_card_creation_timeout(lark, log_marker, wait_for_log):
    """D2: 卡片创建超时（10s）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e D2] 测试")
    assert wait_for_log(r"card creation timed out", since=start, timeout=30)


@pytest.mark.xfail(reason="UnavailableGuard 需删除消息触发，难自动化", strict=False)
def test_d3_unavailable_guard(lark, log_marker, wait_for_log):
    """D3: 消息删除触发 UnavailableGuard 自动收尾。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e D3] 测试")
    assert wait_for_log(r"unavailable|auto.?terminat", since=start, timeout=20)
