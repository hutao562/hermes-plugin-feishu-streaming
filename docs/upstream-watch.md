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

## 2026-10-21 上游 0b8dee0（基线 2237be3，较前记录 20acba0 增量 1068 commits）

- 状态：锚点全绿（内容级验证），增量未触及 draft 契约面与插件父类 → 维持 2026-10-18 条目的「疑似影响待升级实测」总判定，本条无单独动作
- 方法备注：当日 git 协议挂起（ls-remote 无响应、匿名 API 403），本次经 gh api（compare/commits/contents）+ raw CDN 完成；compare files 数组 300 上限不可信，逐文件 commits API 才是准确口径
- 上游改动（相对 20acba0，按插件关注面逐文件核验）：
  - **draft 契约面零改动**：stream_consumer 五文件、response_filters、scheduler_delivery、think_scrubber、stream_delivery、api_request_hooks（usage started_at 字段）、plugins/platforms/feishu/adapter.py（父类）、tools/clarify_gateway.py 全部未动
  - 锚点 4 文件中 3 个未动；cron/scheduler.py 改 3 行 + gateway/run.py ±4：secret-scope 构建器内部迁移（`agent.secret_scope.build_profile_secret_scope` → `tui_gateway.launch_profile_policy.owner_secret_scope`，旧函数保留）；`get_secret` 本体仍在（secret_scope.py:230），vendor import 无恙；锚点 needle `**Status:** script failed` 仍在
  - gateway/run_inbound.py +2 注释行；run_busy.py：busy 快速词表新增 "discard"（与插件前缀识别无关）
  - gateway/run_turn.py +4/−4：执行中 admission 的用户中断不再向 FIFO 重复入队中断消息（内部去重）；CANCELLED / edit_message(finalize) 缝隙未动
  - agent/interrupt_control.py +9/−2：gateway shutdown 的停止不再覆盖已挂起的用户 Stop 归因（user's Stop wins）；pending redirect 在重启时被丢弃并归因为重启——影响卡片 NOTICE 文本的归因来源，非契约
  - **gateway/run_notifications.py +75/−5**：watch 通知注入被拒后从每 2s 热重试改为有界指数退避（#140899）——permanent 拒绝计入丢弃上限，transient 拒绝（重启等待期、parked profile、FIFO 满等）无限等待。与 2026-10-19 健康检查的「watch 通知注入疑似拆卡缺口待观察」同一区域，上游正在活跃重构该面
- 影响面：无新增破坏；bg/watch 通知到达时序可能因退避变化（插件 append_notice 按内容合并、对时序不敏感）；interrupt 归因细化只影响状态行/NOTICE 文本来源
- 建议处理方案：同 2026-10-18 条目；升级回归清单追加一项——bg/watch 通知送达时序观察（退避生效后是否仍及时合并进最近卡）
- 关联：hermes-compat issue #118 仍 open（10-15 起无更新，已定性为旧注入工作流误报，可关闭）

## 2026-11-02 本机升级 bf0203a（用户升级实测，非自动巡检）

