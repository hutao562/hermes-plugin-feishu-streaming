# Installation Guide — feishu-streaming (platform plugin)

A step-by-step guide to install the feishu-streaming platform plugin into an
existing Hermes Agent deployment. Intended to be read and executed by an AI
agent or a human following the commands verbatim.

本插件是 `kind: platform` 的 hermes 插件：**自包含、零 pip 安装**——部署 =
拷贝目录 + 两处 config 开关 + 重启网关。

## Requirements

- Hermes Agent `>= 0.21.1`，`hermes` 命令可用（`hermes --version` 正常），
  且飞书平台已配置（官方 FeishuAdapter 能收发消息）。
- 若 `hermes` 不在 PATH，先修 Hermes 安装。
- 无需 pip 安装任何包：插件自包含，只依赖 hermes 运行环境已有的 `lark-oapi`。

## Step 1 — Deploy the plugin directory

```bash
git clone https://github.com/hutao562/hermes-plugin-feishu-streaming.git
cp -R hermes-lark-streaming/plugin ~/.hermes/plugins/feishu-streaming
```

部署后自检（可选但推荐）：

```bash
python3 ~/.hermes/plugins/feishu-streaming/doctor.py
```

## Step 2 — Enable in config.yaml

编辑 `~/.hermes/config.yaml`（**三处开关缺一不可**，这是历史上最常见的
「装了没卡片」原因）：

```yaml
plugins:
  enabled:
    - feishu-streaming-platform   # platform 插件是 opt-in，必须显式列出
streaming:
  enabled: true                   # 官方 draft transport 总开关
display:
  platforms:
    feishu:
      streaming: true             # 官方 draft 契约开关

  # （可选但推荐，第 4 开关）思考流增量进卡——不开卡照常出、只是没有思考面板：
  plugins:
    stream_reasoning_deltas: true

> **多 profile（multiplex）注意**：上面三处开关是 **per-profile** 的——每个
> profile 自己的 `config.yaml` 都要开一份（`~/.hermes/config.yaml` 一份 +
> `~/.hermes/profiles/<名字>/config.yaml` 各一份）。doctor 会逐 profile 检查。
> 退出某个 profile：在其 config 的 `plugins.disabled` 写 `feishu-streaming-platform`。
```

凭据复用官方飞书平台的配置（`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 环境变量或
`~/.hermes/.env`），本插件不单独管理凭据。

multiplex 网关（多 profile）：插件注册后会自动 fan-out 到每个 live profile；
某个 profile 要退出流式，在自己的 config.yaml 写
`plugins.disabled: [feishu-streaming-platform]`。

## Step 3 — Restart the gateway

```bash
hermes gateway restart
```

重启约 50 秒，会打断进行中的回合。确认插件装配：

```bash
grep "adapter factory" ~/.hermes/logs/gateway.log | tail
# 每个 profile 应有一行 "... -> StreamingFeishuAdapter"
```

## Step 4 — Verify

```bash
python3 ~/.hermes/plugins/feishu-streaming/doctor.py
# 可选：装机时顺手探一次 CardKit 权限（唯一发网络请求的检查）
python3 ~/.hermes/plugins/feishu-streaming/doctor.py --probe-cardkit
```

### 验收（零工具版）

给机器人发一条消息：回复是**流式卡片**（不是纯文本/post 富文本）就算过。
带工具的回合，工具面板会从第一步就滚动。

### 验收（agent 版，前后对照）

判断标准是 `msg_type == interactive`，不是日志：

```bash
# 装前先记一条旧回复的形态（post / text）
lark-cli im +chat-messages-list --chat <chat_id> --limit 3
# 以你的身份发测试消息
lark-cli im +messages-send --as user --chat <chat_id> --text "卡片插件测试"
# 装后新回复应为 interactive
lark-cli im +chat-messages-list --chat <chat_id> --limit 3
```

> footer 的 ⏱ 与 gateway 日志 `response ready time=` 口径不同（卡片=API 跨度，
> 网关=含排队），网关侧略大是正常的；`⚡ <1 t/s` 是短回答的正常显示。

## Uninstall

```bash
rm -rf ~/.hermes/plugins/feishu-streaming
# config.yaml 的 plugins.enabled 移除 feishu-streaming-platform
hermes gateway restart    # 回退官方内置飞书适配器（无流式卡片）
```

## Rollback to the archived injection mode

v0.14.0 之前的 AST 注入形态已归档（完整实现保留在 git tag 里）：

```bash
git checkout archive/injection-mode
# 按该版本的 README/INSTALL.md 操作（pip install -e . + install 命令）
```

## Troubleshooting

- **footer 的 ⏱ 和 gateway 日志 `response ready time=` 数字不一致**：口径不同，
  不是故障——卡片时长 = 首次 API 调用开始 → 最后一次 API 结束；网关时长含
  入站排队/前置处理，所以总是略大。
- **`⚡ <1 t/s`**：短回答的正常显示（输出 token 少、回合长），不是速度坏了。

- 一切异常先跑 `doctor.py`；它覆盖本插件历史上所有静默失效模式。
- 网关日志诊断：`grep '\[feishu-streaming\]' ~/.hermes/logs/gateway.log`。
- 常见病征（卡片超体积 / sequence 冲突 / 非法 reply 目标）doctor 会直接标出，
  详见仓库 AGENTS.md。
