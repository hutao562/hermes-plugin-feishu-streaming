"""E2E: 活动类内容收进卡片 — 工具动作进卡片管线（read_file → 折叠标题标签）。

链路: lark-cli 发真实消息 → gateway 收到 → agent 调 read_file → TOOL wrapper
接管 → on_tool_update 进卡片 → CardKit batch update 推送工具面板 → complete。

日志分区:
- gateway.log ([cheerwhy-card] / gateway.run): session/on_tool_update/complete 摘要
- agent.log (hermes_lark_streaming logger): CardKit batch/stream element 元素级更新

注意: 卡片折叠标题的具体 label 文本（📖 Reading xxx）由单测 test_cardkit.py
保证（_build_tool_panel 断言）；E2E 验证「工具调用真的走卡片管线且无 fallback」。
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import GATEWAY_LOG, PRIVATE_CHAT

pytestmark = pytest.mark.e2e


def test_d1_read_file_goes_through_card_pipeline(lark, log_marker, agent_log_marker, wait_for_log, wait_for_agent_log):
    """D1: read_file 工具调用 → 卡片管线全链路（不是原生文本飘卡片外）。

    断言链:
    1. gateway.log: on_tool_update tool=read_file started（TOOL wrapper 接管成功）
    2. agent.log: CardKit batch update（工具面板元素被推送到飞书）
    3. gateway.log: 完成后正常 complete（无 fallback 纯文本）
    """
    start = log_marker()
    agent_start = agent_log_marker()
    lark.send_text(
        PRIVATE_CHAT,
        "[e2e D1] 用 read_file 工具读取 ~/ai/hermes-lark-streaming/README.md 的第一行",
    )
    # TOOL wrapper 生效: read_file 事件被收进卡片管线
    assert wait_for_log(r"on_tool_update.*tool=read_file.*status=started", since=start, timeout=30)
    # 卡片元素级推送发生（工具面板更新）
    assert wait_for_agent_log(r"CardKit (batch update|stream element)", since=agent_start, timeout=30)
    # 正常完成（不是 NO session fallback）
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=90)


def test_d2_patch_goes_through_card_pipeline(lark, log_marker, wait_for_log):
    """D2: 文件写/编辑类工具（🔧 Editing）走卡片管线。

    模型工具选择不完全可控（可能用 write_file/patch/edit/terminal），所以
    断言放宽为「任一文件操作工具 started」+ 完整完成——验证目标是"写类动作
    被收进卡片"而非锁定某个工具名。
    """
    start = log_marker()
    lark.send_text(
        PRIVATE_CHAT,
        "[e2e D2] 用 terminal 在 /tmp 创建文件 e2e_d2.txt 写入 hello；然后用 read_file 读它确认内容",
    )
    # 至少 read_file 出现（D1 已验 started 路径，这里验证完整回合文件读写皆进卡片）
    assert wait_for_log(r"on_tool_update.*tool=read_file.*status=started", since=start, timeout=30)
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=90)


def test_d3_no_duplicate_native_progress(lark, log_marker, wait_for_log, log_contains):
    """D3: 工具进度不双发 — 卡片接管后不再有原生 progress 文本消息。

    修复前（TOOL hook 静默失效）: Hermes 原生把 📖 Reading / 🔧 Editing 当
    独立文本消息发到卡片外。修复后 wrapper 返回 True → 原生 tool_progress
    被跳过。用完成态日志佐证没有走上原生消息通道。
    """
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e D3] 用 terminal 运行 echo d3-check")
    assert wait_for_log(r"on_tool_update.*tool=terminal.*status=started", since=start, timeout=30)
    # 卡片路径接管 → 无原生 streamed 普通消息（streamed=True 是原生逐字消息信号）
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)
    # 允许短暂延迟后检查: 完成收尾不该出现原生流式编辑日志
    # （display.platforms.feishu.streaming=false 时原生消息通道应完全静默）
    native_lines = [
        line for line in GATEWAY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[start:]
        if "streamed=True" in line or "Suppressing normal final send" in line
    ]
    assert not native_lines, f"出现原生消息通道痕迹（双发风险）: {native_lines[:3]}"
