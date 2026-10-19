# 上游更新影响记录

上游更新影响记录，由每日自动检查写入（NousResearch/hermes-agent main vs `tests/hermes_sources.json` pinned revision）。策略：只记录不处理。

## 2026-10-18 上游 ccc6956（基线 2237be3 / 0.21.1）

- 状态：锚点全绿但疑似语义漂移 —— 上游大版本重构，静态核查未发现插件破坏；升级窗口前需实测
- 上游改动（全仓 735 文件 +114428/−37280；最新 tag v2026.9.24，基线为 2026.9.7）：
  - gateway 回合机器大拆分：`run_turn.py` −2110 行，拆出 ~184 个新模块（`run_turn_followup_ack.py`、`session_*.py`、`run_inbound_*.py`、`run_turn_prepare/progress/persistence` 等）；redirect/interrupt 逻辑移入 `agent/interrupt_control.py`；qqbot/signal/weixin/whatsapp_cloud/yuanbao 平台整体删除。
  - **官方 FeishuAdapter（`plugins/platforms/feishu/adapter.py`，插件直接父类）改 704 行**：覆写面方法签名全部未变（`send` / `edit_message(finalize=)` / `send_typing` / `_card_response` / `send_document` / `_resolve_source_chat_type` / `_dispatch_synthetic_event`），仅 `Dict`→`dict` 现代化；已知缺口①（合成事件 `thread_id=None` 硬编码，话题卡上 🧠⇄ 不生效）上游未修也未恶化。新增 `adapter_hint` / `dashboard_card` / `send_targets` 等模块，与插件无冲突。
  - `platforms/base.py`（FeishuAdapter 间接基类）+1337/−471：final send 新增 delivery-obligation ledger 包裹（`send_final_ledgered` → `_send_with_retry` → `self.send`，**终态拦截链不变**，链尾仍进插件覆写的 `send()`）；clarify 文案 i18n 化（插件类定义期整覆写 `send_clarify`，不受影响）。
  - `response_filters.py`：Sep 2026 的 PLUGIN-COMPAT 垫片（`SILENT_REPLY_TOKEN` 等"revert-scheduled"旧名）已按计划删除——插件未 import 这些名字，无影响；静默标记集合新增中文形态（`静默`/`沉默`）。
  - stream_consumer 家族（consumer +64/−48、transport +73/−5、fallback +43/−8）：溢出拆分先封头再拆尾修正（#25349）、flood 退避期间抑制编辑、`_fallback_len_budget` 合并长度预算、新增 `stream_deltas_enabled`/`_egress_declined` 状态——均在 edit/native 传输内部，**draft 帧路径无改动**。
  - cron：`CRON_WRAP_*` 信封文案（header/job_id 行/分隔线/管理尾）逐字符未变；失败文案前缀 `⚠️ Cron '... failed:` 未变，且已抽出为独立模块 `cron/scheduler_failure_copy.py`（此前未抽出无法锚，见影响面 2）。
- 影响面核查（逐项静态验证，全部 OK）：
  - `contract.check_tree` 对 ccc6956 全树：0 锚点缺失；
  - 插件 hermes 内部 import 面 10 处全部健在且签名兼容：`resolve_gateway_clarify(id, response)`、`clarify_gateway._entries`、`mark_awaiting_text`、`get_scoped_secret(name, default)`、`PlatformEntry`/`platform_registry`、`plugins.platforms.feishu.adapter.FeishuAdapter`、`event.MessageType`、`base.SendResult`、`stream_events.ToolCallChunk`（tool_name/preview/args/index 未变）、`agent.secret_scope.get_secret(name, default)`；
  - 取消语义原样保留（仅 adapter 自路由取消 = CANCELLED，注释文字未变，落 `run_turn_followup_ack.py:48`）；stale-finalize 调和原样（`_run_agent_edit_streamed_message` → `edit_message(finalize=True)` → `already_sent` 契约不变，`run_turn.py:2052`）；probe 带 chat_id / `_draft_metadata`+`reply_to_message_id` / `_MIN_NEW_MSG_CHARS=4` / `cfg.transport` 探测全原样（锚点 + diff 双确认）；
  - usage 钩子 `started_at` 字段原样（`agent/api_request_hooks.py:189`）；reasoning/text 增量钩子 `on_stream_delta(delta=…, kind=…)` 签名原样（`agent/stream_delivery.py:332,362`）；bg 通知 thread_id 元数据链原样（`run_notifications.py`）。
