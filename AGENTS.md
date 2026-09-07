# AGENTS.md

## Project

Hermes Gateway plugin that injects hooks into `~/.hermes/hermes-agent/gateway/run.py` and `cron/scheduler.py` via AST patching to provide real-time streaming Feishu/Lark CardKit v2.0 cards with typewriter effect.

## Commands

```bash
# All commands must use Hermes's venv Python
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3

$HERMES_PYTHON -m hermes_lark_streaming verify     # Check compatibility (safe, no file changes)
$HERMES_PYTHON -m hermes_lark_streaming install    # Inject hooks into run.py and cron/scheduler.py
$HERMES_PYTHON -m hermes_lark_streaming uninstall  # Remove hooks
$HERMES_PYTHON -m hermes_lark_streaming restore    # Restore from .hermes_lark.bak backup
$HERMES_PYTHON -m hermes_lark_streaming status     # Show patch status

# Install for development
$HERMES_PYTHON -m pip install -e .
$HERMES_PYTHON -m pip install -e ".[dev]"  # test dependencies

# Lint
$HERMES_PYTHON -m ruff check hermes_lark_streaming tests
$HERMES_PYTHON -m mypy hermes_lark_streaming/

# Run tests (local run.py first, CI auto-downloads from GitHub)
$HERMES_PYTHON -m pytest tests/ -q

# E2E 测试（需 Hermes 运行 + lark-cli 配置，默认跳过；CI 无飞书环境安全）
HERMES_HOME=~/.hermes $HERMES_PYTHON -m pytest -m e2e tests/e2e/ -v
```

## Architecture

```
gateway/run.py (Hermes)
  └─ AST-injected hooks (patcher.py defines markers + injection logic)
       │
       ├─ on_feishu_normalize   → patch.on_feishu_normalize() (inline, fixes false thread_id)
       ├─ on_message_started    → controller.on_message_started()
       ├─ on_tool_updated       → controller.on_tool_update()
       ├─ on_answer_delta       → controller.on_answer()
       ├─ on_thinking_delta     → controller.on_thinking()
       ├─ on_reasoning_delta    → controller.on_reasoning()
       ├─ on_background_review_message → controller.defer_background_review()
       ├─ on_message_interrupted → controller.on_interrupted()
       ├─ on_queued_followup_boundary → patch.on_queued_followup_boundary() (finalize card before drain, set response_previewed/already_sent)
       ├─ on_queued_followup_result   → patch.on_queued_followup_result() (carry deepest completion ID through recursive merge)
       ├─ on_message_completed_wait → controller.on_completed_wait()
       ├─ on_message_aborted    → controller.on_aborted()
       └─ on_background_deliver → controller.on_background_deliver()
  └─ ADAPTER_INIT (injected AFTER gateway:startup emit in start()) → clarify.patch_feishu_adapter(self.adapters)
       └─ patches FeishuAdapter.send_clarify (class method) + replaces SDK card-action processor.f (clarify inline single-select)

cron/scheduler.py (Hermes)
  └─ CronPatcher (patcher.py) injects on_cron_deliver into _deliver_result
       └─ intercepts feishu/lark targets → build_cron_card → send_card_to_chat

StreamCardController (singleton, controller.py)
  ├─ CardSession per message (state machine: IDLE→CREATING→STREAMING→COMPLETED/FAILED/ABORTED)
  │   └─ stream segments: CardSession.segment_state (SegmentState)
  ├─ _interrupt_map — old_message_id → new_message_id mapping for interrupt redirect
  ├─ FlushController (streaming/flush.py) — throttles CardKit updates (100ms)
  ├─ ToolUseTracker (streaming/tooluse.py) — tracks tool call lifecycle with icon/status mapping
  ├─ UnavailableGuard (streaming/unavailable_guard.py) — auto-terminates on message delete/recall
  └─ ImageResolver (streaming/image.py) — async download + re-upload markdown images as Feishu img_key

Streaming card runtime (streaming/)
  ├─ controller.py — StreamingController: create card, flush, split/rollover, and cron delivery orchestration
  ├─ session.py — CardSession per message (state machine: IDLE→CREATING→STREAMING→COMPLETED/FAILED/ABORTED)
  ├─ segments.py — SegmentState: flat segment list (reasoning / answer / tool), same-type appends, cross-type creates new
  ├─ segment_helper.py — CardKit action builders, element estimates, and tool split point selection
  ├─ text.py — reasoning tag parsing and final answer text cleanup
  ├─ flush.py — FlushController: throttles CardKit updates (100ms)
  ├─ tooluse.py — ToolUseTracker: tool call lifecycle tracking with icon/status mapping
  ├─ image.py — ImageResolver: async download + re-upload markdown images as Feishu img_key
  └─ unavailable_guard.py — UnavailableGuard: auto-terminates on message delete/recall

FeishuClient (feishu.py) — lark-oapi SDK wrapper
  ├─ CardKit streaming API — update single elements at 100ms intervals

Clarify inline single-select (clarify.py) — monkey-patches FeishuAdapter at runtime
  ├─ patch_feishu_adapter(adapters) — patches send_clarify class method + replaces SDK card-action processor.f
  ├─ _find_feishu_adapter_class — scans sys.modules for the real FeishuAdapter (hermes_plugins.feishu_platform, not the source-path shadow)
  ├─ _build_clarify_card — schema-1.0 card: markdown question + numbered options list + numbered buttons (button plain_text can't wrap, so full text lives in markdown)
  └─ _handle_clarify_card_action — choice → resolve_gateway_clarify + resolved card; other → mark_awaiting_text + awaiting card

Self-heal & watchdog (升级自愈三层防御)
  ├─ __init__.py: register(ctx) — hermes_agent.plugins entry point; 网关启动时检测 run.py 补丁态，未完整则 verify+apply 重打 + launchctl kickstart 重启（streaming.self_heal 默认 true）
  └─ watchdog.py — launchd WatchPaths 守护（com.hermes-lark.watchdog）：run.py 一变即 uninstall+install+kickstart（防抖 10s），install/uninstall 命令自动装/卸

Card templates (cardkit/)
  ├─ builder.py — builds Feishu card JSON
  │   ├─ _build_header — card-level header with status-based theming (blue/green/red)
  │   ├─ build_streaming_card_v2 — initial streaming CardKit v2 card (header_enabled, text_size)
  │   ├─ build_complete_card — final card, renders segments in order (header_enabled, body_text_size, footer_enabled, footer_text_size)
  │   ├─ build_cron_card — static card for cron delivery
  │   └─ build_background_card — static card for background task delivery
  ├─ markdown.py — CardKit markdown normalization and table/image helpers
  └─ i18n.py — localized CardKit labels
```

