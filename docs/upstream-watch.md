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

## 2026-11-11 上游 a55a299（基线 2237be3，较前已验证 a3defaf 全窗 11-08~11-11 三天）

- 状态：锚点全绿（missing=0，父类八针 + OPTIONAL env 桥均绿）；疑似影响——clarify 交互语义变化 + 发送不确定仲裁机制上线，插件 import 面与覆写面无破坏
- 巡检备注：11-08~11-10 三天调度未产出执行（任务本身 active、runCount 15、lastRun 11-08 09:03——为宿主侧计算排队堆积，与 10-23~11-05 同因，非仓库/网络问题）；本条覆盖三天窗口。当日执行本身也多磨：raw CDN 并发 429、zsh 数组/目录事故，最终 gh api 全量串行重取定案
- 上游改动（23 关注文件 19 变 / 4 未变，按主题归纳）：
  1. **clarify 交互语义（对插件最直接）**：`tools/clarify_gateway.py` +26/−15——`agent.clarify_timeout` 默认 **3600→90 秒**（`resolve_clarify_timeout`，`get("clarify_timeout", 90)`）；`_ClarifyEntry` 新增 `authority` 字段（默认=当前回合 actor+approvers+admins，`prompt_authority()`）；`run_inbound.py` +130/−26 新增 `_refused_clarify`/`_refused_slash_confirm`/`_hm_update_reply_refused`（谁有资格以**文本消息**回答 prompt 的拒答层）+ clarify 撤回重构为 `_withdraw_clarify`。插件兼容性：`resolve_gateway_clarify(id, response)` / `mark_awaiting_text(id)` 签名未变，按钮点击路径不经 authority 拒答（拒答层只管 inbound 文本）——插件 clarify 全链路可继续工作，但**按钮卡有效期变 90s**
  2. **发送不确定仲裁**：`stream_consumer_transport.py` +56/−11、`stream_consumer_fallback.py` +123/−41、`stream_consumer.py` +21/−7、`stream_consumer_fences.py` +11（`_uncertain_send_fully_shown` 异步化取代 `_send_failure_may_have_delivered`、`_preview_uncertain`/`_delivery_uncertain` 状态、fresh-final 超时不再盲目重投、`raw_response["delivery_uncertain"]` 标记、`open_code_fence_span` 显示侧围栏）——全部在 edit/native 兜底机械内，**draft 帧路径未动**（probe/_draft_metadata/_MIN_NEW 锚点原样）；对插件净效果=插件 send() 抛异常时上游「宁可不发不重发」，重复投递风险下降
  3. stale-finalize 响应契约小改：`run_turn.py` 中 `response["already_sent"]` 旁新增可选 `response["delivery_uncertain"]`——插件 `edit_message(finalize=True)` 拦截返回成功即可，新键取 getattr 默认 False，无需适配
  4. 父类 `plugins/platforms/feishu/adapter.py` +81/−77：send() 分块发送重构（`_send_one` 抽取 + 首个被拒 chunk 即停并标 partial）、`CallBackToast` 进卡片回调响应类型、审批 refusal 引入（`gateway.approval_actor`）——插件全量覆写 send() 不经父类分块体；`_card_response(card_data=)` 签名未变；八针锚点全绿
  5. `platform_registry.py` +33/−4：`deps_resolved_off_loop`（ensure_deps 离线结果缓存）+ `platform_hint_tool_notes` 字段——additive；同名顶替/last-writer-wins 语义未动（OPTIONAL 锚绿）
  6. 其余：`cron/scheduler_delivery.py` +55/−9（Telegram 命名 DM 话题解析、媒体超时按体积放宽——**CRON_WRAP 信封未动**）；`cron/scheduler.py` ±（sys.path append 防遮蔽已装包）；`run_busy`/`run_turn_runner`/`run_notifications`/`run_startup`/`run.py`/`think_scrubber` seam 零命中；`response_filters`/`stream_delivery`/`api_request_hooks`/`interrupt_control`/`gateway_env` 未变
