import ast
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "src" / "desktop_agent"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CATALOG = load("catalog", AGENT_DIR / "catalog.py")


def dict_keys_of(path, func_name, target):
    """Keys of the handler table in one function, read via AST (no Windows-only imports).

    target is the variable name assigned the dict literal, or None for `return {...}`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for stmt in node.body:
                if target is None and isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict):
                    table = stmt.value
                elif (target and isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Dict)
                      and any(isinstance(t, ast.Name) and t.id == target for t in stmt.targets)):
                    table = stmt.value
                else:
                    continue
                return {k.value for k in table.keys if isinstance(k, ast.Constant)}
    raise AssertionError(f"{func_name} handler table not found in {path}")


class CatalogContractTests(unittest.TestCase):
    def test_every_handler_is_described_and_every_description_has_a_handler(self):
        handlers = dict_keys_of(AGENT_DIR / "agent.py", "build_handlers", "table")
        handlers |= dict_keys_of(AGENT_DIR / "core_ext.py", "handlers", None)
        self.assertEqual(set(), handlers - set(CATALOG.ACTIONS), "handlers missing from catalog")
        self.assertEqual(set(), set(CATALOG.ACTIONS) - handlers, "catalog entries without handler")

    def test_browser_catalog_matches_bridge(self):
        bridge = (ROOT / "src" / "browser_bridge" / "bridge.js").read_text(encoding="utf-8")
        implemented = set(re.findall(r"a\.action === '([A-Za-z]+)'", bridge))
        self.assertEqual(implemented, set(CATALOG.BROWSER_ACTIONS))

    def test_specs_are_complete(self):
        effects = {"read", "write", "exec", "physical"}
        for name, spec in CATALOG.ACTIONS.items():
            with self.subTest(name=name):
                self.assertIn(spec["effect"], effects)
                self.assertTrue(spec["summary"])
                for param in spec["required"]:
                    self.assertIn(param, spec["params"])
                if spec["example"] is not None:
                    self.assertEqual(name, spec["example"]["action"])

    def test_physical_group_is_physical_effect(self):
        for name, spec in CATALOG.ACTIONS.items():
            if spec["group"] == "physical":
                self.assertEqual("physical", spec["effect"], name)
        for name in ("move", "click", "drag", "scroll", "key", "hotkey", "type_text", "uia_type_input"):
            self.assertEqual("physical", CATALOG.ACTIONS[name]["effect"], name)

    def test_capabilities_lists_everything(self):
        caps = CATALOG.capabilities(["autocad"])
        listed = {item["name"] for group in caps["actions"].values() for item in group}
        self.assertEqual(set(CATALOG.ACTIONS), listed)
        self.assertEqual(["autocad"], caps["adapters"])
        self.assertIn("snapshot", caps["browser_actions"])

    def test_missing_params_and_suggestions(self):
        spec = CATALOG.ACTIONS["probe_target"]
        self.assertEqual(["pid"], CATALOG.missing_params(spec, {"action": "probe_target"}))
        self.assertEqual([], CATALOG.missing_params(spec, {"pid": 0}))
        self.assertIn("uia_click", CATALOG.suggest("uia_clik", CATALOG.ACTIONS))

    def test_describe_browser_has_usage(self):
        info = CATALOG.describe_browser("click")
        self.assertEqual({"action": "browser", "payload": {"action": "click"}}, info["usage"])


class BrowserGateTests(unittest.TestCase):
    def test_enter_on_send_field_is_gated(self):
        self.assertTrue(CATALOG.browser_side_effect({"action": "press", "key": "Enter", "label": "Send message"}))

    def test_other_keys_are_not_gated(self):
        self.assertFalse(CATALOG.browser_side_effect({"action": "press", "key": "Tab", "label": "Send"}))

    def test_fill_is_not_gated(self):
        self.assertFalse(CATALOG.browser_side_effect({"action": "fill", "label": "Delete reason"}))


class AdapterManifestTests(unittest.TestCase):
    def test_bundled_adapters_declare_manifest(self):
        for path in sorted((AGENT_DIR / "adapters").glob("*.py")):
            if path.name.startswith("_"):
                continue
            with self.subTest(adapter=path.stem):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                names = {t.id for node in tree.body if isinstance(node, ast.Assign)
                         for t in node.targets if isinstance(t, ast.Name)}
                self.assertIn("DESCRIPTION", names)
                self.assertIn("ACTIONS", names)
                funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
                self.assertIn("handle", funcs)


if __name__ == "__main__":
    unittest.main()
