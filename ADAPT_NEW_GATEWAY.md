# hermes-lark-streaming 适配 Hermes v0.21 (2026-09 facade/siblings 重构)

> 状态: 测绘完成 → 设计定稿 → 实现中
> 目标 Hermes: `089bb328` (v0.21.0, 2026-09-06)。上游 Cheerwhy 停更 8/5 未适配；本地领先 16 commits 为资产，本地适配后即成事实上游。

## 新版架构核心变化（2026-09-06 大重构）

| 维度 | 旧版 (≤0.20) | 新版 (0.21) |
|---|---|---|
| run.py | 单文件 ~5000 行, 所有目标函数在内 | facade (5513 行) + 15+ siblings (`run_turn.py`/`run_turn_runner.py`/`run_inbound.py`/`run_notifications.py`/`run_startup.py`) |
| 回合执行 | `_handle_message_with_agent` 模块级函数 + `_run_agent`/`_run_agent_inner` | `GatewayTurnMixin` (run_turn.py) 类方法; `TurnRunner` (run_turn_runner.py) 持 TurnContext |
| 回调装配 | 各 callback 散在 run.py 不同函数 | **收敛到 `TurnRunner._wire_turn_agent_callbacks` 单方法** (run_turn_runner.py:1086) |
| 作用域访问 | 裸闭包 `_run_still_current`/`event_message_id`/`source.chat_id` | TurnRunner 内 `self._ctx.*` (TurnContext 字段); GatewayTurnMixin 内是**形参** (source/event_message_id) |
| reasoning 流 | `agent.reasoning_callback` (旧) | 同名单回调, 流式/非流式都走它 (agent/stream_delivery.py:325 + chat_completion_helpers.py:1599) |
| cron | scheduler.py `_deliver_result` 内嵌循环 `delivered=False`+continue | scheduler_delivery.py 模块函数 + `_prepare_target_delivery`/`_deliver_via_live_adapter`/`_deliver_standalone` helper |
| bg watcher | `_run_process_watcher` 两分支直接 adapter.send | run_notifications.py 集中 `_send_watcher_message` (1393) |
| 官方 hook | 无 | `~/.hermes/hooks/<name>/{HOOK.yaml,handler.py}` HookRegistry (gateway/hooks.py) + 插件 `VALID_HOOKS` (hermes_cli/plugins.py:107) + 流式 observer (`agent/plugin_stream_hooks.py`) |

**战略判断：仍走 AST 注入。** 官方插件 hook 虽有 `pre_gateway_dispatch`/`on_session_*`/`on_stream_delta`(observer, 无返回值)，但：
- observer 无法"卡片吞文本防双发"（决策语义缺失）
- 卡片需要 message_id/anchor/thread 精确生命周期信号，官方 hook context 只给 500 字符 message
- clarify inline 单选需要 patch FeishuAdapter + 换 SDK processor.f，官方 hook 无对应
- 16 个本地 commit 的既有架构（controller/session/segments）全部基于 AST hook 调用契约

## 注入点迁移表（17 hook + cron）

### A. 收敛注入: `TurnRunner._wire_turn_agent_callbacks` (run_turn_runner.py, 单点包 5 hook)

旧 5 个分散 hook (tool/answer/thinking/reasoning/background_review) 全部收敛为在此方法体注入一段"回调包 wrapper"代码（每回合调用, callback 每回合重置, 与旧版每次 run 重绑一致）。

| hook | 旧注入位置 | 新锚点 (run_turn_runner.py) | wrapper 包谁 |
|---|---|---|---|
| tool | progress_callback 函数体 | 1101 `agent.tool_progress_callback = ctx.progress_callback` | ctx.progress_callback (bound method) |
| answer_guard+answer | stream_delta_callback 赋值前 | 1103 `agent.stream_delta_callback = stream_delta_cb` | stream_delta_cb 局部闭包 |
| thinking | interim_assistant_cb 函数体 | 1104 `agent.interim_assistant_callback = interim_assistant_cb if ...` | interim_assistant_cb 局部闭包 |
| reasoning | reasoning_config 赋值后 | 1108 `agent.reasoning_config = reasoning_config` 后 | 直接赋值 `agent.reasoning_callback` |
| background_review | background_review_callback 赋值后 | 1113 `agent.background_review_callback, bg_release = self._make_bg_review_callbacks()` 后 | 再包一层 send |

