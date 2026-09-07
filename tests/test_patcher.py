"""Patcher tests (Hermes ≥0.21 multi-file layout) — copy real gateway siblings,
apply/remove/verify against the copies.

Usage:
    ~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/test_patcher.py -v
"""

from __future__ import annotations

import ast
import shutil
import urllib.request
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hermes_lark_streaming.patcher as patcher_mod
from hermes_lark_streaming.patcher import (
    MARKERS,
    MK_CRON_DELIVER,
    MK_CRON_DELIVER_END,
    CronPatcher,
    Patcher,
    PatcherError,
    _cron_deliver_hook,
    _remove_block,
)

ROOT = Path.home() / ".hermes" / "hermes-agent"
# 新版 6 个目标文件（含 facade run.py 供形态探测）
NEW_TARGETS = {
    "run.py": "gateway/run.py",
    "run_turn.py": "gateway/run_turn.py",
    "run_turn_runner.py": "gateway/run_turn_runner.py",
    "run_inbound.py": "gateway/run_inbound.py",
    "run_startup.py": "gateway/run_startup.py",
    "run_notifications.py": "gateway/run_notifications.py",
    "scheduler_delivery.py": "cron/scheduler_delivery.py",
}

_RAW_BASE = "https://raw.githubusercontent.com/NousResearch/hermes-agent/main"


def _ensure_clean(name: str) -> Path:
    """取干净源：优先 .hermes_lark.bak（install 前原版，无 marker），否则本地文件，CI 下载。

    返回写入 tests/samples-<name> 的路径。注意 run.py facade 不是注入目标，
    它的 .hermes_lark.bak 是旧版单文件残留（勿用），直接用当前文件。
    """
    rel = NEW_TARGETS[name]
    bak = ROOT / (rel + ".hermes_lark.bak")
    use_bak = name != "run.py" and bak.exists()
    src = bak if use_bak else ROOT / rel
    sample_dir = Path(__file__).parent / "samples"
    sample_dir.mkdir(parents=True, exist_ok=True)
    dst = sample_dir / f"new-{name}"
    if src.exists():
        shutil.copy2(src, dst)
        return dst
    try:
        urllib.request.urlretrieve(f"{_RAW_BASE}/{rel}", dst)
    except Exception as exc:
        pytest.skip(f"{rel} not found locally and download failed: {exc}")
    if not dst.exists() or dst.stat().st_size == 0:
        pytest.skip(f"{rel} download returned empty file")
    return dst


def _install_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """搭一套 tmp Hermes 树，monkeypatch _default_*_path 指向它；返回 tree 根。"""
    tree = tmp_path / "hermes-agent"
    gw = tree / "gateway"
    cr = tree / "cron"
    gw.mkdir(parents=True, exist_ok=True)
    cr.mkdir(parents=True, exist_ok=True)
    for name, rel in NEW_TARGETS.items():
        sample = _ensure_clean(name)
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sample, target)

    # 让 patcher 模块的默认路径解析指向 tmp 树
    monkeypatch.setattr(patcher_mod, "_default_run_path", lambda: tree / "gateway/run.py")
    monkeypatch.setattr(patcher_mod, "_default_turn_path", lambda: tree / "gateway/run_turn.py")
    monkeypatch.setattr(
        patcher_mod, "_default_turn_runner_path", lambda: tree / "gateway/run_turn_runner.py"
    )
    monkeypatch.setattr(patcher_mod, "_default_inbound_path", lambda: tree / "gateway/run_inbound.py")
    monkeypatch.setattr(patcher_mod, "_default_startup_path", lambda: tree / "gateway/run_startup.py")
    monkeypatch.setattr(
        patcher_mod, "_default_notif_path", lambda: tree / "gateway/run_notifications.py"
    )
    monkeypatch.setattr(
        patcher_mod, "_default_cron_path", lambda: tree / "cron/scheduler_delivery.py"
    )
    return tree


@pytest.fixture()
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _install_tree(tmp_path, monkeypatch)


def _patcher() -> Patcher:
    return Patcher()


def _cron_patcher() -> CronPatcher:
    return CronPatcher()


