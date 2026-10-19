"""Application-independent Windows discovery and semantic control.

All methods run on the agent's UIA apartment. References own their wrappers on
that thread; neither wrappers nor credentials are serialized. No application
names, screenshots, keyboard input or speculative write fallbacks are used.
"""
import copy
import hashlib
import json
import math
import os
import secrets
import subprocess
import time
from collections import OrderedDict
from pathlib import Path

import psutil
import win32gui
import win32process
from pywinauto import Desktop

import uia_support
from core_ext import ActionError

OPERATIONS = ("invoke", "set_value", "set_checked", "select", "expand", "collapse", "scroll_into_view")
STATE_KEYS = {"value", "checked", "selected", "expanded", "enabled", "visible"}


def bounded_int(value, default, low, high, name):
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ActionError(f"{name} must be an integer in {low}..{high}")
    return value


def bounded_time(value, default=10):
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 30:
        raise ActionError("timeout must be finite seconds in 0..30")
    return float(value)


def window_identity(hwnd):
    hwnd = bounded_int(hwnd, None, 1, 2**64 - 1, "hwnd")
    if not win32gui.IsWindow(hwnd):
        raise ActionError("window no longer exists", code="STALE_TARGET", action_started=False)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return {"hwnd": hwnd, "pid": pid, "created_at": psutil.Process(pid).create_time()}


def running_windows(query="", limit=100):
    items, warnings = [], []
    query = query.casefold()

    def visit(hwnd, unused):
        if len(items) >= limit or not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            identity = window_identity(int(hwnd))
            process = psutil.Process(identity["pid"])
            title = win32gui.GetWindowText(hwnd)[:500]
            name = process.name()
            if query and query not in (title + " " + name).casefold():
                return True
            items.append({**identity, "title": title, "process": name,
                          "class_name": win32gui.GetClassName(hwnd), "backend_candidates": ["uia", "win32"]})
        except Exception:
            warnings.append("a window could not be inspected")
        return True

    win32gui.EnumWindows(visit, None)
    return {"items": items, "truncated": len(items) >= limit, "warnings": sorted(set(warnings))}


def registered_apps(query="", limit=100):
    """Read both App Paths registry views without launching or probing programs."""
    import winreg
    items, seen, warnings = [], set(), []
    subkey = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
    for hive, source in ((winreg.HKEY_CURRENT_USER, "user"), (winreg.HKEY_LOCAL_MACHINE, "machine")):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                root = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with root:
                index = 0
                while len(items) < limit:
                    try:
                        name = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(root, name) as key:
                            value, value_type = winreg.QueryValueEx(key, "")
                        if value_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
                            continue
                        executable = os.path.expandvars(value).strip().strip('"')
                        if query and query.casefold() not in (name + " " + executable).casefold():
                            continue
                        normalized = os.path.normcase(executable)
                        if normalized in seen:
                            continue
                        seen.add(normalized)
                        items.append({"name": name, "executable": executable, "source": "app_paths:" + source,
                                      "exists": Path(executable).is_file()})
                    except OSError:
                        warnings.append("a registration could not be read")
    return {"items": items, "truncated": len(items) >= limit, "warnings": sorted(set(warnings)),
            "coverage": "App Paths only; portable programs and unregistered/store apps may be absent"}


def _interface(ctrl, name):
    try:
        return getattr(ctrl, name)
    except Exception:
        return None


def _runtime_identity(ctrl, backend):
    if backend == "uia":
        # A provider which cannot identify its element must not receive writes.
        runtime_id = tuple(ctrl.element_info.runtime_id)
        if not runtime_id:
            raise ValueError("UIA provider returned no runtime ID")
        return runtime_id
    return (int(ctrl.handle), ctrl.class_name(), int(ctrl.control_id()))


