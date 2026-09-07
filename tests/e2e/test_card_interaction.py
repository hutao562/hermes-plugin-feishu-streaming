"""卡片交互 e2e — 合并版（原 C1-C5 + activity D1-D3）。

覆盖目标（每测一个真实回合，减少重复发消息）:
- C1: 工具回合全链路 — 多工具调用收进卡片（on_tool_update started/completed）
       + CardKit 元素推送 + 完成 + 无双发（原生消息通道静默）
- C2: 纯文本回合全生命周期 — CardKit created → complete → response ready(footer)

长文本 split（原 C3/B2）、媒体投递（A 组）、交互卡（F 组 clarify）各自独立
文件/单测覆盖。真实回合需串行等待每个 complete——agent 回合不排队，新消息
会被 steer 进进行中回合，故同一 chat 的 E2E 不宜连发长任务。

命名规则: 测试按主题字母分组（A=媒体, B=对话流, C=卡片交互, D=活动进卡片,
E=错误恢复 xfail, F=clarify/审批）— 本文件 C1-C2。
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import GATEWAY_LOG, PRIVATE_CHAT

pytestmark = pytest.mark.e2e


def test_c1_tool_roundtrip_no_native_duplicate(lark, log_marker, agent_log_marker, wait_for_log, wait_for_agent_log):
    """C1: 多工具回合 → 卡片全链路 + 无双发（合并原 C1/C5/D1/D3）。

    断言链:
    1. gateway.log: ≥2 个工具 started（多工具都被卡片管线接管）
    2. agent.log: CardKit batch update（工具面板真的推送到飞书）
    3. gateway.log: 正常 complete（无 fallback）
    4. 无双发: 该回合无原生消息通道痕迹（streamed=True / Suppressing final send）
    """
    start = log_marker()
    agent_start = agent_log_marker()
    lark.send_text(
        PRIVATE_CHAT,
        "[e2e C1] 先用 terminal 运行 echo step1，再用 terminal 运行 echo step2，"
        "然后 read_file 读取 ~/ai/hermes-lark-streaming/README.md 第一行",
    )
    # 多工具事件被卡片管线接管
    assert wait_for_log(r"on_tool_update.*tool=terminal.*status=started", since=start, timeout=30)
    assert wait_for_log(r"on_tool_update.*tool=read_file.*status=started", since=start, timeout=30)
    # 卡片元素级推送发生（工具面板更新进卡片）
    assert wait_for_agent_log(r"CardKit (batch update|stream element)", since=agent_start, timeout=30)
    # 正常完成（不是 NO session fallback）
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=120)
    # 无双发: 卡片接管后 Hermes 原生消息通道应完全静默
    native_lines = [
        line for line in GATEWAY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[start:]
        if "streamed=True" in line or "Suppressing normal final send" in line
    ]
    assert not native_lines, f"出现原生消息通道痕迹（双发风险）: {native_lines[:3]}"


def test_c2_text_roundtrip_lifecycle(lark, log_marker, wait_for_log):
    """C2: 纯文本回合全生命周期（合并原 C2/C3/C4/A6）。

    卡片创建（进入 streaming）→ complete 收尾 → response ready（footer
    duration/model 信号）。纯文本回合不调工具，验证卡片通道对普通回复同样工作。
    注: 长文本 split/rollover（原 C3/B2）由单测覆盖——E2E 里长文回合
    （分钟级）会阻塞同一 chat 后续测试消息（agent 回合不排队，新消息被
    steer），故不纳入真实回合。
    """
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e C2] 介绍一下你自己，详细一点")
    assert wait_for_log(r"CardKit card created", since=start, timeout=30)
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=90)
    assert wait_for_log(r"response ready.*time=.*api_calls=", since=start, timeout=90)