def _all_text(tree: Path) -> str:
    return "\n".join((tree / rel).read_text(encoding="utf-8") for rel in NEW_TARGETS.values())


class TestVerify:
    def test_verify_passes_on_real_tree(self, tree: Path) -> None:
        _patcher().verify_target()

    def test_verify_fails_when_turn_handler_missing(self, tmp_path: Path, monkeypatch) -> None:
        # 只放 run_turn_runner（缺 run_turn）→ 报错
        p = tmp_path / "hermes-agent"
        (p / "gateway").mkdir(parents=True)
        (p / "cron").mkdir(parents=True)
        # 骨架文件全部放，但 run_turn.py 无关键方法
        for name, rel in NEW_TARGETS.items():
            t = p / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            if name == "run_turn.py":
                t.write_text("class GatewayTurnMixin:\n    pass\n")
            else:
                t.write_text("# stub\n")
        monkeypatch.setattr(patcher_mod, "_default_run_path", lambda: p / "gateway/run.py")
        monkeypatch.setattr(patcher_mod, "_default_turn_path", lambda: p / "gateway/run_turn.py")
        monkeypatch.setattr(
            patcher_mod, "_default_turn_runner_path", lambda: p / "gateway/run_turn_runner.py"
        )
        monkeypatch.setattr(patcher_mod, "_default_inbound_path", lambda: p / "gateway/run_inbound.py")
        monkeypatch.setattr(patcher_mod, "_default_startup_path", lambda: p / "gateway/run_startup.py")
        monkeypatch.setattr(
            patcher_mod, "_default_notif_path", lambda: p / "gateway/run_notifications.py"
        )
        monkeypatch.setattr(
            patcher_mod, "_default_cron_path", lambda: p / "cron/scheduler_delivery.py"
        )
        with pytest.raises(PatcherError, match="start injection site"):
            _patcher().verify_target()

    def test_verify_fails_missing_wire_callbacks(self, tmp_path: Path, monkeypatch) -> None:
        tree = _install_tree(tmp_path, monkeypatch)
        p = tree / "gateway/run_turn_runner.py"
        content = p.read_text(encoding="utf-8")
        # 去掉关键赋值锚点
        content = content.replace(
            "agent.background_review_callback, bg_release = self._make_bg_review_callbacks()",
            "# removed",
        )
        p.write_text(content, encoding="utf-8")
        with pytest.raises(PatcherError, match="bg review assign"):
            _patcher().verify_target()


