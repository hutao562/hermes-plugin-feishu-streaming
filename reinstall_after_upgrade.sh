#!/bin/bash
# hermes 升级后重装 hermes-lark-streaming（含跨回合合并 + bg_watcher）——一键脚本
# 用法: bash ~/ai/hermes-lark-streaming/reinstall_after_upgrade.sh
#
# 背景：hermes 自动升级覆盖 gateway/run.py（AST hook 注入丢了），但
# ~/ai/hermes-lark-streaming/ 下的源码改动不会丢。此脚本重打 hook 即可恢复功能。
#
# 注：新版 install 已自动装 launchd 守护（run.py 一变即重打）+ 启用 register() 自愈，
# 升级后通常无需手动跑本脚本——它作为兜底/诊断工具保留。

set -euo pipefail
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
SRC=~/ai/hermes-lark-streaming
# 写死清华源：Clash 代理对 pypi.org 转发不通（fake-IP 劫持），清华源走直连可达。
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

echo "=== 1. 确认包（editable，防升级重建 venv 丢失 .pth）==="
cd "$SRC"
# 用临时文件捕获完整输出（便于失败时排错），同时拿到 pip 真实退出码。
# 注意：不能用 `| tail` —— 管道会让 set -e 拿不到 pip 的退出码（PIPESTATUS 才行）。
PIP_LOG=$(mktemp)
set +e
"$HERMES_PYTHON" -m pip install -e . -i "$PIP_INDEX" --timeout 30 --no-build-isolation >"$PIP_LOG" 2>&1
PIP_RC=$?
set -e
tail -2 "$PIP_LOG"
if [ "$PIP_RC" -ne 0 ]; then
    echo ""
    echo "❌ pip install 失败（exit $PIP_RC）。完整输出："
    cat "$PIP_LOG"
    rm -f "$PIP_LOG"
    echo ""
    echo "   常见原因：网络/代理问题（pypi 不可达）。已用清华源 $PIP_INDEX，"
    echo "   若仍失败请检查网络或手动换源。"
    exit 1
fi
rm -f "$PIP_LOG"

echo ""
echo "=== 2. 查兼容性（新 hermes 的 run.py 函数名是否匹配 hook 注入点）==="
if ! $HERMES_PYTHON -m hermes_lark_streaming verify; then
    echo ""
    echo "❌ 不兼容：新 hermes 改了 hook 注入点的函数名/锚点"
    echo "   选项："
    echo "     a) 等 hermes-lark-streaming 上游更新支持（git pull 后重跑本脚本）"
    echo "     b) 回退 hermes 到当前可用版本"
    echo "     c) 手动适配 patcher.py 的 marker/锚点（改函数名匹配）"
    exit 1
fi

echo ""
echo "=== 3. 重新注入 AST hook（install 自动：14 主 hook + bg_watcher + cron + 守护 + 插件启用）==="
$HERMES_PYTHON -m hermes_lark_streaming install

echo ""
echo "=== 4. 检查 config.yaml 的 streaming 段 ==="
if grep -q "^streaming:" ~/.hermes/config.yaml && grep -q "panel_expanded" ~/.hermes/config.yaml; then
    echo "✅ streaming 段在（panel_expanded 配置 OK）"
else
    echo "⚠️ streaming 段缺失！需在 config.yaml 顶层加："
    cat <<'YAML'
streaming:
  enabled: true              # 共用 hermes 内置 streaming.enabled
  panel_expanded: false      # 完成态折叠
  self_heal: true            # 启动时自愈（升级覆盖 run.py 后自动重打）
  header: {enabled: true}
  body: {text_size: normal_v2}
  footer:
    enabled: true
    text_size: notation
    fields: [[status, elapsed, context, model]]
    show_label: false
YAML
fi

echo ""
echo "=== 5. 确认合并改动还在（7 文件）==="
if grep -q "_chat_index" "$SRC/hermes_lark_streaming/controller.py" \
   && grep -q "begin_new_turn" "$SRC/hermes_lark_streaming/streaming/segments.py"; then
    echo "✅ 跨回合合并改动在"
else
    echo "⚠️ 合并改动丢了！源码被覆盖（git pull 或重新 clone）"
fi

echo ""
echo "=== 6. 确认 bg_watcher 已自动注入（patcher 现在自动打，≥4 处）==="
_bgw_count=$(grep -c "on_bg_watcher_notify" ~/.hermes/hermes-agent/gateway/run.py 2>/dev/null || echo 0)
if [ "$_bgw_count" -ge 4 ]; then
    echo "✅ background watcher 注入在（on_bg_watcher_notify 出现 $_bgw_count 处）"
else
    echo "⚠️ background watcher 注入异常（$_bgw_count/4 处）— 重跑 install 或检查 patcher"
fi

echo ""
echo "=== 7. 确认守护已装（launchd WatchPaths 监听 run.py）==="
if [ -f ~/Library/LaunchAgents/com.hermes-lark.watchdog.plist ]; then
    echo "✅ 守护 plist 在"
else
    echo "⚠️ 守护未装 — 重跑 install（本次可能 launchctl 不可用）"
fi

echo ""
echo "=== 8. 重启 gateway ==="
if ! "$HERMES_PYTHON" -m hermes_cli.main gateway restart; then
    echo "⚠️  gateway restart 命令返回非 0 —— 可能仍已重启（launchd KeepAlive 拉起）。"
    echo "    用 launchctl list | grep hermes 或 ps 确认进程状态。"
fi

echo ""
echo "✅ 完成。飞书 DM 发条消息测试折叠卡片 + 合并。"
echo "   升级覆盖 run.py 后，register() 自愈 + 守护会自动重打+重启，通常无需手动跑本脚本。"
