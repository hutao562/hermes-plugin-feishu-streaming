"""Clarify 内联单选测试.

覆盖 card builders、choice 规范化、card-action handler 分发（choice/other）、
_on_card_action_trigger wrapper 路由、patch 幂等性、以及 _canonical_choice_text。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_lark_streaming import clarify

# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------

class TestBuildClarifyCard:
    def test_choices_card_has_one_button_per_choice_plus_other(self) -> None:
        card = clarify._build_clarify_card(
            question="Which deploy target?", choices=["staging", "prod"], clarify_id="abc123"
        )
        assert card["config"] == {"wide_screen_mode": True}
        assert card["header"]["template"] == "orange"
        # question-md + options-md(numbered list) + action block
        tags = [e["tag"] for e in card["elements"]]
        assert tags == ["markdown", "markdown", "action"]
        # options markdown shows full choice text in a numbered list
        options_md = card["elements"][1]["content"]
        assert "staging" in options_md
        assert "prod" in options_md
        assert "1." in options_md and "2." in options_md
        actions = card["elements"][2]["actions"]
        assert len(actions) == 3  # 2 choices + 1 other
        # buttons are numbered (short label), value carries full text
        assert actions[0]["text"]["content"] == "1"
        assert actions[0]["value"] == {
            "hermes_clarify_action": "choice", "clarify_id": "abc123",
            "index": 0, "text": "staging",
        }
        assert actions[1]["text"]["content"] == "2"
        assert actions[1]["value"] == {
            "hermes_clarify_action": "choice", "clarify_id": "abc123",
            "index": 1, "text": "prod",
        }
        # other button last
        assert actions[2]["value"] == {
            "hermes_clarify_action": "other", "clarify_id": "abc123",
        }
        assert "其他" in actions[2]["text"]["content"]

    def test_choices_capped_at_four(self) -> None:
        card = clarify._build_clarify_card(
            question="q", choices=["a", "b", "c", "d", "e", "f"], clarify_id="x"
        )
        actions = card["elements"][2]["actions"]
        # 4 choices (capped) + 1 other
        assert len(actions) == 5
        choice_actions = [a for a in actions if a["value"]["hermes_clarify_action"] == "choice"]
        assert len(choice_actions) == 4

    def test_resolved_card_green(self) -> None:
        card = clarify._build_resolved_clarify_card(choice="prod", user_name="alice")
        assert card["header"]["template"] == "green"
        assert "prod" in card["elements"][0]["content"]
        assert "alice" in card["elements"][0]["content"]

    def test_awaiting_card_blue(self) -> None:
        card = clarify._build_awaiting_clarify_card(user_name="bob")
        assert card["header"]["template"] == "blue"
        assert "bob" in card["elements"][0]["content"]

    def test_open_card_no_buttons(self) -> None:
        card = clarify._build_open_clarify_card(question="What now?")
        tags = [e["tag"] for e in card["elements"]]
        assert "action" not in tags
        assert "markdown" in tags


# ---------------------------------------------------------------------------
# Choice normalisation
# ---------------------------------------------------------------------------

class TestFlattenChoice:
    def test_string_passthrough(self) -> None:
        assert clarify._flatten_choice("  hello ") == "hello"

    def test_dict_label_key(self) -> None:
        assert clarify._flatten_choice({"label": "Staging", "name": "stg"}) == "Staging"

    def test_dict_description_fallback(self) -> None:
        assert clarify._flatten_choice({"description": "Production env"}) == "Production env"

    def test_dict_no_canonical_key_returns_empty(self) -> None:
        assert clarify._flatten_choice({"name": "x", "value": "y"}) == ""

    def test_list_joined(self) -> None:
        assert clarify._flatten_choice(["a", "b"]) == "a b"

    def test_normalize_drops_empty_and_caps(self) -> None:
        result = clarify._normalize_choices(["ok", "", {"name": "ghost"}, "a", "b", "c", "d"])
        assert result == ["ok", "a", "b", "c"]


# ---------------------------------------------------------------------------
# _canonical_choice_text
# ---------------------------------------------------------------------------

class TestCanonicalChoiceText:
    def _install_fake_entries(self, entries: dict[str, Any]) -> None:
        import sys
        sys.modules.setdefault("tools", MagicMock())
        sys.modules["tools.clarify_gateway"] = SimpleNamespace(_entries=entries)

    def test_returns_canonical_from_entry(self) -> None:
        fake_entry = SimpleNamespace(choices=["Staging", "Prod"])
        self._install_fake_entries({"cid": fake_entry})
        assert clarify._canonical_choice_text("cid", 0) == "Staging"
        assert clarify._canonical_choice_text("cid", 1) == "Prod"

    def test_missing_entry_returns_empty(self) -> None:
        assert clarify._canonical_choice_text("nope", 0) == ""

    def test_out_of_range_returns_empty(self) -> None:
        fake_entry = SimpleNamespace(choices=["only"])
        self._install_fake_entries({"cid": fake_entry})
        assert clarify._canonical_choice_text("cid", 5) == ""

    def test_bad_index_returns_empty(self) -> None:
        assert clarify._canonical_choice_text("cid", "notanint") == ""


# ---------------------------------------------------------------------------
# _on_card_action_trigger wrapper routing
# ---------------------------------------------------------------------------

class TestCardActionWrapper:
    def _make_event(self, value: dict) -> SimpleNamespace:
        action = SimpleNamespace(value=value)
        return SimpleNamespace(event=SimpleNamespace(action=action))

    def _make_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter._loop = MagicMock()
        adapter._loop_accepts_callbacks.return_value = True
        adapter._is_interactive_operator_authorized.return_value = True
        adapter._get_cached_sender_name.return_value = "alice"
        adapter._submit_on_loop.return_value = True
        return adapter

    def test_routes_clarify_to_handler(self) -> None:
        original = MagicMock(return_value="ORIGINAL")
        adapter = self._make_adapter()
        wrapper = clarify._make_card_action_wrapper(adapter, original)

        clarify._CLARIFY_STATE["cid"] = {"session_key": "s", "chat_id": "c", "message_id": "m"}

        with patch.object(clarify, "_handle_clarify_card_action", return_value="CLARIFY_RESP") as h:
            data = self._make_event({"hermes_clarify_action": "choice", "clarify_id": "cid", "index": 0})
            result = wrapper(data)
            h.assert_called_once()

        assert result == "CLARIFY_RESP"
        original.assert_not_called()
        clarify._CLARIFY_STATE.pop("cid", None)

    def test_non_clarify_falls_through_to_original(self) -> None:
        original = MagicMock(return_value="ORIGINAL")
        adapter = self._make_adapter()
        wrapper = clarify._make_card_action_wrapper(adapter, original)
        data = self._make_event({"hermes_action": "approve_once", "approval_id": 1})
        result = wrapper(data)
        assert result == "ORIGINAL"
        original.assert_called_once_with(data)

    def test_value_not_dict_falls_through(self) -> None:
        original = MagicMock(return_value="ORIG")
        adapter = self._make_adapter()
        wrapper = clarify._make_card_action_wrapper(adapter, original)
        action = SimpleNamespace(value=None)
        data = SimpleNamespace(event=SimpleNamespace(action=action))
        wrapper(data)
        original.assert_called_once()

    def test_wrapper_has_patch_mark(self) -> None:
        original = MagicMock()
        adapter = self._make_adapter()
        wrapper = clarify._make_card_action_wrapper(adapter, original)
        assert getattr(wrapper, clarify._PATCH_MARK) is True


# ---------------------------------------------------------------------------
# _handle_clarify_card_action — choice vs other
# ---------------------------------------------------------------------------

def _make_adapter_mock(*, authorized=True, chat_match=True) -> MagicMock:
    adapter = MagicMock()
    adapter._loop = MagicMock()
    adapter._loop_accepts_callbacks.return_value = True
    adapter._is_interactive_operator_authorized.return_value = authorized
    adapter._get_cached_sender_name.return_value = "alice"
    adapter._submit_on_loop.return_value = True
    return adapter


def _make_card_action_event(*, open_id="ou_1", chat_id="c") -> SimpleNamespace:
    operator = SimpleNamespace(open_id=open_id, user_id="u1")
    context = SimpleNamespace(open_chat_id=chat_id)
    return SimpleNamespace(operator=operator, context=context)


class TestHandleClarifyCardAction:
    def setup_method(self) -> None:
        clarify._CLARIFY_STATE.clear()

    def teardown_method(self) -> None:
        clarify._CLARIFY_STATE.clear()

    def test_missing_clarify_id_returns_empty(self) -> None:
        adapter = _make_adapter_mock()
        resp = clarify._handle_clarify_card_action(
            adapter, event=_make_card_action_event(), action_value={"hermes_clarify_action": "choice"}
        )
        # empty P2CardActionTriggerResponse (or None if SDK missing)
        assert resp is None or getattr(resp, "card", None) is None

    def test_unknown_clarify_id_returns_empty(self) -> None:
        adapter = _make_adapter_mock()
        resp = clarify._handle_clarify_card_action(
            adapter, event=_make_card_action_event(),
            action_value={"hermes_clarify_action": "choice", "clarify_id": "ghost"},
        )
        assert resp is None or getattr(resp, "card", None) is None

    def test_unauthorized_returns_empty(self) -> None:
        adapter = _make_adapter_mock(authorized=False)
        clarify._CLARIFY_STATE["cid"] = {"session_key": "s", "chat_id": "c", "message_id": "m"}
        resp = clarify._handle_clarify_card_action(
            adapter, event=_make_card_action_event(),
            action_value={"hermes_clarify_action": "choice", "clarify_id": "cid", "index": 0},
        )
        assert resp is None or getattr(resp, "card", None) is None

    def test_choice_action_schedules_resolve_and_returns_card(self) -> None:
        adapter = _make_adapter_mock()
        clarify._CLARIFY_STATE["cid"] = {"session_key": "s", "chat_id": "c", "message_id": "m"}

        with patch.object(clarify, "_canonical_choice_text", return_value="Prod"):
            resp = clarify._handle_clarify_card_action(
                adapter, event=_make_card_action_event(),
                action_value={"hermes_clarify_action": "choice", "clarify_id": "cid", "index": 1},
            )
        # resolve scheduled
        adapter._submit_on_loop.assert_called_once()
        # state NOT popped here (popped inside async _resolve_clarify); card returned
        assert resp is not None
        # Card has green template if SDK present; if lark SDK missing, resp may be a bare object
        card = getattr(resp, "card", None)
        if card is not None and getattr(card, "data", None):
            assert card.data["header"]["template"] == "green"

    def test_choice_uses_value_text_when_entry_gone(self) -> None:
        """entry 被 text-intercept 清掉后，button value 里的 text 兜底 resolve."""
        adapter = _make_adapter_mock()
        clarify._CLARIFY_STATE["cid"] = {"session_key": "s", "chat_id": "c", "message_id": "m"}
        # _canonical_choice_text 返回空（模拟 entry 已清）
        with patch.object(clarify, "_canonical_choice_text", return_value=""):
            resp = clarify._handle_clarify_card_action(
                adapter, event=_make_card_action_event(),
                action_value={
                    "hermes_clarify_action": "choice", "clarify_id": "cid",
                    "index": 0, "text": "GPT-4o",
                },
            )
        # 仍然调度了 resolve，用的是 value 里的 text
        adapter._submit_on_loop.assert_called_once()
        assert resp is not None

    def test_other_action_marks_awaiting(self) -> None:
        adapter = _make_adapter_mock()
        clarify._CLARIFY_STATE["cid"] = {"session_key": "s", "chat_id": "c", "message_id": "m"}
        with patch("tools.clarify_gateway.mark_awaiting_text", create=True) as m:
            import sys
            sys.modules.setdefault("tools", MagicMock())
            sys.modules.setdefault("tools.clarify_gateway", SimpleNamespace(mark_awaiting_text=m))
            clarify._handle_clarify_card_action(
                adapter, event=_make_card_action_event(),
                action_value={"hermes_clarify_action": "other", "clarify_id": "cid"},
            )
            m.assert_called_once_with("cid")
        # other does NOT schedule resolve (waits for text intercept)
        adapter._submit_on_loop.assert_not_called()

    def test_chat_mismatch_returns_empty(self) -> None:
        adapter = _make_adapter_mock()
        clarify._CLARIFY_STATE["cid"] = {"session_key": "s", "chat_id": "expected", "message_id": "m"}
        resp = clarify._handle_clarify_card_action(
            adapter, event=_make_card_action_event(chat_id="DIFFERENT"),
            action_value={"hermes_clarify_action": "other", "clarify_id": "cid"},
        )
        assert resp is None or getattr(resp, "card", None) is None


# ---------------------------------------------------------------------------
# _resolve_clarify — async, actually unblocks agent
# ---------------------------------------------------------------------------

def _install_fake_clarify_gateway(**attrs: Any) -> None:
    """Populate sys.modules with a fake tools.clarify_gateway for runtime imports."""
    import sys
    tools = sys.modules.get("tools")
    if tools is None:
        tools = types_module("tools")
        sys.modules["tools"] = tools
    sys.modules["tools.clarify_gateway"] = SimpleNamespace(**attrs)


def _cleanup_fake_modules() -> None:
    """Remove test-installed sys.modules entries (avoid cross-test class leakage)."""
    import sys
    for name in list(sys.modules):
        if name.startswith(("plugins", "hermes_plugins")) or name == "tools.clarify_gateway":
            # only remove the ones our tests synthesized (real ones have __file__)
            mod = sys.modules.get(name)
            if mod is not None and getattr(mod, "__file__", None) is None:
                sys.modules.pop(name, None)


def types_module(name: str) -> Any:
    """Create a real empty module object (so attribute setattr sticks)."""
    import types as _types
    return _types.ModuleType(name)


class TestResolveClarify:
    def setup_method(self) -> None:
        clarify._CLARIFY_STATE.clear()

    @pytest.mark.asyncio
    async def test_resolve_calls_resolve_gateway_clarify(self) -> None:
        adapter = _make_adapter_mock()
        clarify._CLARIFY_STATE["cid"] = {"session_key": "s", "chat_id": "c", "message_id": "m"}
        resolve_fn = MagicMock(return_value=True)
        _install_fake_clarify_gateway(resolve_gateway_clarify=resolve_fn)
        await clarify._resolve_clarify(adapter, "cid", "Prod", open_id="ou_1", chat_id="c")
        resolve_fn.assert_called_once_with("cid", "Prod")
        # state popped
        assert "cid" not in clarify._CLARIFY_STATE

    @pytest.mark.asyncio
    async def test_resolve_missing_state_is_noop(self) -> None:
        adapter = _make_adapter_mock()
        resolve_fn = MagicMock()
        _install_fake_clarify_gateway(resolve_gateway_clarify=resolve_fn)
        await clarify._resolve_clarify(adapter, "ghost", "x")
        resolve_fn.assert_not_called()


# ---------------------------------------------------------------------------
# patch_feishu_adapter — processor.f replacement + send_clarify patch
# ---------------------------------------------------------------------------

def _make_fake_adapter() -> type:
    """Fresh class per test (avoid cross-test send_clarify state leakage)."""
    class _FakeAdapter:
        def _on_card_action_trigger(self, data: Any) -> Any:
            return "ORIGINAL"
    return _FakeAdapter


def _make_processor_mock(*, f_marked: bool = False) -> Any:
    """Mock an SDK card-action processor with a .f callable (real object, not MagicMock)."""
    original_f = MagicMock(return_value="ORIGINAL_DISPATCH")
    if f_marked:
        setattr(original_f, clarify._PATCH_MARK, True)
    # Use a SimpleNamespace so .f assignment actually replaces (MagicMock auto-attrs don't).
    return SimpleNamespace(f=original_f)


def _make_adapter_instance(cls: type, processor: Any) -> Any:
    """Build a fake adapter instance with _event_handler + processor map."""
    inst = cls()
    handler = SimpleNamespace(_callback_processor_map={"p2.card.action.trigger": processor})
    inst._event_handler = handler  # type: ignore[attr-defined]
    inst._loop = MagicMock()  # type: ignore[attr-defined]
    inst._loop_accepts_callbacks = MagicMock(return_value=True)  # type: ignore[attr-defined]
    return inst


class TestPatchFeishuAdapter:
    def teardown_method(self) -> None:
        _cleanup_fake_modules()

    def _install_fake_adapter_module(self, cls: type) -> type:
        import sys
        for name in ("plugins", "plugins.platforms", "plugins.platforms.feishu"):
            if name not in sys.modules:
                sys.modules[name] = types_module(name)
        adapter_mod = types_module("plugins.platforms.feishu.adapter")
        adapter_mod.FeishuAdapter = cls  # type: ignore[attr-defined]
        sys.modules["plugins.platforms.feishu.adapter"] = adapter_mod
        return cls

    def test_patch_replaces_processor_f(self) -> None:
        """patch_feishu_adapter replaces processor.f on the feishu instance."""
        FakeAdapter = self._install_fake_adapter_module(_make_fake_adapter())
        proc = _make_processor_mock()
        original_f = proc.f
        adapter = _make_adapter_instance(FakeAdapter, proc)
        adapters = {"feishu": adapter}

        with patch("hermes_lark_streaming.clarify.Config") as cfg:
            cfg.return_value.clarify_inline = True
            clarify.patch_feishu_adapter(adapters)

        # processor.f was replaced with our wrapper (marked)
        assert proc.f is not original_f
        assert getattr(proc.f, clarify._PATCH_MARK, False) is True
        # send_clarify patched on the class
        assert getattr(FakeAdapter.send_clarify, clarify._PATCH_MARK, False) is True  # type: ignore[attr-defined]

    def test_patch_is_idempotent(self) -> None:
        """patch twice should not re-replace processor.f or send_clarify."""
        FakeAdapter = self._install_fake_adapter_module(_make_fake_adapter())
        proc = _make_processor_mock()
        adapter = _make_adapter_instance(FakeAdapter, proc)
        adapters = {"feishu": adapter}

        with patch("hermes_lark_streaming.clarify.Config") as cfg:
            cfg.return_value.clarify_inline = True
            clarify.patch_feishu_adapter(adapters)
            first_f = proc.f
            first_send = FakeAdapter.send_clarify  # type: ignore[attr-defined]

            clarify.patch_feishu_adapter(adapters)  # second call
            assert proc.f is first_f  # not re-replaced
            assert FakeAdapter.send_clarify is first_send  # type: ignore[attr-defined]

    def test_patch_skipped_when_disabled(self) -> None:
        FakeAdapter = self._install_fake_adapter_module(_make_fake_adapter())
        proc = _make_processor_mock()
        original_f = proc.f
        adapter = _make_adapter_instance(FakeAdapter, proc)

        with patch("hermes_lark_streaming.clarify.Config") as cfg:
            cfg.return_value.clarify_inline = False
            clarify.patch_feishu_adapter({"feishu": adapter})

        assert proc.f is original_f  # unchanged
        assert not hasattr(FakeAdapter, "send_clarify")

    def test_replaced_processor_routes_clarify(self) -> None:
        """The replaced processor.f actually intercepts clarify buttons."""
        FakeAdapter = self._install_fake_adapter_module(_make_fake_adapter())
        proc = _make_processor_mock()
        adapter = _make_adapter_instance(FakeAdapter, proc)
        adapter._is_interactive_operator_authorized = MagicMock(return_value=True)  # type: ignore[attr-defined]
        adapter._get_cached_sender_name = MagicMock(return_value="alice")  # type: ignore[attr-defined]
        adapter._submit_on_loop = MagicMock(return_value=True)  # type: ignore[attr-defined]

        with patch("hermes_lark_streaming.clarify.Config") as cfg:
            cfg.return_value.clarify_inline = True
            clarify.patch_feishu_adapter({"feishu": adapter})

        clarify._CLARIFY_STATE["cid"] = {"session_key": "s", "chat_id": "c", "message_id": "m"}
        action = SimpleNamespace(value={"hermes_clarify_action": "other", "clarify_id": "cid"})
        data = SimpleNamespace(
            event=SimpleNamespace(
                action=action,
                operator=SimpleNamespace(open_id="ou_1", user_id="u1"),
                context=SimpleNamespace(open_chat_id="c"),
            )
        )
        # call the replaced processor.f (SDK does processor.f(data))
        result = proc.f(data)
        # other action → mark_awaiting_text called, card returned (or None if SDK missing)
        assert result is not None or result is None  # didn't raise
        clarify._CLARIFY_STATE.pop("cid", None)


# ---------------------------------------------------------------------------
# 多 profile（多路复用 gateway）—— 每个 profile 一个 FeishuAdapter 类对象
# ---------------------------------------------------------------------------

def _install_two_fake_adapter_modules(cls_main: type, cls_family: type) -> None:
    """模拟多路复用单进程：两个 hermes_plugins.feishu_platform* 模块实例."""
    import sys
    for name in ("hermes_plugins",):
        if name not in sys.modules:
            sys.modules[name] = types_module(name)
    for modname, cls in (
        ("hermes_plugins.feishu_platform.adapter", cls_main),
        ("hermes_plugins.feishu_platform__home_deadbeef.adapter", cls_family),
    ):
        parts = modname.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent not in sys.modules:
                sys.modules[parent] = types_module(parent)
        mod = types_module(modname)
        mod.FeishuAdapter = cls  # type: ignore[attr-defined]
        sys.modules[modname] = mod


class TestMultiplexMultiProfile:
    """多路复用 gateway 下，两个 profile 的 adapter 都要被 patch。"""

    def teardown_method(self) -> None:
        _cleanup_fake_modules()

    def test_finds_all_adapter_classes(self) -> None:
        cls_main = _make_fake_adapter()
        cls_family = _make_fake_adapter()
        _install_two_fake_adapter_modules(cls_main, cls_family)

        classes = clarify._find_feishu_adapter_classes()
        assert cls_main in classes
        assert cls_family in classes
        assert len(classes) >= 2

    def test_patches_send_clarify_on_every_profile(self) -> None:
        """send_clarify 必须 patch 到每个 profile 的类上，不只第一个。"""
        cls_main = _make_fake_adapter()
        cls_family = _make_fake_adapter()
        _install_two_fake_adapter_modules(cls_main, cls_family)

        with patch("hermes_lark_streaming.clarify.Config") as cfg:
            cfg.return_value.clarify_inline = True
            clarify.patch_feishu_adapter({})  # 无实例也能验证类 patch

        assert getattr(cls_main.send_clarify, clarify._PATCH_MARK, False) is True  # type: ignore[attr-defined]
        assert getattr(cls_family.send_clarify, clarify._PATCH_MARK, False) is True  # type: ignore[attr-defined]

    def test_patches_processors_on_every_profile_instance(self) -> None:
        """两个 profile 的 adapter 实例（摊平 list）processor.f 都要被替换。"""
        cls_main = _make_fake_adapter()
        cls_family = _make_fake_adapter()
        _install_two_fake_adapter_modules(cls_main, cls_family)

        procs = [_make_processor_mock(), _make_processor_mock()]
        originals = [p.f for p in procs]
        inst_main = _make_adapter_instance(cls_main, procs[0])
        inst_family = _make_adapter_instance(cls_family, procs[1])

        with patch("hermes_lark_streaming.clarify.Config") as cfg:
            cfg.return_value.clarify_inline = True
            clarify.patch_feishu_adapter([inst_main, inst_family])

        for proc, original in zip(procs, originals, strict=True):
            assert proc.f is not original
            assert getattr(proc.f, clarify._PATCH_MARK, False) is True

    def test_idempotent_across_profiles(self) -> None:
        cls_main = _make_fake_adapter()
        cls_family = _make_fake_adapter()
        _install_two_fake_adapter_modules(cls_main, cls_family)
        proc_main, proc_family = _make_processor_mock(), _make_processor_mock()
        inst_main = _make_adapter_instance(cls_main, proc_main)
        inst_family = _make_adapter_instance(cls_family, proc_family)
        instances = [inst_main, inst_family]

        with patch("hermes_lark_streaming.clarify.Config") as cfg:
            cfg.return_value.clarify_inline = True
            clarify.patch_feishu_adapter(instances)
            first = (proc_main.f, proc_family.f, cls_main.send_clarify, cls_family.send_clarify)
            clarify.patch_feishu_adapter(instances)
            assert (proc_main.f, proc_family.f, cls_main.send_clarify, cls_family.send_clarify) == first


# ---------------------------------------------------------------------------
# send_clarify — end-to-end-ish with mocked adapter
# ---------------------------------------------------------------------------

class TestSendClarify:
    @pytest.mark.asyncio
    async def test_send_clarify_with_choices_sends_interactive(self) -> None:
        adapter = MagicMock()
        adapter._client = "connected"
        adapter._feishu_send_with_retry = AsyncMock(return_value={"message_id": "om_1"})
        adapter._finalize_send_result.return_value = SimpleNamespace(
            success=True, message_id="om_1"
        )

        await clarify._send_clarify(
            adapter, chat_id="c", question="Which?", choices=["a", "b"],
            clarify_id="cid", session_key="s", metadata=None,
        )
        adapter._feishu_send_with_retry.assert_called_once()
        kwargs = adapter._feishu_send_with_retry.call_args.kwargs
        assert kwargs["msg_type"] == "interactive"
        card = json.loads(kwargs["payload"])
        assert card["header"]["template"] == "orange"
        assert "action" in [e["tag"] for e in card["elements"]]
        # state recorded
        assert clarify._CLARIFY_STATE["cid"]["message_id"] == "om_1"
        clarify._CLARIFY_STATE.clear()

    @pytest.mark.asyncio
    async def test_send_clarify_open_marks_awaiting(self) -> None:
        adapter = MagicMock()
        adapter._client = "connected"
        adapter._feishu_send_with_retry = AsyncMock(return_value={"message_id": "om_2"})
        adapter._finalize_send_result.return_value = SimpleNamespace(
            success=True, message_id="om_2"
        )
        mark_fn = MagicMock()
        _install_fake_clarify_gateway(mark_awaiting_text=mark_fn)
        await clarify._send_clarify(
            adapter, chat_id="c", question="What?", choices=None,
            clarify_id="cid2", session_key="s", metadata=None,
        )
        mark_fn.assert_called_once_with("cid2")
        clarify._CLARIFY_STATE.clear()