作用域: TurnRunner 方法, `self._ctx` = TurnContext → `ctx.event_message_id`/`ctx.source.chat_id`/`ctx._run_still_current()` 全可用 (turn_context.py:15/50)。与旧 hook 代码几乎一致。
注意: 1101 行 `agent.tool_progress_callback` 是 ALWAYS attached (注释明说绝不 None)。`_wire_turn_agent_callbacks` 是每回合必调 (run_sync:1644)。

### B. 生命周期 hook: GatewayTurnMixin (run_turn.py)

| hook | 旧位置 | 新位置 (run_turn.py) | 作用域 |
|---|---|---|---|
| normalize | `_handle_message` source=event.source | **run_inbound.py** `_hm_admit_event` 返回后 / `_handle_message` 1176 内 `event, source, is_internal = _admitted` (1183) | source/event 形参 + self._reply_anchor_for_event |
| start | `_handle_message_with_agent` 函数体开头 | `_handle_message_with_agent` (1918) 方法体开头 (1958 `_run_agent` 调用前) | self + event/source/run_generation 形参 |
| complete | `_already_sent = bool(` 前 | `_hmwa_deliver_turn_response` (1708) 方法体开头 (1714 前) | 全部形参: agent_result/agent_messages/response/session_entry/event/source/_footer_line/_intentional_silence; await 可用; return None=跳过发送 |
| followup_complete | was_interrupted=result.get 处 | `_run_agent_inner` (3774) drain 后 `if pending_event or pending:` (3838) 前 或 `_run_agent_queued_followup` (3392) 开头 | turn_ctx.result_holder/response; await 可用 |
| followup_result | return _preserve_... | `_run_agent_queued_followup` 尾部 `return _preserve_queued_followup_history_offset(result, followup_result)` (3455) 前 | result/followup_result/pending_event/next_message_id 作用域 |
| abort | Discarding stale agent result | `_hmwa_discard_stale_result` (1813) 内 return 前, 或调用点 (1965) `_hmwa_discard_stale_result(...)` 后 `return None` 前 | 需 event.message_id — 看函数签名 |
| interrupt | Restart typing indicator | `_run_agent_queued_followup` 内 typing 重启 (3474) 附近; was_interrupted 语义在 result.get("interrupted") | pending_event/source/event_message_id |
| bg_deliver | adapter.extract_images(response) 后 | `_run_background_task_inner` (2088) 内 2188 `images, text_content = adapter.extract_images(response)` 后 | source/event_message_id/prompt/text_content/images/media_files |

### C. 其他文件

| hook | 新文件/位置 |
|---|---|
| adapter_init | run_startup.py:1142 `await self.hooks.emit("gateway:startup", ...)` emit 语句后 (需 AST end_lineno) |
| bg_watcher_finished | run_notifications.py `_run_process_watcher` (1456) finished 分支: 1512-1513 `_send_watcher_message` 调用前 — **结构变了**, 无 adapter=None/if adapter 守卫, 改为包 `_send_watcher_message` 或改写其调用处 (`_hermes_lark_bg_handled` 守卫) |
| bg_watcher_running | run_notifications.py:1519-1521 running 分支同上 |
| cron | cron/scheduler_delivery.py `_deliver_result` (1594) `for target in targets:` (1678) 内, `_prepare_target_delivery` (1687) 前 — 平台判断 target["platform"], 无 delivered=False 单行; 语义改为 `_hermes_lark_cron_seen` 去重 + 拦截 target 级 |

### D. reasoning 关键发现