def describe_control(ctrl, backend, max_text):
    """Normalize supported actions and inspectable state, not application names."""
    if backend == "uia":
        def metadata(w, redact_text=False, use_name=False):
            info = w.element_info
            return {"title": "[redacted]" if redact_text else str(info.name)[:max_text],
                    "control_type": info.control_type, "automation_id": info.automation_id,
                    "class_name": info.class_name, "enabled": bool(w.is_enabled()), "visible": bool(w.is_visible())}
        inspected = uia_support.inspect_control(ctrl, metadata, max_text=max_text)
        state = {key: inspected["control"][key] for key in ("enabled", "visible") if key in inspected["control"]}
        patterns = inspected["patterns"]
        operations = []
        for operation, iface in (("invoke", "iface_invoke"), ("scroll_into_view", "iface_scroll_item")):
            if _interface(ctrl, iface) is not None:
                operations.append(operation)
        for name, operation, key, field in (
            ("value", "set_value", "value", "value"),
            ("range_value", "set_value", "value", "value"),
            ("toggle", "set_checked", "checked", "state"),
            ("selection_item", "select", "selected", "selected"),
            ("expand_collapse", "expand", "expanded", "state"),
        ):
            pattern = patterns.get(name, {})
            if pattern.get("supported") is not True:
                continue
            writable = pattern.get("read_only") is False if name in {"value", "range_value"} else True
            if writable and not pattern.get("redacted") and operation not in operations:
                operations.append(operation)
                if operation == "expand":
                    operations.append("collapse")
            if field in pattern:
                value = pattern[field]
                if key == "checked":
                    value = {0: False, 1: True}.get(value)
                elif key == "expanded":
                    value = {0: False, 1: True}.get(value)
                state[key] = value
                if key == "value":
                    state["value_truncated"] = bool(pattern.get("value_truncated", False))
        if inspected.get("password") is not False:
            # No password value reads or writes through this generic path.
            operations = []
        return {**inspected["control"], "state": state, "operations": operations,
                "password": inspected.get("password"), "patterns": patterns}

    kind = ctrl.friendly_class_name()
    password = kind == "Edit" and bool(ctrl.style() & 0x20)
    state = {"enabled": bool(ctrl.is_enabled()), "visible": bool(ctrl.is_visible())}
    # Win32 has no universal password indicator. Text reads are restricted to
    # standard control classes with known contracts; unknown/custom types stay opaque.
    known = kind in {"Edit", "Button", "CheckBox", "RadioButton", "ComboBox", "Static", "Dialog"}
    name = ctrl.window_text() if known and not password and max_text else ""
    operations = []
    if not password and kind == "Edit":
        state.update(value=name[:max_text], value_truncated=len(name) > max_text)
        if not ctrl.style() & 0x800:  # ES_READONLY
            operations.append("set_value")
    elif kind == "Button":
        operations.append("invoke")
    elif kind == "CheckBox":
        state["checked"] = {0: False, 1: True}.get(ctrl.get_check_state())
        operations.append("set_checked")
    elif kind == "RadioButton":
        state["selected"] = ctrl.get_check_state() == 1
        operations.append("select")
    return {"title": "[redacted]" if password else name[:max_text], "control_type": kind,
            "class_name": ctrl.class_name(), "state": state, "operations": operations, "password": password,
            "limitations": ["native HWND identity is best-effort; re-observe after UI replacement"]}


