import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pc_agent", ROOT / "tools" / "pc_agent.py")
PC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PC)


class PcAgentParseTests(unittest.TestCase):
    def test_look_flags(self):
        payload = PC.parse(["look", "--window", "AutoCAD", "--max-side", "900"])
        self.assertEqual(
            {"action": "look", "save": True, "window": {"title_re": "AutoCAD"}, "max_side": 900}, payload
        )

    def test_look_region(self):
        payload = PC.parse(["look", "--region", "10,20,300,200"])
        self.assertEqual({"left": 10, "top": 20, "width": 300, "height": 200}, payload["region"])

    def test_bare_action_and_describe(self):
        self.assertEqual({"action": "capabilities"}, PC.parse(["capabilities"]))
        self.assertEqual({"action": "describe", "name": "uia_click"}, PC.parse(["describe", "uia_click"]))
        self.assertEqual({"action": "describe", "browser": "attach"}, PC.parse(["describe", "--browser", "attach"]))
        self.assertEqual({"action": "describe", "adapter": "autocad"}, PC.parse(["describe", "--adapter", "autocad"]))

    def test_json_argument(self):
        self.assertEqual({"action": "windows", "limit": 3}, PC.parse(['{"action": "windows",', '"limit": 3}']))

    def test_ascii_path_keeps_ascii_and_resolves_to_same_file(self):
        import os
        import tempfile
        self.assertEqual(r"C:\plain\x.jpg", PC.ascii_path(r"C:\plain\x.jpg"))
        if os.name != "nt":
            return
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Пользователь" / "look.jpg"
            target.parent.mkdir()
            target.write_bytes(b"x")
            short = PC.ascii_path(str(target))
            self.assertEqual(target.resolve(), Path(short).resolve())
            self.assertTrue(short.endswith("look.jpg"), short)

    def test_look_share_flag(self):
        self.assertTrue(PC.parse(["look", "--share"])["_share"])
        self.assertNotIn("_share", PC.parse(["look"]))

    def test_base64_never_reaches_terminal(self):
        blob = "A" * 50000
        cleaned = PC.strip_images({"ok": True, "result": {"image": {"data": blob}, "path": "x"}})
        self.assertNotIn(blob, str(cleaned))
        self.assertEqual("x", cleaned["result"]["path"])