新版 reasoning 有**两个入口但同一个 callback**:
1. 流式中: `_fire_reasoning_delta` (agent/stream_delivery.py:318-334) → `self._call_quietly(self.reasoning_callback, text)` — 每段 reasoning delta 都调
2. 回合收尾: `_assistant_reasoning_text` (agent/chat_completion_helpers.py:1585-1602) — `if reasoning_text and agent.reasoning_callback and not stream_delta_callback...` 非流式兜底

`agent.reasoning_callback` 从未在 gateway 侧被赋值 (grep 确认 gateway 无赋值) → **我们在 `_wire_turn_agent_callbacks` 1108 后赋值即可接管全部 reasoning**。旧 hook 语义 (agent.reasoning_callback = wrapper → on_reasoning_delta) 原样可用。

## Patcher 架构改造

旧: 1 个 Patcher(run.py) + 1 个 CronPatcher(scheduler.py), 单文件 AST。
新: **多文件目标管理器**。同一套 marker 分到 4 个目标文件 + cron 1 个:

```python
GATEWAY_TARGETS = [
  FileTarget("gateway/run_turn.py", [MK_START, MK_COMPLETE, MK_FOLLOWUP_COMPLETE, MK_FOLLOWUP_RESULT, MK_ABORT, MK_INTERRUPT, MK_BG_DELIVER]),
  FileTarget("gateway/run_turn_runner.py", [MK_TOOL, MK_ANSWER_GUARD, MK_ANSWER, MK_THINKING, MK_REASONING, MK_BACKGROUND_REVIEW]),
  FileTarget("gateway/run_inbound.py", [MK_NORMALIZE]),
  FileTarget("gateway/run_startup.py", [MK_ADAPTER_INIT]),
  FileTarget("gateway/run_notifications.py", [MK_BG_WATCHER_FINISHED, MK_BG_WATCHER_RUNNING]),
]
CRON_TARGET = FileTarget("cron/scheduler_delivery.py", [MK_CRON_DELIVER])
```

每个 FileTarget: 独立 `verify_target()` (该文件锚点 AST/字符串检查)、独立 `.bak`、独立注入/移除。`Patcher.is_fully_patched()` = 所有文件 marker 齐。`status`/`__init__.py` self-heal/`__main__.py` 显示全部目标。

注意: __init__.py `_run_self_heal` mtime 比较现在要看"最新被改的目标文件"而非仅 run.py; verify_target 失败仍降级不 crash。

## 测试策略

tests/test_patcher.py 目前拷单文件 run.py。新版需: 每个目标文件从本地 ~/.hermes/hermes-agent/ 拷贝到 tmp (CI 从 GitHub 对应路径下载), 分别 verify/apply/remove。fixture 结构: `run_turn_copy`/`run_turn_runner_copy`/`run_inbound_copy`/`run_startup_copy`/`run_notifications_copy`/`scheduler_delivery_copy`。

## 风险与回退

- Hermes 持续演进 (今天 upstream 又 +205 commits)。适配锁定 089bb328 本地树; 上游下次大重构时本适配可能再失效 — 已接受 (涛拍板本地适配)。
- 不改 patcher 注入点以外的运行时代码 (controller/segments/builder 等 8 月修复全部复用, 435 测试中与 patcher 无关的应继续绿)。
- 备份: 改造前 git commit 当前工作区 (21 modified = 8月修复累积未提交), 或至少 stash 可回退。

## 验证清单 (做完一项勾一项)

- [ ] verify 对新版 6 文件全绿
- [ ] uninstall 全清 (所有文件 marker 移除 + bg_watcher 守卫还原)
- [ ] install 全打 + config.yaml 无垃圾行
- [ ] 435 回归 (patcher 相关测试重写后)
- [ ] gateway restart 后飞书真发一条: 卡片创建/流式/complete 全链路
- [ ] 工具调用卡片、reasoning 折叠面板、image 产物、双发抑制、clarify 按钮各验一次
