"""Unit contracts for application-independent state/identity/operation control."""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

AGENT_DIR = Path(__file__).resolve().parents[1] / "src" / "desktop_agent"
sys.path.insert(0, str(AGENT_DIR))
AVAILABLE = os.name == "nt" and all(importlib.util.find_spec(n) for n in ("pywinauto", "win32gui", "psutil"))
if AVAILABLE:
    import app_control


class FakeControl:
    def __init__(self):
        self.element_info = SimpleNamespace(runtime_id=(12, 34))
        self.state = {"enabled": True, "visible": True, "value": "before", "checked": False, "selected": False, "expanded": False}
        self.calls = []
        self.operations = ["set_value", "set_checked", "select", "expand", "collapse", "invoke", "scroll_into_view"]
        self.iface_value = SimpleNamespace(SetValue=lambda value: self.set("value", value))
        self.iface_range_value = self.iface_value
        self.iface_toggle = SimpleNamespace(Toggle=lambda: self.set("checked", not self.state["checked"]))
        self.iface_selection_item = SimpleNamespace(Select=lambda: self.set("selected", True))
        self.iface_expand_collapse = SimpleNamespace(Expand=lambda: self.set("expanded", True), Collapse=lambda: self.set("expanded", False))
        self.iface_invoke = SimpleNamespace(Invoke=lambda: self.calls.append("invoke"))
        self.iface_scroll_item = SimpleNamespace(ScrollIntoView=lambda: self.calls.append("scroll"))
        self.handle = 100

    def set(self, key, value):
        self.calls.append((key, value))
        self.state[key] = value

    def children(self):
        return []

    def class_name(self):
        return "Edit"

    def control_id(self):
        return 11


def description(ctrl, backend, max_text):
    return {"title": "random application control", "state": dict(ctrl.state), "operations": list(ctrl.operations),
            "patterns": {"value": {"supported": True, "read_only": False}, "range_value": {"supported": True, "read_only": False}}}


