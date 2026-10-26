"""Behaviour tests against a real Desktop Agent process (Windows + requirements.txt only)."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "src" / "desktop_agent" / "agent.py"


def _deps_available():
    if os.name != "nt":
        return False
    return all(importlib.util.find_spec(m) for m in ("pywinauto", "win32gui", "mss", "pyautogui", "psutil"))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(_deps_available(), "needs Windows and desktop_agent requirements")
class AgentRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.port = _free_port()
        env = dict(
            os.environ,
            DESKTOP_AGENT_PORT=str(cls.port),
            PW_BRIDGE_PORT=str(_free_port()),  # nothing listens: browser forwards must fail cleanly
            RDC_HARNESS_TOKEN_DIR=str(tmp / "tokens"),
            DESKTOP_AGENT_ARTIFACT_DIR=str(tmp / "artifacts"),
            RDC_HARNESS_AUDIT_LOG=str(tmp / "audit.ndjson"),
            RDC_HARNESS_STATE_DIR=str(tmp / "task-state"),
            RDC_HARNESS_SKILL_DIRS="",
            RDC_HARNESS_TRUSTED_WORKSPACES="[]",
        )
        cls.proc = subprocess.Popen(
            [sys.executable, str(AGENT)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        cls.token_path = tmp / "tokens" / "desktop.token"
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/health", timeout=1)
                break
            except Exception:
                if cls.proc.poll() is not None:
                    raise RuntimeError(cls.proc.stderr.read().decode(errors="replace"))
                time.sleep(0.3)
        else:
            raise RuntimeError("agent did not start")
        cls.token = cls.token_path.read_text(encoding="utf-8").strip()

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.proc.stdout.close()
        cls.proc.stderr.close()
        cls.tmp.cleanup()

    def call(self, payload, token=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/action",
            data=json.dumps(payload).encode(),
            headers={"X-Desktop-Agent-Token": token or self.token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_universal_tools_are_discoverable_and_launcher_dry_run_is_read_only(self):
        status, body = self.call({"action": "capabilities"})
        self.assertEqual(200, status, body)
        names = {item["name"] for group in body["result"]["actions"].values() for item in group}
        self.assertTrue({"app_catalog", "app_open", "app_observe", "app_act", "app_wait"}.issubset(names))
        status, body = self.call({"action": "app_open", "executable": sys.executable, "dry_run": True})
        self.assertEqual(200, status, body)
        self.assertTrue(body["result"]["dry_run"])
        self.assertFalse(body["result"]["shell"])
        self.assertNotIn("pid", body["result"])
        status, body = self.call({"action": "app_open", "executable": sys.executable, "dry_run": "true"})
        self.assertEqual(400, status, body)
        self.assertFalse(body["action_started"])

    def test_rejects_bad_token(self):
        status, body = self.call({"action": "capabilities"}, token="x")
        self.assertEqual(403, status)

    def test_capabilities_cover_agent_actions(self):
        status, body = self.call({"action": "capabilities"})
        self.assertEqual(200, status)
        names = {i["name"] for g in body["result"]["actions"].values() for i in g}
        for expected in ("uia_click", "windows", "browser", "probe_target", "describe"):
            self.assertIn(expected, names)
        self.assertIn("autocad", body["result"]["adapters"])

    def test_describe_action_and_adapter(self):
        _, body = self.call({"action": "describe", "name": "uia_click"})
        self.assertEqual("write", body["result"]["effect"])
        _, body = self.call({"action": "describe", "adapter": "autocad"})
        self.assertTrue(body["result"]["manifest"])
        self.assertIn("status", body["result"]["actions"])

    def test_skill_task_workflow_runs_through_authenticated_router(self):
        status, body = self.call({"action": "skill_list"})
        self.assertEqual(200, status, body)
        skills = body["result"]["skills"]
        skill = next(item for item in skills if item["name"] == "verify-export")
        status, body = self.call({"action": "skill_read", "skill_id": skill["id"]})
        self.assertEqual(200, status, body)
        self.assertIn("content", body["result"])
        status, body = self.call({"action": "task_prepare", "goal": "Verify a test export",
                                  "workspace": self.tmp.name, "requested_skills": [skill["id"]]})
        self.assertEqual(200, status, body)
        task_id = body["result"]["task_id"]
        self.assertTrue(body["result"]["checks"])
        status, body = self.call({"action": "task_finish", "task_id": task_id})
        self.assertEqual(200, status, body)
        self.assertFalse(body["result"]["completed"])
        self.assertTrue(body["result"]["coverage"]["blockers"])
        status, body = self.call({"action": "task_resume", "task_id": task_id})
        self.assertEqual(200, status, body)
        self.assertTrue(body["result"]["skills"][0]["content"])
        self.assertFalse(body["result"]["completed"])

    def test_invalid_skill_task_inputs_are_client_errors(self):
        for request in ({"action": "skill_read", "skill_id": "../outside"},
                        {"action": "task_status", "task_id": "invalid"},
                        {"action": "task_prepare", "goal": "   "}):
            with self.subTest(request=request):
                status, body = self.call(request)
                self.assertEqual(400, status, body)
                self.assertIn("code", body)

    def test_unknown_action_suggests(self):
        status, body = self.call({"action": "uia_clik"})
        self.assertEqual(400, status)
        self.assertIn("uia_click", body["did_you_mean"])
        self.assertIn("hint", body)

    def test_missing_param_is_explained(self):
        status, body = self.call({"action": "probe_target"})
        self.assertEqual(400, status)
        self.assertIn("pid", body["error"])
        self.assertIn("pid", body["params"])

    def test_physical_requires_opt_in(self):
        status, body = self.call({"action": "click", "x": 1, "y": 1})
        self.assertEqual(403, status)

    def test_probe_target_lists_windows(self):
        _, body = self.call({"action": "probe_target", "pid": os.getpid(), "module_limit": 1})
        self.assertIn("windows", body["result"])
        # Regression: a missing win32process import used to be swallowed silently.
        _, body = self.call({"action": "processes", "name_re": "^explorer\\.exe$", "limit": 1})
        if body["result"]["items"]:
            pid = body["result"]["items"][0]["pid"]
            _, probe = self.call({"action": "probe_target", "pid": pid, "module_limit": 1})
            self.assertTrue(probe["result"]["windows"])

    def test_file_handoff_refuses_tokens(self):
        status, body = self.call({"action": "file_handoff", "path": str(self.token_path)})
        self.assertEqual(403, status)
        self.assertIn("credentials", body["error"])

    def test_browser_commit_gate(self):
        payload = {"action": "click", "role": "button", "name": "Send message"}
        status, body = self.call({"action": "browser", "payload": payload})
        self.assertEqual(403, status)
        self.assertIn("commit=true", body["error"])

    def test_unknown_browser_action(self):
        status, body = self.call({"action": "browser", "payload": {"action": "snapshott"}})
        self.assertEqual(400, status)
        self.assertIn("snapshot", body["did_you_mean"])

    def _decode(self, result):
        import base64
        import io
        from PIL import Image
        self.assertEqual("image/jpeg", result["image"]["mime_type"])
        return Image.open(io.BytesIO(base64.b64decode(result["image"]["data"])))

    def test_look_screen_is_downscaled_image(self):
        status, body = self.call({"action": "look", "max_side": 400})
        self.assertEqual(200, status, body)
        image = self._decode(body["result"])
        self.assertLessEqual(max(image.size), 400)
        self.assertEqual([image.width, image.height],
                         [body["result"]["image_size"]["width"], body["result"]["image_size"]["height"]])
        self.assertLessEqual(body["result"]["scale"], 1)

    def test_look_region_keeps_coordinates(self):
        region = {"left": 0, "top": 0, "width": 120, "height": 80}
        _, body = self.call({"action": "look", "region": region})
        result = body["result"]
        self.assertEqual({"left": 0, "top": 0, "right": 120, "bottom": 80}, result["screen_rect"])
        self.assertEqual(1, result["scale"])
        self.assertEqual((120, 80), self._decode(result).size)

    def test_look_save_returns_path_not_base64(self):
        _, body = self.call({"action": "look", "save": True, "max_side": 300})
        result = body["result"]
        self.assertNotIn("image", result)
        path = Path(result["path"])
        self.assertEqual("chat", path.parent.name)
        self.assertEqual(result["bytes"], path.stat().st_size)
        self.assertEqual(b"\xff\xd8", path.read_bytes()[:2])  # JPEG

    def test_pc_agent_cli_look(self):
        env = dict(os.environ, DESKTOP_AGENT_PORT=str(self.port),
                   RDC_HARNESS_TOKEN_DIR=str(self.token_path.parent))
        cli = ROOT / "tools" / "pc_agent.py"
        run = subprocess.run([sys.executable, str(cli), "look", "--max-side", "300"],
                             env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertLess(len(run.stdout), 3000)  # no base64 in the terminal
        self.assertTrue(run.stdout.isascii())  # survives any console code page
        out = json.loads(run.stdout)
        self.assertTrue(out["result"]["path"].isascii() or not str(self.tmp.name).isascii())
        self.assertTrue(Path(out["result"]["path"]).exists())
        self.assertIn("read_file", out["next"])

        run = subprocess.run([sys.executable, str(cli), "describe", "uia_clik"],
                             env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(1, run.returncode)
        self.assertEqual(400, json.loads(run.stdout)["http_status"])

    def test_missing_window_is_explained(self):
        status, body = self.call({"action": "look", "window": {"title_re": "no-such-window-xyz"}})
        self.assertEqual(400, status)
        self.assertIn("windows", body["hint"])

    def test_look_window_by_hwnd(self):
        _, active = self.call({"action": "windows", "limit": 50})
        candidates = [w for w in active["result"]["items"]
                      if w.get("handle") and w.get("visible") and w["rect"]["width"] > 50]
        if not candidates:
            self.skipTest("no visible window in this session")
        hwnd = candidates[0]["handle"]
        status, body = self.call({"action": "look", "hwnd": hwnd, "max_side": 300})
        self.assertEqual(200, status, body)
        self.assertIn(body["result"]["source"], ("window", "screen_region"))
        self.assertEqual(hwnd, body["result"]["hwnd"])
        self._decode(body["result"])

    def test_com_handles(self):
        _, created = self.call({"action": "com_create", "progid": "Scripting.Dictionary"})
        handle = created["result"]["handle"]
        status, body = self.call({"action": "com_set", "handle": handle})
        self.assertEqual(400, status)  # used to crash with AttributeError on None
        _, listed = self.call({"action": "com_list"})
        self.assertIn(handle, [i["handle"] for i in listed["result"]["items"]])
        _, released = self.call({"action": "com_release", "handle": handle})
        self.assertTrue(released["result"]["released"])

    def test_com_object_can_be_used_across_independent_http_requests(self):
        status, created = self.call({"action": "com_create", "progid": "Scripting.Dictionary"})
        self.assertEqual(200, status, created)
        handle = created["result"]["handle"]
        try:
            status, called = self.call({
                "action": "com_call", "handle": handle, "path": "Add", "args": ["synthetic-key", 7],
            })
            self.assertEqual(200, status, called)
            status, count = self.call({"action": "com_get", "handle": handle, "path": "Count"})
            self.assertEqual(200, status, count)
            self.assertEqual(1, count["result"]["value"])
        finally:
            self.call({"action": "com_release", "handle": handle})

    def test_uia_wait_for_absent_synthetic_window(self):
        status, result = self.call({
            "action": "uia_wait", "window": {"title": "rdc-test-absent-window-93de1d4a"},
            "state": "absent", "timeout": 0.1,
        })
        self.assertEqual(200, status, result)
        self.assertFalse(result["result"]["exists"])

    def test_uia_input_escape_requires_opt_in_before_window_resolution(self):
        status, result = self.call({
            "action": "uia_call", "window": {"title": "rdc-test-absent-window-93de1d4a"},
            "method": "type_keys", "args": ["synthetic text"],
        })
        self.assertEqual(403, status, result)
        self.assertIn("disabled by default", result["error"])

    def test_adapter_uia_callback_stays_on_broker_and_raw_wrappers_are_rejected(self):
        name = "rdc_runtime_uia_context_test"
        code = '''DESCRIPTION = "Synthetic adapter context test"
ACTIONS = {"safe": {"effect": "read"}, "raw": {"effect": "read"}}
def handle(payload, context):
    if payload["action"] == "raw":
        return context["find_uia"]({"title": "rdc-test-absent-window-93de1d4a"})
    return context["uia_action"]({"action": "uia_wait", "window": {"title": "rdc-test-absent-window-93de1d4a"}, "state": "absent", "timeout": 0.1})
'''
        path = AGENT.parent / "adapters" / (name + ".py")
        try:
            status, installed = self.call({"action": "adapter_install", "name": name, "code": code})
            self.assertEqual(200, status, installed)
            status, result = self.call({"action": "adapter_call", "name": name, "payload": {"action": "safe"}})
            self.assertEqual(200, status, result)
            self.assertFalse(result["result"]["exists"])
            status, result = self.call({"action": "adapter_call", "name": name, "payload": {"action": "raw"}})
            self.assertEqual(400, status, result)
            self.assertIn("uia_action", result["hint"])
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
