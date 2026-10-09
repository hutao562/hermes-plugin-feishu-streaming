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

编辑 `~/.hermes/config.yaml`（**两处开关缺一不可**，这是历史上最常见的
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
```

发一条飞书消息：应立即出现流式卡片（带工具的回合，工具面板从第一步就滚动）。

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

- 一切异常先跑 `doctor.py`；它覆盖本插件历史上所有静默失效模式。
- 网关日志诊断：`grep '\[feishu-streaming\]' ~/.hermes/logs/gateway.log`。
- 常见病征（卡片超体积 / sequence 冲突 / 非法 reply 目标）doctor 会直接标出，
  详见仓库 AGENTS.md。
