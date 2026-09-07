"""错误恢复 e2e — 占位（E1 组，全 xfail）。

以下场景需要 mock 才能可靠触发（真实环境无法稳定制造）:
- CardKit 创建失败 → 纯文本 fallback（需 mock 飞书 API 失败）
- 卡片创建超时（需 mock 慢响应）
- UnavailableGuard（需真实删除飞书消息触发）

这些路径由单元测试覆盖（streaming/controller.py 的 fallback 分支 + zombie
guard 测试），E2E 留占位记录意图，避免假绿（xfail strict=False + 真发消息
浪费 3 个回合）。
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skip(
        reason="错误恢复需 mock 触发（创建失败/超时/消息删除），真实环境不可控；由单测覆盖",
    ),
]


def test_e1_fallback_paths_placeholder() -> None:
    """占位：CardKit 失败 fallback / 创建超时 / UnavailableGuard 的 E2E 触发方式待 mock 方案。"""
    assert True  # 占位，xfail 不实际跑
