"""Read-only, bounded discovery of portable Agent Skills.

Skills are instructions, not authorization. This module never imports or executes
bundled code. Workspace skills require a locally configured trust decision.
"""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

import yaml


MAX_SKILL_BYTES = 128 * 1024
MAX_CONTRACT_BYTES = 64 * 1024
MAX_RESOURCE_BYTES = 128 * 1024
MAX_DIRECTORIES = 2000
MAX_SKILLS = 200
MAX_RESOURCES = 100
MAX_DEPTH = 4
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
CHECK_ID = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
CHECK_KINDS = {"manual", "file_exists", "csv_schema"}
FRONTMATTER_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
STOP_WORDS = {"with", "when", "this", "that", "from", "into", "then", "only", "have", "must",
              "skill", "using", "used", "read", "file", "files", "task", "tasks", "the", "and",
              "для", "при", "или", "как", "это", "нужно", "после", "перед", "через"}


class SkillError(ValueError):
    def __init__(self, message, code="INVALID_SKILL", hint=None):
        super().__init__(message)
        self.code = code
        self.hint = hint


class _SafeLoader(yaml.SafeLoader):
    """Reject duplicate keys and aliases instead of silently changing metadata."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise SkillError("YAML aliases are not supported in skill metadata")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise SkillError("Skill metadata keys must be strings")
            if key in result:
                raise SkillError(f"Duplicate metadata key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _string(value, label, maximum, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise SkillError(f"{label} must be non-empty text of at most {maximum} characters")
    return value.strip()


def _strings(value, label, maximum=32, item_max=256):
    if not isinstance(value, list) or len(value) > maximum:
        raise SkillError(f"{label} must be an array with at most {maximum} items")
    result = [_string(item, label, item_max) for item in value]
    if len(result) != len(set(result)):
        raise SkillError(f"{label} must not contain duplicates")
    return result


def _within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_inside(path, root):
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as error:
        raise SkillError("Skill file path cannot be resolved", code="SKILL_PATH_DENIED") from error
    if not _within(resolved, root):
        raise SkillError("Skill path escapes its configured boundary", code="SKILL_PATH_DENIED")
    return resolved


def _bounded_bytes(path, root, limit, scope_root=None):
    # A package may be reached through a Windows junction, which is distinct from
    # a symlink. Never let resolving the package itself silently expand trust.
    package_root = _resolve_inside(root, scope_root) if scope_root is not None else root.resolve()
    resolved = _resolve_inside(path, package_root)
    if not resolved.is_file():
        raise SkillError("Skill resource is not a regular file", code="SKILL_RESOURCE_NOT_FOUND")
    with resolved.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise SkillError(f"Skill file exceeds {limit} bytes", code="SKILL_TOO_LARGE")
    return raw


def _decode(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SkillError("Skill resources must be UTF-8 text") from error
    if "\0" in text:
        raise SkillError("Skill resources must not contain NUL bytes")
    return text


def _frontmatter(text, directory_name):
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise SkillError("SKILL.md must start with YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise SkillError("SKILL.md frontmatter has no closing delimiter") from error
    header = "\n".join(lines[1:end])
    if len(header) > 16 * 1024:
        raise SkillError("Skill frontmatter is too large")
    try:
        metadata = yaml.load(header, Loader=_SafeLoader)
    except (yaml.YAMLError, RecursionError) as error:
        raise SkillError("Invalid skill YAML frontmatter") from error
    if not isinstance(metadata, dict):
        raise SkillError("Skill frontmatter must be a mapping")
    unknown = set(metadata) - FRONTMATTER_FIELDS
    if unknown:
        raise SkillError("Unknown skill frontmatter fields: " + ", ".join(sorted(unknown)))
    name = _string(metadata.get("name"), "name", 64)
    if not NAME.fullmatch(name) or name != directory_name:
        raise SkillError("Skill name must match its lowercase-hyphenated directory name")
    metadata["name"] = name
    metadata["description"] = _string(metadata.get("description"), "description", 1024)
    for key, maximum in (("license", 256), ("compatibility", 500), ("allowed-tools", 4096)):
        if key in metadata:
            metadata[key] = _string(metadata[key], key, maximum)
    extra = metadata.get("metadata", {})
    if not isinstance(extra, dict) or len(extra) > 32:
        raise SkillError("metadata must contain at most 32 string pairs")
    for key, value in extra.items():
        _string(key, "metadata key", 64)
        _string(value, "metadata value", 512)
    if not "\n".join(lines[end + 1:]).strip():
        raise SkillError("Skill instructions must not be empty")
    return metadata


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SkillError(f"Duplicate contract key: {key}")
        result[key] = value
    return result


def validate_contract(value):
    """Validate the optional harness extension; no field grants permissions."""
    if not isinstance(value, dict):
        raise SkillError("contract.json must contain an object")
    allowed = {"schema_version", "checks", "required_capabilities", "match_terms"}
    if set(value) - allowed:
        raise SkillError("Unknown contract fields: " + ", ".join(sorted(set(value) - allowed)))
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise SkillError("Unsupported skill contract schema_version; expected 1")
    checks = value.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 32:
        raise SkillError("checks must contain at most 32 checks")
    normalized, identifiers = [], set()
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"id", "title", "kind", "required", "allow_not_applicable"}:
            raise SkillError("Invalid check fields in contract.json")
        check_id = _string(check.get("id"), "check id", 64)
        if not CHECK_ID.fullmatch(check_id) or check_id in identifiers:
            raise SkillError("Check IDs must be unique lowercase words separated by hyphens or underscores")
        identifiers.add(check_id)
        title = _string(check.get("title"), "check title", 300)
        kind = check.get("kind")
        if not isinstance(kind, str) or kind not in CHECK_KINDS:
            raise SkillError("Check kind must be manual, file_exists or csv_schema")
        required = check.get("required", True)
        allow_na = check.get("allow_not_applicable", False)
        if type(required) is not bool or type(allow_na) is not bool:
            raise SkillError("Check required/allow_not_applicable must be booleans")
        if allow_na and kind != "manual":
            raise SkillError("Only manual checks may allow not_applicable")
        normalized.append({"id": check_id, "title": title, "kind": kind,
                           "required": required, "allow_not_applicable": allow_na})
    return {"schema_version": 1, "checks": normalized,
            "required_capabilities": _strings(value.get("required_capabilities", []), "required_capabilities"),
            "match_terms": _strings(value.get("match_terms", []), "match_terms", 64, 100)}


def _package_hash(skill_bytes, contract_bytes):
    digest = hashlib.sha256()
    for label, data in ((b"SKILL.md", skill_bytes), (b"contract.json", contract_bytes)):
        digest.update(label + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return digest.hexdigest()


def _tokens(value):
    return re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)


class SkillRegistry:
    """Discover configured roots; qualified IDs never become arbitrary file paths."""

    def __init__(self, builtin_root=None, user_roots=None):
        self.builtin_root = Path(builtin_root) if builtin_root is not None else Path(__file__).parent / "skillpacks"
        if user_roots is None:
            user_roots = [item for item in os.getenv("RDC_HARNESS_SKILL_DIRS", "").split(os.pathsep) if item]
        if not isinstance(user_roots, (list, tuple)) or len(user_roots) > 16:
            raise SkillError("Configure at most 16 user skill roots", code="INVALID_SKILL_CONFIG")
        self.user_roots = [Path(item).expanduser() for item in user_roots]

    def _roots(self, workspace, warnings):
        roots = []
        raw = os.getenv("RDC_HARNESS_TRUSTED_WORKSPACES", "[]")
        try:
            trusted = json.loads(raw)
            if not isinstance(trusted, list) or len(trusted) > 64 or any(not isinstance(x, str) or not x for x in trusted):
                raise ValueError()
        except (ValueError, TypeError) as error:
            raise SkillError("RDC_HARNESS_TRUSTED_WORKSPACES must be a JSON array of directory paths",
                             code="INVALID_SKILL_CONFIG") from error
        if workspace is not None:
            _string(workspace, "workspace", 4096)
            target = Path(workspace).expanduser().resolve()
            if target in {Path(item).expanduser().resolve() for item in trusted}:
                skill_root = target / ".agents" / "skills"
                if _within(skill_root.resolve(), target):
                    roots.append(("workspace", skill_root))
                else:
                    warnings.append({"code": "SKILL_PATH_DENIED", "message": "Workspace skills directory escapes the trusted workspace"})
            elif (target / ".agents" / "skills").exists():
                warnings.append({"code": "WORKSPACE_SKILLS_UNTRUSTED",
                                 "message": "Workspace skills skipped; configure trust locally before loading them"})
        roots.extend((f"user{index}", root) for index, root in enumerate(self.user_roots))
        roots.append(("builtin", self.builtin_root))
        return roots

    @staticmethod
    def _discover(root, warnings):
        """Bound directory traversal and do not recurse into symlinked directories."""
        if not root.exists():
            return []
        if not root.is_dir():
            warnings.append({"code": "INVALID_SKILL_ROOT", "message": "A configured skill root is not a directory"})
            return []
        root = root.resolve()
        packages, visited = [], 0
        for directory, children, filenames in os.walk(root, followlinks=False):
            visited += 1
            if visited > MAX_DIRECTORIES:
                warnings.append({"code": "SKILL_SCAN_LIMIT", "message": "Skill directory scan reached its bound"})
                break
            current = Path(directory)
            try:
                _resolve_inside(current, root)
            except SkillError:
                children[:] = []
                warnings.append({"code": "SKILL_PATH_DENIED", "message": "A skill directory escapes its configured root"})
                continue
            depth = len(current.relative_to(root).parts)
            safe_children = []
            for name in sorted(children) if depth < MAX_DEPTH else []:
                child = current / name
                if name in {".git", "node_modules", ".venv", "__pycache__"} or child.is_symlink():
                    continue
                try:
                    _resolve_inside(child, root)
                except SkillError:
                    warnings.append({"code": "SKILL_PATH_DENIED", "message": "A skill directory escapes its configured root"})
                    continue
                safe_children.append(name)
            # os.walk consumes this list after yielding. Prune escapes before it
            # enumerates a junction target, not merely after that directory yields.
            children[:] = safe_children
            if "SKILL.md" in filenames:
                packages.append(current)
                children[:] = []
            if len(packages) >= MAX_SKILLS:
                warnings.append({"code": "SKILL_SCAN_LIMIT", "message": "Skill package scan reached its bound"})
                break
        return packages

    def _scan(self, workspace=None):
        warnings, records, chosen = [], [], {}
        for source, configured_root in self._roots(workspace, warnings):
            root = configured_root.resolve()
            for package in self._discover(root, warnings):
                if len(records) >= MAX_SKILLS:
                    warnings.append({"code": "SKILL_SCAN_LIMIT", "message": "The registry reached its package bound"})
                    return records, warnings
                relative = package.relative_to(root).as_posix()
                skill_id = f"{source}:{relative if relative != '.' else package.name}"
                try:
                    # Returned IDs must fit task_prepare and qualified check IDs,
                    # including packages nested beneath long category names.
                    if len(skill_id) > 256:
                        raise SkillError("Source-qualified skill ID exceeds the 256-character interface limit",
                                         code="SKILL_ID_TOO_LONG")
                    _resolve_inside(package, root)
                    skill_bytes = _bounded_bytes(package / "SKILL.md", package, MAX_SKILL_BYTES, root)
                    metadata = _frontmatter(_decode(skill_bytes), package.name)
                    contract_path = package / "contract.json"
                    has_contract = contract_path.exists() or contract_path.is_symlink()
                    contract_bytes = _bounded_bytes(contract_path, package, MAX_CONTRACT_BYTES, root) if has_contract else b""
                    contract = validate_contract(json.loads(_decode(contract_bytes), object_pairs_hook=_json_pairs)) if has_contract else validate_contract({"schema_version": 1})
                    record = {"id": skill_id, "name": metadata["name"], "description": metadata["description"],
                              "source": source, "version": metadata.get("metadata", {}).get("version"),
                              "sha256": _package_hash(skill_bytes, contract_bytes),
                              "required_capabilities": contract["required_capabilities"],
                              "compatibility": metadata.get("compatibility"),
                              "active": metadata["name"] not in chosen,
                              "_package": package, "_root": root, "_content": _decode(skill_bytes), "_contract": contract}
                    if not record["active"]:
                        record["shadowed_by"] = chosen[record["name"]]
                        warnings.append({"code": "SKILL_SHADOWED", "skill_id": skill_id,
                                         "message": "Same-name skill is shadowed by " + record["shadowed_by"]})
                    else:
                        chosen[record["name"]] = skill_id
                    records.append(record)
                except (SkillError, OSError, ValueError, RecursionError) as error:
                    warnings.append({"code": getattr(error, "code", "INVALID_SKILL"), "skill_id": skill_id,
                                     "message": str(error)[:500]})
        return records, warnings

    @staticmethod
    def _metadata(record):
        return {key: value for key, value in record.items() if not key.startswith("_")}

    def list(self, workspace=None):
        records, warnings = self._scan(workspace)
        return {"skills": [self._metadata(record) for record in records], "warnings": warnings}

    @staticmethod
    def _find(records, skill_id):
        _string(skill_id, "skill_id", 512)
        matches = [item for item in records if item["id"] == skill_id]
        if not matches:
            matches = [item for item in records if item["name"] == skill_id and item["active"]]
        if not matches:
            raise SkillError(f"Unknown or unavailable skill: {skill_id}", code="SKILL_NOT_FOUND",
                             hint="List configured skills and use a returned ID")
        return matches[0]

    @staticmethod
    def _resources(package, scope_root=None):
        package_root = _resolve_inside(package, scope_root) if scope_root is not None else package.resolve()
        resources, visited = [], 0
        for directory, children, names in os.walk(package, followlinks=False):
            visited += 1
            if visited > MAX_DIRECTORIES:
                break
            current = Path(directory)
            try:
                _resolve_inside(current, package_root)
            except SkillError:
                children[:] = []
                continue
            safe_children = []
            depth = len(current.relative_to(package).parts)
            for name in sorted(children) if depth < MAX_DEPTH else []:
                child = current / name
                if name.startswith(".") or child.is_symlink():
                    continue
                try:
                    _resolve_inside(child, package_root)
                except SkillError:
                    continue
                safe_children.append(name)
            children[:] = safe_children
            for name in sorted(names):
                path = current / name
                relative = path.relative_to(package).as_posix()
                if relative in {"SKILL.md", "contract.json"} or name.startswith("."):
                    continue
                try:
                    resolved = _resolve_inside(path, package_root)
                except SkillError:
                    continue
                if resolved.is_file():
                    resources.append(relative)
                if len(resources) >= MAX_RESOURCES:
                    return resources
        return resources

    def read(self, skill_id, workspace=None, resource=None, expected_hash=None):
        records, warnings = self._scan(workspace)
        record = self._find(records, skill_id)
        if expected_hash is not None and expected_hash != record["sha256"]:
            raise SkillError("Skill content changed since it was selected", code="SKILL_CHANGED",
                             hint="Start a new task or explicitly select the updated skill; do not silently resume")
        result = self._metadata(record)
        result.update(content=record["_content"], contract=record["_contract"],
                      resources=self._resources(record["_package"], record["_root"]), warnings=warnings)
        if resource is not None:
            _string(resource, "resource", 512)
            path = PurePosixPath(resource)
            if (path.is_absolute() or PureWindowsPath(resource).is_absolute() or "\\" in resource
                    or ":" in resource or ".." in path.parts or resource.startswith("~")):
                raise SkillError("Resource must be a relative path inside its skill", code="SKILL_PATH_DENIED")
            raw = _bounded_bytes(record["_package"] / path, record["_package"], MAX_RESOURCE_BYTES, record["_root"])
            result.update(resource=path.as_posix(), content=_decode(raw), resource_sha256=hashlib.sha256(raw).hexdigest())
        return result

    @staticmethod
    def _score(record, goal_tokens):
        terms = record["_contract"]["match_terms"]
        if not terms:
            terms = sorted({word for word in _tokens(record["name"] + " " + record["description"])
                            if len(word) >= 4 and word not in STOP_WORDS})
        normalized = " " + " ".join(goal_tokens) + " "
        matches = []
        for term in terms:
            if term.endswith("*") and len(term[:-1]) >= 4:
                stem = term[:-1].casefold()
                matched = any(token.startswith(stem) for token in goal_tokens)
            else:
                phrase = " ".join(_tokens(term))
                matched = bool(phrase) and " " + phrase + " " in normalized
            if matched:
                matches.append(term)
        return len(matches), matches

    def resolve(self, goal, workspace=None, requested_skills=None, limit=3):
        _string(goal, "goal", 4000)
        if type(limit) is not int or not 1 <= limit <= 3:
            raise SkillError("limit must be an integer from 1 to 3")
        records, warnings = self._scan(workspace)
        if requested_skills is not None:
            requested = _strings(requested_skills, "requested_skills", 3, 512)
            selected = []
            for skill_id in requested:
                item = self._metadata(self._find(records, skill_id))
                if any(existing["id"] == item["id"] for existing in selected):
                    raise SkillError("The same skill was requested more than once")
                selected.append(dict(item, reason="Explicitly requested", score=None))
            return {"selected": selected, "candidates": selected, "warnings": warnings}
        candidates = []
        goal_tokens = _tokens(goal)
        for record in records:
            if not record["active"]:
                continue
            score, terms = self._score(record, goal_tokens)
            if score:
                candidates.append(dict(self._metadata(record), score=score,
                                       reason="Matched task terms: " + ", ".join(terms)))
        candidates.sort(key=lambda item: -item["score"])
        return {"selected": candidates[:limit], "candidates": candidates[:10], "warnings": warnings}