@unittest.skipUnless(AVAILABLE, "requires Windows desktop dependencies")
class UniversalControlTests(unittest.TestCase):
    def setUp(self):
        self.ctrl = FakeControl()
        self.now = 100.0
        self.identity = {"hwnd": 100, "pid": 50, "created_at": 1.0}
        self.backends = []

        def desktop(backend):
            self.backends.append(backend)
            return SimpleNamespace(window=lambda **kw: SimpleNamespace(wrapper_object=lambda: self.ctrl))

        self.controller = app_control.AppController(desktop_factory=desktop, identity=lambda hwnd: dict(self.identity), clock=lambda: self.now)
        self.mock = patch.object(app_control, "describe_control", side_effect=description)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.ref = self.controller.observe({"hwnd": 100})["items"][0]["ref"]

    def act(self, **kwargs):
        return self.controller.act({"ref": self.ref, "operation": "set_value", "value": "after", "request_id": "attempt-1", **kwargs})

    def test_observation_selects_capabilities_not_application_names(self):
        self.assertEqual(["uia"], self.backends)
        self.assertTrue(self.ref.startswith("control-"))

    def test_read_only_negotiation_falls_back_to_win32(self):
        def read(ctrl, backend, limit):
            info = description(ctrl, backend, limit)
            if backend == "uia":
                info["operations"] = []
            return info
        with patch.object(app_control, "describe_control", side_effect=read):
            result = self.controller.observe({"hwnd": 100})
        self.assertEqual("win32", result["backend"])
        self.assertEqual(["uia", "uia", "win32"], self.backends)

    def test_explicit_backend_does_not_fallback(self):
        result = self.controller.observe({"hwnd": 100, "backend": "win32"})
        self.assertEqual("win32", result["backend"])
        self.assertEqual(["uia", "win32"], self.backends)

    def test_verified_set_value_and_wait(self):
        result = self.act()
        self.assertTrue(result["verified"])
        self.assertEqual("after", result["after"]["value"])
        self.assertTrue(self.controller.wait({"ref": self.ref, "expected": {"value": "after"}, "timeout": 0})["matched"])

    def test_idempotent_desired_checkbox_state_does_not_toggle_twice(self):
        result = self.act(operation="set_checked", checked=False)
        self.assertEqual("already_satisfied", result["outcome"])
        self.assertEqual([], self.ctrl.calls)
        result = self.act(operation="set_checked", checked=True, request_id="attempt-2")
        self.assertTrue(result["verified"])
        result = self.act(operation="set_checked", checked=True, request_id="attempt-3")
        self.assertEqual("already_satisfied", result["outcome"])
        self.assertEqual([("checked", True)], self.ctrl.calls)

    def test_repeated_id_returns_receipt_not_second_write(self):
        first = self.act()
        self.ctrl.state["value"] = "user changed it"
        second = self.act()
        self.assertTrue(second["replayed"])
        self.assertEqual(first["after"], second["after"])
        self.assertEqual([("value", "after")], self.ctrl.calls)
        self.assertEqual("user changed it", self.ctrl.state["value"])

    def test_id_conflict_is_rejected(self):
        self.act()
        with self.assertRaisesRegex(app_control.ActionError, "different arguments"):
            self.act(value="different")
        self.assertEqual(1, len(self.ctrl.calls))

    def test_provider_failure_has_uncertain_outcome_and_no_fallback(self):
        def bad(value):
            self.ctrl.set("value", value)
            raise RuntimeError("provider failure after writing")
        self.ctrl.iface_value.SetValue = bad
        result = self.act()
        self.assertEqual("unknown", result["outcome"])
        self.assertIsNone(result["verified"])
        self.assertTrue(self.act()["replayed"])
        self.assertEqual(1, len(self.ctrl.calls))
        self.assertEqual(["uia"], self.backends)

    def test_failed_precondition_prevents_write(self):
        with self.assertRaisesRegex(app_control.ActionError, "precondition"):
            self.act(expected={"value": "not-current"})
        self.assertEqual([], self.ctrl.calls)

    def test_missing_or_truncated_state_never_matches(self):
        self.assertFalse(self.controller._matches({}, {"checked": False}))
        self.assertFalse(self.controller._matches({"value": "abc", "value_truncated": True}, {"value": "abc"}))

    def test_dry_run_does_not_write_or_consume_attempt(self):
        result = self.act(dry_run=True)
        self.assertFalse(result["action_started"])
        self.assertEqual([], self.ctrl.calls)
        self.assertEqual({}, self.controller.receipts)
        self.assertTrue(self.act()["verified"])

    def test_reused_process_or_window_is_stale(self):
        self.identity["created_at"] = 2.0
        with self.assertRaisesRegex(app_control.ActionError, "identity changed"):
            self.act()
        self.assertEqual([], self.ctrl.calls)

    def test_replaced_uia_element_is_stale(self):
        self.ctrl.element_info.runtime_id = (12, 35)
        with self.assertRaisesRegex(app_control.ActionError, "identity changed"):
            self.act()

    def test_expired_ref_never_retargets(self):
        self.now += 121
        with self.assertRaisesRegex(app_control.ActionError, "expired"):
            self.act()

    def test_invoke_requires_boolean_commit(self):
        for flag in (None, False, "true", 1):
            with self.subTest(flag=flag), self.assertRaises(PermissionError):
                self.act(operation="invoke", commit=flag)
        result = self.act(operation="invoke", commit=True)
        self.assertEqual(["invoke"], self.ctrl.calls)
        self.assertIsNone(result["verified"])

    def test_unsupported_action_and_disabled_control_never_execute(self):
        with self.assertRaises(app_control.ActionError):
            self.act(operation="click_input")
        self.ctrl.state["enabled"] = False
        with self.assertRaises(app_control.ActionError):
            self.act()
        self.assertEqual([], self.ctrl.calls)

    def test_invalid_values_are_rejected_before_execution(self):
        for value in (True, float("nan"), float("inf"), ["text"], "x" * 4001, "a\x00b"):
            with self.subTest(value=repr(value)[:40]), self.assertRaises(app_control.ActionError):
                self.act(value=value)
        with self.assertRaises(app_control.ActionError):
            self.act(operation="set_checked", checked="false")
        self.assertEqual([], self.ctrl.calls)

    def test_wait_does_not_write_or_retry(self):
        result = self.controller.wait({"ref": self.ref, "expected": {"value": "other"}, "timeout": 0})
        self.assertFalse(result["matched"])
        self.assertTrue(result["timed_out"])
        self.assertEqual([], self.ctrl.calls)

    def test_bounds_are_strict(self):
        for value in (-1, 31, float("nan"), True, "1"):
            with self.assertRaises(app_control.ActionError):
                app_control.bounded_time(value)
        for value in (0, 301, True, 1.5):
            with self.assertRaises(app_control.ActionError):
                self.controller.observe({"hwnd": 100, "limit": value})

    def test_invalid_expected_contract(self):
        for expected in ({}, {"anything": True}, {"checked": "false"}, {"value": float("nan")}, None):
            with self.assertRaises(app_control.ActionError):
                self.controller.wait({"ref": self.ref, "expected": expected, "timeout": 0})

    def test_relative_launcher_and_shell_scripts_rejected(self):
        for executable in ("program.exe", "script.cmd", "https://example.test/app", ""):
            with self.assertRaises(app_control.ActionError):
                self.controller.open({"executable": executable})

    def test_non_boolean_dry_run_is_rejected_on_every_entrypoint(self):
        for flag in ("true", "false", 1, 0, None):
            with self.subTest(flag=flag):
                with self.assertRaises(app_control.ActionError):
                    self.act(dry_run=flag)
                with self.assertRaises(app_control.ActionError):
                    self.controller.open({"executable": sys.executable, "dry_run": flag})
        self.assertEqual([], self.ctrl.calls)

    def test_dry_run_checks_the_actual_writable_pattern(self):
        def readonly(ctrl, backend, limit):
            info = description(ctrl, backend, limit)
            info["patterns"]["value"]["read_only"] = True
            return info
        with patch.object(app_control, "describe_control", side_effect=readonly):
            with self.assertRaises(app_control.ActionError):
                self.act(dry_run=True)
        self.assertEqual([], self.ctrl.calls)

    def test_full_receipt_cache_rejects_new_writes_without_evicting_receipts(self):
        for index in range(256):
            self.act(request_id="attempt-" + str(index))
        with self.assertRaisesRegex(app_control.ActionError, "cache is full"):
            self.act(request_id="overflow")
        self.assertTrue(self.act(request_id="attempt-0")["replayed"])
        self.assertEqual(1, len(self.ctrl.calls))
        self.assertEqual(256, len(self.controller.receipts))

    def test_slow_provider_does_not_expire_its_unfinished_receipt(self):
        def slow(value):
            self.now += 121
            self.ctrl.set("value", value)
        self.ctrl.iface_value.SetValue = slow
        result = self.act()
        self.assertTrue(result["action_started"])
        self.assertIsNone(result["verified"])  # control ref expired while provider ran
        self.assertTrue(self.act()["replayed"])
        self.assertEqual(1, len(self.ctrl.calls))

    def test_open_dry_run_launches_nothing(self):
        with patch.object(app_control.subprocess, "Popen") as popen:
            result = self.controller.open({"executable": sys.executable, "args": ["-c", "print('demo')"], "dry_run": True})
        popen.assert_not_called()
        self.assertFalse(result["shell"])


if __name__ == "__main__":
    unittest.main()
