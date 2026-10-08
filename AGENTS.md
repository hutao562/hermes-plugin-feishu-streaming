# AGENTS.md

## Project

Hermes Gateway **platform 插件**（`plugin/`，`kind: platform`）：子类化官方 `FeishuAdapter` 实现 draft-streaming 契约（`supports_draft_streaming`/`send_draft` → CardKit v2），以 `register_platform(name="feishu")` 同名注册顶替 bundled（registry last-writer-wins），把每回合回复渲染成打字机流式卡片。

**形态（v0.14.0 收敛）**：本仓库只有这一种形态，不再是可 pip 安装的包——分发物 = `plugin/` 目录拷贝（**自包含**：底层件 vendor 在 `plugin/_vendor/`，运行环境无需安装本包）。旧 AST 注入形态已归档在 git tag `archive/injection-mode`（回滚：checkout 该 tag 按当时 README 操作）。

**锚点**：draft 帧 metadata 的 `reply_to_message_id`（transport `_draft_metadata()` 注入）→ 卡片 reply 用户消息；无锚直发 chat（chat_id 做 reply 目标会 230001）。**sequence**：cardkit close/update 必须各自独立 +1（同号 300317）。**短回答**：transport `_MIN_NEW_MSG_CHARS=4` 吞帧 + finalize 时 draft 让位真发 → send() 无会话兜底现场开卡即完成。诊断日志 grep `[feishu-streaming]`（插件 logger 不进 gateway.log，engine logger 走 `gateway.run`）。部署需 `streaming.enabled: true` + `display.platforms.feishu.streaming: true`（transport: auto）。

## Commands

```bash
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3

# 测试（上游样本按 pinned commit 缓存 tests/samples/，缺失时下载校验；e2e 默认跳过）
$HERMES_PYTHON -m pytest tests/ -q
# E2E（需 Hermes 运行 + lark-cli 配置，默认跳过）
HERMES_HOME=~/.hermes $HERMES_PYTHON -m pytest -m e2e tests/e2e/ -v

# Lint / 类型（mypy 配置在 pyproject，files=plugin）
$HERMES_PYTHON -m ruff check plugin tests
$HERMES_PYTHON -m mypy

# 部署（plugin/ 自包含，改完拷贝 + 重启；重启约 50s，会打断进行中回合）
rm -rf ~/.hermes/plugins/feishu-streaming && cp -R plugin ~/.hermes/plugins/feishu-streaming
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway

# 自检（只读；仓库内运行会额外比对部署目录漂移）
$HERMES_PYTHON plugin/doctor.py          # 或 python3 ~/.hermes/plugins/feishu-streaming/doctor.py
```

**上游契约检查**：`plugin/contract.py` 是单一定义源，三个通道共用——
① `tests/test_upstream_compat.py` 对 pinned revision 回归；② 每日 hermes-check.yml 对上游 main 检查（失败自动开 hermes-compat issue）；③ doctor 对本机 hermes 验证。上游改 draft 契约时：核对语义 → 改 `plugin/adapter.py` → 同步锚点清单 + `tests/hermes_sources.json` revision。

## Architecture

```
plugin/
  __init__.py      register(ctx)：register_platform(name="feishu") + 钩子注册
                   + _fanout_to_profile_scopes（multiplex 每 profile 补注册）
  adapter.py       StreamingFeishuMixin：send_draft/send/edit_message/send_typing/
                   supports_draft_streaming/format_tool_event/send_document 覆写；
                   clarify 内联单选（类定义期覆写，零 monkey-patch）
  engine.py        ChatCardEngine：chat 键会话（draft 帧无 message 身份）；
                   ANSWER 整段置换（draft=全量快照）；redirect/followup 边界；
                   straggler_guard；NOTICE 追加（跨回合合并）
  contract.py      上游契约锚点清单（见上）
  doctor.py        只读自检（config 开关/凭据/部署漂移/契约/运行时/病征）
  _clarify.py      clarify 按钮卡构建 + 卡片回调处理（CLARIFY_STATE）
  _vendor/         streaming 核心（segments/flush/tooluse/segment_helper/text）+
                   cardkit builder/markdown + feishu client + config —— 现役唯一拷贝，
                   tests/test_cardkit|flush|segments|tooluse|text|config|feishu 直接测它
```

会话生命周期：探针/typing 即建卡（`on_turn_started`）→ draft 快照整段置换 ANSWER（100ms FlushController）→ reasoning 增量/工具事件进面板 → 终态 close + 完成卡重渲（footer 统计）。会话以 **chat** 为键（draft 帧天然不带 message 身份）；终态会话保留在 `_sessions` 直到被顶替（跨回合合并的前提）。

## Key Constraints

