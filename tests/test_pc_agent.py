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

    def test_base64_never_reaches_terminal(self):
        blob = "A" * 50000
        cleaned = PC.strip_images({"ok": True, "result": {"image": {"data": blob}, "path": "x"}})
        self.assertNotIn(blob, str(cleaned))
        self.assertEqual("x", cleaned["result"]["path"])


if __name__ == "__main__":
    unittest.main()