## Key Constraints

- Hermes `>= 0.14.0` (2026.5.16) required. `patcher.py` targets specific function names in Hermes's `gateway/run.py` (`_handle_message_with_agent`, `progress_callback`, `_stream_delta_cb`, `_interim_assistant_cb`) and `cron/scheduler.py` (`_deliver_result`). If Hermes changes these, `verify` will catch it.
- The interrupt hook is injected at the `"Restart typing indicator"` comment in `_run_agent`. It fires when `was_interrupted and next_message_id` are both truthy. The `_interrupt_map` redirects completion from `old_id` to the new session, handling nested interrupts (A→B→C).
- The completion hook installed into `gateway/run.py` is async: `on_message_completed_wait` awaits queued CardKit creation/finalization before setting `already_sent`. Upgrades must rerun `uninstall` + `install` so older sync completion hooks are removed from Hermes gateway.
- The `_thinking_hook` has a `not already_streamed` guard (patcher.py:103) — thinking deltas are skipped once answer streaming has begun.
- The NORMALIZE hook (`on_feishu_normalize`) is injected at `source = event.source` in `_handle_message`, before any other processing. It detects Feishu quoted messages with a false `thread_id` (set by the Feishu adapter but absent in raw event) and clears it, preventing `_reply_anchor_for_event` from returning the wrong ID.
- The `anchor_id` mechanism: for Feishu quoted messages, `_reply_anchor_for_event(event)` returns `reply_to_message_id` instead of `event.message_id`. The START hook passes both — `message_id` for session identity and streaming callback lookup, `anchor_id` for card delivery (reply target). Sessions are registered under both keys.
- Reasoning display depends on upstream providing `<thinking>`/`<thought>`/`<antthinking>` tags or `Reasoning:\n` prefix in text. Native API reasoning blocks (Anthropic extended thinking, DeepSeek reasoning_content) are available via `on_reasoning_delta` hook when `display.platforms.feishu.show_reasoning` is enabled.
- CardKit v2.0 elements (collapsible_panel, streaming_mode) only work with `"schema": "2.0"` cards.
- Streaming cards use a single CardKit card for the message lifecycle: elements are dynamically created in event arrival order. When CardKit creation fails, the plugin yields to the Hermes Gateway default reply.
- The follow-up drain hooks manage card lifecycle for Hermes's queued follow-up messages (triggered when `busy_text_mode: queue` or `busy_input_mode: queue`). `on_queued_followup_boundary` is injected at `was_interrupted = result.get("interrupted")` in `_run_agent` — it finalizes the current card and sets `response_previewed`/`already_sent` on the result dict before the drain loop processes the queued message. `on_queued_followup_result` is injected at `return _preserve_queued_followup_history_offset(...)` and uses `setdefault` to carry the deepest `_hermes_lark_completion_id` back through the recursive merge chain.
- The COMPLETE hook uses `_lark_completion_id = agent_result.get('_hermes_lark_completion_id') or event.message_id` — in follow-up scenarios the deepest message_id propagates up via `on_queued_followup_result`, ensuring the correct card session is finalized. Non-follow-up scenarios fall back to `event.message_id`.
- The background deliver hook (`on_background_deliver`) is injected in `_run_background_task` after `adapter.extract_images(response)`. It uses `ReplyMessage` API with `event_message_id` as anchor, so cards land in the correct topic. On success, `text_content` is cleared to avoid duplicate text delivery, while images and media files continue through the original Hermes loops. On failure, the original Hermes delivery logic runs as fallback.
- **Clarify 内联单选** (`clarify.py`)：飞书 adapter 没实现 `send_clarify`，默认走 base.py 的数字列表 text fallback。本插件 monkey-patch 补上单选按钮卡，两处 patch：(1) `FeishuAdapter.send_clarify` 类方法 → 渲染 schema-1.0 卡（markdown 编号列表展示完整选项 + 编号按钮，因飞书 button `plain_text` 不支持换行/长文本截断）；(2) **替换 lark SDK 卡片回调 processor.f** —— SDK 在 `connect()` 时把 `adapter._on_card_action_trigger`（绑定方法）快照进 `event_handler._callback_processor_map["p2.card.action.trigger"].f`（注意 key 是**点号** `p2.card.action.trigger`，不是下划线——register 函数名 `register_p2_card_action_trigger` 带下划线，但 dict key 带点号，极易搞混），事后 patch 类无效，所以直接换该 processor 的 `.f` 指向 wrapper。wrapper 检测 `hermes_clarify_action` key → 进 clarify handler，否则转发原逻辑（approval/update-prompt 不受影响）。点选项 → 同步返回 resolved 卡 + 异步 `resolve_gateway_clarify` 唤醒 agent 线程；点「其他」→ `mark_awaiting_text` + 下条非斜杠消息由 gateway 文本拦截接手。**关键时序**：注入点 `HERMES_LARK_ADAPTER_INIT` 在 run.py 的 `await self.hooks.emit("gateway:startup", ...)` 之后（所有 adapter 已 connect、event_handler 已建），此时才能拿到 feishu 实例去替换它的 processor。**模块路径陷阱**：hermes plugin loader 把 `plugins/platforms/feishu` 加载成 `hermes_plugins.feishu_platform`（slug 派生），和源码 import 路径不同——直接按源码路径 import 会拿到影子类，patch 打上去对运行实例无效（症状：日志显示 patched 但按钮卡/回调不生效）。`_find_feishu_adapter_class` 扫 `sys.modules` 找真身（优先 `hermes_plugins.*`）。**entry 失活陷阱**：gateway text-intercept（`_maybe_intercept_clarify_text`，`include_choice_prompts=True`）会在用户发**任意**文字时提前 resolve 掉按钮卡 clarify（即使没点「其他」），之后按钮点击因 entry 已清会失败——button value 里多带一份 `"text": choice` 兜底，`_handle_clarify_card_action` 优先用 value text 而非 entry round-trip。配置开关 `streaming.clarify_inline`（默认 true）关闭后退回 text fallback。改了 `clarify.py` 后只需 `gateway restart`（editable install 即时生效），但改了 `patcher.py` 的注入点逻辑必须 `uninstall && install` 重打 run.py。
- Commit messages: body should use bullet list format (unnumbered `- item`).

