"""Native integration using only the disposable desktop_fixture.py window."""
import importlib.util
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "desktop_agent"))
AVAILABLE = os.name == "nt" and all(importlib.util.find_spec(n) for n in ("pywinauto", "win32gui", "psutil"))
if AVAILABLE:
    import app_control
    import win32con
    import win32gui


@unittest.skipUnless(AVAILABLE, "requires Windows desktop dependencies")
class NativeAppControlTests(unittest.TestCase):
    def test_disposable_window_uia_and_win32(self):
        fixture = ROOT / "tests" / "desktop_fixture.py"
        controller = app_control.AppController()
        # This handle belongs to our child process, never to a discovered user app.
        with subprocess.Popen([sys.executable, str(fixture)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE) as process:
            hwnd = None
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    windows = [w for w in app_control.running_windows("RDC disposable semantic fixture")["items"]
                               if w["pid"] == process.pid]
                    if windows:
                        hwnd = windows[0]["hwnd"]
                        break
                    if process.poll() is not None:
                        self.fail(process.stderr.read().decode(errors="replace"))
                    time.sleep(0.05)
                self.assertIsNotNone(hwnd)
                for index, backend in enumerate(("uia", "win32"), start=1):
                    with self.subTest(backend=backend):
                        observed = controller.observe({"hwnd": hwnd, "pid": process.pid, "backend": backend})
                        self.assertNotIn("fixture-private-value", str(observed))
                        edit = next(item for item in observed["items"] if "set_value" in item["operations"])
                        check = next(item for item in observed["items"] if "set_checked" in item["operations"])
                        button = next(item for item in observed["items"] if item.get("title") == "Run fixture")
                        request = {"ref": edit["ref"], "operation": "set_value", "value": "written-" + backend,
                                   "request_id": "edit-" + backend}
                        written = controller.act(request)
                        self.assertTrue(written["verified"], written)
                        self.assertTrue(controller.act(request)["replayed"])
                        checked = controller.act({"ref": check["ref"], "operation": "set_checked", "checked": True,
                                                  "request_id": "check-" + backend})
                        self.assertTrue(checked["verified"], checked)
                        invoke = {"ref": button["ref"], "operation": "invoke", "request_id": "invoke-" + backend}
                        with self.assertRaises(PermissionError):
                            controller.act(invoke)
                        invoke["commit"] = True
                        self.assertTrue(controller.act(invoke)["action_started"])
                        self.assertTrue(controller.act(invoke)["replayed"])
                        matched = controller.wait({"ref": edit["ref"], "expected": {"value": "invoked-" + str(index)}, "timeout": 5})
                        self.assertTrue(matched["matched"], matched)
            finally:
                if hwnd is not None and win32gui.IsWindow(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                process.wait(timeout=10)
            if hwnd is not None:
                with self.assertRaises(app_control.ActionError):
                    controller.observe({"hwnd": hwnd, "pid": process.pid})


if __name__ == "__main__":
    unittest.main()