class TestApplyRemove:
    def test_apply_injects_all_markers_across_files(self, tree: Path) -> None:
        patcher = _patcher()
        patcher.apply()
        all_text = _all_text(tree)
        for begin, end in MARKERS:
            assert begin in all_text, f"Missing marker: {begin}"
            assert end in all_text, f"Missing marker: {end}"

    def test_apply_injects_adapter_init_after_startup_emit(self, tree: Path) -> None:
        patcher = _patcher()
        patcher.apply()
        content = (tree / "gateway/run_startup.py").read_text(encoding="utf-8")
        assert "# HERMES_LARK_ADAPTER_INIT_BEGIN" in content
        assert "patch_feishu_adapter" in content
        init_pos = content.index("# HERMES_LARK_ADAPTER_INIT_BEGIN")
        emit_pos = content.index('hooks.emit("gateway:startup"')
        assert init_pos > emit_pos
        ast.parse(content)

    def test_apply_produces_valid_python_all_files(self, tree: Path) -> None:
        _patcher().apply()
        for name, rel in NEW_TARGETS.items():
            if name == "scheduler_delivery.py":
                continue  # cron 由 CronPatcher 单独打
            ast.parse((tree / rel).read_text(encoding="utf-8"))

    def test_apply_runner_contains_wrapper_logic(self, tree: Path) -> None:
        _patcher().apply()
        content = (tree / "gateway/run_turn_runner.py").read_text(encoding="utf-8")
        assert "# HERMES_LARK_ANSWER_GUARD_BEGIN" in content
        assert "def _hermes_lark_guarded_delta_cb(text):" in content
        assert "on_answer_delta(" in content
        assert "self._ctx._run_still_current()" in content
        assert "agent.stream_delta_callback = _hermes_lark_guarded_delta_cb" in content
        assert "# HERMES_LARK_REASONING_BEGIN" in content
        assert "agent.reasoning_callback = _reasoning_cb" in content

    def test_apply_turn_contains_lifecycle_hooks(self, tree: Path) -> None:
        _patcher().apply()
        content = (tree / "gateway/run_turn.py").read_text(encoding="utf-8")
        assert "# HERMES_LARK_START_BEGIN" in content
        assert "# HERMES_LARK_COMPLETE_BEGIN" in content
        assert "on_message_completed_wait(" in content
        assert "agent_result['already_sent'] = True" in content
        assert "# HERMES_LARK_ABORT_BEGIN" in content
        assert "# HERMES_LARK_BG_DELIVER_BEGIN" in content
        assert "# HERMES_LARK_FOLLOWUP_COMPLETE_BEGIN" in content
        assert "# HERMES_LARK_FOLLOWUP_RESULT_BEGIN" in content
        assert "# HERMES_LARK_INTERRUPT_BEGIN" in content

    def test_apply_idempotent(self, tree: Path) -> None:
        patcher = _patcher()
        patcher.apply()
        after_first = _all_text(tree)
        patcher.apply()
        after_second = _all_text(tree)
        assert after_first == after_second

    def test_apply_upgrades_partial_patch(self, tree: Path) -> None:
        patcher = _patcher()
        patcher.apply()
        # 从 run_turn.py 里剥掉 START 块模拟部分补丁
        p = tree / "gateway/run_turn.py"
        begin, end = next(pair for pair in MARKERS if "START" in pair[0])
        content = _remove_block(p.read_text(encoding="utf-8"), begin, end)
        p.write_text(content, encoding="utf-8")

        patcher.apply()
        upgraded = p.read_text(encoding="utf-8")
        assert upgraded.count(begin) == 1
        assert upgraded.count(end) == 1

    def test_apply_hard_fails_when_injection_site_missing(self, tree: Path) -> None:
        patcher = _patcher()
        with (
            patch("hermes_lark_streaming.patcher._find_turn_complete_site", return_value=None),
            pytest.raises(PatcherError, match="complete injection site"),
        ):
            patcher.apply()

    def test_remove_restores_original(self, tree: Path) -> None:
        patcher = _patcher()
        before = _all_text(tree)
        patcher.apply()
        patcher.remove()
        assert _all_text(tree) == before

    def test_remove_produces_valid_python(self, tree: Path) -> None:
        _patcher().apply()
        _patcher().remove()
        ast.parse((tree / "gateway/run_turn.py").read_text(encoding="utf-8"))
        ast.parse((tree / "gateway/run_turn_runner.py").read_text(encoding="utf-8"))

    def test_remove_on_unpatched_is_noop(self, tree: Path) -> None:
        before = _all_text(tree)
        _patcher().remove()
        assert _all_text(tree) == before

    def test_apply_then_remove_repeatedly(self, tree: Path) -> None:
        patcher = _patcher()
        before = _all_text(tree)
        for _ in range(3):
            patcher.apply()
            patcher.remove()
        assert _all_text(tree) == before


class TestBackupRestore:
    # run.py facade 不是注入目标（Patcher.targets 只含 5 个 sibling），不生成 .bak。
    _INJECTED_RELS: ClassVar[list[str]] = [
        "gateway/run_turn.py",
        "gateway/run_turn_runner.py",
        "gateway/run_inbound.py",
        "gateway/run_startup.py",
        "gateway/run_notifications.py",
    ]

    def test_backup_created_on_apply(self, tree: Path) -> None:
        patcher = _patcher()
        patcher.apply()
        for rel in self._INJECTED_RELS:
            bak = tree / (rel + ".hermes_lark.bak")
            assert bak.exists(), f"missing backup: {bak}"

    def test_restore_recovers_original(self, tree: Path) -> None:
        patcher = _patcher()
        before = _all_text(tree)
        patcher.apply()
        patcher.restore()
        assert _all_text(tree) == before

    def test_restore_fails_without_backup(self, tree: Path) -> None:
        # 主动删除注入目标全部 .bak 后 restore 应报错
        for rel in self._INJECTED_RELS:
            bak = tree / (rel + ".hermes_lark.bak")
            if bak.exists():
                bak.unlink()
        with pytest.raises(PatcherError, match="No backup"):
            _patcher().restore()


