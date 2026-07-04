# E2E 测试集设计 — hermes-lark-streaming

**日期**: 2026-07-04
**状态**: 设计已确认，待写实施计划

## 1. 背景

hermes-lark-streaming 目前有 439 个单元测试（`tests/`），覆盖 controller/session/segments/patcher 等纯逻辑。但**没有端到端测试**——真实 Hermes + 飞书环境下，流式卡片、跨回合合并、话题场景诊断、文件投递等功能无法被单测覆盖。

生产侧曾出现：话题群发图 99992402（`send_image_file` 不处理 thread_id）、prune 重复 6 条 warning、话题场景诊断漏判根消息等问题。这些都需要真实环境 e2e 才能发现。

## 2. 目标

- 建立可重复运行的 e2e 测试集，覆盖：文件类型投递、对话流、卡片交互、错误恢复
- 默认跳过（CI 无飞书环境），本地 `pytest -m e2e` 手动跑
- 复用 pytest 框架 + lark-cli（user 身份，已配）+ `gateway.log` 断言

## 3. 非目标（范围排除）

- 不测 Hermes 本身（只测浮浮酱插件行为，Hermes bug 用 memory 记）
- 不测飞书 API 稳定性
- P2 边界场景（嵌套中断、background 合并、错误恢复）可能 xfail 或留后

## 4. 形态

pytest e2e 标记：
- `pyproject.toml` 注册 `e2e` marker，`addopts = "-m 'not e2e'"` 默认跳过
- 测试加 `@pytest.mark.e2e`
- `conftest.py` 的 autouse fixture 检查环境，不就绪则 `pytest.skip`
- `pytest tests/` 安全（不跑 e2e）；`pytest -m e2e tests/e2e/` 跑全部

## 5. 目录结构

```
tests/
  e2e/
    __init__.py
    conftest.py                  # 环境检查 + lark helper + 日志断言
    test_file_delivery.py        # A 文件类型投递（A1-A7）
    test_conversation_flow.py    # B 对话流（B1-B6）
    test_card_interaction.py     # C 卡片交互（C1-C5）
    test_error_recovery.py       # D 错误恢复（D1-D3，部分 xfail）
```

`pyproject.toml` 新增：
```toml
[tool.pytest.ini_options]
markers = [
  "e2e: end-to-end tests requiring real Hermes + Feishu (deselected by default)",
]
addopts = "-m 'not e2e'"
```

## 6. 场景矩阵

### P0 必测（8 个）

| ID | 场景 | 文件 | 断言 |
|----|------|------|------|
| A1 | 图片→私聊 | test_file_delivery | session/card created + on_completed complete |
| A2 | 图片→话题（reply-in-thread） | test_file_delivery | `[话题场景]` + **无 99992402**（痛点回归）|
| A3 | 文件→私聊 | test_file_delivery | session/card + complete |
| A4 | 文件→话题 | test_file_delivery | `[话题场景]` + 落话题（omt_） |
| A5 | 视频→私聊 | test_file_delivery | session/card + complete |
| B1 | 中断重定向 A→B | test_conversation_flow | `on_interrupted` + B 新建 + A=ABORTED |
| C1 | 工具调用段展示 | test_card_interaction | `on_tool_update tool=X started+completed` |
| C2 | 完成态折叠 | test_card_interaction | on_completed + state=complete |

### P1 重要（11 个）

| ID | 场景 | 备注 |
|----|------|------|
| A6 | markdown→私聊 | 富文本 |
| A7 | post→话题 | 飞书 post |
| B2 | 长文本 split/rollover | 触发单卡上限 |
| B3 | reasoning 展示 | `<thinking>` 或 native reasoning |
| B4 | 中断后完成重定向 | complete hook 重定向到新 session |
| C3 | 状态色 | streaming 蓝 / complete 绿 / failed 红 |
| C4 | footer 字段 | duration/model/tokens/context |
| C5 | 多工具段 | 多个 tool 调用展示 |

### P2 边界（5 个，xfail 或留后）

| ID | 场景 | 难点 |
|----|------|------|
| B5 | 嵌套中断 A→B→C | 需精确时序 |
| B6 | 跨回合合并 | 需 background 回合（message_id=None）|
| D1 | CardKit 失败 fallback | 需 mock 强制失败 |
| D2 | 卡片创建超时 | 需 mock 慢响应 |
| D3 | UnavailableGuard | 需删除消息触发 |

## 7. fixture 设计（tests/e2e/conftest.py）

```python
PRIVATE_CHAT = "oc_eeba2715144be520aa6a768342c023ae"
TOPIC_CHAT = "oc_d5c00959e919bad9d889ddfa3ff93bd1"
TOPIC_ROOT = "om_x100b6bb3ec02b4a4b27e0fe38eef710"

@pytest.fixture(autouse=True)
def require_e2e_env():
    """环境就绪检查：HERMES_HOME + lark-cli whoami available + gateway 在跑，否则 skip。"""
    ...

@pytest.fixture
def lark():
    """lark-cli helper，封装 send_text / reply_in_thread / search_messages。"""
    return LarkCLIDriver()

@pytest.fixture
def wait_for_log():
    """poll gateway.log 直到出现模式（超时 60s），返回匹配行；超时 raise。"""
    ...

@pytest.fixture
def log_since():
    """记录当前 gateway.log 偏移，返回 reset token；后续断言用 log_contains(token, pattern)。"""
    ...
```

`LarkCLIDriver` 方法：
- `send_text(chat_id, text)` → message_id
- `reply_in_thread(root_msg_id, text)` → message_id
- `search_messages(chat_id, since_ts)` → 消息列表

## 8. 断言策略

发消息 → poll `gateway.log` → 断言含特定模式 + 不含错误码。

```python
def test_a2_image_to_topic(lark, wait_for_log, log_since):
    start = log_since()
    lark.reply_in_thread(TOPIC_ROOT, "[e2e] 发张桌面图到话题")
    assert wait_for_log("[cheerwhy-card] 话题场景", since=start, timeout=60)
    assert not log_contains(start, "99992402")
    assert wait_for_log(r"on_completed_wait.*state=streaming.*complete", since=start)
```

## 9. chat 策略

- 用现有私聊 + 话题群（`PRIVATE_CHAT` / `TOPIC_CHAT` 常量）
- 测试消息加 `[e2e]` 前缀，便于识别 + 清理
- 不新建测试群（避免拉 Hermes bot 的复杂度）
- 话题用 reply-in-thread 落指定话题根（`TOPIC_ROOT`）

## 10. 依赖

- `HERMES_HOME=~/.hermes`（lark-cli 必需前缀）
- lark-cli 配置（user 身份，profile=hermes，OAuth 完成 — 见 memory `lark-cli-setup`）
- Hermes gateway 运行（restart 后加载最新 patch）
- 现有 chat（私聊 + 话题群）+ 话题根消息

## 11. 风险

- e2e 测试消耗 Hermes API（钱）+ 飞书 token
- 测试消息污染主人对话（用 `[e2e]` 前缀识别）
- 真实环境波动（网络/Hermes 状态）可能误判 — 断言带超时 + 重试
- P2 场景难稳定触发（xfail 标记，不阻塞 P0/P1）

## 12. 后续扩展（YAGNI 砍掉，留备）

- 新文件类型（share_chat / interactive card）
- cron 推送 / background 推送测试
- 多 chat 并发
- 性能基准（卡片创建延迟）
