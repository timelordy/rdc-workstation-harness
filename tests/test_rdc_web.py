import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "rdc_web.py"
SPEC = importlib.util.spec_from_file_location("rdc_web", MODULE_PATH)
RDC_WEB = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(RDC_WEB)


class SideEffectGateTests(unittest.TestCase):
    def test_safe_navigation_is_not_blocked(self):
        self.assertFalse(RDC_WEB.looks_side_effect({"action": "goto", "url": "https://example.com"}))

    def test_safe_read_click_is_not_blocked(self):
        self.assertFalse(RDC_WEB.looks_side_effect({"action": "click", "name": "Open details"}))

    def test_english_submit_click_is_blocked(self):
        self.assertTrue(RDC_WEB.looks_side_effect({"action": "click", "name": "Submit order"}))

    def test_russian_send_click_is_blocked(self):
        self.assertTrue(RDC_WEB.looks_side_effect({"action": "click", "name": "Отправить сообщение"}))

    def test_russian_delete_click_is_blocked(self):
        self.assertTrue(RDC_WEB.looks_side_effect({"action": "click", "text": "Удалить проект"}))


if __name__ == "__main__":
    unittest.main()
