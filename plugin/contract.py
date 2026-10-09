"""插件依赖的上游 hermes 契约锚点 — 单一定义源.

本插件以 ``kind: platform`` 子类化官方 ``FeishuAdapter`` 实现 draft-streaming
契约，运行正确性依赖上游若干内部行为（probe 签名、draft 元数据、短回答吞帧
口径等）。此模块把这些依赖收敛成**可检查的锚点清单**，三方消费：

- ``tests/test_upstream_compat.py`` — 对 CI 固定（revision-pinned）上游样本验证；
- ``hermes-check.yml``（每日）— 对 hermes main 分支验证，失败自动开 issue；
- ``plugin/doctor.py`` — 对本机安装的 hermes 验证（自检入口）。

锚点升级方式：上游改名时，先在本地核对新版源码语义，再同步本清单 +
``tests/hermes_sources.json`` 的 pinned revision。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# 相对 hermes 源码根的文件 → 该文件必须包含的锚点（子串匹配，带语义注释）。
# 子串而非正则：锚点应力求贴近"契约"本身（调用形态/元数据键），上游无害的
# 重排不该误报——宁可锚点粒度粗一点，也别让检查流于形式。

# ── cron wrap 信封（cron 卡解析的字面契约，v0.16.0）──
# plugin/adapter._parse_cron_payload 的输入格式；常量即锚点 needle 的来源：
# 上游改文案 → 锚点缺失 → 每日 hermes-check 当天报警，而非 cron 卡静默降级。
CRON_WRAP_HEADER = "Cronjob Response: "
CRON_WRAP_JOBID_LINE = "(job_id: "
CRON_WRAP_DIVIDER = "\n-------------\n\n"
CRON_WRAP_FOOTER_PREFIX = '\n\nTo stop or manage this job, send me a new message (e.g. "stop reminder '

CONTRACT_ANCHORS: dict[str, tuple[tuple[str, str], ...]] = {
    "gateway/stream_consumer_transport.py": (
        ("supports_draft_streaming(chat_id=", "探针带 chat_id（插件在探针时机建卡）"),
        ("def _draft_metadata", "draft 帧 metadata 构造（卡片 reply 锚的来源）"),
        ('md.setdefault("reply_to_message_id", self._initial_reply_to_id)',
         "draft 锚 = 用户消息 id（followup 边界 / 新卡 reply 锚依赖）"),
        ("await self.adapter.send_draft(", "draft 帧下发调用（send_draft 覆写契约）"),
        ("draft_id=self._draft_id", "draft_id 传参（插件日志按 draft_id 去重）"),
        ("_MIN_NEW_MSG_CHARS", "短回答吞帧口径（send() 无会话兜底开卡的存在理由）"),
        ('(self.cfg.transport or "edit").lower()', "transport 选择读 cfg.transport"),
    ),
    "gateway/stream_consumer.py": (
        ("draft_stream_is_message", "流即消息（一回合一张卡，工具边界不封卡真发）"),
        ("def _bump_draft_id", "segment 边界换 draft_id（锚回摆/新卡判定相关）"),
        ('meta["reply_to_message_id"] = self._initial_reply_to_id',
         "非 draft 路径同样带 reply 锚元数据"),
    ),
    "cron/scheduler_delivery.py": (
        (f'f"{CRON_WRAP_HEADER}{{task_name}}', "cron wrap 头文案（cron 卡任务名/信封解析输入）"),
        ('f"(job_id: ', "cron wrap job_id 行前缀（cron 卡追溯行解析——表达式可变，前缀不可变）"),
        ('f"-------------\\n\\n"', "cron wrap 分隔线（cron 卡正文起点解析）"),
        ('"To stop or manage this job, send me a new message "',
         "cron wrap 管理提示尾（cron 卡尾部解析）"),
        ('get("wrap_response", True)', "cron.wrap_response 默认开（false → cron 卡无 header 降级）"),
    ),
    "cron/scheduler.py": (
        ("**Status:** script failed",
         "脚本门失败形状（cron 卡红 header 判定之一；⚠️ Cron ' 前缀在 "
         "scheduler_failure_copy.py，pinned revision 尚未抽出该模块，未锚——改版只降级为蓝卡）"),
    ),
}


@dataclass
class AnchorMiss:
    file: str
    needle: str
    why: str


def check_tree(tree: Path) -> list[AnchorMiss]:
    """对一份 hermes 源码树检查全部契约锚点，返回缺失清单（空 = 兼容）.

    ``tree`` 是 hermes-agent 源码根（含 gateway/）。文件缺失按整文件缺失报告。
    """
    misses: list[AnchorMiss] = []
    for relative, anchors in CONTRACT_ANCHORS.items():
        path = tree / relative
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            misses.append(AnchorMiss(relative, "<file missing>", f"上游文件不存在：{relative}"))
            continue
        for needle, why in anchors:
            if needle not in source:
                misses.append(AnchorMiss(relative, needle, why))
    return misses


def render_misses(misses: list[AnchorMiss]) -> str:
    """人读版缺失报告（doctor / CI issue 复用）."""
    lines = []
    for miss in misses:
        lines.append(f"  ✗ {miss.file}: 缺少锚点 {miss.needle!r} — {miss.why}")
    return "\n".join(lines)


def pinned_revision() -> str:
    """CI 固定验证的上游 revision（tests/hermes_sources.json）."""
    manifest_path = Path(__file__).resolve().parent.parent / "tests" / "hermes_sources.json"
    try:
        return str(json.loads(manifest_path.read_text(encoding="utf-8"))["revision"])
    except Exception:
        return "unknown"