- **官方契约依赖**（`plugin/contract.py` 锚点）：probe 带 `chat_id`（插件在探针时机建卡）；`_draft_metadata` 的 `reply_to_message_id`（reply 锚来源）；`draft_stream_is_message`（一回合一张卡，工具边界不封卡）；`_MIN_NEW_MSG_CHARS`（短回答吞帧 → send() 兜底开卡）。
- **Clarify 内联单选**（`adapter.py` + `_clarify.py`）：`send_clarify` 类定义期覆写，**绝不能走 self.send**——终态拦截会误完成 streaming 卡，text 兜底直发 `_feishu_send_with_retry`。按钮 value 携带完整 choice 文本（gateway text-intercept 会提前 resolve entry，按钮点击靠 value 兜底）。
- **redirect 即刻拆卡**：hermes redirect 是**同回合改锚续跑**（`agent.redirect` 取消当前 model 请求、注入纠正、循环重试），draft 锚不变。↪ ack 一到 `mark_redirect` 立即收旧开新：旧卡红标 NOTICE（红 header 强制显示，不受配置约束），新卡以 ack 的 reply_to（=用户纠正消息 id）为锚 loading 起步，本回合 reasoning/工具/正文从第一毫秒起全落新卡。`straggler_guard` 前缀比对拦截旧请求取消前的残尾快照（老内容超集），首个不相关内容通过后解除。adapter busy-ack 分支含 `creating` 会话（ack 早于建卡也不漏标记），此时返回**无 id 成功**（合成 `lark-card:None` 会让后续 edit 打到原生链路）。
- **followup 边界**：draft 锚变成回合未知新锚 = 新回合（queued followup 被 drain）→ 旧卡按已有内容绿色收尾，新卡以新锚开卡。redirect 回合的 `accepted_anchors` 含新旧两锚——工具边界会换 consumer 重锚，锚回摆不算新回合（曾把新卡拦腰拆成双卡）。
- **跨回合合并**：send 带 `thread_id` metadata 且无 notify = bg 交付特征 → `append_notice` 进最近卡片（**须查 `session_for` 含终态而非 active_session**——bg 回合常在主回合完成后到达）；无可用卡片回原生文本。
- **busy ack 进心跳行**：`_BUSY_ACK_PREFIXES`（↪⏳⚡⚠️♻️）识别，进卡片末尾状态行，完成卡重渲自然消失；不渲染成卡、不并进完成卡。
- **卡片体积上限**：飞书 JSON 体积上限 200860 "card over max size"——思考面板头尾摘录（`cap_reasoning_text`）、工具步数封顶 15、单步结果 900+240 截断、回答正文不截。
- **multiplex / scope 桶**：插件 `register()` 进程级只跑一次；平台注册表按 profile scope 分桶，`_fanout_to_profile_scopes` 把条目补注册到每个 live profile（profile 在自己 config 的 `plugins.disabled` 写 `feishu-streaming-platform` 可退出，fail-open）。每 profile 独立 engine + client（凭据按 profile 作用域绑定 adapter 实例的 lark client）。诊断特征：gateway.log 每 boot 只 1 行 `adapter factory`（应为每 profile 1 行）。
- **诊断黑洞**：`hermes_lark_streaming.plugin` logger 不进 gateway.log（hermes logging 配置）；诊断日志统一 `logging.getLogger("gateway.run").info("[feishu-streaming] ...")`；register() 期 hermes logging 未配置，关键生命周期事件走 `_diag_log` 落 `~/.hermes/logs/feishu-streaming-plugin.log`。
- **live 运行时**：`ps aux | grep gateway run` → shim 进程，真源码在 `~/.hermes/installs/*/environments/*/venv` 的 editable 指向 workspace；`~/.hermes/hermes-agent/` 有副本但不被 import。判「包装没装进某 venv」必须看 site-packages，cwd import 会假报警。
- **测试**：注入形态的单测已随归档移除；streaming 核心单测（cardkit/flush/segments/tooluse/text/config/feishu）测 `plugin._vendor`；插件行为测试在 `test_plugin_mode.py`/`test_plugin_features.py`（`_FakeBaseAdapter` + `_mock_client`，不依赖 hermes 源树）；契约测试 `test_upstream_compat.py`（pinned 样本）；doctor 测试 `test_doctor.py`（tmp HERMES_HOME 树）。
- Commit messages: body 用无序 bullet list（`- item`）。

## hermes 升级影响

插件模式对上游改动的敏感面比注入模式小得多（不改上游源码），但依赖契约锚点。升级 hermes 后跑一次 `doctor.py`（「上游契约」项变 ✗ 即是破坏）+ 发一条消息实测流式。已知锚点破坏的症状：探针签名变化 → 卡片不建/迟建；`_draft_metadata` 变化 → 卡片 reply 锚丢失（230001 或直发）；`draft_stream_is_message` 移除 → 工具边界封卡真发、一回合多卡。

## 注入模式（已归档）

完整实现（patcher/split_gateway/split_cron/watchdog/self-heal 及其测试）在 git tag `archive/injection-mode`。生产已切插件模式（2026-10-07），注入模式的 pip entry-point 已从运行环境卸载、gateway 源码零 marker。**不要在主干复活注入代码**；回滚需求走 tag checkout。