# --- CronPatcher (scheduler_delivery.py 0.21+) ---


def _build_cron_hook_runner():
    namespace: dict = {
        "job": {
            "name": "test",
            "next_run_at": "2026-06-10T14:30:00+08:00",
        }
    }
    source = (
        "def _hermes_lark_cron_target_feishu(target):\n"
        "    plat = target.get('platform')\n"
        "    val = getattr(plat, 'value', plat)\n"
        "    return str(val).lower() in ('feishu', 'lark')\n"
        "def deliver(targets, cleaned_delivery_content, loop):\n"
        "    fallback = []\n"
        "    for target in targets:\n"
        f"{_cron_deliver_hook('        ')}"
        "        fallback.append(target['chat_id'])\n"
        "    return fallback\n"
    )
    exec(compile(source, "<cron-hook-test>", "exec"), namespace)
    return namespace["deliver"]


class TestCronVerify:
    def test_verify_passes(self, tree: Path) -> None:
        _cron_patcher().verify_target()

    def test_verify_fails_missing_loop(self, tmp_path: Path, monkeypatch) -> None:
        p = tmp_path / "scheduler_delivery.py"
        p.write_text("def _deliver_result():\n    pass\ncleaned_delivery_content = ''\n")
        monkeypatch.setattr(patcher_mod, "_default_cron_path", lambda: p)
        with pytest.raises(PatcherError, match="for target in targets:"):
            _cron_patcher().verify_target()

    def test_verify_fails_missing_cleaned_content(self, tmp_path: Path, monkeypatch) -> None:
        p = tmp_path / "scheduler_delivery.py"
        p.write_text("def _deliver_result():\n    for target in targets:\n        pass\n")
        monkeypatch.setattr(patcher_mod, "_default_cron_path", lambda: p)
        with pytest.raises(PatcherError, match="cleaned_delivery_content"):
            _cron_patcher().verify_target()


