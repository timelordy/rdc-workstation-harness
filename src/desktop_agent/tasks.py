"""Durable task context and bounded result checks, independent of Windows providers.

This journal stores instructions, declared criteria and evidence. It never runs
application commands, retries side effects, grants permissions, or guarantees
exactly-once execution. A delivered skill is not proof that a model followed it.
"""

import copy
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_CSV_ROWS = 100000
MAX_CHECKS = 96
TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
STATES = {"prepared", "in_progress", "incomplete", "completed"}
CHECK_STATUSES = {"passed", "failed", "not_applicable", "unknown"}
LIMITATIONS = [
    "Skill delivery and recorded manual evidence do not prove instruction compliance.",
    "Machine checks cover only the recorded, caller-declared criteria, not all task semantics.",
    "This journal does not execute, replay or deduplicate application actions.",
    "Saved UI observations and authorizations must be revalidated before a new action.",
]


class TaskError(ValueError):
    """An invalid task request, suitable for conversion to the HTTP action error."""

    def __init__(self, message, code="INVALID_TASK_REQUEST", hint=None):
        super().__init__(message)
        self.code = code
        self.hint = hint


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, field, maximum, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise TaskError(f"{field} must be non-empty text of at most {maximum} characters")
    return value.strip()


def _integer(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise TaskError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _workspace(value):
    if value is None:
        return None
    raw = _text(value, "workspace", 4096)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise TaskError("workspace must be an absolute existing directory")
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise TaskError("workspace is unavailable", hint=type(error).__name__) from None
    if not path.is_dir():
        raise TaskError("workspace must be a directory")
    return str(path)


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _criteria(kind, value):
    """Validate immutable caller-declared verifier criteria before any evidence."""
    if not isinstance(value, dict):
        raise TaskError("check_inputs entries must be objects")
    allowed = {"min_bytes"} if kind == "file_exists" else {"required_columns", "min_rows", "delimiter"}
    if kind == "manual" or set(value) - allowed:
        raise TaskError(f"unsupported check_inputs for {kind}")
    result = {}
    if kind == "file_exists":
        result["min_bytes"] = _integer(value.get("min_bytes", 1), "min_bytes", 0, MAX_ARTIFACT_BYTES)
    else:
        columns = value.get("required_columns")
        if not isinstance(columns, list) or not 1 <= len(columns) <= 256:
            raise TaskError("csv_schema requires 1..256 declared required_columns at task preparation")
        for item in columns:
            _text(item, "required_columns item", 256)
        if len(set(columns)) != len(columns):
            raise TaskError("required_columns must be unique")
        result["required_columns"] = columns
        result["min_rows"] = _integer(value.get("min_rows", 0), "min_rows", 0, MAX_CSV_ROWS)
        delimiter = value.get("delimiter", ",")
        if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in "\r\n\x00\"":
            raise TaskError("delimiter must be one character other than newline, NUL or quote")
        result["delimiter"] = delimiter
    return result


class TaskRuntime:
    """SQLite-backed context shared by MCP and CLI through the same action router."""

    def __init__(self, registry=None, state_dir=None):
        if registry is None:
            from skills import SkillRegistry
            registry = SkillRegistry()
        self.registry = registry
        home = Path(os.getenv("USERPROFILE", str(Path.home())))
        self.state_dir = Path(state_dir or os.getenv(
            "RDC_HARNESS_STATE_DIR", str(home / ".rdc-workstation-harness" / "state"),
        )).expanduser()
        self.database = self.state_dir / "tasks.sqlite3"

    @contextmanager
    def _connection(self, write=False):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(str(self.database), timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS task_runs ("
                "task_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "state TEXT NOT NULL, document TEXT NOT NULL)"
            )
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _request(a):
        if not isinstance(a, dict):
            raise TaskError("task request must be an object")

    @staticmethod
    def _id(a):
        value = a.get("task_id")
        if not isinstance(value, str) or not TASK_ID.fullmatch(value):
            raise TaskError("task_id must be an ID returned by task_prepare")
        return value

    def _load(self, connection, a):
        row = connection.execute("SELECT document FROM task_runs WHERE task_id = ?", (self._id(a),)).fetchone()
        if row is None:
            raise TaskError("task was not found", code="TASK_NOT_FOUND")
        return json.loads(row[0])

    @staticmethod
    def _save(connection, document):
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise TaskError("task context exceeds the 2 MiB limit")
        connection.execute(
            "INSERT INTO task_runs(task_id, created_at, updated_at, state, document) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET updated_at=excluded.updated_at, state=excluded.state, document=excluded.document",
            (document["task_id"], document["created_at"], document["updated_at"], document["state"], encoded),
        )

    @staticmethod
    def _event(document, event, **details):
        document["revision"] += 1
        document["updated_at"] = _now()
        document["events"].append({"at": document["updated_at"], "revision": document["revision"], "event": event, **details})
        document["events"] = document["events"][-100:]

    @staticmethod
    def _coverage(document):
        checks = document["checks"]
        counts = {status: sum(item["status"] == status for item in checks)
                  for status in ("pending", "passed", "failed", "not_applicable", "unknown")}
        blockers = [item["id"] for item in checks if item["required"] and not (
            item["status"] == "passed" or (item["status"] == "not_applicable" and item["allow_not_applicable"]))]
        covered_skills = {item["skill_id"] for item in checks}
        uncovered_skills = [skill["id"] for skill in document["skills"] if skill["id"] not in covered_skills]
        needs_manual_review = [item["id"] for item in checks if item["kind"] == "manual"
                               and item["status"] == "unknown" and item.get("invalidated_by")]
        counts.update(total=len(checks), required=sum(item["required"] for item in checks), blockers=blockers,
                      uncovered_skills=uncovered_skills, needs_manual_review=needs_manual_review)
        return counts

    @staticmethod
    def _fingerprint(evidence):
        if not isinstance(evidence, dict) or not evidence.get("path") or not evidence.get("sha256"):
            return None
        return {key: evidence.get(key) for key in ("path", "sha256", "size")}

    @classmethod
    def _observe_artifact(cls, document, check, fresh):
        """Invalidate old manual reports whenever an already-verified output changes.

        A bounded fingerprint survives failures, so a direct automatic recheck
        cannot overwrite the only evidence that manual reviews are now stale.
        With no dependency graph, every accepted manual report is conservative
        scope: even a report in another selected skill may describe this output.
        """
        previous = check.get("last_verified_artifact")
        if previous is None and check["status"] == "passed":
            previous = cls._fingerprint(check.get("evidence"))
        observation = check.get("artifact_observation")
        current = cls._fingerprint(fresh)
        changed = previous is not None and (
            fresh["status"] != "passed" or current != previous
            or (observation is not None and observation["status"] != "passed")
        )
        if changed:
            invalidated = []
            for manual in document["checks"]:
                if manual["kind"] == "manual" and manual["status"] in {"passed", "not_applicable"}:
                    manual.update(status="unknown", evidence_kind="none",
                                  reason="ARTIFACT_CHANGED_REVIEW_REQUIRED", invalidated_by=check["id"],
                                  invalidated_at=_now(),
                                  hint="Re-review this manual check against the current artifact; its earlier report is stale.")
                    invalidated.append(manual["id"])
            if invalidated:
                cls._event(document, "manual_checks_invalidated", artifact_check_id=check["id"], check_ids=invalidated)
        check["artifact_observation"] = {"status": fresh["status"], "fingerprint": current, "at": _now()}
        if fresh["status"] == "passed":
            check["last_verified_artifact"] = current
        elif previous is not None:
            check["last_verified_artifact"] = previous

    @classmethod
    def _public(cls, document, instructions=False):
        result = copy.deepcopy(document)
        if not instructions:
            for skill in result["skills"]:
                skill.pop("content", None)
                skill.pop("contract", None)
                skill.pop("resources", None)
        result["coverage"] = cls._coverage(document)
        result["completed"] = document["state"] == "completed"
        result["evidence_is_historical"] = True
        result["limitations"] = list(LIMITATIONS)
        result["events"] = result["events"][-20:]
        result["next"] = ("Report completion with assurance and evidence; do not imply unverified semantics."
                          if result["completed"] else "Read selected instructions, perform authorized work, record every required check, then task_finish.")
        if result["coverage"]["needs_manual_review"]:
            result["next"] = "An artifact changed or failed validation. Recheck its output and re-review the affected manual checks before task_finish."
        if result["coverage"]["uncovered_skills"]:
            result["next"] = "Selected instruction-only skills have no completion checks. Read them separately, or prepare a new task after adding their explicit check contracts."
        return result

    def prepare(self, a):
        self._request(a)
        goal = _text(a.get("goal"), "goal", 4000)
        workspace = _workspace(a.get("workspace"))
        requested = a.get("requested_skills")
        if requested is not None:
            if not isinstance(requested, list) or len(requested) > 3:
                raise TaskError("requested_skills must be a list of at most three IDs")
            requested = [_text(value, "skill ID", 256) for value in requested]
            if len(set(requested)) != len(requested):
                raise TaskError("requested_skills must not contain duplicates")
        inputs = a.get("check_inputs", {})
        if not isinstance(inputs, dict) or len(inputs) > MAX_CHECKS:
            raise TaskError("check_inputs must be an object with at most 96 entries")
        resolved = self.registry.resolve(goal, workspace=workspace, requested_skills=requested, limit=3)
        selected = resolved.get("selected", [])
        if not isinstance(selected, list) or len(selected) > 3:
            raise TaskError("registry returned an invalid skill selection")
        skills, checks = [], []
        for selection in selected:
            loaded = self.registry.read(selection["id"], workspace=workspace, expected_hash=selection.get("sha256"))
            snapshot = {key: copy.deepcopy(loaded.get(key)) for key in (
                "id", "name", "description", "source", "version", "sha256", "content", "contract", "resources",
            )}
            snapshot["reason"] = selection.get("reason", "selected by registry")
            if not isinstance(snapshot["content"], str) or not snapshot["content"].strip():
                raise TaskError("selected skill has no instructions")
            skills.append(snapshot)
            contract = snapshot["contract"] or {}
            for spec in contract.get("checks", []):
                check_id = f"{snapshot['id']}/{spec['id']}"
                kind = spec["kind"]
                configured = check_id in inputs
                criteria = _criteria(kind, inputs[check_id]) if configured else (
                    {"min_bytes": 1} if kind == "file_exists" else None)
                checks.append({
                    "id": check_id, "local_id": spec["id"], "skill_id": snapshot["id"],
                    "title": spec["title"], "kind": kind,
                    "required": spec.get("required", True),
                    "allow_not_applicable": spec.get("allow_not_applicable", False),
                    "criteria": criteria, "status": "pending", "evidence_kind": "none", "evidence": None,
                    "checked_at": None,
                })
        if len(checks) > MAX_CHECKS or len({check["id"] for check in checks}) != len(checks):
            raise TaskError("too many checks or duplicate check IDs")
        unmatched = set(inputs) - {check["id"] for check in checks}
        if unmatched:
            raise TaskError("check_inputs contains unknown checks", hint=", ".join(sorted(map(str, unmatched)))[:2000])
        warnings = list(resolved.get("warnings", []))
        if not skills:
            warnings.append("No skill selected; generic work may proceed, but this run has no verifiable completion contract.")
        for skill in skills:
            if not any(check["skill_id"] == skill["id"] for check in checks):
                warnings.append(f"{skill['id']}: no completion checks. Instruction-only skills may be read separately; this selected skill prevents task completion.")
        for check in checks:
            if check["kind"] == "csv_schema" and check["criteria"] is None:
                warnings.append(f"{check['id']}: missing declared schema. Inspect requirements and prepare a new task with check_inputs.required_columns.")
        created = _now()
        document = {
            "task_id": "task_" + uuid.uuid4().hex, "goal": goal, "workspace": workspace,
            "state": "prepared", "assurance": "none", "created_at": created, "updated_at": created,
            "revision": 0, "skills": skills, "checks": checks,
            "candidates": resolved.get("candidates", []), "warnings": warnings,
            "events": [],
        }
        self._event(document, "prepared")
        with self._connection(write=True) as connection:
            self._save(connection, document)
        return self._public(document, instructions=True)

    def status(self, a):
        self._request(a)
        with self._connection() as connection:
            return self._public(self._load(connection, a))

    @staticmethod
    def _artifact_path(workspace, value):
        raw = _text(value, "path", 4096)
        if workspace is None:
            raise TaskError("artifact checks require a workspace fixed at task preparation")
        root = Path(workspace)
        if not root.is_dir() or root.resolve() != root:
            raise TaskError("task workspace is unavailable or changed", code="WORKSPACE_CHANGED")
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError):
            raise TaskError("artifact path cannot be resolved") from None
        if not _inside(path, root):
            raise TaskError("artifact path escapes task workspace", code="PATH_OUTSIDE_WORKSPACE")
        home = Path(os.getenv("USERPROFILE", str(Path.home())))
        protected = [
            Path(os.getenv("RDC_HARNESS_TOKEN_DIR", str(home / ".chatgpt-desktop-agent"))),
            home / ".desktop-commander-device",
            Path(os.getenv("PW_PROFILE_DIR", str(home / ".rdc-workstation-harness" / "browser-profile"))),
        ]
        if path.suffix.lower() == ".token" or any(_inside(path, item.resolve()) for item in protected):
            raise TaskError("credential and browser profile artifacts are not eligible for verification", code="PROTECTED_ARTIFACT")
        return path

    @staticmethod
    def _read_artifact(path):
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise TaskError("artifact must be a regular file", code="ARTIFACT_NOT_REGULAR")
            if before.st_size > MAX_ARTIFACT_BYTES:
                raise TaskError("artifact exceeds 16 MiB verification limit", code="ARTIFACT_TOO_LARGE")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_ARTIFACT_BYTES + 1)
            after = os.fstat(fd)
            if len(data) > MAX_ARTIFACT_BYTES:
                raise TaskError("artifact exceeds 16 MiB verification limit", code="ARTIFACT_TOO_LARGE")
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise TaskError("artifact changed during verification", code="ARTIFACT_CHANGED")
            return data
        finally:
            os.close(fd)

    def _verify(self, check, workspace, value):
        path = self._artifact_path(workspace, value)
        result = {"path": str(path), "criteria": copy.deepcopy(check["criteria"])}
        if check["kind"] == "csv_schema" and check["criteria"] is None:
            result.update(status="unknown", evidence_kind="none", reason="SCHEMA_NOT_DECLARED",
                          hint="Prepare a new task with declared required_columns in check_inputs before collecting schema evidence.")
            return result
        try:
            data = self._read_artifact(path)
        except FileNotFoundError:
            result.update(status="failed", evidence_kind="machine_observed", reason="ARTIFACT_NOT_FOUND")
            return result
        except OSError as error:
            result.update(status="unknown", evidence_kind="none", reason="ARTIFACT_UNREADABLE", error_type=type(error).__name__)
            return result
        result.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        if check["kind"] == "file_exists":
            passed = len(data) >= check["criteria"]["min_bytes"]
            result.update(status="passed" if passed else "failed", reason="FILE_CHECK_PASSED" if passed else "FILE_TOO_SMALL")
        else:
            criteria = check["criteria"]
            try:
                reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), delimiter=criteria["delimiter"], strict=True)
                headers = next(reader, [])
                missing = [name for name in criteria["required_columns"] if name not in headers]
                duplicate_headers = len(set(headers)) != len(headers)
                rows, malformed_rows = 0, 0
                for row in reader:
                    rows += 1
                    if rows > MAX_CSV_ROWS:
                        result.update(status="unknown", reason="CSV_ROW_LIMIT", row_limit=MAX_CSV_ROWS, evidence_kind="none")
                        return result
                    malformed_rows += len(row) != len(headers)
                passed = bool(headers) and not missing and not duplicate_headers and not malformed_rows and rows >= criteria["min_rows"]
                result.update(status="passed" if passed else "failed", reason="CSV_CHECK_PASSED" if passed else "CSV_CHECK_FAILED",
                              missing_columns=missing, duplicate_headers=duplicate_headers, rows=rows,
                              malformed_rows=malformed_rows, column_count=len(headers))
            except (UnicodeError, csv.Error):
                result.update(status="failed", reason="CSV_INVALID_UTF8_OR_STRUCTURE")
        result["evidence_kind"] = "machine_verified" if result["status"] == "passed" else "machine_observed"
        return result

    def check(self, a):
        self._request(a)
        check_id = _text(a.get("check_id"), "check_id", 400)
        with self._connection(write=True) as connection:
            document = self._load(connection, a)
            check = next((item for item in document["checks"] if item["id"] == check_id), None)
            if check is None:
                raise TaskError("check_id is not in this task's pinned contract", code="CHECK_NOT_FOUND")
            if check["kind"] == "manual":
                if "path" in a:
                    raise TaskError("manual checks accept evidence, not an automatic artifact path")
                status = a.get("status")
                if not isinstance(status, str) or status not in CHECK_STATUSES:
                    raise TaskError("manual status must be passed, failed, not_applicable or unknown")
                evidence = _text(a.get("evidence"), "evidence", 4000)
                reason = _text(a.get("reason"), "reason", 2000, required=False)
                if status == "not_applicable":
                    if not check["allow_not_applicable"]:
                        raise TaskError("this check does not allow not_applicable")
                    if reason is None:
                        raise TaskError("not_applicable requires a reason")
                check.update(status=status, evidence=evidence, reason=reason, evidence_kind="self_reported")
                for key in ("invalidated_by", "invalidated_at", "hint"):
                    check.pop(key, None)
            else:
                if any(key in a for key in ("status", "evidence", "reason", "expectations", "criteria")):
                    raise TaskError("automatic checks accept only a path; results are computed against preparation-time criteria")
                try:
                    evidence = self._verify(check, document["workspace"], a.get("path"))
                except TaskError as error:
                    previously_verified = check.get("last_verified_artifact") or (
                        self._fingerprint(check.get("evidence")) if check["status"] == "passed" else None)
                    if previously_verified is None or error.code not in {
                        "ARTIFACT_TOO_LARGE", "ARTIFACT_CHANGED", "ARTIFACT_NOT_REGULAR", "WORKSPACE_CHANGED",
                    }:
                        raise
                    evidence = {"status": "unknown", "evidence_kind": "none", "reason": error.code,
                                "hint": "The earlier artifact can no longer be verified; inspect it and re-review manual checks."}
                self._observe_artifact(document, check, evidence)
                check.update(status=evidence["status"], evidence=evidence, evidence_kind=evidence["evidence_kind"])
            check["checked_at"] = _now()
            document.update(state="in_progress", assurance="none")
            self._event(document, "check_recorded", check_id=check_id, status=check["status"])
            self._save(connection, document)
        return self._public(document)

    def finish(self, a):
        self._request(a)
        with self._connection(write=True) as connection:
            document = self._load(connection, a)
            for check in document["checks"]:
                if check["kind"] != "manual" and check["status"] == "passed":
                    previous = check["evidence"]
                    previous_fingerprint = self._fingerprint(previous)
                    try:
                        fresh = self._verify(check, document["workspace"], previous["path"])
                    except TaskError as error:
                        fresh = {"status": "unknown", "evidence_kind": "none", "reason": error.code}
                    self._observe_artifact(document, check, fresh)
                    if fresh["status"] != "passed" or self._fingerprint(fresh) != previous_fingerprint:
                        check.update(status="unknown", evidence_kind="none", evidence={
                            "status": "unknown", "reason": "ARTIFACT_CHANGED_SINCE_CHECK",
                            "previous_fingerprint": previous_fingerprint,
                            "current": {key: fresh.get(key) for key in ("status", "reason", "path", "sha256", "size")},
                            "hint": "Recheck the current output and re-review affected manual checks before finishing.",
                        })
                        check["checked_at"] = _now()
            coverage = self._coverage(document)
            passed = [item for item in document["checks"] if item["status"] == "passed"]
            complete = (bool(document["checks"]) and bool(passed) and not coverage["blockers"]
                        and not coverage["uncovered_skills"] and not coverage["needs_manual_review"])
            assurance = "none"
            if complete:
                kinds = {item["evidence_kind"] for item in document["checks"]
                         if item["status"] in {"passed", "not_applicable"}}
                assurance = ("verified" if kinds == {"machine_verified"} else
                             "self_reported" if kinds == {"self_reported"} else "mixed")
            document.update(state="completed" if complete else "incomplete", assurance=assurance)
            self._event(document, "finished" if complete else "completion_incomplete", assurance=assurance)
            self._save(connection, document)
        result = self._public(document)
        if not complete and not coverage["uncovered_skills"] and not coverage["needs_manual_review"]:
            result["next"] = "Resolve required pending/failed/unknown checks; at least one check must pass. Instruction delivery or all not_applicable cannot complete a task."
        return result

    def resume(self, a):
        self._request(a)
        with self._connection() as connection:
            document = self._load(connection, a)
        result = self._public(document, instructions=True)
        drift = []
        for skill in document["skills"]:
            try:
                current = self.registry.read(skill["id"], workspace=document["workspace"], expected_hash=skill["sha256"])
                if current["sha256"] != skill["sha256"]:
                    drift.append({"skill_id": skill["id"], "status": "changed"})
            except (ValueError, OSError, KeyError) as error:
                drift.append({"skill_id": skill["id"], "status": "changed_or_unavailable", "error_type": type(error).__name__})
        result["skill_drift"] = drift
        result["next"] = (
            "Resume from the recorded checks using pinned instructions. Re-observe documents, windows and tabs, "
            "revalidate authorization, and reconcile unknown effects before any new write. Do not replay commands from this journal."
        )
        return result

    def list(self, a=None):
        a = {} if a is None else a
        self._request(a)
        limit = _integer(a.get("limit", 20), "limit", 1, 100)
        state = a.get("state")
        if state is not None and (not isinstance(state, str) or state not in STATES):
            raise TaskError("state must be prepared, in_progress, incomplete or completed")
        with self._connection() as connection:
            query = "SELECT document FROM task_runs"
            params = []
            if state is not None:
                query += " WHERE state = ?"
                params.append(state)
            query += " ORDER BY updated_at DESC, task_id LIMIT ?"
            rows = connection.execute(query, params + [limit]).fetchall()
        items = []
        for row in rows:
            document = json.loads(row[0])
            items.append({key: document[key] for key in (
                "task_id", "goal", "state", "assurance", "created_at", "updated_at", "revision",
            )})
        return {"tasks": items, "limit": limit}