- 状态：**升级即破坏，当日修复（v0.19.4）**——feishu 平台整体未启用，非 draft 契约面问题（锚点全绿）
- 事故形态：升级重启后 `Gateway running with 1 platform(s)`（仅 a2a），无 `Connecting to feishu`、无 `adapter factory`、插件零活动日志；插件 register() 正常（diag 日志有 platform registered），doctor 全绿——契约检查对此面完全盲区
- 根因：新版 hermes 平台启用由**条目自带 env 桥**驱动——config pass 跑 `env_overrides`（EnvCred 步骤）把 .env 凭据写进 `platforms.feishu.extra` 并在场即启用，凭据门 `is_connected` 看 `extra.app_id`。官方条目带 `gateway_env.ENV_OVERRIDES`/`ENV_ENABLE_CREDENTIALS`，插件同名顶替条目没透传 → 凭据门恒 False → feishu 永不启用；且同名条目存在还屏蔽 `_bundled_env_fallbacks`（bundled env 兜底只查无条目平台）。config.yaml 从未显式 `platforms.feishu.enabled`（一直走凭据自动启用），故两份 profile 同时失效
- 修复（v0.19.4，当日部署实测恢复 3 platform(s)）：`plugin/__init__._official_feishu_env_bridge` 延迟解析官方桥并透传，`_platform_entry_field_names` 能力探测（老版本 PlatformEntry 无 env 桥字段，盲透传会被 `ctx.register_platform` 以未知字段 TypeError 拒绝——保持 0.21.1 双版本兼容，基线不 bump）；doctor 新增「平台启用面」病征项
- 补锚（本次事故的直接教训，即 2026-10-18 记录的盲区 1）：①`OPTIONAL_ANCHORS` 新层——`plugins/platforms/feishu/gateway_env.py` + `gateway/platform_registry.py`（env 桥面 + last-writer-wins 同名顶替语义），文件在场才检查（pinned 基线无此文件，缺席不算缺失），hermes-check 每日对 main 自动覆盖；②插件父类 `plugins/platforms/feishu/adapter.py` 进必选锚点（八针：类名 + send/edit_message/send_typing/send_document/_dispatch_synthetic_event/_resolve_source_chat_type/_feishu_send_with_retry，2237be3 与 bf0203a 双版核对稳定）+ 进 `hermes_sources.json` manifest（同 revision 扩清单，sha256 3381601b…）
- 遗留：`tools/clarify_gateway.py`（clarify 交互依赖）仍未锚——2026-10-18 盲区清单的最后一项，待下个锚点窗口
- 追记（同日 23 时，升级适配全链路收口）：梦珊机（Windows 桌面版）升级后实测暴露两个后续问题并当日修复——①CLI 安装布局（目录名 = manifest 名 feishu-streaming-platform）doctor 误报三连 + Windows venv `Lib\site-packages` 布局漏探（v0.19.5）；②该机 PM venv 无 PyYAML，`_vendor/config.py` 顶层裸 import yaml 建卡全败（v0.19.6：回退链 PyYAML→ruamel→降级）。另清出 7 份影子目录（.bak-0190 按扫描序 last-writer-wins 长期压赢，该机实际跑的是 0.19.0）。双机 v0.19.6 验证通过，流式卡恢复。

## 2026-11-06 上游 90d6b98（基线 2237be3，较前记录 ebc9960 全窗 10-23~11-06）