- 影响面：①clarify 卡按钮 90s 过期（升级后点旧按钮走插件「已过期」反馈分支，此前 1h 窗口）——最直接的体感变化；②authority 语义不对称：上游文本路径会拒答非授权者，插件按钮路径不拒（非发起人点击仍可解 clarify）——是否对齐由用户定；③cron 卡/流式卡/bg 通知面无接口变化
- 建议处理方案：总判定同 2026-10-18 条目；升级回归清单追加——①clarify 全流程实测（90s 内点击有效、超时后过期反馈正确；产品上若需长窗口，hermes config 设 `agent.clarify_timeout`）②评估 authority 拒答是否要覆盖插件按钮路径（上游语义=发起人+approver+admin）③重复投递回归（ambiguous 仲裁上线后）。**补锚重申**：`tools/clarify_gateway.py` 已两次在无锚状态下变动（11-06 SAME、本次 +26/−15），强烈建议下个锚点窗口补锚（resolve_gateway_clarify/mark_awaiting_text/attempt_text_response/resolve_clarify_timeout 四针）。
- 关联：hermes-compat issue 0 open；本地 v0.20.0（f2da601，hermes 0.23.0 适配）在案。

- 追记（同日，用户批准执行 11-11 条目的补锚建议）：`tools/clarify_gateway.py` 已补锚——`CONTRACT_ANCHORS` 四针（resolve_gateway_clarify / mark_awaiting_text / resolve_clarify_timeout / get_pending 签名，均在 pinned 2bd378e 与上游 main a55a299 双版核对）+ `TREE_FILES`/manifest 同 revision 扩清单（sha 7052ea98…，样本自动下载验证）；`attempt_text_response` 太新（a55a299 窗口才拆出）刻意不锚。pytest 契约回归 12 通过、ruff/mypy 全绿、`hermes plugins validate` passed。至此 2026-10-18 盲区清单（父类覆写面 11-02 已锚、env 桥 11-02 已锚、clarify_gateway 本次）全部闭合。待办仅剩：authority 是否覆盖插件按钮路径（产品决策）、部署/版本号随下次发版。

## 2026-11-18 上游 da9c2bd（基线 2bd378e，较前已验证 fae118d 全窗 11-13~11-18 六天）

- 状态：**锚点缺失 1 处（结构性，非行为破坏）**——`plugins/platforms/feishu/adapter.py: _dispatch_synthetic_event(` 在上游 main 缺席：合成事件面整体迁出到新文件 `plugins/platforms/feishu/adapter_synthetic_events.py`（`FeishuSyntheticEventsMixin`，与 11-06 choice_cards 同一拆分模式）；**签名逐字未变**（`text/message_type/chat_id/sender_id/event_chat_type/raw_message/message_id` 六 keyword-only 参数一致）——footer 🧠⇄ / clarify 按钮的 SDK 入口拦截与覆写面无实质破坏，插件可继续工作；其余 27 锚全绿
- 巡检备注：11-13~11-17 每日调度未产出执行（队列堆积），本条覆盖六天窗口
- 上游改动（24 关注文件 18 变 / 6 未变，按主题归纳）：
  1. **父类模块化继续**：`adapter.py` +38/−127，合成事件面迁出（`_dispatch_synthetic_event` 现于 adapter_synthetic_events.py:129）；choice_cards ±3——**锚点清单须跟进上游模块化**（本次缺失即此因）
  2. **draft 传输韧性增强**：`stream_consumer_transport.py` +52/−10——draft 帧发送失败从「永久禁用本回合 draft」改为**预算延迟重试**（`_defer_draft_retry`/`_draft_retry_until`/`_should_push_draft`，flood 冷却期暂停而非放弃）；stream_consumer +138/−35、fallback +124/−16 同主题（发送不确定仲裁继续演进）——对插件利好：临时失败不再全回合降级到纯文本
  3. **cron 调度拆模块**：scheduler_delivery +117/−215、scheduler +86/−117——CRON_WRAP 信封 needle 内容级验证仍在（新 scheduler_delivery 中 header/管理尾 count=2）
  4. `run_notifications.py` +97/−122：继续演进，thread_id 媒体路由面仍在（23 处），v0.21.0 三锚绿
  5. `clarify_gateway.py` +3/−21：删 per-session `_notify_cbs` 桥 + `_coerce_text_response` 薄包装——四针（resolve_gateway_clarify/mark_awaiting_text/resolve_clarify_timeout/get_pending）绿
  6. `stream_delivery.py` +60/−31：`_deliver_interim` 加参 + codex commentary 提取——**on_stream_delta/started_at 钩子签名零命中**，usage/reasoning 钩子面未动
  7. 其余 run.py/run_inbound/run_busy/run_turn/run_turn_runner/run_startup/platform_registry/response_filters ±（scope/入站/调度演进）；stream_consumer_think/fences/think_scrubber/api_request_hooks/interrupt_control/gateway_env 未动