- 影响面（结构性发现，非破坏）：
  1. **契约监视盲区**：`CONTRACT_ANCHORS`/`hermes_sources.json` 均未覆盖 `plugins/platforms/feishu/`（插件父类，本次改 704 行）与 `tools/clarify_gateway.py`（clarify 交互依赖）——本次靠人工逐缝核查无恙，但每日自动检查对这两个面是盲的，父类未来任何签名漂移都不会报警。
  2. `cron/scheduler_failure_copy.py` 已抽出为独立模块，`⚠️ Cron '` 前缀现可补锚（消除 cron 失败红卡判定的蓝卡降级盲区，AGENTS.md 已注明"未锚"）。
  3. 生产运行时仍为已安装 hermes 0.21.1（=pinned 基线），上游 main 的重构在用户升级前不影响生产。
- 建议处理方案（升级 0.21.1 → 上游 main 时执行；本次不处理）：
  - 升级 hermes 后先跑 `$HERMES_PYTHON plugin/doctor.py`（「上游契约」项须 ✓）+ `$HERMES_PYTHON -m pytest tests/ -q`；
  - 实测清单：一条 draft 流式回合（含 reasoning + 工具面板）、`/stop` 收红卡（CANCELLED 链）、redirect ↪ 开新卡、话题回合建卡与收卡、cron 卡与失败红卡、footer 🧠⇄ 两段切换、bg 通知合并进最近卡；
  - 确认兼容后：同步 `tests/hermes_sources.json` revision 至升级版本（按 HERMES_SAMPLES.md 口径单 revision 重算 sha256，勿混版本）+ 补锚（上面盲区 1/2）+ `hermes plugins validate` + 部署重启；
  - 建议先补锚点盲区再执行升级，否则升级窗口内每日检查对父类改动不可见。
- 关联发现（运维侧，非上游兼容）：本地 main 领先 `origin/main` 88 提交未 push——origin 的每日 GH Action 仍在跑**旧注入模式验证**，并于 2026-10-15 开出 issue #118（traceback 为 `split_gateway.py`/`patcher.py`，与插件模式契约无关，可关闭）；catalog 安装引用若取 origin 默认分支将拿不到 `plugin/`。是否 push 由用户决定。

## 2026-10-19 上游 20acba0（基线 2237be3，较昨日 ccc6956 增量 116 文件 +4089/−895）

- 状态：锚点全绿（missing=0），增量触及 busy 语义面与插件父类 → 疑似影响；整体结论不变，仍按 2026-10-18 条目的升级实测方案执行，本条无单独动作
- 上游改动（相对昨日已记录的 ccc6956 的增量）：
  - `plugins/platforms/feishu/adapter.py`（插件父类）仅 +1 行：`serial_receive_loop = True`——feishu 接入 per-chat 串行收信循环（wecom/irc/ntfy 早已如此设置），同 chat 上一条消息处理完才读下一条，burst 门（`session_ingress_burst.burst_gate`，base 同步由 `_shared_burst_pending` 重构为此）随之生效。插件每 chat 卡片会话本就假设串行入口（card_op_lock / 会话互斥），中性偏好。
  - `gateway/run_busy.py` +132：slash 命令产生的回合（/queue、/steer 兜底、/plan、/learn、skill/bundle 回合）改入会话 authority 的 durable FIFO 排队（原先在当前回合内插队执行），ack 改由入列 commit 时发送；新增 `_stop_epoch`/`_route_stop_epoch`，/stop、/new 会丢弃 FIFO 中未执行的命令回合。busy ack 发送链原样（`_send_busy_reply` → `adapter._send_with_retry` → `self.send`，带 thread 元数据）；↪ redirect 面本增量未触及。
  - `run_turn_persistence/prepare/progress/runner` 四模块 delta 零触及插件缝隙（CANCELLED / edit_message(finalize) / probe / draft / _MIN_NEW / reply_to 全无命中）。
  - `cron/scheduler.py` 36 行：worktree GC 运维（per-profile 节流 + worktree_registry），投递面未动（`scheduler_delivery.py` 未变，CRON_WRAP 信封安全）；`tools/clarify_gateway.py` 未变。
- 影响面：升级实测清单追加一项——busy 期间发 slash 命令（如 /model）观察 ack 时机与 FIFO 排队行为；footer 🧠⇄ 按钮合成的 /model 走 `_dispatch_synthetic_event` 通道，是否入 FIFO 上游未明示（`_command_turn_admitted` 排除 `event.internal`，合成事件归属待核），升级回归时一并验证。
- 建议处理方案：同 2026-10-18 条目（先补锚点盲区 → 升级 → doctor/pytest/实测清单 → 同步 revision → 部署重启）。
- 其他：本次 gh 查 compat issue 遇网络 EOF 未复查（昨日记录的 #118 为旧注入工作流误报，与插件无关）。
