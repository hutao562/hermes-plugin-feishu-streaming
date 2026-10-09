"""doctor 自检脚本 — 用临时 HERMES_HOME 树验证各检查项."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugin.doctor import FAIL, OK, WARN, run_checks

PLUGIN_CORE_FILES = (
    "plugin.yaml", "__init__.py", "adapter.py", "engine.py",
    "_clarify.py", "_compat.py", "contract.py", "doctor.py",
    "_vendor/cardkit/builder.py", "_vendor/cardkit/markdown.py",
    "_vendor/feishu.py", "_vendor/config.py",
    "_vendor/streaming/segments.py", "_vendor/streaming/flush.py",
    "_vendor/streaming/tooluse.py", "_vendor/streaming/segment_helper.py",
    "_vendor/streaming/text.py",
)

GOOD_CONFIG = """
plugins:
  enabled:
    - feishu-streaming-platform
streaming:
  enabled: true
  transport: auto
display:
  platforms:
    feishu:
      streaming: true
"""

REPO = Path(__file__).resolve().parent.parent / "plugin"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """最小健康环境：config 开关齐全 + 从仓库复制的插件目录."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "config.yaml").write_text(GOOD_CONFIG, encoding="utf-8")
    plugin_dir = tmp_path / "plugins" / "feishu-streaming"
    plugin_dir.mkdir(parents=True)
    for rel in PLUGIN_CORE_FILES:
        target = plugin_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((REPO / rel).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text("FEISHU_APP_ID=cli_x\nFEISHU_APP_SECRET=sec\n", encoding="utf-8")
    return tmp_path


def _by_name(report, name: str):
    return [c for c in report.checks if c.name == name]


def test_healthy_home_has_no_fail(home: Path) -> None:
    report = run_checks(home, repo_plugin_dir=None)
    fails = [c for c in report.checks if c.status == FAIL]
    # hermes 源码/网关日志缺失只降级为 WARN（doctor 不吓唬人）
    assert not fails, [c.render() for c in fails]
    assert _by_name(report, "default: plugins.enabled")[0].status == OK
    assert _by_name(report, "插件目录: 核心文件")[0].status == OK
    assert _by_name(report, "凭据: .env")[0].status == OK


def test_missing_streaming_switch_fails(home: Path) -> None:
    (home / "config.yaml").write_text("plugins:\n  enabled: [feishu-streaming-platform]\n",
                                      encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=None)
    assert _by_name(report, "default: streaming.enabled")[0].status == FAIL
    assert _by_name(report, "default: display.platforms.feishu.streaming")[0].status == FAIL


def test_plugin_not_in_enabled_fails(home: Path) -> None:
    (home / "config.yaml").write_text(GOOD_CONFIG.replace("feishu-streaming-platform", "other-plugin"),
                                      encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=None)
    assert _by_name(report, "default: plugins.enabled")[0].status == FAIL


def test_profile_disabled_is_fail(home: Path) -> None:
    profile = home / "profiles" / "family"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        GOOD_CONFIG + "plugins:\n  disabled: [feishu-streaming-platform]\n", encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=None)
    assert _by_name(report, "profile:family: plugins.disabled")[0].status == FAIL


def test_deploy_freshness_warns_on_stale_copy(home: Path) -> None:
    deployed = home / "plugins" / "feishu-streaming"
    (deployed / "engine.py").write_text("# 比仓库旧", encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=REPO)
    assert _by_name(report, "部署一致性")[0].status == WARN


def test_deploy_freshness_ok_when_fresh(home: Path) -> None:
    report = run_checks(home, repo_plugin_dir=REPO)
    assert _by_name(report, "部署一致性")[0].status == OK


def test_missing_core_file_fails(home: Path) -> None:
    (home / "plugins" / "feishu-streaming" / "engine.py").unlink()
    report = run_checks(home, repo_plugin_dir=None)
    assert _by_name(report, "插件目录: 核心文件")[0].status == FAIL


def test_broken_contract_fails(home: Path) -> None:
    src = home / "hermes-agent" / "gateway"
    src.mkdir(parents=True)
    real = (REPO.parent / "tests" / "samples" / "gateway" / "stream_consumer_transport.py")
    broken = real.read_text(encoding="utf-8").replace(
        "supports_draft_streaming(chat_id=", "supports_draft_streaming(")
    (src / "stream_consumer_transport.py").write_text(broken, encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=None)
    assert _by_name(report, "上游契约")[0].status == FAIL


def test_injection_residue_warns(home: Path) -> None:
    src = home / "hermes-agent" / "gateway"
    src.mkdir(parents=True)
    (src / "run.py").write_text("# HERMES_LARK_ADAPTER_INIT\n", encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=None)
    residues = [c for c in report.checks if c.name == "注入模式残留" and c.status == WARN]
    assert residues and "marker" in residues[0].detail


def test_error_signature_scan(home: Path) -> None:
    log = home / "logs" / "gateway.log"
    log.write_text("2026-10-08 INFO [feishu-streaming] draft ok\n"
                   "2026-10-08 ERROR card over max size\n", encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=None)
    assert _by_name(report, "病征: card over max size")[0].status == WARN
    assert _by_name(report, "近期活动")[0].status == OK


def test_no_factory_line_fails(home: Path) -> None:
    (home / "logs" / "gateway.log").write_text("boot ok, nothing else\n", encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=None)
    assert _by_name(report, "装配日志")[0].status == FAIL


def test_cron_card_checks_ok_by_default(home: Path) -> None:
    """wrap_response 未配置（默认开）+ 部署目录含 cron 分支 → 双 ✓."""
    report = run_checks(home, repo_plugin_dir=None)
    wrap = _by_name(report, "cron.wrap_response")[0]
    assert wrap.status == OK
    branch = _by_name(report, "cron 卡分支")[0]
    assert branch.status == OK


def test_cron_wrap_response_false_warns(home: Path) -> None:
    """wrap_response: false → WARN（卡无任务名 header，降级可用）."""
    (home / "config.yaml").write_text(
        GOOD_CONFIG + "\ncron:\n  wrap_response: false\n", encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=None)
    wrap = _by_name(report, "cron.wrap_response")[0]
    assert wrap.status == WARN
    assert "无任务名 header" in wrap.hint


def test_cron_branch_missing_from_deploy_fails(home: Path) -> None:
    """部署目录是旧版（adapter.py 无 send_cron_card）→ FAIL 指向重新部署."""
    (home / "plugins" / "feishu-streaming" / "adapter.py").write_text(
        "# 旧版部署：无 cron 卡分支\n", encoding="utf-8")
    report = run_checks(home, repo_plugin_dir=None)
    branch = _by_name(report, "cron 卡分支")[0]
    assert branch.status == FAIL
    assert "重新拷贝" in branch.hint