- 影响面：①**锚点体系与上游模块化的赛跑**——父类已拆三块（adapter/choice_cards/synthetic_events），needle 锚「文件路径+子串」形态对拆分敏感；②draft 重试增强、cron 拆分、clarify 内部精简均无插件接口变化；③合成事件语义未变（event_chat_type/raw_type 口径照旧）
- 建议处理方案（下个锚点窗口执行，本次不处理）：①`contract.py` 合成事件面锚迁移——`adapter.py` 保留 MRO 锚（`FeishuSyntheticEventsMixin` import 行）+ `adapter_synthetic_events.py` 新文件锚 `_dispatch_synthetic_event(` 签名，**双文件锚**防再次拆分；②`adapter_choice_cards.py`/`adapter_synthetic_events.py` 纳入 manifest/TREE_FILES（同 revision 扩清单）；③验证：pytest 契约回归 + check_tree 对 pinned/main 双树 + ruff/mypy + `hermes plugins validate`
- 关联：hermes-compat issue 0 open（hutao562 #1 已于 11-11 关闭，GH Action 已禁用，本记录为唯一报警通道）

## 2026-11-25 上游 1bddac5（基线 2bd378e，较前已验证 da9c2bd 全窗 11-19~11-25 七天）

- 状态：**明确影响——插件有 1 处真实调用面断裂（升级即断），另有锚点缺失 2 处**。上游 11-19 把卡片点击鉴权从 `FeishuAdapter` 类整体搬进新子包 `plugins/platforms/feishu/cards/`，其中 `_is_interactive_operator_authorized` 方法被移除，而插件有三处无保护调用。这与 11-18 那次（纯锚点位置漂移、签名未变）性质不同。
- 巡检备注：11-19~11-24 六天调度未产出执行；这批提交全部日期为 11-19，本应当天发现，实际晚 6 天。本条覆盖七天窗口：比对 35 个文件（关注清单 34 个 + `gateway/platforms/base.py`），9 个有变、26 个字节级相同；另通读了新子包 `cards/` 的 2 个新文件。draft 契约面（stream consumer 五件、transport、fallback）、`run_notifications.py`、`clarify_gateway.py`、`stream_delivery.py`、`api_request_hooks.py`、`base.py`、`gateway_env.py`、`platform_registry.py` 本窗口全部未变。
- 上游改动（11-19 五个提交，全部是 feishu 卡片点击鉴权的修复与搬迁）：
  - `b3d632d` fix(feishu): resolve card-click chat type from cache and try every operator id form。修 `_is_interactive_operator_authorized` 的网关准入回退：原先会话类型硬编码 `"group"`、只试一种 id。上游提交说明称它「拒绝所有已准入用户的每一次卡片点击」（errors.log 每次点击一条 `[Feishu] Unauthorized approval click by ou_...`，审批超时按拒绝处理，而同一用户的 DM 消息正常）。pinned 2bd378e（0.23.0）里的就是这版实现。
  - `6bb6ea0` fix(feishu): derive chat type from raw_mode, not raw_type。`get_chat_info` 的 `"type"` 原先由 `raw_type` 映射，而 `raw_type` 取自飞书 `chat_type`，是 private/public（可见性）；映射器要的是 p2p/group/topic，所以 private 可见性的聊天全部落到 `"dm"` 默认值。现改由 `raw_mode`（取自 `chat_mode`）映射。
  - `bf353dd` / `797be25` / `e97efa9` refactor：鉴权与 chat 类型映射搬进新子包 `plugins/platforms/feishu/cards/`（`__init__.py` + `authz.py`），父类内部 import 改为包相对（`from .cards import …`）。
  - 方法迁移对照（旧 = pinned 2bd378e 的 `FeishuAdapter` 方法；新 = `cards.authz` 的模块级函数，首参为 adapter）：

    | 旧方法 | 新函数 | 插件是否调用 |
    |---|---|---|
    | `_is_interactive_operator_authorized(open_id, *, user_id, union_id, chat_id)` | `is_operator_authorized(adapter, open_id, *, user_id="", union_id="", chat_id="")` | **是，3 处** |
    | `_validate_card_action(...)` | `validate_card_action(adapter, ...)` | 否 |
    | `_pop_validated_prompt_state(...)` | `pop_validated_prompt_state(adapter, ...)` | 否 |
    | `_map_chat_type(raw)` | `map_chat_type(raw)` | 否 |
    | `_resolve_source_chat_type(*, chat_info, event_chat_type)` | `resolve_source_chat_type(*, chat_info, event_chat_type)`（签名不变） | 否（仅注释提及） |

  - 新 `is_operator_authorized` 相对旧实现的差别：未配置 FEISHU admins / `FEISHU_ALLOWED_USERS` 时，从 `_chat_info_cache` 里该 chat 的 `raw_mode` 解析真实 dm/group/forum（缓存未命中按 `"group"`，fail-closed），并依次尝试 user_id / union_id / open_id 三种 id 形态，任一通过即放行。
  - 其余变化均与插件无关：`cron/scheduler_delivery.py` +16（新增 `_snapshot_delivery_job`）、`cron/scheduler.py`（一段代码缩进平移）、`gateway/response_filters.py` +115（静默标记受损形态的容错识别）、`gateway/run_startup.py` +27（可选的状态上报 watcher）、`gateway/run.py`、`feishu_comment_rules.py`。CRON_WRAP 信封 needle 在位。