## 跨回合合并（浮浮酱的本地改动，官方上游没有）

官方 Cheerwhy 是「单消息单卡」——每个 agent 回合（message_id）一张卡。hermes background process 完成会注入 `synth_event(message_id=None, internal=True)`（`run.py:14588`）触发新回合，官方对 `message_id=None` 直接跳过（`on_message_started` return）+ `_get_active_session` 对终态返 None → background 回合内容走纯文本，「一个对话任务」被拆成多段散落（卡片外）。

**改动目标**：让 `message_id=None` 的回合复用同 chat 最近卡片（即使已 COMPLETED），实现「一个对话任务（用户消息 + 触发的所有 background 回合）全合并一张卡」。用 `message_id` 区分：用户新消息（`om_xxx`）→ 新卡；background/内部回合（`None`）→ 复用同 chat 卡。

**改的 7 文件**（`hermes_lark_streaming/`）：
- `controller.py`：加 `_chat_index`（chat→msg 反查）+ `_find_session_by_chat` + `_reactivate_session`（COMPLETED→STREAMING + `flush.reset_for_reactivate` + `segment_state.begin_new_turn` + `reused=True`）+ `_resolve_session`（message_id 优先，None 时 chat fallback + 终态重激活）；`on_message_started` None 分支复用（不新建）；delta 回调（`on_answer`/`on_thinking`/`on_reasoning`/`on_tool_update`）+ `on_completed_wait` 加 `chat_id: str | None` 参数；`_apply_completion_payload` 复用场景（`session.reused`）强制新建 ANSWER segment（绕过原 `not any(ANSWER)` 检查）；`_completion_session` 加 chat_id fallback 接受 COMPLETED 复用
- `streaming/segments.py`：加 `_force_new_segment` 标志 + `begin_new_turn()`（终结末尾 segment + 强制下个 delta 新建，回合分隔，避免两回合 answer 拼在一起）；`on_reasoning_delta`/`on_answer_delta`/`on_tool_event` 加 `and not self._force_new_segment` 检查
- `streaming/session.py`：加 `reused: bool` 字段
- `streaming/flush.py`：加 `reset_for_reactivate()`（撤销 `mark_completed`，重置 `_completed`/`_flush_in_progress`/timer）
- `streaming/controller.py`：`_do_complete_card` finally 改为 **COMPLETED 不立即 cleanup**（`if session.state != COMPLETED: cleanup`）——保留供 background 复用，靠 `_prune_stale_sessions` TTL 清理。**关键 bug 修复**：原 `if not reused: cleanup` 因首回合完成时 `reused=False` 会立即清掉 session，background 回合 `_find_session_by_chat` 找不到
- `patcher.py`：delta hook（`_tool_hook`/`_answer_hook`/`_thinking_hook`/`_reasoning_hook`）+ `_complete_hook` 加 `chat_id=source.chat_id`（这些 hook 注入在 `_run_agent_inner` 的闭包 callback，`source` 是形参，能直接读 `source.chat_id`）
- `patch.py`：delta 函数（`on_answer_delta` 等）加 `chat_id` 透传给 controller + `message_id` 类型放宽 `str | None`

