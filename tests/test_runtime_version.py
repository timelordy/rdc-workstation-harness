import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rdc_runtime_version", ROOT / "src/desktop_agent/runtime_version.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RuntimeVersionTests(unittest.TestCase):
    def test_checkout_uses_product_version(self):
        self.assertEqual((ROOT / "VERSION").read_text().strip(), MODULE.VERSION)

    def test_installed_layout_and_missing_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "runtime" / "desktop-agent"
            base.mkdir(parents=True)
            self.assertEqual("unknown", MODULE.read_version(base))
            (base / "VERSION").write_text("9.8.7\n")
            self.assertEqual("9.8.7", MODULE.read_version(base))
            (base / "VERSION").write_text("invalid")
            self.assertEqual("unknown", MODULE.read_version(base))


if __name__ == "__main__":
    unittest.main()