class SkillTaskCommandTests(unittest.TestCase):
    def test_skill_discovery_and_resource_loading_preserve_scope_and_hash(self):
        self.assertEqual({"action": "skill_list"}, PC.parse(["skills", "list"]))
        workspace = r"C:\Work\Проект"
        self.assertEqual({"action": "skill_list", "workspace": workspace},
                         PC.parse(["skills", "list", "--workspace", workspace]))
        expected = {"action": "skill_read", "skill_id": "builtin:verify-export", "workspace": workspace,
                    "resource": "references/checks.md", "expected_hash": "a" * 64}
        self.assertEqual(expected, PC.parse(["skills", "read", "builtin:verify-export", "--workspace", workspace,
                                            "--resource", "references/checks.md", "--expected-hash", "a" * 64]))

    def test_prepare_reads_bom_json_file_and_keeps_expected_output(self):
        payload = {"goal": "Проверить таблицу", "workspace": r"C:\Work\Проект",
                   "requested_skills": ["builtin:validate-table"],
                   "check_inputs": {"builtin:validate-table/schema": {"required_columns": ["id", "name"], "min_rows": 2}}}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "task.json"
            source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")
            self.assertEqual({**payload, "action": "task_prepare"}, PC.parse(["task", "prepare", "@" + str(source)]))

    def test_resolve_and_manual_check_accept_stdin_without_shell_execution(self):
        with patch.object(PC.sys, "stdin", io.StringIO('{"goal":"Inspect report", "requested_skills":[]}')):
            self.assertEqual({"action": "skill_resolve", "goal": "Inspect report", "requested_skills": []},
                             PC.parse(["skills", "resolve", "-"]))
        check = {"task_id": "task-1", "check_id": "builtin:fill-form/values", "status": "passed",
                 "evidence": "Observed value contains literal $(not-a-command) and `text`."}
        self.assertEqual({**check, "action": "task_check"}, PC.parse(["task", "check", json.dumps(check)]))

    def test_artifact_check_uses_path_only_and_state_commands_keep_task_id(self):
        check = {"task_id": "task-1", "check_id": "builtin:verify-export/output", "path": "result.csv"}
        self.assertEqual({**check, "action": "task_check"}, PC.parse(["task", "check", json.dumps(check)]))
        for command in ("status", "resume", "finish"):
            with self.subTest(command=command):
                self.assertEqual({"action": "task_" + command, "task_id": "task-1"},
                                 PC.parse(["task", command, "task-1"]))

    def test_named_commands_cannot_override_action_or_set_verification(self):
        invalid = [
            ["task", "prepare", '{"action":"exec","goal":"test"}'],
            ["skills", "resolve", '{"action":"task_prepare","goal":"test"}'],
            ["task", "check", '{"task_id":"task-1","check_id":"x","verified":true}'],
            ["task", "check", '{"task_id":"task-1","check_id":"x","required_columns":["new"]}'],
            ["task", "check", '{"task_id":"task-1","check_id":"x","expectations":{"min_rows":0}}'],
            ["task", "prepare", '{"goal":"test","trust_workspace":true}'],
        ]
        for argv in invalid:
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                PC.parse(argv)

    def test_named_commands_reject_non_object_malformed_or_unbounded_json(self):
        for source in ("[]", "null", "{broken", json.dumps({"goal": "a" * 131073})):
            with self.subTest(source=source[:30]), self.assertRaises(SystemExit):
                PC.parse(["task", "prepare", source])

    def test_main_forwards_the_same_backend_payload_and_reports_incomplete_task(self):
        expected = {"action": "task_finish", "task_id": "task-1"}
        result = {"ok": True, "result": {"task_id": "task-1", "state": "incomplete", "completed": False}}
        output = io.StringIO()
        with patch.object(PC, "request", return_value=result) as request, redirect_stdout(output):
            code = PC.main(["task", "finish", "task-1"])
        request.assert_called_once_with("POST", "/action", expected)
        self.assertEqual(0, code)  # Successful transport is distinct from task completion.
        self.assertFalse(json.loads(output.getvalue())["result"]["completed"])


class ShareTests(unittest.TestCase):
    """The share hook runs a user-chosen command; nothing is uploaded by default."""

    def setUp(self):
        import os
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "shot.jpg"
        self.file.write_bytes(b"\xff\xd8x")
        self._saved = os.environ.get(PC.SHARE_ENV)
        self._saved_cmd = PC.share_command

    def tearDown(self):
        import os
        PC.share_command = self._saved_cmd
        if self._saved is None:
            os.environ.pop(PC.SHARE_ENV, None)
        else:
            os.environ[PC.SHARE_ENV] = self._saved
        self.tmp.cleanup()

    def _uploader(self, body):
        script = Path(self.tmp.name) / "up.py"
        script.write_text(body, encoding="utf-8")
        import sys
        PC.share_command = lambda: f'"{sys.executable}" "{script}"'

    def test_not_configured_uploads_nothing(self):
        PC.share_command = lambda: None
        result = PC.share(self.file)
        self.assertFalse(result["ok"])
        self.assertIn(PC.SHARE_ENV, result["error"])

    def test_last_stdout_line_is_the_url_and_path_is_last_arg(self):
        self._uploader(
            "import sys, pathlib\n"
            "assert pathlib.Path(sys.argv[-1]).read_bytes()[:2] == b'\\xff\\xd8'\n"
            "print('uploading...')\n"
            "print('https://example.test/s/abc')\n"
        )
        self.assertEqual({"ok": True, "url": "https://example.test/s/abc"}, PC.share(self.file))

    def test_command_without_url_fails(self):
        self._uploader("import sys\nprint('oops', file=sys.stderr)\nsys.exit(2)\n")
        result = PC.share(self.file)
        self.assertFalse(result["ok"])
        self.assertIn("oops", result["stderr"])


if __name__ == "__main__":
    unittest.main()
