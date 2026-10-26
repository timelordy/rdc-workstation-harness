"""Portable skill discovery tests use disposable packages, never local user skills."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "desktop_agent" / "skills.py"
SPEC = importlib.util.spec_from_file_location("rdc_test_skills", MODULE_PATH)
SKILLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SKILLS)


class SkillRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Windows TEMP may use an 8.3 alias (RUNNER~1); the registry deliberately
        # works with resolved root snapshots. Mocks must use that same identity.
        self.root = Path(temporary.name).resolve()
        self.builtin = self.root / "builtin"
        self.user = self.root / "user"
        self.workspace = self.root / "workspace"
        for directory in (self.builtin, self.user, self.workspace):
            directory.mkdir()
        environment = mock.patch.dict(os.environ, {
            "RDC_HARNESS_SKILL_DIRS": "", "RDC_HARNESS_TRUSTED_WORKSPACES": "[]",
            "USERPROFILE": str(self.root),  # isolate local operator policy from fixture checks
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.registry = SKILLS.SkillRegistry(self.builtin, [self.user])

    @staticmethod
    def package(root, name="sample-skill", description="Validate example datasets", body="Check the actual result.", contract=None):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        text = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
        (directory / "SKILL.md").write_text(text, encoding="utf-8")
        if contract is not None:
            (directory / "contract.json").write_text(json.dumps(contract), encoding="utf-8")
        return directory

    @staticmethod
    def contract(**changes):
        value = {"schema_version": 1, "checks": [
            {"id": "result", "title": "Check result", "kind": "manual", "required": True,
             "allow_not_applicable": False},
        ]}
        value.update(changes)
        return value

    def link(self, target, destination, is_directory=False):
        try:
            destination.symlink_to(target, target_is_directory=is_directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symbolic links unavailable: {error}")

    def test_metadata_only_catalog_and_multiline_yaml(self):
        package = self.package(self.builtin)
        (package / "SKILL.md").write_text(
            "---\nname: sample-skill\ndescription: >-\n  Inspect a table\n  and verify its rows.\n"
            "metadata:\n  version: '2.0'\ncompatibility: Python\nallowed-tools: Read\n---\n\nRead inputs.\n",
            encoding="utf-8",
        )
        listed = self.registry.list()
        self.assertEqual([], listed["warnings"])
        metadata = listed["skills"][0]
        self.assertEqual("Inspect a table and verify its rows.", metadata["description"])
        self.assertEqual("2.0", metadata["version"])
        self.assertNotIn("content", metadata)
        self.assertNotIn("contract", metadata)
        self.assertNotIn("_package", metadata)
        read = self.registry.read(metadata["id"])
        self.assertIn("Read inputs.", read["content"])
        self.assertEqual([], read["contract"]["checks"])

    def test_malformed_packages_do_not_hide_valid_packages(self):
        self.package(self.builtin, "valid-skill")
        invalid = self.package(self.builtin, "wrong-name")
        (invalid / "SKILL.md").write_text("---\nname: another-name\ndescription: Example\n---\nRead.\n", encoding="utf-8")
        result = self.registry.list()
        self.assertEqual(["valid-skill"], [item["name"] for item in result["skills"]])
        self.assertEqual("builtin:wrong-name", result["warnings"][0]["skill_id"])

    def test_unsafe_or_ambiguous_yaml_is_rejected(self):
        package = self.package(self.builtin)
        cases = [
            "name: sample-skill\nname: other\ndescription: Example",
            "name: sample-skill\ndescription: &a Example\nlicense: *a",
            "name: sample-skill\ndescription: !!python/object/apply:os.system ['echo wrong']",
            "name: sample-skill\ndescription: [not, text]",
            "name: sample-skill\ndescription: Example\nmetadata:\n  version: 2",
            "name: sample-skill\ndescription: Example\nmetadata:\n  x: {nested: value}",
            "name: sample-skill\ndescription: Example\npermissions: admin",
            "name: sample-skill\ndescription: Example\nmetadata:\n  key: first\n  key: second",
        ]
        for header in cases:
            with self.subTest(header=header):
                (package / "SKILL.md").write_text("---\n" + header + "\n---\nRead input.\n", encoding="utf-8")
                result = self.registry.list()
                self.assertEqual([], result["skills"])
                self.assertTrue(result["warnings"])

    def test_invalid_contract_is_not_treated_as_unrestricted_skill(self):
        package = self.package(self.builtin)
        cases = [
            {"schema_version": True},
            {"schema_version": 2},
            {"schema_version": 1, "permissions": ["exec"]},
            self.contract(checks=[{"id": "x", "title": "Check", "kind": "shell"}]),
            self.contract(checks=[{"id": "../escape", "title": "Check", "kind": "manual"}]),
            self.contract(checks=[{"id": "x", "title": "Check", "kind": "manual", "required": "true"}]),
            self.contract(checks=[{"id": "x", "title": "Check", "kind": "manual", "allow_not_applicable": 1}]),
            self.contract(checks=[{"id": "x", "title": "Check", "kind": "file_exists", "allow_not_applicable": True}]),
            self.contract(checks=[{"id": "x", "title": "Check", "kind": "manual"}] * 2),
            self.contract(required_capabilities=["browser", "browser"]),
            self.contract(match_terms=[{}]),
        ]
        for value in cases:
            with self.subTest(value=value):
                (package / "contract.json").write_text(json.dumps(value), encoding="utf-8")
                result = self.registry.list()
                self.assertEqual([], result["skills"])
                self.assertTrue(result["warnings"])
        (package / "contract.json").write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
        self.assertEqual([], self.registry.list()["skills"])
        (package / "contract.json").write_text("", encoding="utf-8")
        self.assertEqual([], self.registry.list()["skills"])

    def test_local_operator_policy_is_bounded_and_pinned_for_clients(self):
        profile = self.root / ".rdc-workstation-harness" / "codex-profile"
        profile.mkdir(parents=True)
        policy = profile / "WORKSTATION_POLICY.md"
        policy.write_text("Use kill-slop for code and humanizer for prose.\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"USERPROFILE": str(self.root)}):
            listed = self.registry.list()
            advice = listed["operator_policy"]
            self.assertEqual("local-user-profile", advice["source"])
            self.assertIn("kill-slop", advice["content"])
            resolved = self.registry.resolve("Review the code")
            self.assertEqual(advice, resolved["operator_policy"])
            policy.write_text("Different guidance.\n", encoding="utf-8")
            self.assertNotEqual(advice["sha256"], self.registry.list()["operator_policy"]["sha256"])
            policy.write_text("x" * 8193, encoding="utf-8")
            with self.assertRaises(SKILLS.SkillError) as caught:
                self.registry.list()
            self.assertEqual("SKILL_TOO_LARGE", caught.exception.code)

    def test_workspace_requires_exact_locally_configured_trust(self):
        self.package(self.workspace / ".agents" / "skills")
        result = self.registry.list(str(self.workspace))
        self.assertEqual([], result["skills"])
        self.assertEqual("WORKSPACE_SKILLS_UNTRUSTED", result["warnings"][0]["code"])
        os.environ["RDC_HARNESS_TRUSTED_WORKSPACES"] = json.dumps([str(self.workspace)])
        self.assertEqual("workspace:sample-skill", self.registry.list(str(self.workspace))["skills"][0]["id"])
        child = self.workspace / "nested"
        self.package(child / ".agents" / "skills")
        self.assertEqual([], self.registry.list(str(child))["skills"])

    def test_bad_trust_configuration_fails_closed(self):
        for raw in ("invalid JSON", '"all"', '[true]', '[""]'):
            with self.subTest(raw=raw):
                os.environ["RDC_HARNESS_TRUSTED_WORKSPACES"] = raw
                with self.assertRaises(SKILLS.SkillError) as caught:
                    self.registry.list(str(self.workspace))
                self.assertEqual("INVALID_SKILL_CONFIG", caught.exception.code)

    def test_shadowing_is_explicit_and_qualified_ids_remain_selectable(self):
        for root in (self.builtin, self.user, self.workspace / ".agents" / "skills"):
            self.package(root)
        os.environ["RDC_HARNESS_TRUSTED_WORKSPACES"] = json.dumps([str(self.workspace)])
        result = self.registry.list(str(self.workspace))
        self.assertEqual(3, len(result["skills"]))
        self.assertEqual(2, len(result["warnings"]))
        self.assertEqual(["workspace:sample-skill"], [s["id"] for s in result["skills"] if s["active"]])
        self.assertEqual("workspace", self.registry.read("sample-skill", str(self.workspace))["source"])
        self.assertEqual("builtin", self.registry.read("builtin:sample-skill", str(self.workspace))["source"])

    def test_user_roots_can_be_configured_without_loading_workspace_skills(self):
        self.package(self.user)
        os.environ["RDC_HARNESS_SKILL_DIRS"] = str(self.user)
        registry = SKILLS.SkillRegistry(self.builtin)
        self.assertEqual("user0:sample-skill", registry.list()["skills"][0]["id"])

    def test_nested_package_ids_are_stable_and_do_not_grant_path_access(self):
        self.package(self.user / "office")
        self.assertEqual("user0:office/sample-skill", self.registry.list()["skills"][0]["id"])
        self.assertEqual("sample-skill", self.registry.read("user0:office/sample-skill")["name"])
        with self.assertRaises(SKILLS.SkillError) as caught:
            self.registry.read(str(self.user / "office" / "sample-skill"))
        self.assertEqual("SKILL_NOT_FOUND", caught.exception.code)

    def test_overlong_parent_path_is_skipped_before_read_or_selection(self):
        # Avoid OS long-path policy differences: a traversal candidate must be
        # rejected before opening it, even when its parent categories are long.
        package = self.builtin / ("a" * 120) / ("b" * 120) / "sample-skill"
        registry = SKILLS.SkillRegistry(self.builtin, [])
        with mock.patch.object(registry, "_discover", return_value=[package]), \
                mock.patch.object(SKILLS, "_bounded_bytes", side_effect=AssertionError("overlong skill was read")):
            listed = registry.list()
            resolved = registry.resolve("Validate example datasets")
        self.assertEqual([], listed["skills"])
        self.assertEqual([], resolved["selected"])
        self.assertEqual("SKILL_ID_TOO_LONG", listed["warnings"][0]["code"])
        self.assertIn("256-character", listed["warnings"][0]["message"])

    def test_resolver_has_no_match_and_explicit_selection_paths(self):
        self.package(self.builtin, contract=self.contract(match_terms=["csv", "таблиц*"]))
        self.assertEqual([], self.registry.resolve("Compose a birthday greeting")["selected"])
        self.assertEqual(["builtin:sample-skill"], [s["id"] for s in self.registry.resolve("Проверь таблицу CSV")["selected"]])
        self.assertEqual([], self.registry.resolve("CSV", requested_skills=[])["selected"])
        explicit = self.registry.resolve("Unrelated task", requested_skills=["sample-skill"])
        self.assertEqual("Explicitly requested", explicit["selected"][0]["reason"])
        with self.assertRaises(SKILLS.SkillError):
            self.registry.resolve("CSV", requested_skills=["sample-skill", "builtin:sample-skill"])
        with self.assertRaises(SKILLS.SkillError):
            self.registry.resolve("CSV", requested_skills=["missing"])
        with self.assertRaises(SKILLS.SkillError):
            self.registry.resolve("CSV", limit=True)

    def test_plain_standard_skill_can_match_without_harness_contract(self):
        self.package(self.builtin, description="Inspect invoices and compare totals")
        selected = self.registry.resolve("Inspect these invoices")["selected"]
        self.assertEqual("builtin:sample-skill", selected[0]["id"])
        self.assertEqual([], self.registry.resolve("Create a file")["selected"])

    def test_package_hash_changes_for_instructions_and_contract(self):
        package = self.package(self.builtin, contract=self.contract())
        first_hash = self.registry.read("sample-skill")["sha256"]
        with (package / "SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("\nVerify output twice.\n")
        with self.assertRaises(SKILLS.SkillError) as caught:
            self.registry.read("sample-skill", expected_hash=first_hash)
        self.assertEqual("SKILL_CHANGED", caught.exception.code)
        second_hash = self.registry.read("sample-skill")["sha256"]
        (package / "contract.json").write_text(json.dumps(self.contract(match_terms=["invoice"])), encoding="utf-8")
        with self.assertRaises(SKILLS.SkillError):
            self.registry.read("sample-skill", expected_hash=second_hash)

    def test_references_are_lazy_and_script_reads_never_execute(self):
        package = self.package(self.builtin)
        scripts = package / "scripts"
        scripts.mkdir()
        marker = self.root / "executed.txt"
        source = "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n"
        # Preserve fixture bytes on Windows too: write_text would translate LF
        # to CRLF, while skill resource reads intentionally preserve the bytes.
        (scripts / "check.py").write_bytes(source.encode("utf-8"))
        read = self.registry.read("sample-skill")
        self.assertEqual(["scripts/check.py"], read["resources"])
        resource = self.registry.read("sample-skill", resource="scripts/check.py", expected_hash=read["sha256"])
        self.assertEqual(source, resource["content"])
        self.assertEqual(64, len(resource["resource_sha256"]))
        self.assertFalse(marker.exists())
        self.assertNotIn(source, read["content"])

    def test_resource_path_traversal_and_windows_paths_are_rejected(self):
        self.package(self.builtin)
        for path in ("../outside", "refs/../../outside", "/etc/passwd", "C:/windows/system.ini", "C:secret",
                     "refs\\outside", "\\\\server\\share", "~/.token"):
            with self.subTest(path=path):
                with self.assertRaises(SKILLS.SkillError) as caught:
                    self.registry.read("sample-skill", resource=path)
                self.assertEqual("SKILL_PATH_DENIED", caught.exception.code)

    def test_symlinked_packages_and_skill_files_cannot_escape_root(self):
        external = self.package(self.root / "outside")
        self.link(external, self.builtin / "sample-skill", True)
        self.assertEqual([], self.registry.list()["skills"])
        (self.builtin / "sample-skill").unlink()
        package = self.builtin / "sample-skill"
        package.mkdir()
        self.link(external / "SKILL.md", package / "SKILL.md")
        result = self.registry.list()
        self.assertEqual([], result["skills"])
        self.assertEqual("SKILL_PATH_DENIED", result["warnings"][0]["code"])

    def test_trusted_workspace_does_not_follow_skill_root_outside_workspace(self):
        external = self.root / "outside"
        self.package(external)
        (self.workspace / ".agents").mkdir()
        self.link(external, self.workspace / ".agents" / "skills", True)
        os.environ["RDC_HARNESS_TRUSTED_WORKSPACES"] = json.dumps([str(self.workspace)])
        result = self.registry.list(str(self.workspace))
        self.assertEqual([], result["skills"])
        self.assertEqual("SKILL_PATH_DENIED", result["warnings"][0]["code"])

    def test_resource_symlink_cannot_read_outside_package(self):
        package = self.package(self.builtin)
        secret = self.root / "outside.txt"
        secret.write_text("private", encoding="utf-8")
        self.link(secret, package / "reference.txt")
        self.assertEqual([], self.registry.read("sample-skill")["resources"])
        with self.assertRaises(SKILLS.SkillError) as caught:
            self.registry.read("sample-skill", resource="reference.txt")
        self.assertEqual("SKILL_PATH_DENIED", caught.exception.code)

    def test_escaped_directory_is_pruned_before_walker_recurses(self):
        # Model a junction: is_symlink() is false, while resolve() leaves the root.
        alias = self.builtin / "junction"
        alias.mkdir()
        external = self.root / "external"
        external.mkdir()
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            return external if path == alias else original_resolve(path, *args, **kwargs)

        def walk(root, followlinks=False):
            self.assertEqual(self.builtin, root)
            children = ["junction"]
            yield str(root), children, []
            self.assertEqual([], children, "the escaped directory must be removed before recursion")

        warnings = []
        with mock.patch.object(Path, "resolve", resolve), mock.patch.object(SKILLS.os, "walk", walk):
            self.assertEqual([], self.registry._discover(self.builtin, warnings))
        self.assertEqual("SKILL_PATH_DENIED", warnings[0]["code"])

    def test_package_containment_is_rechecked_before_any_file_read(self):
        # Even a stale traversal result must not turn an escaped package into a root.
        alias = self.builtin / "junction"
        package = alias / "sample-skill"
        package.mkdir(parents=True)
        outside = self.package(self.root / "external")
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == package:
                return outside
            return original_resolve(path, *args, **kwargs)

        registry = SKILLS.SkillRegistry(self.builtin, [])
        with mock.patch.object(Path, "resolve", resolve), \
                mock.patch.object(registry, "_discover", return_value=[package]), \
                mock.patch.object(SKILLS, "_bounded_bytes", side_effect=AssertionError("escaped package was read")):
            result = registry.list()
        self.assertEqual([], result["skills"])
        self.assertEqual("SKILL_PATH_DENIED", result["warnings"][0]["code"])

    def test_resource_junction_is_pruned_before_listing_target(self):
        package = self.package(self.builtin)
        alias = package / "references"
        alias.mkdir()
        external = self.root / "external"
        external.mkdir()
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            return external if path == alias else original_resolve(path, *args, **kwargs)

        def walk(root, followlinks=False):
            self.assertEqual(package, root)
            children = ["references"]
            yield str(root), children, ["SKILL.md"]
            self.assertEqual([], children, "resource traversal must not enter an escaped directory")

        with mock.patch.object(Path, "resolve", resolve), mock.patch.object(SKILLS.os, "walk", walk):
            self.assertEqual([], self.registry._resources(package, self.builtin))

    @unittest.skipUnless(os.name == "nt", "Windows junction fixture")
    def test_windows_junctions_cannot_expand_workspace_or_resource_trust(self):
        external = self.root / "external"
        self.package(external)
        (external / "reference.txt").write_text("outside the package", encoding="utf-8")

        def junction(destination):
            destination.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(destination), str(external)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            # Remove only the junction before TemporaryDirectory cleanup runs.
            self.addCleanup(os.rmdir, destination)
            return destination

        escape = junction(self.workspace / ".agents" / "skills" / "junction")
        os.environ["RDC_HARNESS_TRUSTED_WORKSPACES"] = json.dumps([str(self.workspace)])
        listed = self.registry.list(str(self.workspace))
        self.assertEqual([], listed["skills"])
        self.assertTrue(any(item["code"] == "SKILL_PATH_DENIED" for item in listed["warnings"]))

        # A root explicitly configured by the user is intentional trust, even if
        # the configured path itself is a junction; only traversal is confined.
        explicit = SKILLS.SkillRegistry(self.builtin, [escape])
        self.assertEqual("user0:sample-skill", explicit.list()["skills"][0]["id"])

        package = self.package(self.builtin)
        junction(package / "references")
        self.assertEqual([], self.registry.read("builtin:sample-skill")["resources"])
        with self.assertRaises(SKILLS.SkillError) as caught:
            self.registry.read("builtin:sample-skill", resource="references/reference.txt")
        self.assertEqual("SKILL_PATH_DENIED", caught.exception.code)

    def test_file_size_and_utf8_limits(self):
        package = self.package(self.builtin)
        (package / "large.txt").write_text("x" * 100, encoding="utf-8")
        with mock.patch.object(SKILLS, "MAX_RESOURCE_BYTES", 32):
            with self.assertRaises(SKILLS.SkillError) as caught:
                self.registry.read("sample-skill", resource="large.txt")
            self.assertEqual("SKILL_TOO_LARGE", caught.exception.code)
        (package / "binary.dat").write_bytes(b"\xff\x00")
        with self.assertRaises(SKILLS.SkillError):
            self.registry.read("sample-skill", resource="binary.dat")
        with mock.patch.object(SKILLS, "MAX_SKILL_BYTES", 32):
            result = self.registry.list()
            self.assertEqual([], result["skills"])
            self.assertEqual("SKILL_TOO_LARGE", result["warnings"][0]["code"])

    def test_discovery_bound_and_ignored_directories(self):
        self.package(self.builtin / "node_modules")
        self.package(self.builtin / ".git")
        self.assertEqual([], self.registry.list()["skills"])
        for number in range(5):
            self.package(self.user, f"sample-{number}")
        with mock.patch.object(SKILLS, "MAX_SKILLS", 2):
            result = self.registry.list()
            self.assertEqual(2, len(result["skills"]))
            self.assertTrue(any(item["code"] == "SKILL_SCAN_LIMIT" for item in result["warnings"]))


class BundledSkillsTests(unittest.TestCase):
    def test_all_bundled_skills_are_portable_and_have_complete_contracts(self):
        with mock.patch.dict(os.environ, {"RDC_HARNESS_SKILL_DIRS": "", "RDC_HARNESS_TRUSTED_WORKSPACES": "[]"}):
            registry = SKILLS.SkillRegistry()
            listed = registry.list()
            self.assertEqual([], listed["warnings"])
            self.assertEqual({"validate-table", "fill-form", "verify-export"}, {s["name"] for s in listed["skills"]})
            for entry in listed["skills"]:
                skill = registry.read(entry["id"], expected_hash=entry["sha256"])
                self.assertLess(len(skill["content"].splitlines()), 100)
                self.assertGreaterEqual(len(skill["contract"]["checks"]), 5)
                self.assertTrue(all(check["required"] for check in skill["contract"]["checks"]))
            examples = {"Заполни форму заявления": "fill-form", "Проверь CSV таблицу": "validate-table",
                        "Экспортируй документ и проверь результат": "verify-export"}
            for goal, name in examples.items():
                self.assertEqual(name, registry.resolve(goal, limit=1)["selected"][0]["name"])


if __name__ == "__main__":
    unittest.main()
