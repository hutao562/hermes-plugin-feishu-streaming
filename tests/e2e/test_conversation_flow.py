"""对话流 e2e — 合并版（原 B1-B6 → B1）。

中断类测试（原 B1/B4/B5）真实触发不稳定: 取决于 Hermes 是否处于可中断阶段
（API streaming 可中断 / 工具执行不可中断）+ agent 是否调工具，同输入两次
结果不同；且 on_interrupted 日志用 hermes_lark_streaming logger 不进
gateway.log。合并保留 1 个尽力触发（不 xfail，失败即暴露回归），其余意图
在占位里注明。

长文本 split/reasoning（原 B2/B3）并入 test_card_interaction.py C3
（长回复回合自然覆盖流式推送 + 完成）。
跨回合合并（原 B6）需 background 回合（message_id=None），普通对话不触发，
留占位。
"""

from __future__ import annotations

import time

import pytest

from tests.e2e.conftest import PRIVATE_CHAT

pytestmark = pytest.mark.e2e


def test_b1_interrupt_redirects_to_new_card(lark, log_marker, wait_for_log):
    """B1: 长回合被新消息中断 → 新回合正常完成（卡片链路不因中断破坏）。

    尽力触发: 先发长任务进入 streaming，3s 后发短消息中断。可观测信号是
    短消息回合最终 complete（on_completed_wait），证明中断后卡片通道仍工作。
    注: 中断是否真正发生取决于 Hermes 内部状态机，本测试验证的是「无论中断
    与否，后续回合都完整走卡片」— 稳定的回归保护。
    """
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e B1-a] 详细介绍 Python 语言的历史和特性，写长一点")
    time.sleep(3)  # 让 B1-a 进入处理
    lark.send_text(PRIVATE_CHAT, "[e2e B1-b] 停，现在几点了？")
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=120)


@pytest.mark.skip(
    reason="跨回合合并需 background 回合（message_id=None），普通对话不触发；单测覆盖",
)
def test_b6_cross_turn_merge_placeholder() -> None:
    """占位: 跨回合合并（background 复用同 chat 卡）E2E 触发方式待 background 任务通道。"""
    assert True