**踩坑**：曾试合成 message_id `bg_proc_{session_id}` 给 background 回合，但飞书 API 拒绝（message_id 必须 `om_xxx`）→ 改成按 chat 复用（不创建新卡，避开 API 校验）。

**测试**：`tests/test_merge_background.py`（5 个：复用+重激活 / 无可复用跳过 / 用户新消息新卡 / delta chat fallback / 重激活仅 COMPLETED）+ 原 430 回归 = **435 全绿**。**改了 `patcher.py` 后必须 `uninstall && install` 重打 AST patch**（否则 run.py 还是旧 hook），再 restart。

**诊断**：`hermes_lark_streaming` logger 不进 `gateway.log`（hermes logging 配置问题），合并诊断用 `logging.getLogger("gateway.run").info("[cheerwhy-merge] ...")`（在 `on_message_started` None 分支 + `_reactivate_session`），grep `[cheerwhy-merge]` 看合并是否触发（`bg turn msg=None ... reactivated=True` / `session reactivated ... cross-turn merge`）。

## hermes 升级后自愈（三层防御）

hermes 自动升级是**原子流程**：拉新代码覆盖 `gateway/run.py`（清掉 AST hook）→ 立即 `gateway restart`。补丁赶不上这趟车。为此建了三层防御，升级后**通常无需手动操作**：

