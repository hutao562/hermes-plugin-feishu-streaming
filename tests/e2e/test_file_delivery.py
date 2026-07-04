"""A1-A7 文件投递 e2e 测试 — 图片/文件/视频/markdown/post 落私聊 + 话题。"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import PRIVATE_CHAT, TOPIC_ROOT_MSG

pytestmark = pytest.mark.e2e


def test_a1_image_to_private_chat(lark, log_marker, wait_for_log, log_contains):
    """A1: 让 Hermes 发图到私聊，验证卡片正常完成。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e A1] 发一张桌面图片给我")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start, timeout=90)


def test_a2_image_to_topic(lark, log_marker, wait_for_log, log_contains):
    """A2: 让 Hermes 发图到话题（痛点回归 99992402）。"""
    start = log_marker()
    lark.reply_in_thread(TOPIC_ROOT_MSG, "[e2e A2] 用 reply 格式发一张桌面图片到这个话题")
    assert wait_for_log(r"\[cheerwhy-card\] 话题场景", since=start)
    assert not log_contains(start, r"99992402"), "踩了 send_image_file 不处理 thread_id 的坑"
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_a3_file_to_private_chat(lark, log_marker, wait_for_log):
    """A3: 让 Hermes 发文件到私聊。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e A3] 发我桌面任意一个文档文件")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_a4_file_to_topic(lark, log_marker, wait_for_log):
    """A4: 让 Hermes 发文件到话题。"""
    start = log_marker()
    lark.reply_in_thread(TOPIC_ROOT_MSG, "[e2e A4] 发我桌面任意一个文档到这个话题")
    assert wait_for_log(r"\[cheerwhy-card\] 话题场景", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_a5_video_to_private_chat(lark, log_marker, wait_for_log):
    """A5: 让 Hermes 发视频到私聊。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e A5] 发我桌面任意一个视频文件")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=90)


def test_a6_markdown_to_private_chat(lark, log_marker, wait_for_log):
    """A6: 让 Hermes 发 markdown 富文本到私聊。"""
    start = log_marker()
    lark.send_text(PRIVATE_CHAT, "[e2e A6] 用 markdown 格式列三个水果")
    assert wait_for_log(r"\[cheerwhy-card\] session created", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=60)


def test_a7_post_to_topic(lark, log_marker, wait_for_log):
    """A7: 让 Hermes 发 post 富文本到话题。"""
    start = log_marker()
    lark.reply_in_thread(TOPIC_ROOT_MSG, "[e2e A7] 用富文本 post 格式回复三个水果")
    assert wait_for_log(r"\[cheerwhy-card\] 话题场景", since=start)
    assert wait_for_log(r"on_completed_wait.*complete", since=start, timeout=60)
