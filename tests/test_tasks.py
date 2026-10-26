"""Portable behavioural tests for task persistence and verifiable completion."""

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("harness_tasks", ROOT / "src" / "desktop_agent" / "tasks.py")
TASKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TASKS)


def spec(identifier="review", kind="manual", required=True, allow_not_applicable=False):
    return {"id": identifier, "title": identifier.replace("-", " "), "kind": kind,
            "required": required, "allow_not_applicable": allow_not_applicable}


class Registry:
    def __init__(self, checks):
        self.skill = {
            "id": "builtin:test-skill", "name": "test-skill", "description": "Fixture method",
            "source": "builtin", "version": "1", "sha256": "a" * 64,
            "content": "Inspect the target; verify every required check.", "resources": [],
            "contract": {"schema_version": 1, "checks": checks},
        }

    def resolve(self, goal, workspace=None, requested_skills=None, limit=3):
        selected = [] if goal == "unrelated request" else [{"id": self.skill["id"], "sha256": self.skill["sha256"], "reason": "fixture"}]
        return {"selected": selected, "candidates": [], "warnings": []}

    def read(self, skill_id, workspace=None, resource=None, expected_hash=None):
        if expected_hash is not None and expected_hash != self.skill["sha256"]:
            raise ValueError("changed hash")
        return copy.deepcopy(self.skill)


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "work"
        self.workspace.mkdir()
        self.state_dir = self.root / "state"
        self.registry = Registry([spec()])
        self.runtime = TASKS.TaskRuntime(self.registry, self.state_dir)

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, checks=None, **extra):
        if checks is not None:
            self.registry.skill["contract"]["checks"] = checks
        return self.runtime.prepare({"goal": "Fixture task", "workspace": str(self.workspace), **extra})

    def check_id(self, local="review"):
        return "builtin:test-skill/" + local

    def manual(self, task, local="review", **extra):
        return self.runtime.check({"task_id": task["task_id"], "check_id": self.check_id(local),
                                   "status": "passed", "evidence": "Observed the intended target and value.", **extra})

    def test_initialization_is_lazy_and_returns_pinned_instructions(self):
        self.assertFalse(self.state_dir.exists())
        prepared = self.prepare()
        self.assertTrue(self.runtime.database.is_file())
        self.assertEqual("prepared", prepared["state"])
        self.assertEqual("a" * 64, prepared["skills"][0]["sha256"])
        self.assertIn("Inspect the target", prepared["skills"][0]["content"])
        self.assertEqual("pending", prepared["checks"][0]["status"])
        status = self.runtime.status({"task_id": prepared["task_id"]})
        self.assertNotIn("content", status["skills"][0])
        self.assertTrue(status["evidence_is_historical"])

    def test_restart_preserves_checks_and_resume_reports_skill_drift(self):
        prepared = self.prepare()
        self.manual(prepared)
        self.registry.skill.update(sha256="b" * 64, content="New instructions must not replace the pinned copy.")
        restored = TASKS.TaskRuntime(self.registry, self.state_dir)
        result = restored.resume({"task_id": prepared["task_id"]})
        self.assertEqual("passed", result["checks"][0]["status"])
        self.assertIn("Inspect the target", result["skills"][0]["content"])
        self.assertEqual("a" * 64, result["skills"][0]["sha256"])
        self.assertEqual("changed_or_unavailable", result["skill_drift"][0]["status"])
        self.assertIn("Do not replay", result["next"])

    def test_finish_requires_all_required_checks_and_manual_is_self_reported(self):
        prepared = self.prepare([spec("first"), spec("second")])
        self.manual(prepared, "first")
        incomplete = self.runtime.finish({"task_id": prepared["task_id"]})
        self.assertFalse(incomplete["completed"])
        self.assertEqual([self.check_id("second")], incomplete["coverage"]["blockers"])
        self.manual(prepared, "second", status="failed")
        self.assertFalse(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])
        self.manual(prepared, "second", status="unknown")
        self.assertFalse(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])
        self.manual(prepared, "second")
        complete = self.runtime.finish({"task_id": prepared["task_id"]})
        self.assertTrue(complete["completed"])
        self.assertEqual("self_reported", complete["assurance"])
        self.assertTrue(all(check["evidence_kind"] == "self_reported" for check in complete["checks"]))

    def test_na_is_explicit_reasoned_and_never_completes_an_entire_task(self):
        prepared = self.prepare()
        with self.assertRaises(TASKS.TaskError):
            self.manual(prepared, status="not_applicable", reason="No applicable field")
        prepared = self.prepare([spec(allow_not_applicable=True)])
        with self.assertRaises(TASKS.TaskError):
            self.manual(prepared, status="not_applicable")
        self.manual(prepared, status="not_applicable", reason="No applicable field")
        result = self.runtime.finish({"task_id": prepared["task_id"]})
        self.assertEqual("incomplete", result["state"])
        self.assertEqual("none", result["assurance"])

    def test_empty_contract_and_no_match_do_not_imply_completion(self):
        for extra in ({"checks": []}, {"goal": "unrelated request"}):
            with self.subTest(extra=extra):
                prepared = self.prepare(**extra)
                result = self.runtime.finish({"task_id": prepared["task_id"]})
                self.assertFalse(result["completed"])
                self.assertEqual("none", result["assurance"])
                self.assertTrue(prepared["warnings"])

    def test_contract_is_pinned_even_when_registry_mutates(self):
        prepared = self.prepare()
        self.registry.skill["contract"]["checks"][0]["required"] = False
        self.registry.skill["contract"]["checks"].clear()
        result = self.runtime.finish({"task_id": prepared["task_id"]})
        self.assertFalse(result["completed"])
        self.assertTrue(result["checks"][0]["required"])

    def test_unchecked_selected_skill_cannot_hide_behind_another_verified_skill(self):
        checked = Registry([spec("output", "file_exists")]).skill
        notes = copy.deepcopy(checked)
        notes.update(id="builtin:notes-only", name="notes-only", contract={"schema_version": 1, "checks": []})

        class MixedRegistry:
            def resolve(self, goal, workspace=None, requested_skills=None, limit=3):
                return {"selected": [{"id": item["id"], "sha256": item["sha256"]} for item in (checked, notes)],
                        "candidates": [], "warnings": []}

            def read(self, skill_id, workspace=None, resource=None, expected_hash=None):
                return copy.deepcopy(checked if skill_id == checked["id"] else notes)

        runtime = TASKS.TaskRuntime(MixedRegistry(), self.state_dir)
        prepared = runtime.prepare({"goal": "Verify output and apply notes", "workspace": str(self.workspace)})
        self.assertEqual([notes["id"]], prepared["coverage"]["uncovered_skills"])
        self.assertTrue(any(notes["id"] in item for item in prepared["warnings"]))
        (self.workspace / "output.txt").write_text("verified bytes", encoding="utf-8")
        runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": "output.txt"})
        result = runtime.finish({"task_id": prepared["task_id"]})
        self.assertEqual([], result["coverage"]["blockers"])
        self.assertEqual([notes["id"]], result["coverage"]["uncovered_skills"])
        self.assertFalse(result["completed"])
        self.assertEqual("none", result["assurance"])

    def test_changed_export_requires_fresh_manual_review_after_finish_and_auto_recheck(self):
        registry_spec = importlib.util.spec_from_file_location("export_registry_fixture", ROOT / "src" / "desktop_agent" / "skills.py")
        module = importlib.util.module_from_spec(registry_spec)
        registry_spec.loader.exec_module(module)
        runtime = TASKS.TaskRuntime(module.SkillRegistry(user_roots=[]), self.state_dir)
        prepared = runtime.prepare({"goal": "Verify exported report", "workspace": str(self.workspace),
                                    "requested_skills": ["builtin:verify-export"]})
        target = self.workspace / "report.txt"
        target.write_text("original export", encoding="utf-8")
        manual_ids = [check["id"] for check in prepared["checks"] if check["kind"] == "manual"]
        output_id = "builtin:verify-export/output-file"

        def review():
            for check_id in manual_ids:
                runtime.check({"task_id": prepared["task_id"], "check_id": check_id, "status": "passed",
                               "evidence": "Reviewed this exact current artifact and target."})

        automatic = {"task_id": prepared["task_id"], "check_id": output_id, "path": str(target)}
        runtime.check(automatic)
        review()
        self.assertTrue(runtime.finish({"task_id": prepared["task_id"]})["completed"])
        target.write_text("changed export", encoding="utf-8")
        changed = runtime.finish({"task_id": prepared["task_id"]})
        self.assertFalse(changed["completed"])
        self.assertEqual(set(manual_ids), set(changed["coverage"]["needs_manual_review"]))
        runtime.check(automatic)
        after_automatic_only = runtime.finish({"task_id": prepared["task_id"]})
        self.assertFalse(after_automatic_only["completed"])
        self.assertEqual(set(manual_ids), set(after_automatic_only["coverage"]["blockers"]))
        self.assertIn("re-review", after_automatic_only["next"])
        review()
        self.assertTrue(runtime.finish({"task_id": prepared["task_id"]})["completed"])

    def test_direct_changed_artifact_recheck_invalidates_old_manual_evidence(self):
        for change in ("different_content", "different_path", "missing", "empty", "too_large"):
            with self.subTest(change=change):
                prepared = self.prepare([spec(), spec("output", "file_exists")])
                target = self.workspace / "output.txt"
                target.write_text("original", encoding="utf-8")
                automatic = {"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(target)}
                self.runtime.check(automatic)
                self.manual(prepared)
                self.assertTrue(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])
                if change == "different_content":
                    target.write_text("changed", encoding="utf-8")
                elif change == "different_path":
                    target = self.workspace / "other.txt"
                    target.write_text("original", encoding="utf-8")
                    automatic["path"] = str(target)
                elif change == "missing":
                    target.unlink()
                elif change == "empty":
                    target.write_text("", encoding="utf-8")
                if change == "too_large":
                    with patch.object(TASKS, "MAX_ARTIFACT_BYTES", 1):
                        changed = self.runtime.check(automatic)
                else:
                    changed = self.runtime.check(automatic)
                self.assertEqual([self.check_id()], changed["coverage"]["needs_manual_review"])
                self.assertFalse(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])
                target.write_text("recovered", encoding="utf-8")
                self.runtime.check(automatic)
                self.assertFalse(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])
                self.manual(prepared)
                self.assertTrue(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])

    def test_recovered_same_hash_does_not_reuse_manual_report_from_failed_output(self):
        prepared = self.prepare([spec(), spec("output", "file_exists")])
        target = self.workspace / "output.txt"
        target.write_text("original", encoding="utf-8")
        automatic = {"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(target)}
        self.runtime.check(automatic)
        self.manual(prepared)
        target.unlink()
        self.runtime.check(automatic)
        self.manual(prepared, evidence="This report was entered while the artifact was missing.")
        target.write_text("original", encoding="utf-8")
        recovered = self.runtime.check(automatic)
        self.assertEqual([self.check_id()], recovered["coverage"]["needs_manual_review"])
        self.assertFalse(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])

    def test_previously_accepted_optional_report_requires_explicit_reassessment(self):
        prepared = self.prepare([spec(required=False), spec("output", "file_exists")])
        target = self.workspace / "output.txt"
        target.write_text("original", encoding="utf-8")
        automatic = {"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(target)}
        self.runtime.check(automatic)
        self.manual(prepared)
        target.write_text("changed", encoding="utf-8")
        self.runtime.check(automatic)
        result = self.runtime.finish({"task_id": prepared["task_id"]})
        self.assertEqual([], result["coverage"]["blockers"])
        self.assertEqual([self.check_id()], result["coverage"]["needs_manual_review"])
        self.assertFalse(result["completed"])
        self.manual(prepared, status="unknown", evidence="Optional review explicitly not repeated for the new output.")
        self.assertTrue(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])

    def test_file_check_is_computed_and_revalidated_before_finish(self):
        prepared = self.prepare([spec("output", "file_exists")])
        target = self.workspace / "output.txt"
        target.write_text("first output", encoding="utf-8")
        request = {"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(target)}
        for injected in ({"status": "passed"}, {"evidence": "trust me"}, {"criteria": {"min_bytes": 0}}):
            with self.subTest(injected=injected), self.assertRaises(TASKS.TaskError):
                self.runtime.check({**request, **injected})
        checked = self.runtime.check(request)
        self.assertEqual("passed", checked["checks"][0]["status"])
        self.assertEqual("machine_verified", checked["checks"][0]["evidence_kind"])
        self.assertEqual("verified", self.runtime.finish({"task_id": prepared["task_id"]})["assurance"])
        target.write_text("changed output", encoding="utf-8")
        result = self.runtime.finish({"task_id": prepared["task_id"]})
        self.assertFalse(result["completed"])
        self.assertEqual("unknown", result["checks"][0]["status"])
        self.assertEqual("ARTIFACT_CHANGED_SINCE_CHECK", result["checks"][0]["evidence"]["reason"])
        self.runtime.check(request)
        self.assertTrue(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])
        target.unlink()
        self.assertFalse(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])

    def test_file_check_missing_and_empty_output(self):
        prepared = self.prepare([spec("output", "file_exists")])
        target = self.workspace / "output.txt"
        request = {"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(target)}
        self.assertEqual("failed", self.runtime.check(request)["checks"][0]["status"])
        target.touch()
        result = self.runtime.check(request)
        self.assertEqual("FILE_TOO_SMALL", result["checks"][0]["evidence"]["reason"])

    def test_csv_criteria_are_declared_at_prepare_and_raw_values_not_returned(self):
        prepared = self.prepare([spec("schema", "csv_schema")], check_inputs={
            self.check_id("schema"): {"required_columns": ["id", "name"], "min_rows": 2, "delimiter": ";"},
        })
        target = self.workspace / "result.csv"
        target.write_text("id;name\n1;private-name-one\n2;private-name-two\n", encoding="utf-8")
        result = self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("schema"), "path": "result.csv"})
        self.assertEqual("passed", result["checks"][0]["status"])
        self.assertEqual(2, result["checks"][0]["evidence"]["rows"])
        self.assertNotIn("private-name", json.dumps(result))
        self.assertTrue(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])
        target.write_text("id;wrong\n1;x\n", encoding="utf-8")
        result = self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("schema"), "path": "result.csv"})
        self.assertEqual("failed", result["checks"][0]["status"])
        self.assertEqual(["name"], result["checks"][0]["evidence"]["missing_columns"])

    def test_missing_declared_schema_stays_unknown(self):
        prepared = self.prepare([spec("schema", "csv_schema")])
        target = self.workspace / "result.csv"
        target.write_text("id\n1\n", encoding="utf-8")
        result = self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("schema"), "path": "result.csv"})
        self.assertEqual("unknown", result["checks"][0]["status"])
        self.assertEqual("SCHEMA_NOT_DECLARED", result["checks"][0]["evidence"]["reason"])
        self.assertFalse(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])

    def test_csv_duplicate_headers_and_irregular_rows_fail(self):
        for content in ("id,id\n1,2\n", "id,name\n1\n", "id,name\n1,a,extra\n", "id,name\n\"broken\n"):
            with self.subTest(content=content):
                prepared = self.prepare([spec("schema", "csv_schema")], check_inputs={self.check_id("schema"): {"required_columns": ["id"]}})
                (self.workspace / "result.csv").write_text(content, encoding="utf-8")
                result = self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("schema"), "path": "result.csv"})
                self.assertEqual("failed", result["checks"][0]["status"])

    def test_csv_resource_limits_are_honest_not_truncated_success(self):
        prepared = self.prepare([spec("schema", "csv_schema")], check_inputs={self.check_id("schema"): {"required_columns": ["id"]}})
        (self.workspace / "result.csv").write_text("id\n1\n2\n", encoding="utf-8")
        with patch.object(TASKS, "MAX_CSV_ROWS", 1):
            result = self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("schema"), "path": "result.csv"})
        self.assertEqual("unknown", result["checks"][0]["status"])
        self.assertEqual("CSV_ROW_LIMIT", result["checks"][0]["evidence"]["reason"])

    def test_workspace_is_required_and_file_cannot_escape(self):
        target = self.root / "outside.csv"
        target.write_text("id\n1\n", encoding="utf-8")
        prepared = self.prepare([spec("output", "file_exists")])
        for path in (str(target), "../outside.csv"):
            with self.subTest(path=path), self.assertRaises(TASKS.TaskError):
                self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": path})
        prepared = self.prepare([spec("output", "file_exists")], workspace=None)
        with self.assertRaises(TASKS.TaskError):
            self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(target)})

    def test_symlink_escape_is_rejected(self):
        target = self.root / "outside.txt"
        target.write_text("outside", encoding="utf-8")
        link = self.workspace / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        prepared = self.prepare([spec("output", "file_exists")])
        with self.assertRaises(TASKS.TaskError):
            self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(link)})

    def test_credential_artifacts_are_rejected(self):
        prepared = self.prepare([spec("output", "file_exists")])
        target = self.workspace / "desktop.token"
        target.write_text("fixture-secret", encoding="utf-8")
        with self.assertRaises(TASKS.TaskError) as error:
            self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(target)})
        self.assertEqual("PROTECTED_ARTIFACT", error.exception.code)

    @unittest.skipIf(os.name == "nt", "FIFO test is POSIX-only")
    def test_special_file_does_not_block_verifier(self):
        target = self.workspace / "pipe"
        os.mkfifo(target)
        prepared = self.prepare([spec("output", "file_exists")])
        with self.assertRaises(TASKS.TaskError):
            self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(target)})

    def test_mixed_results_and_manual_exemptions_are_not_machine_only(self):
        target = self.workspace / "output.txt"
        target.write_text("result", encoding="utf-8")
        prepared = self.prepare([spec(allow_not_applicable=True), spec("output", "file_exists")])
        self.manual(prepared, status="not_applicable", reason="No manual review target")
        self.runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id("output"), "path": str(target)})
        self.assertEqual("mixed", self.runtime.finish({"task_id": prepared["task_id"]})["assurance"])

    def test_invalid_requests_leave_no_partial_update(self):
        prepared = self.prepare()
        before = self.runtime.status({"task_id": prepared["task_id"]})
        for bad in ({"evidence": ""}, {"status": []}, {"path": "x"}, {"status": "verified"}):
            with self.subTest(bad=bad), self.assertRaises(TASKS.TaskError):
                self.manual(prepared, **bad)
        after = self.runtime.status({"task_id": prepared["task_id"]})
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(before["checks"], after["checks"])
        with self.assertRaises(TASKS.TaskError):
            self.runtime.status({"task_id": "../../anything"})
        with self.assertRaises(TASKS.TaskError) as error:
            self.runtime.status({"task_id": "task_" + "0" * 32})
        self.assertEqual("TASK_NOT_FOUND", error.exception.code)

    def test_bad_prepare_inputs_are_rejected(self):
        cases = [
            {"goal": ""}, {"goal": 10}, {"workspace": "relative"},
            {"requested_skills": "skill"}, {"requested_skills": ["a"] * 4},
            {"check_inputs": {"unselected/check": {"min_bytes": 1}}},
        ]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(TASKS.TaskError):
                self.prepare(**bad)
        for criteria in ({"required_columns": []}, {"required_columns": ["id", "id"]},
                         {"required_columns": ["id"], "min_rows": True},
                         {"required_columns": ["id"], "delimiter": "::"}, {"script": "anything"}):
            with self.subTest(criteria=criteria), self.assertRaises(TASKS.TaskError):
                self.prepare([spec("schema", "csv_schema")], check_inputs={self.check_id("schema"): criteria})

    def test_concurrent_checks_do_not_lose_updates(self):
        prepared = self.prepare([spec(f"check-{index}") for index in range(12)])
        def record(index):
            runtime = TASKS.TaskRuntime(self.registry, self.state_dir)
            return runtime.check({"task_id": prepared["task_id"], "check_id": self.check_id(f"check-{index}"),
                                  "status": "passed", "evidence": f"Observation {index}"})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(record, range(12)))
        result = self.runtime.status({"task_id": prepared["task_id"]})
        self.assertEqual(12, result["coverage"]["passed"])
        self.assertEqual(13, result["revision"])
        self.assertTrue(self.runtime.finish({"task_id": prepared["task_id"]})["completed"])

    def test_list_is_bounded_filterable_and_does_not_return_instructions(self):
        first = self.prepare()
        self.prepare()
        self.manual(first)
        self.runtime.finish({"task_id": first["task_id"]})
        result = self.runtime.list({"state": "completed", "limit": 1})
        self.assertEqual(1, len(result["tasks"]))
        self.assertEqual(first["task_id"], result["tasks"][0]["task_id"])
        self.assertNotIn("skills", result["tasks"][0])
        for bad in ({"limit": True}, {"limit": 101}, {"state": []}):
            with self.subTest(bad=bad), self.assertRaises(TASKS.TaskError):
                self.runtime.list(bad)

    def test_bundled_table_skill_runs_through_real_registry_and_verifiers(self):
        registry_spec = importlib.util.spec_from_file_location("tasks_registry_fixture", ROOT / "src" / "desktop_agent" / "skills.py")
        module = importlib.util.module_from_spec(registry_spec)
        registry_spec.loader.exec_module(module)
        runtime = TASKS.TaskRuntime(module.SkillRegistry(user_roots=[]), self.state_dir)
        prepared = runtime.prepare({
            "goal": "Проверить таблицу", "workspace": str(self.workspace),
            "requested_skills": ["builtin:validate-table"],
            "check_inputs": {"builtin:validate-table/schema": {"required_columns": ["id", "name"], "min_rows": 1}},
        })
        (self.workspace / "result.csv").write_text("id,name\n1,Example\n", encoding="utf-8")
        for check in prepared["checks"]:
            request = {"task_id": prepared["task_id"], "check_id": check["id"]}
            if check["kind"] == "manual":
                request.update(status="passed", evidence="Test fixture manually reviewed; this claim remains self-reported.")
            else:
                request.update(path="result.csv")
            runtime.check(request)
        result = runtime.finish({"task_id": prepared["task_id"]})
        self.assertTrue(result["completed"])
        self.assertEqual("mixed", result["assurance"])
        self.assertEqual(len(prepared["checks"]), result["coverage"]["passed"])
        self.assertFalse(result["coverage"]["blockers"])


if __name__ == "__main__":
    unittest.main()
