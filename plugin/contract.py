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
    # 插件直接父类（2026-11-02 补锚：ccc6956 改 704 行时每日检查对此文件是
    # 盲的，只靠人工核查——覆写面签名漂移必须自动报警）
    "plugins/platforms/feishu/adapter.py": (
        ("class FeishuAdapter", "插件直接父类（子类化对象）"),
        ("def send(", "send 覆写契约（终态拦截/兜底开卡/跨回合合并入口）"),
        ("def edit_message(", "edit_message(finalize=) 覆写契约（stale-finalize 重定向）"),
        ("def send_typing(", "send_typing 覆写（typing 徽章）"),
        ("def send_document(", "文档交付覆写（上传 + reply 卡片消息）"),
        ("def _dispatch_synthetic_event(", "合成事件通道（footer 🧠⇄ /model、clarify 按钮）"),
        ("def _resolve_source_chat_type(", "chat_type 解析（event_chat_type 传 raw_type 的对端）"),
        ("_feishu_send_with_retry", "text 兜底直发通道（clarify/降级路径）"),
    ),
    # clarify 交互依赖面（2026-11-11 补锚：本文件第二次在无锚状态下变动——
    # 11-06 SAME、a55a299 +26/−15（authority 字段 + 超时默认 3600→90s）——
    # 插件 _clarify 的按钮回调与「其他」转文本全走此模块。四针在 pinned
    # （2bd378e / 0.23.0）与上游 main（a55a299）双版核对在场。
    # attempt_text_response 太新（a55a299 窗口才拆出）不锚。
    "tools/clarify_gateway.py": (
        ("def resolve_gateway_clarify(clarify_id: str, response: str) -> bool",
         "网关侧解 clarify（插件按钮回调的落点；id+response 口径可变，签名不可变）"),
        ("def mark_awaiting_text(clarify_id: str) -> bool",
         "「其他」按钮转文本捕获（插件 clarify other 分支依赖）"),
        ("def resolve_clarify_timeout(config: dict) -> int",
         "clarify 过期时长解析（插件过期反馈分支依赖；上游 11-11 起默认 90s，"
         "产品需长窗口在 hermes config 设 agent.clarify_timeout）"),
        ("def get_pending(clarify_id: str)",
         "按 id 取挂起 clarify 条目（engine/adapter 过期反馈判定的查询面）"),
    ),
}

# ── 平台启用面（2026-10-22 上游一线引入；2026-11-02 升级事故后锚定）──
# 新版 hermes 的平台启用由条目自带 env 桥驱动：config pass 跑 EnvCred 步骤把
# .env 凭据写进 platforms.<name>.extra 并在凭据在场时启用平台。插件同名顶替
# feishu 条目后必须透传官方桥（plugin/__init__._official_feishu_env_bridge），
# 否则凭据门 is_connected 恒 False → feishu 永不启用（2026-11-02 实测：插件
# register 正常但 Gateway running with 1 platform(s)、无 Connecting to feishu）。
# 这些文件在 pinned 基线（2237be3 / 0.21.1）尚不存在——插件按能力探测透传，
# 老版本无此面也完整支持（kwargs 省略）——故按「可选锚点」处理：文件在场才
# 检查（hermes-check 每日对 main、doctor 对本机），缺席不算缺失。
OPTIONAL_ANCHORS: dict[str, tuple[tuple[str, str], ...]] = {
    "plugins/platforms/feishu/gateway_env.py": (
        ("ENV_OVERRIDES", "env→config 桥步骤来源（顶替条目必须透传——缺了 feishu 永不启用）"),
        ('ENV_ENABLE_CREDENTIALS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")',
         "凭据名清单（is_connected 凭据门的 env 侧对应物）"),
    ),
    "gateway/platform_registry.py": (
        ("env_overrides", "PlatformEntry env 桥字段（能力探测目标）"),
        ("last writer wins on name clash", "同名顶替语义（register_platform 顶替 bundled 的前提）"),
    ),
}


@dataclass
class AnchorMiss:
    file: str
    needle: str
    why: str


def check_tree(tree: Path) -> list[AnchorMiss]:
    """对一份 hermes 源码树检查全部契约锚点，返回缺失清单（空 = 兼容）.

    ``tree`` 是 hermes-agent 源码根（含 gateway/）。必选锚点文件缺失按整文件
    缺失报告；可选锚点（OPTIONAL_ANCHORS）文件缺席不算缺失（老版本 hermes
    无此面），在场则逐针检查。
    """
    misses: list[AnchorMiss] = []
    for relative, anchors in (*CONTRACT_ANCHORS.items(), *OPTIONAL_ANCHORS.items()):
        path = tree / relative
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            if relative in CONTRACT_ANCHORS:
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