- 影响面：
  1. **升级即断（确定）**。插件三处直接调用该方法，都没有 try/except 或 getattr 兜底：`plugin/_clarify.py:141`（`resolve_clarify`，点击 clarify 按钮后唤醒 agent 的异步分支）、`plugin/_clarify.py:210`（`handle_clarify_card_action`，SDK 同步回调）、`plugin/adapter.py:572`（`_handle_model_switch_action`，行号基于 1682bef）。最后一处排在 `pick` / `admin` / `toggle` / `admin_done` / `switch` 全部分支之前。升级到含这批提交的 hermes 后，clarify 按钮与模型选择相关的全部按钮（footer 🧠⇄、选择卡、管理卡）一点就 `AttributeError`。流式卡片本体、cron 卡、话题媒体、/stop 收卡不走这条路径。
  2. **只改名字不够（确定）**。三处调用都只传 `open_id`。按新实现，`chat_id` 为空时会话类型一律按 `"group"` 判，且只有 open_id 一种 id 形态——行为与旧实现相同，拿不到这次的修复。要吃到修复需补传 `chat_id`（三处都有现成值）与 `user_id` / `union_id`（取自 `event.operator`）。
  3. **`event_chat_type` 的取值依据存疑（代码事实确定，运行时表现未实测）**。`plugin/adapter.py` 的 `_dispatch_model_switch` 把 `chat_info["raw_type"]` 当作 `event_chat_type` 传给合成事件，注释与测试 mock（`tests/test_plugin_features.py` 的 `test_dispatch_model_switch_uses_raw_type_for_dm`，`raw_type="p2p"`）都假定它是 p2p/group；但 `raw_type` 是 private/public，p2p/group/topic 在 `raw_mode`。pinned 样本 `adapter.py:2011-2014` 即如此；本机 lark SDK 的 `GetChatResponseBody` 里 `chat_mode` 与 `chat_type` 是两个独立字段（取值含义依据上游 6bb6ea0 的提交说明与代码注释）。按代码推演 `resolve_source_chat_type`：
     - DM：新旧版 `"type"` 都是 `"dm"`，都回退到比较 `event_chat_type == "p2p"`。传真实的 `"private"` 得 `"group"`；只有 `get_chat_info` 走 fallback 或抛异常（字典里没有 `raw_type` 键）时，插件才兜底成 `"p2p"` 得 `"dm"`。
     - 普通群：新旧版结果都是 `"group"`。
     - 话题群：旧版 `"group"`；新版 `"type"` 变成 `"forum"`，合成事件的 chat_type 也随之变成 `"forum"`——这是本次升级带来的新行为，对 `/model` override 键的影响需实测。
     AGENTS.md 记载 v0.17.6 以传 `raw_type` 定稿（0.17.4 / 0.17.5 两次踩坑实测），与上面对 DM 的推演对不上，原因我没能确证——测试用的是 mock，不是飞书真实返回值的证据。
  4. **锚点与监视清单**。缺失 2 处：`_dispatch_synthetic_event(`（11-18 已记录，尚未迁移）与本次新增的 `_resolve_source_chat_type(`。更要紧的是 `_is_interactive_operator_authorized` 被插件调用 3 次却从未入锚——父类八针只覆盖了覆写面。这次把插件引用的 26 个继承成员名逐个在新旧上游树里查了定义，只有它是旧有新无（其余 17 个在新树均有定义，另 8 个是实例属性/类常量）；这个「调用面差集」检查比锚点更不容易漏。监视清单另补上三处盲区：feishu 目录的子目录（此前列目录只筛了文件，漏掉 `cards/`）、`gateway/platforms/base.py`（间接基类，从未进每日比对；本次补查未变）、feishu 目录其余模块。
  5. **已核对不受影响**：父类按正常包模块导入（`_import_base_adapter` 优先 `hermes_plugins.*` 真身，回退 `plugins.platforms.feishu.adapter`），不是单文件影子加载，父类内部的包相对 import 在官方包内自洽，不构成加载期风险；新版 `is_operator_authorized` 读取的 `_chat_info_cache` / `_admins` / `_allowed_group_users` / `_is_sender_authorized`，插件子类实例都继承得到。