class TestCronApplyRemove:
    def test_apply_injects_markers(self, tree: Path) -> None:
        cp = _cron_patcher()
        cp.apply()
        content = (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8")
        assert MK_CRON_DELIVER in content
        assert MK_CRON_DELIVER_END in content

    def test_apply_produces_valid_python(self, tree: Path) -> None:
        cp = _cron_patcher()
        cp.apply()
        content = (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8")
        compile(content, str(tree / "cron/scheduler_delivery.py"), "exec")

    def test_apply_injects_feishu_helper(self, tree: Path) -> None:
        cp = _cron_patcher()
        cp.apply()
        content = (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8")
        assert "_hermes_lark_cron_target_feishu" in content
        assert "def _hermes_lark_cron_target_feishu" in content

    def test_apply_idempotent(self, tree: Path) -> None:
        cp = _cron_patcher()
        cp.apply()
        first = (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8")
        cp.apply()
        assert (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8") == first

    def test_remove_restores_original(self, tree: Path) -> None:
        cp = _cron_patcher()
        original = (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8")
        cp.apply()
        cp.remove()
        assert (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8") == original

    def test_remove_on_unpatched_is_noop(self, tree: Path) -> None:
        cp = _cron_patcher()
        original = (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8")
        cp.remove()
        assert (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8") == original

    def test_injected_hook_references_on_cron_deliver(self, tree: Path) -> None:
        cp = _cron_patcher()
        cp.apply()
        content = (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8")
        assert "on_cron_deliver" in content
        assert "delivered = True" in content

    def test_injected_hook_skips_duplicate_card_target(self) -> None:
        deliver = _build_cron_hook_runner()
        sent = []

        def fake_on_cron_deliver(*, chat_id, content, loop, task_name, run_time, job_id):
            sent.append((chat_id, content, task_name, run_time))
            return True

        class _Feishu:
            value = "feishu"

        targets = [
            {"platform": _Feishu(), "chat_id": "oc_same"},
            {"platform": _Feishu(), "chat_id": "oc_same"},
        ]
        with patch(
            "hermes_lark_streaming.patch.on_cron_deliver",
            side_effect=fake_on_cron_deliver,
        ):
            fallback = deliver(targets, " failed ", object())

        assert sent == [("oc_same", "failed", "test", "2026-06-10T14:30:00+08:00")]
        assert fallback == []

    def test_injected_hook_retries_duplicate_target_after_failure(self) -> None:
        deliver = _build_cron_hook_runner()

        class _Feishu:
            value = "feishu"

        targets = [
            {"platform": _Feishu(), "chat_id": "oc_same"},
            {"platform": _Feishu(), "chat_id": "oc_same"},
        ]
        with patch(
            "hermes_lark_streaming.patch.on_cron_deliver",
            side_effect=[False, True],
        ) as mock_deliver:
            fallback = deliver(targets, "failed", object())

        assert mock_deliver.call_count == 2
        assert fallback == ["oc_same"]


class TestCronBackupRestore:
    def test_backup_created_on_apply(self, tree: Path) -> None:
        cp = _cron_patcher()
        cp.apply()
        backup = tree / "cron/scheduler_delivery.py.hermes_lark.bak"
        assert backup.exists()

    def test_restore_recovers_original(self, tree: Path) -> None:
        cp = _cron_patcher()
        original = (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8")
        cp.apply()
        cp.restore()
        assert (tree / "cron/scheduler_delivery.py").read_text(encoding="utf-8") == original

    def test_restore_fails_without_backup(self, tree: Path) -> None:
        # 去掉 backup 文件
        bak = tree / "cron/scheduler_delivery.py.hermes_lark.bak"
        if bak.exists():
            bak.unlink()
        with pytest.raises(PatcherError, match="No backup found"):
            _cron_patcher().restore()


class TestOnCronDeliverHook:
    def test_returns_false_when_disabled(self) -> None:
        from hermes_lark_streaming.patch import on_cron_deliver

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = False
            mock_get.return_value = ctrl
            assert on_cron_deliver(chat_id="c1", content="text", loop=MagicMock()) is False

    def test_returns_false_when_no_loop(self) -> None:
        from hermes_lark_streaming.patch import on_cron_deliver

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            mock_get.return_value = ctrl
            assert on_cron_deliver(chat_id="c1", content="text", loop=None) is False

    def test_delegates_to_controller(self) -> None:
        from hermes_lark_streaming.patch import on_cron_deliver

        loop = MagicMock()
        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            ctrl.on_cron_deliver.return_value = True
            mock_get.return_value = ctrl
            result = on_cron_deliver(chat_id="c1", content="hello", loop=loop)
            assert result is True
            ctrl.on_cron_deliver.assert_called_once_with(
                chat_id="c1", content="hello", loop=loop,
                task_name="", run_time="", job_id="",
            )


class TestQueuedFollowupHooks:
    @pytest.mark.asyncio
    async def test_boundary_marks_result_when_card_sent(self) -> None:
        from hermes_lark_streaming.patch import on_queued_followup_boundary

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            ctrl.on_completed_wait = AsyncMock(return_value=True)
            mock_get.return_value = ctrl
            result = {"final_response": "ok", "model": "m"}

            assert await on_queued_followup_boundary(message_id="msg", result=result) is True

            assert result["response_previewed"] is True
            assert result["already_sent"] is True

    @pytest.mark.asyncio
    async def test_boundary_consumes_fallback_when_card_not_sent(self) -> None:
        from hermes_lark_streaming.patch import on_queued_followup_boundary

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            ctrl.on_completed_wait = AsyncMock(return_value=False)
            mock_get.return_value = ctrl
            result = {"final_response": "plain"}

            assert await on_queued_followup_boundary(message_id="msg", result=result) is False

            ctrl.consume_text_fallback.assert_called_once_with("msg")
            assert "response_previewed" not in result

    def test_result_hook_preserves_deepest_completion_id(self) -> None:
        from hermes_lark_streaming.patch import on_queued_followup_result

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            mock_get.return_value = ctrl
            result = {"_hermes_lark_completion_id": "deep"}

            on_queued_followup_result(message_id="outer", followup_result=result)

            assert result["_hermes_lark_completion_id"] == "deep"
