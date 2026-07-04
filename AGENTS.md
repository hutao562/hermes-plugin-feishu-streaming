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

## hermes 升级后恢复（持续用合并功能）

hermes 自动升级覆盖 `~/.hermes/hermes-agent/gateway/run.py` → AST hook 丢（折叠卡片 + 合并失效），但本目录的合并**源码改动不丢**（editable install 指向这里）。一键恢复：

```bash
bash ~/ai/hermes-lark-streaming/reinstall_after_upgrade.sh
```

脚本做：`pip install -e .`（防 venv 重建丢包）→ `verify`（查新 hermes 函数名匹配）→ `install`（重打 hook）→ 检查 `~/.hermes/config.yaml` 的 `streaming:` 段 → 检查合并改动（grep `_chat_index`/`begin_new_turn`）→ restart。

**风险**：新 hermes 改了 hook 注入点函数名（`_handle_message_with_agent`/`progress_callback`/`_stream_delta_cb`/`_interim_assistant_cb`/`reasoning_callback`/background `synth_event` 注入点）→ `verify` 失败 → 三选一：a) `git pull` 等 Cheerwhy 上游适配 b) 回退 hermes 版本 c) 手动适配 `patcher.py` 的 marker（改函数名匹配）。完整升级流程 + 改动清单见记忆 `reference_cheerwhy-migration.md`。
