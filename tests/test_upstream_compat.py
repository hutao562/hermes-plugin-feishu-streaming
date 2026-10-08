"""上游契约锚点检查 — 对 CI 固定 revision 的 hermes 源码验证.

锚点清单在 ``plugin/contract.py``（doctor / 每日 hermes-check 复用同一份）。
本测试保证：pinned 上游样本永远满足插件依赖的 draft-streaming 契约；
上游发版破坏契约时，这里是第一个红灯。
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_sources import MANIFEST_PATH, TREE_FILES, source_at

from plugin.contract import CONTRACT_ANCHORS, check_tree, render_misses


def test_contract_files_covered_by_pinned_manifest() -> None:
    """契约文件必须在 pinned 清单与下载清单里（清单漂移护栏）.

    TREE_FILES 是超集：额外固定的上游文件给未来锚点扩容预留。
    """
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert set(CONTRACT_ANCHORS) <= set(manifest["sha256"])
    assert set(CONTRACT_ANCHORS) <= set(TREE_FILES)


def test_pinned_upstream_satisfies_contract(tmp_path: Path) -> None:
    """pinned revision 的上游源码满足全部契约锚点."""
    for relative in TREE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source_at(relative), encoding="utf-8")
    misses = check_tree(tmp_path)
    assert not misses, f"上游契约被破坏（pinned revision）:\n{render_misses(misses)}"


def test_check_tree_reports_missing_file_and_anchor(tmp_path: Path) -> None:
    """空树 / 被改文件 → 明确的缺失报告（doctor 的失败形态）."""
    (tmp_path / "gateway").mkdir(parents=True)
    real = source_at("gateway/stream_consumer_transport.py")
    broken = real.replace("supports_draft_streaming(chat_id=", "supports_draft_streaming(")
    (tmp_path / "gateway" / "stream_consumer_transport.py").write_text(broken, encoding="utf-8")

    misses = check_tree(tmp_path)
    files = {miss.file for miss in misses}
    assert "gateway/stream_consumer.py" in files  # 整文件缺失
    assert any("supports_draft_streaming(chat_id=" in miss.needle
               and miss.file == "gateway/stream_consumer_transport.py" for miss in misses)
    assert render_misses(misses)  # 人读报告非空
