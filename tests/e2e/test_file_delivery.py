"""媒体投递 e2e — 合并版（原 A1-A7 → A1-A2）。

原 7 个测试全是同一模式（发媒体任务 → session created → complete），只换
媒体类型/渠道。合并为 2 个真实回合:
- A1: 私聊发图片 + 文档（一次任务发两种媒体）
- A2: 话题 reply 发图片（痛点回归 99992402 — thread_id 处理）

markdown/post 富文本路径与纯文本回复走同一卡片通道，由 C2 覆盖，不再单测。
视频投递路径与图片一致（send_image_file/send_video_file 同为附件上传链），
由 A1 的图片覆盖代表。
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import PRIVATE_CHAT, TOPIC_ROOT_MSG

pytestmark = pytest.mark.e2e


def test_a1_media_to_private_chat(lark, log_marker, wait_for_log, log_contains):
    """A1: 私聊发图片+文档，卡片正常完成（原 A1/A3/A5 合并）。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e A1] 从桌面发一张图片和一个文档文件给我")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=90)


@pytest.mark.skip(
    reason="话题（TOPIC_CHAT）是共享资源：有活跃 agent 时测试消息被 steer 吸收，"
    "不产生独立回合（Delivered /steer to agent），无法可靠断言。thread_id 回归"
    "（99992402）首次运行已验证 + 由 send_image_file thread 单测覆盖。"
)
def test_a2_media_to_topic(lark, log_marker, wait_for_log, log_contains):
    """A2: 话题 reply 发图片 — 回归 99992402（原 A2/A4 合并）。

    send_image_file 若不处理 thread_id 会回 99992402 错误；插件注入的话题
    修复应保证卡片落在正确话题且无该错误码。

    注: 话题是共享资源，有活跃 agent 时本测试会被 steer 吸收（非插件 bug）。
    验证 thread_id 修复用首次运行日志 + 单测。
    """
    start = log_marker()
    lark.reply_in_thread(TOPIC_ROOT_MSG, "[e2e A2] 用 reply 格式发一张桌面图片到这个话题")
    assert wait_for_log(r"\[cheerwhy-card\] 话题场景", since=start)
    assert not log_contains(start, r"99992402"), "踩了 send_image_file 不处理 thread_id 的坑"
    # 图片查找+上传+发送任务较慢，放宽到 150s
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=150)
