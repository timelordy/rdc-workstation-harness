"""Portable regression tests: no Windows session or physical input is used."""

import ast
import importlib.util
import io
import json
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "src" / "desktop_agent"
MODULE_SPEC = importlib.util.spec_from_file_location("uia_support", AGENT_DIR / "uia_support.py")
UIA = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(UIA)


class ActionError(ValueError):
    def __init__(self, message, hint=None, **details):
        super().__init__(message)
        self.hint = hint
        self.extra = details
        self.details = details


class WaitTimeout(Exception):
    pass


def agent_functions(names, **context):
    """Exercise actual action functions without importing Windows dependencies."""
    tree = ast.parse((AGENT_DIR / "agent.py").read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = dict(context, ActionError=ActionError, uia_support=UIA)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(AGENT_DIR / "agent.py"), "exec"), namespace)
    return namespace


def description(control, redact_text=False, use_name=False):
    return {"title": "[redacted]" if redact_text else "Example field", "use_name": use_name}


def control(**interfaces):
    return SimpleNamespace(element_info=SimpleNamespace(element=SimpleNamespace(CurrentIsPassword=False)), **interfaces)


class UiaWaitTests(unittest.TestCase):
    def test_wait_uses_lazy_specification_without_resolving_missing_wrapper(self):
        spec = Mock()
        spec.wrapper_object.side_effect = AssertionError("lookup happened before the control appeared")
        context = agent_functions(
            {"uia_wait"}, uia_spec=Mock(return_value=spec), UiaTimeoutError=WaitTimeout,
            find_uia=Mock(side_effect=AssertionError("must not resolve first")),
        )
        result = context["uia_wait"]({"window": {"title": "Starting"}, "state": "visible enabled", "timeout": 2})
        self.assertEqual({"ok": True, "state": "visible enabled", "exists": True}, result)
        spec.wait.assert_called_once_with("visible enabled", timeout=2, retry_interval=0.2)
        spec.wrapper_object.assert_not_called()
        context["find_uia"].assert_not_called()

    def test_absent_wait_does_not_try_to_inspect_a_destroyed_control(self):
        spec = Mock()
        for state in ("absent", "not_exists"):
            with self.subTest(state=state):
                result = UIA.wait_for_spec(spec, {"state": state, "timeout": 0})
                self.assertFalse(result["exists"])
        spec.wait_not.assert_called_with("exists", timeout=0, retry_interval=0.2)
        spec.wrapper_object.assert_not_called()

    def test_hidden_does_not_claim_the_control_was_destroyed(self):
        spec = Mock()
        result = UIA.wait_for_spec(spec, {"state": "hidden"})
        self.assertNotIn("exists", result)
        spec.wait_not.assert_called_once_with("visible", timeout=10, retry_interval=0.2)

    def test_bounded_wait_rejects_nonfinite_or_invalid_values_before_provider(self):
        for changes in (
            {"timeout": -1}, {"timeout": 121}, {"timeout": float("inf")}, {"timeout": float("nan")},
            {"timeout": True}, {"timeout": "5"}, {"retry_interval": 0}, {"retry_interval": 3},
            {"state": ""}, {"state": "absent enabled"}, {"state": ["exists"]},
        ):
            spec = Mock()
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                UIA.wait_for_spec(spec, changes)
            spec.wait.assert_not_called()
            spec.wait_not.assert_not_called()

    def test_timeout_is_an_action_error_with_recovery_hint(self):
        spec = Mock()
        spec.wait.side_effect = WaitTimeout()
        context = agent_functions({"uia_wait"}, uia_spec=lambda _: spec, UiaTimeoutError=WaitTimeout)
        with self.assertRaises(ActionError) as raised:
            context["uia_wait"]({"state": "ready", "timeout": 1})
        self.assertIn("requested state", str(raised.exception))
        self.assertEqual("ready", raised.exception.details["state"])