### 第 1 层：`register()` 启动时自愈（核心）
`hermes_lark_streaming/__init__.py` 的 `register(ctx)` 是 `hermes_agent.plugins` entry point（`pyproject.toml` 已声明）。网关每次启动（含升级后自动 restart）经 `discover_plugins()`（`gateway/run.py` 的 startup）调用到这里。`register()` 逻辑（`streaming.self_heal` 默认 true，关闭后退回手动）：
1. `Patcher.is_fully_patched()` 检测 run.py 磁盘标记。
2. 未完整打补丁（升级抹掉了）→ `verify_target()` + `apply()` 原地重打 + 同步 cron hook。
3. 已打补丁但 run.py mtime 晚于本进程启动时间（补丁是后打的）→ 判定当前进程没加载补丁。
4. 任一返回 True → `_maybe_restart_gateway()` 用 detached `nohup sleep 2 && launchctl kickstart gui/$UID/ai.hermes.gateway` 延迟重启，让补丁版重新加载。
5. `verify_target()` 失败（hermes 改了函数名）→ 只记日志不 crash，降级为等手动 reinstall。

**防无限 restart 循环**：重打后磁盘变完整，下次启动 `is_fully_patched()`=True，再走 mtime 比较——restart 后新进程启动时间 > run.py mtime → 不再触发。**关键前提**：插件必须列在 `config.yaml` 的 `plugins.enabled`（entry-point 插件 opt-in），`install` 命令自动追加 `hermes-lark-streaming`，`uninstall` 自动移除。**日志走 `gateway.run` logger**（`hermes_lark_streaming` logger 不进 gateway.log），grep `[hermes-lark] self-heal` 看自愈是否触发。

### 第 2 层：launchd WatchPaths 守护（双保险）
`hermes_lark_streaming/watchdog.py` 装 launchd job（label `com.hermes-lark.watchdog`）监听 `gateway/run.py` 变化，一变就跑 `~/.hermes/.hermes_lark_watchdog.sh`（防抖 10s）：`uninstall && install && launchctl kickstart`。由 `install`/`uninstall` 命令自动装/卸到 `~/Library/LaunchAgents/`。即便 `register()` 路径出问题（如插件没 enable），run.py 一变守护也会重打+重启。日志 `~/.hermes/logs/hermes_lark_watchdog.log`。

### 第 3 层：`status` 运行时检测 + 一键脚本兜底
`status` 命令新增运行时检测：比较最近 `gateway run` 进程的启动时间与 run.py mtime，若 run.py 在进程启动后被改过 → 打印 `⚠️ hooks patched but NOT loaded by running gateway — restart needed`（直接暴露「补丁打了但进程没加载」的失效症状）。还修复了 `Feishu credentials: MISSING` 误报（status 常在非网关 shell 跑，env 里没凭据 → 现在也读 `~/.hermes/.env`）。一键兜底脚本：

```bash
bash ~/ai/hermes-lark-streaming/reinstall_after_upgrade.sh
```

脚本做：`pip install -e .` → `verify` → `install`（自动装守护 + 启用插件）→ 检查 streaming 段 → 检查合并改动 → 检查 bg_watcher 注入（≥4 处）→ 检查守护 plist → restart。

### background watcher 自动注入（不再手贴）
`on_bg_watcher_notify`（注入点 12）原是手贴 hook（reinstall 脚本 step 5.5 检查但只警告不修复）。现已纳入 `patcher.py` 自动注入——两个 marker（`BG_WATCHER_FINISHED` / `BG_WATCHER_RUNNING`）注入到 `_run_process_watcher` 的 "finished with exit code" 和 "is still running~" 两个分支，调用 `on_bg_watcher_notify(chat_id, message_text)`，handled 时通过改写守卫 `if adapter and chat_id and not _hermes_lark_bg_handled:` 跳过原 adapter.send 纯文本。和其它 14 个 hook 一样自动化，`install` 自动打。

**风险**：新 hermes 改了 hook 注入点函数名（`_handle_message_with_agent`/`progress_callback`/`_stream_delta_cb`/`_interim_assistant_cb`/`reasoning_callback`/background `synth_event` 注入点/bg_watcher 锚点）→ `verify` 失败 → 自愈降级（只记日志不重打），三选一：a) `git pull` 等上游适配 b) 回退 hermes 版本 c) 手动适配 `patcher.py` 的 marker（改函数名匹配）。clarify 单选的注入点锚点是 `self.hooks.emit("gateway:startup"`（start() 内），若 hermes 改了这行 → `verify` 报 "gateway startup emit anchor" 缺失 → 手动适配 `patcher.py` 的 `_find_adapter_init_site`。完整升级流程 + 改动清单见记忆 `reference_cheerwhy-migration.md`。
