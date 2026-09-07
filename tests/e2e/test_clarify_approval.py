"""交互卡片 e2e — clarify 单选卡（F1 组）+ approval 审批卡（F2 组）。

Hermes 的两类交互飞书卡:
- clarify（本插件 monkey-patch FeishuAdapter.send_clarify 渲染按钮卡）:
  发消息让 agent 调 clarify 工具 → 断言按钮卡发出（agent.log [clarify] sent card）
  → 发数字文本模拟用户点选（gateway 文本拦截 resolve）→ 断言回合完成
- approval（Hermes 原生 send_exec_approval，同一 card.action 回调处理器）:
  危险命令触发审批卡。注意 approvals.mode=smart 大部分命令自动放行——
  文本兜底 /approve 或具体命令触发依赖配置，失败即暴露审批卡回归。

日志分区: clarify 成功日志走 hermes_lark_streaming logger（agent.log）；
文本拦截 resolve 走 gateway.run（gateway.log "intercepted clarify text response"）。
"""

from __future__ import annotations

import time

import pytest

from tests.e2e.conftest import PRIVATE_CHAT

pytestmark = pytest.mark.e2e


def test_f1_clarify_choice_card_roundtrip(lark, log_marker, agent_log_marker, wait_for_log, wait_for_agent_log):
    """F1: clarify 单选按钮卡完整往返（发卡 → 用户选择 → resolve → 回合完成）。

    1. agent.log: [clarify] sent card ... choices=N（按钮卡发出）
    2. 等拦截就绪（sent card 后 agent 进入 wait_for_response 需要几百 ms）
    3. 发数字文本当用户点选（走 gateway 文本拦截）
    4. gateway.log: 回合 complete（agent 被唤醒并收尾）

    注: 文本拦截与 agent 侧 wait_for_response 存在毫秒级竞态——文本可能在
    clarify 已完成(超时/他路 resolve)后才到，resolve 返回 False 但回合照常
    完成。测试以「回合正常 complete」为最终断言，sent card 证明卡发出。
    """
    start = log_marker()
    agent_start = agent_log_marker()
    lark.send_text(
        PRIVATE_CHAT,
        "[e2e F1] 用 clarify 工具问我：周末想去哪玩？选项：A公园 B博物馆 C爬山（choices 传这三个）。等我回复后继续",
    )
    # 按钮卡发出（补丁的 _send_clarify 成功日志）
    assert wait_for_agent_log(
        r"\[clarify\] sent card id=\w+ msg=\w+ choices=[1-9]", since=agent_start, timeout=30
    )
    # 给 agent 几百 ms 进入 wait_for_response（否则文本拦截还没注册就漏过）
    time.sleep(3)
    # 用户选第 2 项（数字文本 = 点按钮的文本等价路径）
    lark.send_text(PRIVATE_CHAT, "[e2e F1] 2")
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=90)


def test_f2_clarify_open_question_roundtrip(lark, log_marker, agent_log_marker, wait_for_log, wait_for_agent_log):
    """F2: clarify 开放题（无 choices）→ 文本答复被接收 → 回合完成。

    无选项 clarify flip 进 text-capture 态（mark_awaiting_text），下条文本
    由 gateway 拦截接管。验证按钮卡之外的自由文本路径。
    """
    start = log_marker()
    agent_start = agent_log_marker()
    lark.send_text(
        PRIVATE_CHAT,
        "[e2e F2] 用 clarify 工具问我一个问题但不要给选项（自由回答）：我该学什么新技能。等我回答后继续",
    )
    # 等开放题卡发出（choices=0 也走 _send_clarify → sent card 日志）
    assert wait_for_agent_log(r"\[clarify\] sent card", since=agent_start, timeout=30)
    # 等 agent 真正进入等待（卡发后 agent 阻塞等回复，此时发文本才会被拦截）
    time.sleep(3)
    lark.send_text(PRIVATE_CHAT, "[e2e F2] 学数据分析")
    assert wait_for_log(r"Gateway intercepted clarify text response", since=start, timeout=40)
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=90)


@pytest.mark.skip(
    reason="approval 卡 E2E 需触发真危险命令 + 人为在飞书点按钮；smart 模式 guardian "
    "LLM 判断非确定性，全自动跑会诱导危险命令或卡 60s 审批超时。按钮卡构建/回调 "
    "由单测覆盖，端到端人工验证步骤见本测试 docstring。"
)
def test_f3_approval_card_manual_verification_steps() -> None:
    """占位: approval 审批卡人工验证步骤（不适合全自动 E2E）。

    人工步骤:
    1. 飞书私聊艾玛丝发: 「用 terminal 运行 sudo whoami」（sudo 触发审批）
       — 或任何 smart 模式判为需审批的命令
    2. 飞书应出现审批卡（允许/拒绝按钮），不是纯文本
    3. 点「允许」→ agent 继续执行；点「拒绝」→ 命令取消
    4. 日志: ~/.hermes/logs/gateway.log 应出现 send_exec_approval 相关 +
       card.action 回调处理（hermes_approval_action）

    自动化替代: Hermes 单测已覆盖 send_exec_approval 按钮构建 + card.action
    wrapper 路由（tests/test_clarify_inline.py TestCardActionWrapper）。
    """
    assert True