class UiaInspectionTests(unittest.TestCase):
    def test_patterns_report_values_and_selected_toggle_expand_states(self):
        get_text = Mock(return_value="abcdef")
        target = control(
            iface_value=SimpleNamespace(CurrentValue="abcdef", CurrentIsReadOnly=False),
            iface_toggle=SimpleNamespace(CurrentToggleState=1),
            iface_selection_item=SimpleNamespace(CurrentIsSelected=True),
            iface_expand_collapse=SimpleNamespace(CurrentExpandCollapseState=2),
            iface_range_value=SimpleNamespace(CurrentValue=3, CurrentMinimum=0, CurrentMaximum=10,
                                            CurrentSmallChange=1, CurrentLargeChange=2, CurrentIsReadOnly=False),
            iface_text=SimpleNamespace(DocumentRange=SimpleNamespace(GetText=get_text)),
        )
        result = UIA.inspect_control(target, description, max_text=5)
        patterns = result["patterns"]
        self.assertEqual("abcde", patterns["value"]["value"])
        self.assertTrue(patterns["value"]["value_truncated"])
        self.assertEqual("on", patterns["toggle"]["state_name"])
        self.assertTrue(patterns["selection_item"]["selected"])
        self.assertEqual("partially_expanded", patterns["expand_collapse"]["state_name"])
        self.assertEqual(3, patterns["range_value"]["value"])
        self.assertEqual("abcde", patterns["text"]["text"])
        self.assertTrue(patterns["text"]["text_truncated"])
        get_text.assert_called_once_with(6)
        self.assertTrue(result["control"]["use_name"])
        self.assertEqual(5, len(result["control"]["title"]))

    def test_password_never_reads_text_or_value_even_from_bad_provider(self):
        class PasswordValue:
            @property
            def CurrentValue(self):
                raise AssertionError("password value must not be read")

        get_text = Mock(side_effect=AssertionError("password document must not be read"))
        target = control(
            iface_value=PasswordValue(),
            iface_range_value=PasswordValue(),
            iface_text=SimpleNamespace(DocumentRange=SimpleNamespace(GetText=get_text)),
        )
        target.element_info.element.CurrentIsPassword = True
        metadata = Mock(side_effect=description)
        result = UIA.inspect_control(target, metadata)
        self.assertEqual("[redacted]", result["control"]["title"])
        for name in ("value", "range_value", "text"):
            self.assertTrue(result["patterns"][name]["redacted"])
            self.assertNotIn("value", result["patterns"][name])
        metadata.assert_called_once_with(target, redact_text=True, use_name=True)
        get_text.assert_not_called()

    def test_unknown_password_state_also_redacts_content(self):
        target = control(iface_value=SimpleNamespace(CurrentValue="do not expose"))
        target.element_info = None
        result = UIA.inspect_control(target, description)
        self.assertIsNone(result["password"])
        self.assertEqual("password_state_unavailable", result["patterns"]["value"]["reason"])
        self.assertNotIn("do not expose", str(result))

    def test_one_unavailable_property_does_not_hide_other_patterns(self):
        class UnavailableValue:
            CurrentIsReadOnly = True

            @property
            def CurrentValue(self):
                raise RuntimeError("provider error containing private input")

        target = control(iface_value=UnavailableValue(), iface_toggle=SimpleNamespace(CurrentToggleState=0))
        patterns = UIA.inspect_control(target, description)["patterns"]
        self.assertTrue(patterns["value"]["supported"])
        self.assertTrue(patterns["value"]["read_only"])
        self.assertEqual("RuntimeError", patterns["value"]["errors"]["value"]["type"])
        self.assertEqual("off", patterns["toggle"]["state_name"])
        self.assertFalse(patterns["text"]["supported"])
        self.assertNotIn("private input", str(patterns))

    def test_text_can_be_omitted_without_fetching_provider_content(self):
        get_text = Mock(side_effect=AssertionError("max_text=0 must omit retrieval"))
        target = control(iface_text=SimpleNamespace(DocumentRange=SimpleNamespace(GetText=get_text)))
        self.assertTrue(UIA.inspect_control(target, description, 0)["patterns"]["text"]["text_omitted"])
        get_text.assert_not_called()

    def test_inspect_rejects_unbounded_text_requests(self):
        for value in (-1, 20001, 2.5, True, "4000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                UIA.inspect_control(control(), description, value)


class UiaPhysicalGateTests(unittest.TestCase):
    def _context(self):
        target = Mock()
        context = agent_functions(
            {"uia_call", "require_physical"}, PHYSICAL_INPUT_DEFAULT=False,
            find_uia=Mock(return_value=target), window_info=lambda _: {},
            core_ext=SimpleNamespace(json_safe=lambda value: value),
        )
        return context, target

    def test_generic_wrapper_escape_cannot_bypass_physical_opt_in(self):
        for method in ("click_input", "type_keys", "TypeKeys", "ClickInput", "drag_mouse_input", "wheel_mouse_input"):
            context, target = self._context()
            with self.subTest(method=method), self.assertRaises(PermissionError):
                context["uia_call"]({"method": method})
            context["find_uia"].assert_not_called()
            getattr(target, method).assert_not_called()

    def test_explicit_input_and_semantic_selection_still_work(self):
        for request in ({"method": "click_input", "allow_physical": True}, {"method": "select"}):
            context, target = self._context()
            context["uia_call"](request)
            getattr(target, request["method"]).assert_called_once()

    def test_string_false_is_not_physical_authorization(self):
        context, target = self._context()
        for flag in ("false", "true", 1, None):
            with self.subTest(flag=flag), self.assertRaises(PermissionError):
                context["uia_call"]({"method": "click_input", "allow_physical": flag})
        target.click_input.assert_not_called()


class BrowserErrorForwardingTests(unittest.TestCase):
    def test_ambiguity_and_queue_metadata_survive_proxy_without_overwriting_error(self):
        details = {
            "error": "ambiguous locator", "code": "AMBIGUOUS_LOCATOR", "count": 3,
            "action_started": False, "hint": "choose a unique locator",
            "type": "do not overwrite", "action": "do not overwrite",
        }
        error = urllib.error.HTTPError(
            "http://127.0.0.1/action", 409, "Conflict", {}, io.BytesIO(json.dumps(details).encode()),
        )
        request = SimpleNamespace(Request=Mock(), urlopen=Mock(side_effect=error))
        token_path = Mock()
        token_path.exists.return_value = False
        context = agent_functions(
            {"forward_browser"}, json=json, urllib=SimpleNamespace(request=request, error=urllib.error),
            BROWSER_TOKEN_PATH=token_path, BROWSER_URL="http://127.0.0.1/action",
        )
        with self.assertRaises(ActionError) as raised:
            context["forward_browser"]({"action": "click"})
        exception = raised.exception
        self.assertEqual("browser: ambiguous locator", str(exception))
        self.assertEqual("AMBIGUOUS_LOCATOR", exception.extra["code"])
        self.assertEqual(3, exception.extra["count"])
        self.assertFalse(exception.extra["action_started"])
        self.assertEqual("choose a unique locator", exception.extra["bridge_hint"])
        self.assertIn("describe", exception.hint)
        self.assertNotIn("type", exception.extra)
        self.assertNotIn("action", exception.extra)


if __name__ == "__main__":
    unittest.main()
