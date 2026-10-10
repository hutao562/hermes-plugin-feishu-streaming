"""cardkit 包测试 — markdown 优化、表格处理、卡片构建."""

from __future__ import annotations

import json

import pytest

from plugin._vendor.cardkit.builder import (
    REASONING_TEXT_ELEMENT_ID,
    TOOL_PANEL_ELEMENT_ID,
    _build_footer_elements,
    _build_header,
    _build_reasoning_panel,
    _build_tool_panel,
    _compact,
    _escape_md,
    _format_elapsed,
    _longest_backtick_run,
    build_complete_card,
    build_streaming_card_v2,
)
from plugin._vendor.cardkit.markdown import (
    _downgrade_tables,
    _find_tables_outside_code_blocks,
    _split_long_text,
    _strip_invalid_image_keys,
    optimize_markdown_style,
)
from plugin._vendor.streaming.segments import Segment, SegmentState

# --- Markdown 优化 ---


class TestOptimizeMarkdownStyle:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("# Title", "#### Title"),
            ("## Sub", "##### Sub"),
            ("### Deep", "##### Deep"),
        ],
    )
    def test_headings_are_downgraded(self, source: str, expected: str) -> None:
        assert expected in optimize_markdown_style(source)

    def test_h4_h5_h6_unchanged(self) -> None:
        text = "#### H4\n##### H5\n###### H6"
        result = optimize_markdown_style(text)
        assert "#### H4" in result
        assert "##### H5" in result

    def test_heading_in_code_block_preserved(self) -> None:
        text = "```\n# Should not change\n```"
        assert "# Should not change" in optimize_markdown_style(text)

    def test_blank_line_compression(self) -> None:
        result = optimize_markdown_style("a\n\n\n\n\nb")
        assert "\n\n\n" not in result

    def test_invalid_image_key_removed(self) -> None:
        text = "![alt](not_img_key)"
        assert "not_img_key" not in optimize_markdown_style(text)

    def test_valid_img_key_preserved(self) -> None:
        text = "![alt](img_v3_abc123)"
        assert "img_v3_abc123" in optimize_markdown_style(text)

    def test_no_headings_unchanged(self) -> None:
        text = "plain text\nanother line"
        assert optimize_markdown_style(text) == text

    def test_mixed_headings_and_code(self) -> None:
        text = "# Title\n```\n# Code heading\n```\n## Sub"
        result = optimize_markdown_style(text)
        assert "#### Title" in result
        assert "# Code heading" in result


class TestStripInvalidImageKeys:
    def test_no_images_unchanged(self) -> None:
        assert _strip_invalid_image_keys("no images") == "no images"

    def test_img_prefix_kept(self) -> None:
        assert "img_v3_test" in _strip_invalid_image_keys("![a](img_v3_test)")

    def test_non_img_removed(self) -> None:
        assert "http://example.com/img.png" not in _strip_invalid_image_keys("![a](http://example.com/img.png)")


# --- 表格处理 ---


