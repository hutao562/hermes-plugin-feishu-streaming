#!/bin/bash
# hermes 升级后重装 Cheerwhy（含跨回合合并）——一键脚本
# 用法: bash ~/ai/hermes-lark-streaming/reinstall_after_upgrade.sh
#
# 背景：hermes 自动升级会覆盖 gateway/run.py（Cheerwhy 的 AST hook 注入丢了），
# 但 ~/ai/hermes-lark-streaming/ 下的合并源码改动不会丢。此脚本重打 hook 即可恢复折叠卡片功能。

set -e
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
SRC=~/ai/hermes-lark-streaming

echo "=== 1. 确认 Cheerwhy 包（editable，防升级重建 venv 丢失 .pth）==="
cd "$SRC"
$HERMES_PYTHON -m pip install -e . 2>&1 | tail -2

echo ""
echo "=== 2. 查兼容性（新 hermes 的 run.py 函数名是否匹配 hook 注入点）==="
if ! $HERMES_PYTHON -m hermes_lark_streaming verify; then
    echo ""
    echo "❌ 不兼容：新 hermes 改了 hook 注入点的函数名"
    echo "   (_handle_message_with_agent / progress_callback / _stream_delta_cb /"
    echo "    _interim_assistant_cb / reasoning_callback / background synth_event)"
    echo "   选项："
    echo "     a) 等 Cheerwhy 上游更新支持新 hermes（git pull 后重跑本脚本）"
    echo "     b) 回退 hermes 到当前可用版本"
    echo "     c) 让浮浮酱手动适配 patcher.py 的 marker（改函数名匹配）"
    exit 1
fi

echo ""
echo "=== 3. 重新注入 AST hook（含跨回合合并的 chat_id 改动）==="
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
    echo "⚠️ 合并改动丢了！源码被覆盖（git pull 或重新 clone 官方版）"
    echo "   参考 ~/.claude/projects/-Users-hahahexiaobai-ai/memory/reference_cheerwhy-migration.md 的改动清单重打"
fi

echo ""
echo "=== 5.5 确认 background watcher 手贴还在（run.py 两处：finished 14630 + still running 14672）==="
_bgw_count=$(grep -c "on_bg_watcher_notify" ~/.hermes/hermes-agent/gateway/run.py 2>/dev/null || echo 0)
if [ "$_bgw_count" -ge 4 ]; then
    echo "✅ background watcher 手贴在（on_bg_watcher_notify 出现 $_bgw_count 处）"
else
    echo "⚠️ background watcher 手贴丢了（$_bgw_count/4 处）！需重打 run.py 的 background watcher text-only 通知处"
    echo "   （finished + still running 两个分支，调 on_bg_watcher_notify）"
    echo "   参考 ~/.claude/projects/-Users-hahahexiaobai-ai/memory/reference_cheerwhy-migration.md 的 background watcher 段落"
fi

echo ""
echo "=== 6. 重启 gateway ==="
hermes gateway restart

echo ""
echo "✅ 完成。飞书 DM 发条消息测试折叠卡片 + 合并。"
echo "   合并源码改动（~/ai/hermes-lark-streaming/）升级不丢；丢的只是 run.py 的 hook 注入，已重打。"