- 建议处理方案（本次不处理；升级到含 11-19 提交的 hermes 之前必须完成第 1 项）：
  1. **鉴权适配**。在插件侧做双版本兼容的转发层，三处调用不必各自判断版本。可行写法之一是在插件 adapter 类上定义同名方法（导入路径以运行时真身的包名为准，也可沿用 `_official_feishu_attr` 的延迟解析风格）：
     ```python
     def _is_interactive_operator_authorized(self, open_id, *, user_id="", union_id="", chat_id=""):
         legacy = getattr(super(), "_is_interactive_operator_authorized", None)   # hermes ≤ 0.23.0
         if legacy is not None:
             return legacy(open_id, user_id=user_id, union_id=union_id, chat_id=chat_id)
         from plugins.platforms.feishu.cards import is_operator_authorized        # 2026-11-19 起
         return is_operator_authorized(self, open_id, user_id=user_id, union_id=union_id, chat_id=chat_id)
     ```
     同时三处调用补传 `chat_id`、`user_id`、`union_id`。这只是在插件子类上新增方法、不写 hermes 私有字段，不触碰 catalog 的 "no core override" 红线。
  2. **`event_chat_type` 取值**。新版的正确来源是 `chat_info["raw_mode"]`（p2p/group/topic），pinned 0.23.0 没有这个键，需做兼容；`resolve_source_chat_type` 的 `"p2p"` 字面比较在新位置原样保留，所以传入值仍须是原始的 `p2p` 而非映射后的 `dm`。实现前先按影响面 3 在真实 DM / 普通群 / 话题群各点一次，确认当前实际落点；AGENTS.md 里「raw_type（飞书原始 p2p/group）」这句的前提随之复核。
  3. **锚点迁移与清单扩充（与升级同批做）**。两针迁到新位置（`adapter_synthetic_events.py` / `cards/authz.py`），新增 `def is_operator_authorized(` 针，并按「插件实际调用的父类成员」补齐选针；`cards/`、`adapter_synthetic_events.py`、`adapter_choice_cards.py` 入 manifest 与 TREE_FILES；每日比对清单加入 `gateway/platforms/base.py` 与 feishu 目录全部模块（含子包）。pinned 2bd378e 没有 `cards/`，现在迁锚会让 doctor 与 pinned 回归对 0.23.0 报缺失，所以只能跟 bump revision 一起做——**在升级之前，每日巡检会持续报这 2 处缺失，属已知状态**。
  4. **验证**。pytest / ruff / mypy / `hermes plugins validate` / doctor；升级后实测 clarify 按钮、footer 🧠⇄ `pick`、选择卡 `switch`、管理卡 `admin` / `toggle` / `admin_done`，在 DM、普通群、话题群各点一遍；另在未配置 FEISHU admins / `FEISHU_ALLOWED_USERS` 的部署上单独验一次点击不再被拒（b3d632d 修的就是这个场景）。
- 关联：hermes-compat issue 0 open（Cheerwhy、hutao562 两仓库）。检查期间本地 HEAD 由 9cc2542 前移到 1682bef（v0.22.0，用户提交在途改动），调用面核查已对 1682bef 复跑，结论不变；`plugin/contract.py` 与 manifest 自 11-18 起未动。