class TestFindTablesOutsideCodeBlocks:
    def test_no_tables(self) -> None:
        assert _find_tables_outside_code_blocks("no tables here") == []

    def test_single_table(self) -> None:
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        results = _find_tables_outside_code_blocks(text)
        assert len(results) == 1

    def test_table_inside_code_block_ignored(self) -> None:
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        assert _find_tables_outside_code_blocks(text) == []

    def test_mixed(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = f"{table}\n\n```\n{table}\n```"
        results = _find_tables_outside_code_blocks(text)
        assert len(results) == 1


class TestDowngradeTables:
    def test_within_limit_unchanged(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = f"{table}\n\n{table}\n\n{table}"
        assert _downgrade_tables(text) == text

    def test_over_limit_downgraded(self) -> None:
        # fork 阈值 _MAX_CARD_TABLES=8：9 个表格起降级
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = "\n\n".join([table] * 9)
        result = _downgrade_tables(text)
        assert result.count("```") >= 2  # 超限表格被包装为代码块


# --- 文本拆分 ---


class TestSplitLongText:
    def test_short_text_not_split(self) -> None:
        assert _split_long_text("short") == ["short"]

    def test_long_text_split_at_paragraph(self) -> None:
        chunk = "x" * 1200
        text = f"{chunk}\n\n{chunk}\n\n{chunk}"
        parts = _split_long_text(text, limit=2000)
        assert len(parts) > 1

    def test_no_paragraph_break_falls_back_to_newline(self) -> None:
        lines = ["word " * 100 for _ in range(30)]
        text = "\n".join(lines)
        parts = _split_long_text(text, limit=500)
        assert len(parts) > 1

    def test_exact_limit_not_split(self) -> None:
        text = "a" * 2400
        assert len(_split_long_text(text)) == 1


# --- 工具面板 ---

_STEP_RUNNING = {
    "name": "read",
    "title": "Read",
    "status": "running",
    "detail": "",
    "output": "",
    "error": "",
    "icon": "icon",
    "elapsed_ms": 0,
    "result_block": None,
    "error_block": None,
}
_STEP_SUCCESS = {**_STEP_RUNNING, "status": "success", "output": "ok", "elapsed_ms": 100}
_STEP_ERROR = {**_STEP_RUNNING, "status": "error", "error": "boom"}


class TestBuildToolPanel:
    def test_empty_steps(self) -> None:
        panel = _build_tool_panel([])
        assert panel["element_id"] == TOOL_PANEL_ELEMENT_ID
        assert "Tool use" in panel["header"]["title"]["content"]

    def test_with_steps(self) -> None:
        panel = _build_tool_panel([_STEP_SUCCESS], elapsed_ms=500)
        assert panel["element_id"] == TOOL_PANEL_ELEMENT_ID

    def test_with_elapsed(self) -> None:
        panel = _build_tool_panel([_STEP_RUNNING], elapsed_ms=3000)
        title = panel["header"]["title"]["content"]
        assert "3.0s" in title

    def test_running_step_shows_action_label(self) -> None:
        """流式期折叠标题显示动作标签（📖 Reading 文件.md），不展开即见 agent 在干嘛。"""
        step: dict = {**_STEP_RUNNING, "label": "📖 Reading 幼儿园与学习.md", "emoji": "📖"}
        panel = _build_tool_panel([step], elapsed_ms=0)  # type: ignore[list-item]
        title = panel["header"]["title"]["content"]
        assert "📖 Reading 幼儿园与学习.md" in title
        assert "1 step" in title
        # label 自带 emoji，不再叠 🛠️
        assert title.startswith("📖")

    def test_completed_steps_fall_back_to_tool_use(self) -> None:
        """无 running（全完成）退回 🛠️ Tool use 骨架，不含动作标签。"""
        step: dict = {**_STEP_SUCCESS, "label": "📖 Reading 幼儿园与学习.md", "emoji": "📖"}
        panel = _build_tool_panel([step], elapsed_ms=0)  # type: ignore[list-item]
        title = panel["header"]["title"]["content"]
        assert title.startswith("🛠️ Tool use")
        assert "Reading 幼儿园与学习.md" not in title

    def test_failed_steps_marked_in_collapsed_title(self) -> None:
        """有失败步骤：折叠标题带 ⚠️ N failed 且转红——不展开也能发现回合出过错。"""
        panel = _build_tool_panel([_STEP_SUCCESS, _STEP_ERROR])  # type: ignore[list-item]
        title = panel["header"]["title"]
        assert "⚠️ 1 failed" in title["content"]
        assert title["text_color"] == "red"
        assert "⚠️ 1 步失败" in title["i18n_content"]["zh_cn"]

    def test_success_only_title_stays_grey(self) -> None:
        panel = _build_tool_panel([_STEP_SUCCESS])  # type: ignore[list-item]
        title = panel["header"]["title"]
        assert "failed" not in title["content"]
        assert title["text_color"] == "grey"

    def test_running_with_failure_shows_both(self) -> None:
        """running 步骤 + 前序失败并存：动作标签后跟 ⚠️ 失败计数。"""
        step: dict = {**_STEP_RUNNING, "label": "📖 Reading x.md", "emoji": "📖"}
        panel = _build_tool_panel([_STEP_ERROR, step])  # type: ignore[list-item]
        title = panel["header"]["title"]["content"]
        assert "📖 Reading x.md" in title
        assert "⚠️ 1 failed" in title

    def test_step_offset_range_in_title(self) -> None:
        """多段工具面板：offset>0 标题带全局步区间（steps 6–7 / 第 6–7 步）。"""
        steps = [_STEP_SUCCESS, _STEP_SUCCESS]  # type: ignore[list-item]
        t0 = _build_tool_panel(steps)["header"]["title"]["content"]
        assert "2 steps" in t0
        assert "steps 1–2" not in t0
        panel5 = _build_tool_panel(steps, step_offset=5)
        assert "steps 6–7" in panel5["header"]["title"]["content"]
        assert "第 6–7 步" in panel5["header"]["title"]["i18n_content"]["zh_cn"]

    def test_max_steps_param_caps_with_hidden_marker(self) -> None:
        """max_steps（完成卡动态预算入口）截断展示，保留已折叠标注。"""
        steps = [_STEP_SUCCESS] * 4  # type: ignore[list-item]
        panel = _build_tool_panel(steps, max_steps=2)
        divs = [e for e in panel["elements"] if e.get("tag") == "div"]
        assert len(divs) == 2
        assert any("已折叠前 2 步" in str(e.get("content", "")) for e in panel["elements"])

    def test_pending_panel_has_hint(self) -> None:
        """pending 面板展开不再是空白：带提示行。"""
        card = build_streaming_card_v2(
            show_tool_use=True, show_reasoning=False, show_streaming_element=False)
        panel = next(e for e in card["body"]["elements"]
                     if e.get("tag") == "collapsible_panel")
        assert panel["elements"]
        assert "Tool activity will appear here" in str(panel["elements"])


# --- Footer ---


class TestBuildFooterElements:
    def test_status_completed(self) -> None:
        result = _build_footer_elements({}, fields=[["status"]])
        assert len(result) >= 2  # hr + markdown 元素
        assert "Completed" in result[1]["content"]

    def test_empty_data_renders_nothing_with_local_default_fields(self) -> None:
        # fork 默认字段为 elapsed/model/context（不含 status）——空数据无渲染
        assert _build_footer_elements({}) == []

    def test_status_error(self) -> None:
        result = _build_footer_elements({}, is_error=True, fields=[["status"]])
        assert "red" in result[1]["content"]

    def test_status_aborted(self) -> None:
        result = _build_footer_elements({}, is_aborted=True, fields=[["status"]])
        assert "Stopped" in result[1]["content"]

    def test_elapsed_displayed(self) -> None:
        result = _build_footer_elements({"duration": 12.5}, fields=[["elapsed"]])
        assert "12.5s" in result[1]["content"]

    def test_model_displayed(self) -> None:
        result = _build_footer_elements({"model": "claude-3"}, fields=[["model"]])
        assert "claude-3" in result[1]["content"]

    def test_context_displayed(self) -> None:
        result = _build_footer_elements(
            {"context_used": 50000, "context_max": 200000},
            fields=[["context"]],
        )
        content = result[1]["content"]
        assert "25%" in content
        assert "200.0K" in content
        assert "50.0K" not in content  # 不显示已用

    def test_tokens_displayed(self) -> None:
        result = _build_footer_elements(
            {"input_tokens": 1000, "output_tokens": 500},
            fields=[["tokens"]],
        )
        assert "↑" in result[1]["content"]
        assert "↓" in result[1]["content"]

    def test_speed_displayed(self) -> None:
        result = _build_footer_elements({"tps": 42.4}, fields=[["speed"]])
        assert "42 t/s" in result[1]["content"]
        assert "⚡" in result[1]["content"]

    def test_speed_hidden_without_data(self) -> None:
        assert _build_footer_elements({}, fields=[["speed"]]) == []

    def test_show_label(self) -> None:
        result = _build_footer_elements(
            {"duration": 5},
            fields=[["elapsed"]],
            show_label=True,
        )
        assert "Elapsed" in result[1]["content"]

    def test_multi_row_fields(self) -> None:
        result = _build_footer_elements(
            {"duration": 5, "model": "gpt"},
            fields=[["elapsed"], ["model"]],
        )
        assert "\n" in result[1]["content"]

    def test_none_footer_data_renders_status(self) -> None:
        result = _build_footer_elements(None, fields=[["status"]])
        assert len(result) >= 2

    def test_no_matching_fields(self) -> None:
        assert _build_footer_elements({}, fields=[["tokens"]]) == []


# --- 推理面板 ---


class TestBuildReasoningPanel:
    def test_without_elapsed(self) -> None:
        panel = _build_reasoning_panel("thinking content")
        assert "Thought" in panel["header"]["title"]["content"]
        assert not panel["expanded"]

    def test_with_elapsed(self) -> None:
        panel = _build_reasoning_panel("thoughts", elapsed_ms=5000)
        title = panel["header"]["title"]["content"]
        assert "5.0s" in title

    def test_expanded_true(self) -> None:
        panel = _build_reasoning_panel("text", expanded=True)
        assert panel["expanded"] is True

    def test_element_id_default_none(self) -> None:
        panel = _build_reasoning_panel("text")
        assert "element_id" not in panel

    def test_inner_markdown_has_element_id(self) -> None:
        panel = _build_reasoning_panel("text")
        inner = panel["elements"][0]
        assert inner["element_id"] == REASONING_TEXT_ELEMENT_ID

    def test_title_is_plain_text_grey(self) -> None:
        panel = _build_reasoning_panel("text")
        title = panel["header"]["title"]
        assert title["tag"] == "plain_text"
        assert title["text_color"] == "grey"
        assert title["text_size"] == "notation"

    def test_empty_text_shows_thinking_title(self) -> None:
        panel = _build_reasoning_panel(" ")
        assert "Thinking" in panel["header"]["title"]["content"]

    def test_with_content_shows_thought_title(self) -> None:
        panel = _build_reasoning_panel("reasoning here")
        assert "Thought" in panel["header"]["title"]["content"]
        assert "Thinking" not in panel["header"]["title"]["content"]


# --- 数字格式化 ---


class TestCompact:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (42, "42"),
            (1_500, "1.5K"),
            (2_500_000, "2.5M"),
            (1_000, "1.0K"),
            (250_000_000, "250M"),
        ],
    )
    def test_compacts_numbers(self, value: int, expected: str) -> None:
        assert _compact(value) == expected