class AppController:
    def __init__(self, desktop_factory=Desktop, identity=window_identity, clock=time.monotonic):
        self.desktop_factory, self.identity, self.clock = desktop_factory, identity, clock
        self.refs = OrderedDict()
        self.receipts = OrderedDict()
        self.ttl = 120

    def _prune(self):
        now = self.clock()
        for cache in (self.refs, self.receipts):
            for key in list(cache):
                if cache[key]["expires"] <= now:
                    del cache[key]
        while len(self.refs) > 1600:
            self.refs.popitem(last=False)

    def catalog(self, a):
        limit = bounded_int(a.get("limit"), 100, 1, 300, "limit")
        query = a.get("query", "")
        if not isinstance(query, str) or len(query) > 500:
            raise ActionError("query must be a string of at most 500 characters")
        return {"running": running_windows(query, limit), "registered": registered_apps(query, limit)}

    def open(self, a):
        if "dry_run" in a and not isinstance(a["dry_run"], bool):
            raise ActionError("dry_run must be a boolean", action_started=False)
        executable = a.get("executable")
        args = a.get("args", [])
        if not isinstance(executable, str) or not Path(executable).is_absolute() or not Path(executable).is_file():
            raise ActionError("an existing absolute executable path is required; use app_catalog first")
        if Path(executable).suffix.lower() != ".exe":
            raise ActionError("app_open accepts .exe only; scripts use the explicit exec/python/powershell tools")
        if not isinstance(args, list) or len(args) > 100 or any(not isinstance(v, str) or '\x00' in v for v in args):
            raise ActionError("args must be an array of at most 100 strings")
        timeout = bounded_time(a.get("timeout"), default=0)
        cwd = a.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir()):
            raise ActionError("cwd must be an existing absolute directory")
        if a.get("dry_run") is True:
            return {"dry_run": True, "executable": executable, "argument_count": len(args), "shell": False}
        process = subprocess.Popen([executable, *args], cwd=cwd, shell=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = self.clock() + timeout
        candidates = []
        while True:
            # Only this process and its observed descendants, never all same-name apps.
            pids = {process.pid}
            try:
                pids.update(p.pid for p in psutil.Process(process.pid).children(recursive=True))
            except psutil.Error:
                pass
            candidates = [w for w in running_windows(limit=300)["items"] if w["pid"] in pids]
            if candidates or self.clock() >= deadline or process.poll() is not None:
                break
            time.sleep(0.1)
        return {"pid": process.pid, "launched": True, "window_observed": bool(candidates), "windows": candidates,
                "exit_code": process.poll(), "application_ready": None,
                "hint": "A visible window is not proof of document readiness. Observe and wait for a control state; handoff launchers may require app_catalog."}

    def observe(self, a):
        self._prune()
        backend = a.get("backend", "auto")
        if backend not in {"auto", "uia", "win32"}:
            raise ActionError("backend must be auto, uia or win32")
        target = self.identity(a.get("hwnd"))
        if a.get("pid") is not None and a["pid"] != target["pid"]:
            raise ActionError("window belongs to a different process", code="STALE_TARGET", action_started=False)
        depth = bounded_int(a.get("depth"), 4, 0, 8, "depth")
        limit = bounded_int(a.get("limit"), 150, 1, 300, "limit")
        max_text = bounded_int(a.get("max_text"), 300, 0, 4000, "max_text")
        snapshots, diagnostics = [], []
        for candidate in (["uia", "win32"] if backend == "auto" else [backend]):
            try:
                root = self.desktop_factory(backend=candidate).window(handle=target["hwnd"]).wrapper_object()
                pending, nodes = [(root, 0)], []
                visited, cut = 0, False
                while pending and visited < limit:
                    ctrl, level = pending.pop(0)
                    visited += 1
                    try:
                        info = describe_control(ctrl, candidate, max_text)
                        identity = _runtime_identity(ctrl, candidate)
                        nodes.append((ctrl, identity, {**info, "depth": level}))
                    except Exception as error:
                        diagnostics.append({"backend": candidate, "error": type(error).__name__})
                    if level < depth:
                        try:
                            children = ctrl.children()
                            room = max(0, limit - visited - len(pending))
                            cut = cut or len(children) > room
                            pending.extend((child, level + 1) for child in children[:room])
                        except Exception as error:
                            diagnostics.append({"backend": candidate, "error": type(error).__name__})
                score = sum(bool(node[2]["operations"]) for node in nodes)
                snapshots.append((score, candidate, nodes, cut or bool(pending) or visited >= limit))
                if backend == "auto" and candidate == "uia" and score > 0:
                    break
            except Exception as error:
                diagnostics.append({"backend": candidate, "error": type(error).__name__})
        if not snapshots:
            raise ActionError("no readable accessibility backend", diagnostics=diagnostics,
                              hint="check app privilege level; use desktop_look or a documented CLI/CDP/API", action_started=False)
        score, chosen, nodes, truncated = max(snapshots, key=lambda x: x[0])
        if self.identity(target["hwnd"]) != target:
            raise ActionError("window changed during observation", code="STALE_TARGET", action_started=False)
        items = []
        for ctrl, identity, info in nodes:
            ref = "control-" + secrets.token_hex(12)
            self.refs[ref] = {"ctrl": ctrl, "identity": identity, "target": target, "backend": chosen,
                              "expires": self.clock() + self.ttl}
            items.append({"ref": ref, **info})
        self._prune()
        return {"target": target, "backend": chosen, "items": items, "truncated": truncated,
                "ref_ttl_seconds": self.ttl, "actionable_controls": score, "diagnostics": diagnostics[:20],
                "next": "app_act with ref + operation; app_wait verifies state. No application-specific plugin required.",
                "fallback": None if score else "No semantic operations exposed. Try explicit win32, DOM/CDP, CLI/API or desktop_look; no automatic physical input."}

    def _resolve(self, ref):
        self._prune()
        if not isinstance(ref, str) or ref not in self.refs:
            raise ActionError("unknown, expired or evicted control reference", code="STALE_TARGET", action_started=False,
                              hint="call app_observe again; refs are local to one agent process")
        record = self.refs[ref]
        try:
            valid = self.identity(record["target"]["hwnd"]) == record["target"]
            valid = valid and _runtime_identity(record["ctrl"], record["backend"]) == record["identity"]
            if not valid:
                raise ValueError("identity changed")
        except Exception:
            self.refs.pop(ref, None)
            raise ActionError("window or control identity changed", code="STALE_TARGET", action_started=False) from None
        return record

    @staticmethod
    def _expected(value):
        if not isinstance(value, dict) or not value or set(value) - STATE_KEYS:
            raise ActionError("expected must be a non-empty object containing value/checked/selected/expanded/enabled/visible")
        for key, item in value.items():
            if key == "value":
                if isinstance(item, bool) or not isinstance(item, (str, int, float)) or (isinstance(item, (int, float)) and not math.isfinite(item)):
                    raise ActionError("expected.value must be text or a finite number")
            elif not isinstance(item, bool):
                raise ActionError("expected states other than value must be booleans")
        return value

    @staticmethod
    def _matches(state, expected):
        return all(key in state and state[key] == value and not (key == "value" and state.get("value_truncated"))
                   for key, value in expected.items())

    def wait(self, a):
        expected = self._expected(a.get("expected"))
        timeout = bounded_time(a.get("timeout"))
        deadline = self.clock() + timeout
        while True:
            record = self._resolve(a.get("ref"))
            state = describe_control(record["ctrl"], record["backend"], 4000)["state"]
            if self._matches(state, expected):
                return {"matched": True, "state": state}
            if self.clock() >= deadline:
                return {"matched": False, "timed_out": True, "state": state}
            time.sleep(min(0.1, max(0, deadline - self.clock())))

    def act(self, a):
        if "dry_run" in a and not isinstance(a["dry_run"], bool):
            raise ActionError("dry_run must be a boolean", action_started=False)
        self._prune()
        operation = a.get("operation")
        if operation not in OPERATIONS:
            raise ActionError("unsupported operation", operations=list(OPERATIONS), action_started=False)
        if operation == "set_value":
            value = a.get("value")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)) or (isinstance(value, (int, float)) and not math.isfinite(value)):
                raise ActionError("set_value requires text or a finite number", action_started=False)
            if isinstance(value, str) and (len(value) > 4000 or '\x00' in value):
                raise ActionError("value must contain at most 4000 characters and no NUL", action_started=False)
        if operation == "set_checked" and not isinstance(a.get("checked"), bool):
            raise ActionError("set_checked requires checked: true or false", action_started=False)
        expected = self._expected(a["expected"]) if "expected" in a else None
        request_id = a.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ActionError("request_id is required (1..128 characters); reuse it only to retrieve the same attempt")
        fingerprint = hashlib.sha256(json.dumps({k: a[k] for k in ("ref", "operation", "value", "checked", "expected") if k in a},
                                                sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        previous = self.receipts.get(request_id)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ActionError("request_id was already used with different arguments", code="REQUEST_CONFLICT", action_started=False)
            return {**copy.deepcopy(previous["result"]), "replayed": True,
                    "receipt_ttl_seconds": max(0, int(previous["expires"] - self.clock()))}
        record = self._resolve(a.get("ref"))
        ctrl, backend = record["ctrl"], record["backend"]
        before = describe_control(ctrl, backend, 4000)
        if operation not in before["operations"] or not before["state"].get("enabled", False):
            raise ActionError("operation unavailable or control disabled", available_operations=before["operations"], action_started=False)
        if expected and not self._matches(before["state"], expected):
            raise ActionError("control state changed; expected precondition does not hold", code="PRECONDITION_FAILED",
                              state=before["state"], action_started=False)
        if operation == "set_value":
            if backend == "win32" and not isinstance(a["value"], str):
                raise ActionError("Win32 Edit requires a text value", action_started=False)
            if backend == "uia":
                pattern = before.get("patterns", {}).get("value" if isinstance(a["value"], str) else "range_value", {})
                if pattern.get("supported") is not True or pattern.get("read_only") is not False:
                    raise ActionError("control does not expose a writable pattern for this value type", action_started=False)
                if not isinstance(a["value"], str) and (a["value"] < pattern.get("minimum", -math.inf) or a["value"] > pattern.get("maximum", math.inf)):
                    raise ActionError("numeric value is outside the supported range", action_started=False)
        if a.get("dry_run") is True:
            return {"dry_run": True, "backend": backend, "operation": operation, "before": before["state"], "action_started": False}
        # Generic invoke may submit, delete or save. The caller, not a keyword
        # classifier, explicitly authorizes it. This is not an authorization sandbox.
        if operation == "invoke" and a.get("commit") is not True:
            raise PermissionError("invoke requires commit=true after authorization for the intended action")
        if len(self.receipts) >= 256:
            raise ActionError("receipt cache is full; wait for existing receipts to expire", code="RECEIPT_CAPACITY", action_started=False)
        desired = ({"value": a["value"]} if operation == "set_value" else
                   {"checked": a["checked"]} if operation == "set_checked" else
                   {"selected": True} if operation == "select" else
                   {"expanded": operation == "expand"} if operation in {"expand", "collapse"} else None)
        result = {"request_id": request_id, "operation": operation, "backend": backend,
                  "before": before["state"], "action_started": False, "verified": False}
        # Store the attempt before calling the provider. Failures after invocation
        # have an uncertain outcome and must not trigger a fallback or a second write.
        self.receipts[request_id] = {"expires": math.inf, "fingerprint": fingerprint, "result": result}
        try:
            if desired and self._matches(before["state"], desired):
                result.update(outcome="already_satisfied", verified=True, after=before["state"])
            else:
                result["action_started"] = True
                self._perform(ctrl, backend, operation, a)
                result["outcome"] = "dispatched"
                try:
                    self._resolve(a["ref"])
                    after = describe_control(ctrl, backend, 4000)["state"]
                    result.update(after=after, verified=self._matches(after, desired) if desired else None)
                    if desired and not result["verified"]:
                        result["outcome"] = "verification_pending"
                except Exception as error:
                    result.update(verified=None, verification_error=type(error).__name__)
        except Exception as error:
            result.update(outcome="unknown", verified=None, error=type(error).__name__,
                          hint="Provider may have changed state before failing. Re-observe; do not blindly repeat the write.")
        self.receipts[request_id]["expires"] = self.clock() + self.ttl
        result["receipt_ttl_seconds"] = self.ttl
        self._prune()
        return copy.deepcopy(result)

    @staticmethod
    def _perform(ctrl, backend, operation, a):
        if backend == "win32":
            if operation == "set_value":
                if not isinstance(a["value"], str):
                    raise ValueError("Win32 Edit requires a string")
                ctrl.set_edit_text(a["value"])
            elif operation == "set_checked":
                # click sends window messages; click_input (real mouse) is never used.
                ctrl.click()
            elif operation in {"invoke", "select"}:
                ctrl.click()
            return
        if operation == "invoke":
            ctrl.iface_invoke.Invoke()
        elif operation == "set_value":
            if isinstance(a["value"], str):
                ctrl.iface_value.SetValue(a["value"])
            else:
                ctrl.iface_range_value.SetValue(float(a["value"]))
        elif operation == "set_checked":
            # A single transition, then verification. Three-state toggles may need
            # another deliberately chosen action; never guess a sequence of clicks.
            ctrl.iface_toggle.Toggle()
        elif operation == "select":
            ctrl.iface_selection_item.Select()
        elif operation == "expand":
            ctrl.iface_expand_collapse.Expand()
        elif operation == "collapse":
            ctrl.iface_expand_collapse.Collapse()
        elif operation == "scroll_into_view":
            ctrl.iface_scroll_item.ScrollIntoView()