- 状态：锚点全绿（missing=0，含 11-02 补的父类八针与 OPTIONAL env 桥锚，首次实战覆盖父类面）；疑似影响（低危）——父类 choice-cards 重构触及 clarify/按钮卡面，插件入口拦截层未破
- 巡检备注：10-23~11-05 每日巡检未执行（队列堆积），本条为追赶性全窗检查；git 协议仍断（ls-remote 40s 零字节自止）、raw CDN 并发 429 限流，最终口径 = gh api contents（raw Accept 头）双版下载 24 文件 + 本地 blob diff（比逐文件 commits API 快且为 blob 级精确判定）
- 上游改动（24 关注文件：14 变 / 9 未变）：
  - **父类 `plugins/platforms/feishu/adapter.py` +80/−44**：类 MRO 变为 `FeishuAdapter(FeishuChoiceCardsMixin, BasePlatformAdapter)`（新文件 adapter_choice_cards.py）——clarify 从纯文本升级为按钮卡（value 键 `hermes_clarify_action`，与插件**同键**）、新增 choice-picker 卡（`hermes_choice_picker_action`）、`_CARD_ACTION_ROUTES` 路由表化（approval/update-prompt/clarify/picker 四键）、`retire_clarify_card` 网关侧清理已解 clarify 卡、审批解析卡保留正文（body 回显）、interactive 鉴权透传 user_id/union_id、`send_voice` 新增 `is_voice` 参数 + ogg 仅 audio 意图才转 voice。**八针锚点全部在位**（send/edit_message/send_typing/send_document/_dispatch_synthetic_event/_resolve_source_chat_type/_feishu_send_with_retry）
  - draft 契约面：`stream_consumer_transport.py`/`think`/`fences`/`think_scrubber` 未变；`stream_consumer.py` +68/−6 与 `response_filters.py` +84/−3 = 静默标记机制大扩展（`SilenceMarkerStreamHold`、`strip_trailing_silence_marker`、segment 级 `_adopt_recovered_reply` 调和）——edit/native 传输内部，draft 帧路径无改动；fallback +3/−2 同主题
  - 回合机器：run.py/run_inbound/run_busy/run_turn/run_turn_runner/run_notifications/run_startup 七模块 seam 零命中（CANCELLED/edit_message/already_sent/_draft_metadata/reply_to_message_id/_MIN_NEW/supports_draft/_dispatch_synthetic 全无）；run_turn −221 为删减重构
  - cron：`scheduler_delivery.py` +36/−6 新增 room 投递目标 + 媒体 ack 丢失（ambiguous）语义 + is_voice 透传——CRON_WRAP 信封未动；`scheduler.py` +73/−89 取消事件管道清理 + operator_cancelled，锚点 needle `**Status:** script failed` 在位
  - `agent/stream_delivery.py` +40/−6 = held-row 显示机制（display_kind hidden/publish），reasoning/usage 钩子签名未动；`api_request_hooks.py`/`interrupt_control.py`/`clarify_gateway.py`/`platform_registry.py`/`gateway_env.py` 全部未变
- 影响面：
  1. **clarify 入口无冲突但依赖拦截层**：插件 `_on_card_action_trigger`（类定义期覆写，SDK connect 绑定）先于上游路由表拦截 `hermes_clarify_action`/`hermes_model_action`——正常部署下插件 handler 仍独占 clarify 点击。风险场景：若未来 SDK 改绑定方式或上游改回调入口名使插件覆写失效，上游 `_handle_clarify_card_action` 会接手插件格式的值（查它自己的空 `_clarify_state` → no-op）——clarify 点击将静默失效。此面无锚（入口覆写不进 CONTRACT_ANCHORS），doctor 也测不到线上 SDK 绑定，只能靠实测。
  2. **`retire_clarify_card` no-op**：文本回答等方式解掉 clarify 时，网关调 `adapter.retire_clarify_card` 清按钮卡；插件 clarify 卡不注册上游 `_clarify_state` → no-op → 插件卡按钮残留（低危 UX；上游旧实现是纯文本、无此清理概念，11-02 升级后的新版 hermes 上此面已激活）。
  3. 上游 clarify 卡与插件同键同前缀（`hermes_clarify_action`+`clarify_id`）但值格式不同（上游用序号 int，插件用 "choice"/"other"+index/text）——若同一 chat 上两类卡并存（不会：send_clarify 类定义期覆写全量接管，官方卡发不出），无实际碰撞。
  4. 其余均为上游内部演进：静默标记、held-row、room 投递、审批 user_id 透传——插件消费面无接口变化。
- 建议处理方案：总判定同 2026-10-18 条目（升级实测流程）；升级回归清单追加三项——①clarify 内联单选实测（按钮点击 → 插件 handler 仍独占）②文本回答解 clarify 后插件按钮卡是否残留（retire no-op 的 UX 影响评估，可在插件 send_clarify 时顺手注册上游 `_clarify_state` 消除）③点击已解 clarify 卡旧按钮的过期反馈是否正常。锚点面无需改动（全绿）；`tools/clarify_gateway.py` 补锚（11-02 遗留）仍待窗口。
- 关联：hermes-compat issue #118 已于 2026-11-02 关闭（用户操作，与升级事故同日）。