class TestFormatElapsed:
    @pytest.mark.parametrize(
        ("milliseconds", "expected"),
        [(3_500, "3.5s"), (125_000, "2m 5s"), (60_000, "1m 0s")],
    )
    def test_formats_elapsed_time(self, milliseconds: float, expected: str) -> None:
        assert _format_elapsed(milliseconds) == expected


# --- 工具函数 ---


class TestEscapeMd:
    def test_escapes_special_chars(self) -> None:
        result = _escape_md("a`b*c{d}e[f]g<h>i")
        assert "\\" in result

    def test_plain_text_unchanged(self) -> None:
        assert _escape_md("hello world") == "hello world"


class TestLongestBacktickRun:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("no backticks", 0), ("a `b` c", 1), ("```code```", 3)],
    )
    def test_finds_longest_run(self, text: str, expected: int) -> None:
        assert _longest_backtick_run(text) == expected


# --- 完整卡片构建 ---


class TestBuildStreamingCardV2:
    def test_structure(self) -> None:
        card = build_streaming_card_v2()
        assert card["schema"] == "2.0"
        assert card["config"]["streaming_mode"] is True
        assert card["body"]["elements"]

    def test_width_mode_default(self) -> None:
        card = build_streaming_card_v2()
        assert card["config"]["width_mode"] == "default"

    def test_width_mode_custom(self) -> None:
        card = build_streaming_card_v2(width_mode="compact")
        assert card["config"]["width_mode"] == "compact"


