import importlib.util
import unittest
from pathlib import Path

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

    def test_json_argument(self):
        self.assertEqual({"action": "windows", "limit": 3}, PC.parse(['{"action": "windows",', '"limit": 3}']))

    def test_ascii_path_keeps_ascii_and_resolves_to_same_file(self):
        import os
        import tempfile
        self.assertEqual(r"C:\plain\x.jpg", PC.ascii_path(r"C:\plain\x.jpg"))
        if os.name != "nt":
            return
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Анвар" / "look.jpg"
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
