"""hermes 源树类型的延迟导入与测试环境兜底.

gateway 包（SendResult / ToolCallChunk 等）只在 hermes 进程内可导入；CI/单测
环境没有 hermes 源树，这里提供同形状兜底，保证插件包可导入、可单测。
"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass
class _FallbackSendResult:
    """与 BasePlatformAdapter.SendResult 同形状的兜底（成功路径只读 success/message_id）."""

    success: bool
    message_id: str | None = None
    error: str | None = None
    raw_response: Any = None
    retryable: bool = False
    retry_after: float | None = None
    continuation_message_ids: tuple = ()


def send_result(**kwargs: Any) -> Any:
    try:
        from gateway.platforms.base import SendResult
    except ImportError:
        return _FallbackSendResult(**kwargs)
    return SendResult(**kwargs)


def is_tool_chunk(event: Any) -> bool:
    try:
        from gateway.stream_events import ToolCallChunk
    except ImportError:
        return hasattr(event, "tool_name")
    return isinstance(event, ToolCallChunk)