# --- 分段完成态卡片 ---


def _seg(seg_type: str, text: str = "", **kwargs: int | float) -> Segment:
    """创建测试用 Segment mock."""
    seg = Segment(seg_type, f"{seg_type}_0")
    seg.text = text
    if seg_type == "reasoning":
        seg.text_el_id = f"{seg_type}_0_text"
    seg.tool_offset = int(kwargs.get("tool_offset", 0))
    seg.tool_end_offset = int(kwargs.get("tool_end_offset", 0))
    seg.elapsed_ms = float(kwargs.get("elapsed_ms", 0.0))
    seg.start_time = float(kwargs.get("start_time", 0.0))
    seg.created = True
    seg.dirty = False
    return seg


class TestBuildSegmentCompleteCard:
    def test_empty_segments_and_skipped_reasoning(self) -> None:
        """空 segments 渲染 Done；空 reasoning 被跳过."""
        card = build_complete_card(segments=[], all_tool_steps=[])
        assert card["schema"] == "2.0"
        assert any("Done" in str(e) or "完成" in str(e) for e in card["body"]["elements"])

        card2 = build_complete_card(segments=[_seg("reasoning", "")], all_tool_steps=[])
        assert any("Done" in str(e) or "完成" in str(e) for e in card2["body"]["elements"])

    def test_answer_only_no_done(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hello world")],
            all_tool_steps=[],
        )
        elements = card["body"]["elements"]
        assert any("hello world" in str(e) for e in elements)
        assert not any("Done" in str(e) for e in elements)

    def test_reasoning_before_answer(self) -> None:
        card = build_complete_card(
            segments=[_seg("reasoning", "think"), _seg("answer", "reply")],
            all_tool_steps=[],
        )
        contents = [str(e) for e in card["body"]["elements"]]
        r_idx = next(i for i, c in enumerate(contents) if "think" in c)
        a_idx = next(i for i, c in enumerate(contents) if "reply" in c)
        assert r_idx < a_idx

    def test_tool_segment_uses_steps_slice(self) -> None:
        steps = [_STEP_RUNNING, _STEP_SUCCESS, _STEP_RUNNING]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=1, tool_end_offset=3)],
            all_tool_steps=steps,
        )
        tool_elements = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
        assert len(tool_elements) == 1
        assert len(tool_elements[0].get("elements", [])) == 2  # steps[1:3]

    def test_three_round_ordering(self) -> None:
        card = build_complete_card(
            segments=[
                _seg("reasoning", "r1"),
                _seg("answer", "a1"),
                _seg("tool", tool_offset=0, tool_end_offset=2),
                _seg("reasoning", "r2"),
                _seg("answer", "a2"),
            ],
            all_tool_steps=[_STEP_SUCCESS, _STEP_RUNNING],
        )
        contents = [str(e) for e in card["body"]["elements"]]
        r1 = next(i for i, c in enumerate(contents) if "r1" in c)
        a1 = next(i for i, c in enumerate(contents) if "a1" in c)
        r2 = next(i for i, c in enumerate(contents) if "r2" in c)
        a2 = next(i for i, c in enumerate(contents) if "a2" in c)
        assert r1 < a1 < r2 < a2

    def test_tool_end_offset_zero_uses_all_steps(self) -> None:
        steps = [_STEP_SUCCESS, _STEP_RUNNING]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=0)],
            all_tool_steps=steps,
        )
        inner = next(e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel")["elements"]
        assert len(inner) == 2

    def test_complete_card_width_mode_default(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
        )
        assert card["config"]["width_mode"] == "default"

    def test_complete_card_width_mode_custom(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            width_mode="fill",
        )
        assert card["config"]["width_mode"] == "fill"

    def test_tool_empty_steps_skipped(self) -> None:
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=5, tool_end_offset=5)],
            all_tool_steps=[_STEP_SUCCESS],
        )
        assert not any(e.get("tag") == "collapsible_panel" for e in card["body"]["elements"])

    def test_show_tool_use_false_hides_tool_panel(self) -> None:
        """show_tool_use=False → TOOL segment rendered as nothing (无工具面板)."""
        steps = [_STEP_RUNNING, _STEP_SUCCESS]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=2), _seg("answer", "hello")],
            all_tool_steps=steps,
            show_tool_use=False,
        )
        # 无 collapsible_panel（工具面板）
        assert not any(e.get("tag") == "collapsible_panel" for e in card["body"]["elements"])
        # 但 answer 依然在
        assert any(e.get("tag") == "markdown" and "hello" in str(e.get("content", ""))
                   for e in card["body"]["elements"])

    def test_show_tool_use_true_default_shows_panel(self) -> None:
        """show_tool_use 默认 True → 工具面板保留（向后兼容）."""
        steps = [_STEP_RUNNING, _STEP_SUCCESS]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=2)],
            all_tool_steps=steps,
        )
        assert any(e.get("tag") == "collapsible_panel" for e in card["body"]["elements"])

    def test_two_tool_segments_get_range_titles(self) -> None:
        """多段工具（工具→正文→再工具）：第二面板标题带全局步区间，面板间可区分。"""
        steps = [_STEP_SUCCESS, _STEP_RUNNING, _STEP_SUCCESS, _STEP_SUCCESS]  # type: ignore[list-item]
        card = build_complete_card(
            segments=[
                _seg("tool", tool_offset=0, tool_end_offset=2),
                _seg("answer", "mid"),
                _seg("tool", tool_offset=2, tool_end_offset=4),
            ],
            all_tool_steps=steps,
        )
        panels = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
        assert len(panels) == 2
        assert "2 steps" in panels[0]["header"]["title"]["content"]
        assert "steps 3–4" in panels[1]["header"]["title"]["content"]

    def test_complete_card_step_budget_relaxes_beyond_streaming_cap(self) -> None:
        """完成卡动态预算：短正文 + 40 步全展示（流式 15 封顶不再是完成态上限）。"""
        steps = [_STEP_SUCCESS] * 40  # type: ignore[list-item]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=0), _seg("answer", "ok")],
            all_tool_steps=steps,
        )
        panel = next(e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel")
        divs = [e for e in panel["elements"] if e.get("tag") == "div"]
        assert len(divs) == 40
        assert not any("已折叠" in str(e.get("content", "")) for e in panel["elements"])

    def test_complete_card_step_budget_binds_on_huge_answer(self) -> None:
        """超长正文压缩元素预算：按预算截断步数（仍 ≥15），带已折叠标注。"""
        steps = [_STEP_SUCCESS] * 40  # type: ignore[list-item]
        card = build_complete_card(
            segments=[
                _seg("tool", tool_offset=0, tool_end_offset=0),
                _seg("answer", "x" * 168_000),  # ≈71 个正文块，压掉大半元素预算
            ],
            all_tool_steps=steps,
            footer_enabled=False,
        )
        panel = next(e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel")
        divs = [e for e in panel["elements"] if e.get("tag") == "div"]
        assert 15 <= len(divs) < 40
        assert any("已折叠前" in str(e.get("content", "")) for e in panel["elements"])

    def test_complete_card_char_budget_binds_on_heavy_steps(self) -> None:
        """重结果块吃字符预算（防 200860）：步数在 50 内提前截断。"""
        heavy: dict = {
            **_STEP_SUCCESS,
            "detail": "d" * 200,
            "result_block": {"language": "json", "content": "r" * 1100, "fenced": ""},
        }
        steps = [heavy] * 60  # type: ignore[list-item]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=0)],
            all_tool_steps=steps,
        )
        panel = next(e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel")
        # 步数 = 标题行数（每步一个 "Succeeded" 状态标签；div 含 detail/output 不可直接数）
        shown = str(panel["elements"]).count("Succeeded")
        assert 15 <= shown < 60
        assert any("已折叠前" in str(e.get("content", "")) for e in panel["elements"])

    def test_panel_expanded_flags_control_both_panels(self) -> None:
        """完成卡面板默认展开：工具/推理两键独立控制。"""
        card = build_complete_card(
            segments=[_seg("reasoning", "think"), _seg("answer", "a")],
            all_tool_steps=[],
            tool_panel_expanded=True,
            reasoning_panel_expanded=True,
        )
        reasoning = next(e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel")
        assert reasoning["expanded"] is True
        card2 = build_complete_card(
            segments=[_seg("reasoning", "think"), _seg("answer", "a")],
            all_tool_steps=[],
        )
        reasoning2 = next(e for e in card2["body"]["elements"] if e.get("tag") == "collapsible_panel")
        assert reasoning2["expanded"] is False

    def test_summary_truncated_from_last_answer(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "short"), _seg("answer", "x" * 200)],
            all_tool_steps=[],
        )
        summary = card["config"].get("summary", {}).get("content", "")
        assert len(summary) <= 120


class TestBuildCronCard:
    def test_basic_card_structure(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("Hello **world**")
        assert card["schema"] == "2.0"
        assert card["body"]["elements"][0]["tag"] == "markdown"
        assert "Hello **world**" in card["body"]["elements"][0]["content"]

    def test_summary_from_content(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("Line 1\nLine 2\n" + "x" * 200)
        summary = card["config"]["summary"]["content"]
        assert summary.startswith("Line 1 Line 2")
        assert len(summary) <= 120

    def test_empty_content(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("")
        assert card["body"]["elements"] == []

    def test_table_content_preserved(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        content = "| A | B |\n|---|---|\n| 1 | 2 |"
        card = build_cron_card(content)
        assert "| A | B |" in card["body"]["elements"][0]["content"]

    def test_image_keys_rendered_as_markdown(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("desc", image_keys=["img_v3_abc", "img_v3_def"])
        elements = card["body"]["elements"]
        # 文本在前，图片 element 在后（markdown ![image](img_key) 语法）
        assert elements[0]["content"] == "desc"
        assert elements[1] == {"tag": "markdown", "content": "![image](img_v3_abc)"}
        assert elements[2] == {"tag": "markdown", "content": "![image](img_v3_def)"}

    def test_image_keys_none_no_image_elements(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("desc")
        elements = card["body"]["elements"]
        assert len(elements) == 1  # 只文本，无图片 element
        assert elements[0]["content"] == "desc"

    def test_header_with_task_name(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("Hello", task_name="daily-digest")
        assert card["header"]["title"]["content"] == ":Alarm: daily-digest"
        assert card["header"]["title"]["tag"] == "lark_md"
        assert card["header"]["template"] == "blue"

    def test_no_header_without_task_name(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("Hello")
        assert "header" not in card

    def test_header_with_task_name_and_run_time(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card(
            "Hello",
            task_name="daily-digest",
            run_time="2026-06-10T14:30:00+08:00",
        )
        assert card["header"]["title"]["content"] == ":Alarm: daily-digest · 2026-06-10 14:30"

    def test_header_with_run_time_only(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("Hello", run_time="2026-06-10T14:30:00+08:00")
        assert card["header"]["title"]["content"] == ":Alarm: 2026-06-10 14:30"

    def test_header_invalid_run_time_falls_back_to_raw(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("Hello", run_time="not-a-date")
        assert card["header"]["title"]["content"] == ":Alarm: not-a-date"

    def test_header_run_time_without_timezone(self) -> None:
        from plugin._vendor.cardkit.builder import build_cron_card

        card = build_cron_card("Hello", run_time="2026-06-10T14:30:00")
        assert card["header"]["title"]["content"] == ":Alarm: 2026-06-10 14:30"


# --- Header ---


class TestBuildHeader:
    @pytest.mark.parametrize(
        ("status", "template", "title"),
        [
            ("streaming", "blue", "Processing"),
            ("completed", "green", "Completed"),
            ("error", "red", "Error"),
            ("stopped", "red", "Stopped"),
            ("unknown", "green", "Completed"),
        ],
    )
    def test_status_header(self, status: str, template: str, title: str) -> None:
        header = _build_header(status)
        assert header is not None
        assert header["template"] == template
        assert title in header["title"]["content"]

    def test_title_has_i18n(self) -> None:
        header = _build_header("streaming")
        assert "i18n_content" in header["title"]
        assert "zh_cn" in header["title"]["i18n_content"]
        assert "en_us" in header["title"]["i18n_content"]

class TestStreamingCardHeader:
    def test_header_absent_by_default(self) -> None:
        card = build_streaming_card_v2()
        assert "header" not in card

    def test_header_present_when_enabled(self) -> None:
        card = build_streaming_card_v2(header_enabled=True)
        assert "header" in card
        assert card["header"]["template"] == "blue"


class TestCompleteCardHeader:
    def test_completed_has_green_header(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            header_enabled=True,
        )
        assert "header" in card
        assert card["header"]["template"] == "green"

    def test_aborted_has_red_header(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            is_aborted=True,
            header_enabled=True,
        )
        assert "header" in card
        assert card["header"]["template"] == "red"

    def test_error_has_red_header(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            is_error=True,
            header_enabled=True,
        )
        assert "header" in card
        assert card["header"]["template"] == "red"
        assert "Error" in card["header"]["title"]["content"]

    def test_header_disabled(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            header_enabled=False,
        )
        assert "header" not in card


class TestCompleteCardFooter:
    def test_footer_present_by_default(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            footer_data={"duration": 5, "model": "gpt", "context_used": 1000, "context_max": 10000},
        )
        tags = [e.get("tag") for e in card["body"]["elements"]]
        assert "hr" in tags

    def test_footer_disabled(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            footer_enabled=False,
        )
        tags = [e.get("tag") for e in card["body"]["elements"]]
        assert "hr" not in tags


def test_complete_card_notice_suppresses_done_placeholder() -> None:
    """redirect 收尾卡（工具+NOTICE、无回答）：不补「Done.」占位，摘要用重启提示."""
    state = SegmentState()
    state.on_tool_event(2)
    state.add_notice("↪ 任务已按新指令重启，结果见下方新卡片")
    card = build_complete_card(segments=state.segments, all_tool_steps=[])

    body = json.dumps(card["body"], ensure_ascii=False)
    assert "Done." not in body
    assert "任务已按新指令重启" in body
    # 会话列表摘要一眼识别作废旧卡
    assert card["config"]["summary"]["content"].startswith("↪")


def test_complete_card_without_answer_keeps_done_placeholder() -> None:
    """无回答也无 NOTICE 的空卡：保留「Done.」占位（原行为）."""
    state = SegmentState()
    card = build_complete_card(segments=state.segments, all_tool_steps=[])
    assert "Done." in json.dumps(card["body"], ensure_ascii=False)


def test_tool_output_block_capped_for_card_size() -> None:
    """超长工具输出截断（head+tail+标记）：防卡片 JSON 撑爆飞书体积上限（200860）."""
    from plugin._vendor.streaming.tooluse import _fenced_block

    huge = "x" * 5000
    block = _fenced_block("text", huge)
    assert len(block["content"]) < 1400
    assert "已截断" in block["content"]

    short = _fenced_block("text", "ok")
    assert short["content"] == "ok"
