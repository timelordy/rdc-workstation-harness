import ctypes
import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import mss
import mss.tools
import psutil
import pyautogui
import pyperclip
import win32gui
import win32process
import win32com.client
import catalog
import core_ext
from core_ext import ActionError
from pywinauto import Desktop
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.keyboard import send_keys

HOST = "127.0.0.1"  # Deliberately not configurable: never expose this service directly.
PORT = int(os.getenv("DESKTOP_AGENT_PORT", "17322"))
BROWSER_PORT = int(os.getenv("PW_BRIDGE_PORT", "17321"))
BROWSER_URL = os.getenv("BROWSER_BRIDGE_URL", f"http://127.0.0.1:{BROWSER_PORT}/action")
HOME = Path(os.getenv("USERPROFILE", str(Path.home())))
ARTIFACT_DIR = Path(os.getenv("DESKTOP_AGENT_ARTIFACT_DIR", str(HOME / "Downloads" / "rdc-harness")))
ADAPTER_DIR = Path(__file__).resolve().parent / "adapters"
TOKEN_DIR = Path(os.getenv("RDC_HARNESS_TOKEN_DIR", str(HOME / ".chatgpt-desktop-agent")))
TOKEN_PATH = TOKEN_DIR / "desktop.token"
BROWSER_TOKEN_PATH = TOKEN_DIR / "browser.token"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
ADAPTER_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_DIR.mkdir(parents=True, exist_ok=True)
if TOKEN_PATH.exists():
    AGENT_TOKEN = TOKEN_PATH.read_text(encoding="utf-8").strip()
else:
    AGENT_TOKEN = secrets.token_hex(32)
    TOKEN_PATH.write_text(AGENT_TOKEN, encoding="utf-8")
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05
PHYSICAL_INPUT_DEFAULT = False
BROWSER_AUDIT_LOG = Path(os.getenv(
    "RDC_HARNESS_AUDIT_LOG",
    str(HOME / ".rdc-workstation-harness" / "logs" / "browser-actions.ndjson"),
))


def rect_dict(rect):
    return {
        "left": rect.left,
        "top": rect.top,
        "right": rect.right,
        "bottom": rect.bottom,
        "width": rect.width(),
        "height": rect.height(),
    }


def active_window_info():
    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    rect = win32gui.GetWindowRect(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        proc = psutil.Process(pid)
        name = proc.name()
    except Exception:
        name = None
    return {
        "hwnd": int(hwnd),
        "title": title,
        "pid": pid,
        "process": name,
        "rect": {
            "left": rect[0], "top": rect[1],
            "right": rect[2], "bottom": rect[3],
        },
    }
def window_info(w):
    try:
        info = w.element_info
        return {
            "title": w.window_text(),
            "control_type": info.control_type,
            "automation_id": info.automation_id,
            "class_name": info.class_name,
            "pid": info.process_id,
            "handle": getattr(info, "handle", None),
            "visible": w.is_visible(),
            "enabled": w.is_enabled(),
            "rect": rect_dict(w.rectangle()),
        }
    except Exception as e:
        return {"error": str(e)}


def selector_kwargs(a):
    out = {}
    if a.get("title") is not None:
        out["title"] = a["title"]
    if a.get("title_re") is not None:
        out["title_re"] = a["title_re"]
    if a.get("control_type") is not None:
        out["control_type"] = a["control_type"]
    if a.get("automation_id") is not None:
        out["auto_id"] = a["automation_id"]
    if a.get("class_name") is not None:
        out["class_name"] = a["class_name"]
    if a.get("pid") is not None:
        out["process"] = int(a["pid"])
    if a.get("index") is not None:
        out["found_index"] = int(a["index"])
    return out
def find_uia(a):
    try:
        return _find_uia(a)
    except ElementNotFoundError:
        raise ActionError(
            "no window/control matches the selector",
            hint="list candidates with windows {title_re} or uia_find, then retry",
            selector={k: a[k] for k in ("window", "control") if a.get(k)} or selector_kwargs(a),
        ) from None


def _find_uia(a):
    desktop = Desktop(backend="uia")
    if a.get("window"):
        window_criteria = selector_kwargs(a["window"])
        if not window_criteria:
            raise ActionError("window selector required", hint=catalog.SELECTOR)
        spec = desktop.window(**window_criteria)
        if a.get("control"):
            control_criteria = selector_kwargs(a["control"])
            if not control_criteria:
                raise ActionError("control selector required", hint=catalog.SELECTOR)
            spec = spec.child_window(**control_criteria)
        return spec.wrapper_object()

    criteria = selector_kwargs(a)
    if not criteria:
        raise ActionError("UIA selector required", hint="pass window={title_re:...} or top-level selector fields")
    return desktop.window(**criteria).wrapper_object()


def save_screenshot(a):
    ts = int(time.time() * 1000)
    path = Path(a.get("path") or ARTIFACT_DIR / f"desktop-{ts}.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        monitor_index = int(a.get("monitor", 0))
        monitor = sct.monitors[monitor_index]
        if a.get("region"):
            region = a["region"]
            monitor = {
                "left": int(region["left"]),
                "top": int(region["top"]),
                "width": int(region["width"]),
                "height": int(region["height"]),
            }
        image = sct.grab(monitor)
        mss.tools.to_png(image.rgb, image.size, output=str(path))
        return {
            "path": str(path),
            "width": image.width,
            "height": image.height,
            "left": monitor["left"],
            "top": monitor["top"],
        }
def forward_browser(payload):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if BROWSER_TOKEN_PATH.exists():
        headers["X-Desktop-Agent-Token"] = BROWSER_TOKEN_PATH.read_text(
            encoding="utf-8"
        ).strip()
    req = urllib.request.Request(
        BROWSER_URL,
        data=data,
        headers=headers,
        method="POST",
    )
    # Browser timeouts are in ms; wait a bit longer than the bridge itself.
    try:
        wait = max(60.0, float(payload.get("timeout") or 0) / 1000 + 15)
    except (TypeError, ValueError):
        wait = 60.0
    try:
        with urllib.request.urlopen(req, timeout=wait) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # The bridge reports its own errors as JSON with a 4xx/5xx status.
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise e
        raise ActionError(
            "browser: " + str(body.get("error") or e),
            hint="describe {browser: '<action>'} for parameters",
        ) from None


def audit_browser(event):
    try:
        BROWSER_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with BROWSER_AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def do_browser(a):
    payload = dict(a.get("payload") or {})
    if "action" not in payload:
        payload["action"] = a.get("browser_action")
    if payload.get("action") not in catalog.BROWSER_ACTIONS:
        raise ActionError(
            f"unknown browser action: {payload.get('action')}",
            hint="describe {browser: '<action>'}",
            did_you_mean=catalog.suggest(payload.get("action"), catalog.BROWSER_ACTIONS),
            valid_actions=sorted(catalog.BROWSER_ACTIONS),
        )
    side_effect = catalog.browser_side_effect(payload)
    if side_effect and not a.get("commit"):
        audit_browser({"status": "blocked", "side_effect": True, "payload": payload})
        raise PermissionError(
            "externally visible browser action blocked (send/pay/delete/...). "
            "Ask the user to confirm, then repeat with commit=true."
        )
    result = forward_browser(payload)
    if side_effect:
        audit_browser({"status": "executed", "side_effect": True, "payload": payload})
    return result


def list_windows(a):
    limit = min(int(a.get("limit", 100)), 300)
    title_re = a.get("title_re")
    items = []
    for w in Desktop(backend="uia").windows():
        item = window_info(w)
        if title_re and not re.search(title_re, item.get("title", ""), re.I):
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items


def uia_tree(a):
    root = find_uia(a)
    depth = max(0, min(int(a.get("depth", 3)), 8))
    limit = max(1, min(int(a.get("limit", 200)), 1000))
    items = []
    def walk(ctrl, level, path):
        if len(items) >= limit:
            return
        item = window_info(ctrl)
        item["depth"] = level
        item["path"] = path
        items.append(item)
        if level >= depth:
            return
        try:
            children = ctrl.children()
        except Exception:
            return
        for i, child in enumerate(children):
            walk(child, level + 1, path + [i])
            if len(items) >= limit:
                return

    walk(root, 0, [])
    return {
        "root": window_info(root),
        "items": items,
        "truncated": len(items) >= limit,
    }


def do_uia_click(a):
    ctrl = find_uia(a)
    method = a.get("method", "auto")
    if method in ("auto", "invoke"):
        try:
            ctrl.invoke()
            return {"method": "invoke", "control": window_info(ctrl)}
        except Exception:
            if method == "invoke":
                raise
    require_physical(a)
    ctrl.click_input(button=a.get("button", "left"))
    return {"method": "click_input", "control": window_info(ctrl)}
def do_uia_set_text(a):
    ctrl = find_uia(a)
    value = str(a.get("value", ""))
    try:
        ctrl.set_edit_text(value)
        return {"method": "set_edit_text", "control": window_info(ctrl)}
    except Exception:
        require_physical(a)
        ctrl.set_focus()
        pyperclip.copy(value)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.hotkey("ctrl", "v")
        return {"method": "clipboard_paste", "control": window_info(ctrl)}


def do_uia_type_input(a):
    if a.get("window"):
        root = Desktop(backend="uia").window(**selector_kwargs(a["window"])).wrapper_object()
        try:
            root.restore()
        except Exception:
            pass
        root.set_focus()
        time.sleep(float(a.get("window_focus_delay", 0.2)))
    ctrl = find_uia(a)
    rect = ctrl.rectangle()
    x = int((rect.left + rect.right) / 2)
    y = int((rect.top + rect.bottom) / 2)
    pyautogui.click(x=x, y=y)
    time.sleep(float(a.get("focus_delay", 0.15)))
    if a.get("replace", True):
        send_keys("^a", pause=0.03)
    text = str(a.get("value", ""))
    pyperclip.copy(text)
    send_keys("^v", pause=0.03)
    return {"method": "physical_click_sendkeys_paste", "chars": len(text), "x": x, "y": y}


def do_window_op(a):
    ctrl = find_uia(a)
    op = a.get("op", "focus")
    if op == "focus":
        ctrl.set_focus()
    elif op == "minimize":
        ctrl.minimize()
    elif op == "maximize":
        ctrl.maximize()
    elif op == "restore":
        ctrl.restore()
    elif op == "close":
        ctrl.close()
    else:
        raise ActionError(f"Unknown window op: {op}", hint="focus | minimize | maximize | restore | close")
    return {"op": op, "control": window_info(ctrl)}
def require_physical(a):
    if not bool(a.get("allow_physical", PHYSICAL_INPUT_DEFAULT)):
        raise PermissionError(
            "Physical mouse/keyboard input is disabled by default. "
            "Use a semantic/background action, or set allow_physical=true explicitly."
        )


def uia_call(a):
    ctrl = find_uia(a)
    method_name = a.get("method")
    if not method_name or method_name.startswith("_"):
        raise ActionError("public method required")
    method = getattr(ctrl, method_name, None)
    if not callable(method):
        raise ActionError(f"UIA control has no callable method: {method_name}")
    result = method(*(a.get("args") or []), **(a.get("kwargs") or {}))
    return {
        "method": method_name,
        "result": core_ext.json_safe(result),
        "control": window_info(ctrl),
    }


def selftest(a):
    checks = {}

    try:
        browser_check = forward_browser({"action": "status"})
        checks["browser"] = {
            "ok": bool(browser_check.get("ok", False)),
            "running": browser_check.get("running"),
            "pages": browser_check.get("pages"),
        }
    except Exception as e:
        checks["browser"] = {"ok": False, "error": str(e)}

    try:
        windows = list_windows({"limit": 20})
        checks["uia"] = {
            "ok": True,
            "window_count_sample": len(windows),
        }
    except Exception as e:
        checks["uia"] = {"ok": False, "error": str(e)}

    try:
        d = win32com.client.Dispatch("Scripting.Dictionary")
        d.Add("desktop_agent", 1)
        checks["com"] = {"ok": d.Count == 1}
    except Exception as e:
        checks["com"] = {"ok": False, "error": str(e)}

    try:
        adapters = core_ext.list_adapters()
        failed = [item["name"] for item in adapters if item.get("load_error")]
        checks["adapters"] = {
            "ok": not failed,
            "names": [item["name"] for item in adapters],
            "load_errors": failed,
            # Informational: these still work, they just don't describe themselves.
            "without_manifest": [item["name"] for item in adapters
                                 if not item.get("manifest") and not item.get("load_error")],
        }
    except Exception as e:
        checks["adapters"] = {"ok": False, "error": str(e)}

    try:
        source = ARTIFACT_DIR / "selftest-file-handoff.txt"
        source.write_text("desktop-agent file handoff selftest\n", encoding="utf-8")
        staged = core_ext.stage_file({
            "path": str(source),
            "name": "selftest-file-handoff.txt",
            "max_bytes": 1024 * 1024,
        })
        staged_path = Path(staged["path"])
        ok = (
            staged_path.exists()
            and staged_path.read_text(encoding="utf-8")
            == "desktop-agent file handoff selftest\n"
            and staged.get("mime_type") == "text/plain"
        )
        checks["file_handoff"] = {
            "ok": ok,
            "mime_type": staged.get("mime_type"),
            "size": staged.get("size"),
        }
        source.unlink(missing_ok=True)
        staged_path.unlink(missing_ok=True)
    except Exception as e:
        checks["file_handoff"] = {"ok": False, "error": str(e)}

    checks["security"] = {
        "ok": TOKEN_PATH.exists() and BROWSER_TOKEN_PATH.exists() and core_ext.is_protected(TOKEN_PATH),
        "desktop_token": TOKEN_PATH.exists(),
        "browser_token": BROWSER_TOKEN_PATH.exists(),
        "tokens_blocked_from_handoff": core_ext.is_protected(TOKEN_PATH),
        "localhost_only": HOST == "127.0.0.1",
    }
    checks["physical_input"] = {
        "ok": PHYSICAL_INPUT_DEFAULT is False,
        "enabled_by_default": PHYSICAL_INPUT_DEFAULT,
    }

    overall = all(bool(v.get("ok")) for v in checks.values())
    return {"ok": overall, "checks": checks}


def status(a):
    try:
        browser = forward_browser({"action": "status"})
    except Exception as e:
        browser = {"ok": False, "error": str(e)}
    return {
        "ready": True,
        "pid": os.getpid(),
        "port": PORT,
        "admin": bool(ctypes.windll.shell32.IsUserAnAdmin()),
        "screen": list(pyautogui.size()),
        "cursor": list(pyautogui.position()),
        "active_window": active_window_info(),
        "browser": browser,
    }


def screen_info(a):
    with mss.mss() as sct:
        return {
            "monitors": [dict(m) for m in sct.monitors],
            "size": list(pyautogui.size()),
        }


def cursor(a):
    pos = pyautogui.position()
    return {"x": pos.x, "y": pos.y}


def uia_wait(a):
    ctrl = find_uia(a)
    state = a.get("state", "exists enabled visible ready")
    ctrl.wait(state, timeout=float(a.get("timeout", 10)))
    return {"ok": True, "control": window_info(ctrl)}


def uia_find(a):
    root_args = {"window": a.get("window")} if a.get("window") else a
    root = find_uia(root_args)
    criteria = selector_kwargs(a.get("control") or {})
    if not criteria:
        raise ActionError("control selector required")
    criteria.pop("found_index", None)
    limit = max(1, min(int(a.get("limit", 50)), 300))
    matches = root.descendants(**criteria)
    return {
        "items": [window_info(x) for x in matches[:limit]],
        "count": len(matches),
        "truncated": len(matches) > limit,
    }


def physical_move(a):
    pyautogui.moveTo(int(a["x"]), int(a["y"]), duration=float(a.get("duration", 0.15)))
    return {"ok": True, "cursor": list(pyautogui.position())}


def physical_click(a):
    pyautogui.click(
        x=int(a["x"]), y=int(a["y"]),
        clicks=int(a.get("clicks", 1)),
        interval=float(a.get("interval", 0.1)),
        button=a.get("button", "left"),
    )
    return {"ok": True}


def physical_drag(a):
    pyautogui.moveTo(int(a["from_x"]), int(a["from_y"]))
    pyautogui.dragTo(
        int(a["to_x"]), int(a["to_y"]),
        duration=float(a.get("duration", 0.5)),
        button=a.get("button", "left"),
    )
    return {"ok": True}


def physical_scroll(a):
    pyautogui.scroll(int(a.get("clicks", 0)), x=a.get("x"), y=a.get("y"))
    return {"ok": True}


def physical_key(a):
    pyautogui.press(a["key"], presses=int(a.get("presses", 1)), interval=float(a.get("interval", 0.05)))
    return {"ok": True}


def physical_hotkey(a):
    pyautogui.hotkey(*a["keys"], interval=float(a.get("interval", 0.05)))
    return {"ok": True}


def physical_type_text(a):
    text = str(a.get("text", ""))
    mode = a.get("mode", "clipboard")
    if mode == "keys":
        pyautogui.write(text, interval=float(a.get("interval", 0.02)))
    else:
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
    return {"ok": True, "chars": len(text), "mode": mode}


def clipboard(a):
    if "set" in a:
        pyperclip.copy(str(a["set"]))
        return {"ok": True}
    return {"text": pyperclip.paste()}


def launch(a):
    args = [str(x) for x in (a.get("args") or [])]
    proc = subprocess.Popen([str(a["executable"]), *args], cwd=a.get("cwd") or None)
    return {"pid": proc.pid}


def processes(a):
    name_re = a.get("name_re")
    limit = max(1, min(int(a.get("limit", 200)), 1000))
    items = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = proc.info
            if name_re and not re.search(name_re, info.get("name") or "", re.I):
                continue
            items.append(info)
        except Exception:
            continue
        if len(items) >= limit:
            break
    return {"items": items}


def sleep(a):
    time.sleep(min(float(a.get("seconds", 1)), 60))
    return {"ok": True}


def adapter_names():
    try:
        return [item["name"] for item in core_ext.list_adapters(with_manifest=False)]
    except Exception:
        return []


def capabilities(a):
    return catalog.capabilities(adapter_names())


def describe(a):
    if a.get("browser"):
        name = a["browser"]
        if name not in catalog.BROWSER_ACTIONS:
            raise ActionError(f"unknown browser action: {name}",
                              did_you_mean=catalog.suggest(name, catalog.BROWSER_ACTIONS))
        return catalog.describe_browser(name)
    if a.get("adapter"):
        return core_ext.describe_adapter(a["adapter"])
    name = a.get("name")
    if name in catalog.ACTIONS:
        return catalog.describe_action(name)
    if name in adapter_names():
        return core_ext.describe_adapter(name)
    raise ActionError(
        f"nothing to describe: {name}",
        hint="pass name=<action>, browser=<browser action> or adapter=<adapter>",
        did_you_mean=catalog.suggest(name, list(catalog.ACTIONS) + adapter_names()),
    )


def build_handlers():
    context = {
        "find_uia": find_uia,
        "window_info": window_info,
        "forward_browser": forward_browser,
        "artifact_dir": str(ARTIFACT_DIR),
        "physical_input_default": PHYSICAL_INPUT_DEFAULT,
    }
    table = {
        "capabilities": capabilities,
        "describe": describe,
        "selftest": selftest,
        "status": status,
        "windows": lambda a: {"items": list_windows(a)},
        "active_window": lambda a: active_window_info(),
        "uia_info": lambda a: window_info(find_uia(a)),
        "uia_tree": uia_tree,
        "uia_find": uia_find,
        "uia_wait": uia_wait,
        "uia_click": do_uia_click,
        "uia_set_text": do_uia_set_text,
        "uia_call": uia_call,
        "uia_type_input": do_uia_type_input,
        "window": do_window_op,
        "screen_info": screen_info,
        "screenshot": save_screenshot,
        "cursor": cursor,
        "clipboard": clipboard,
        "move": physical_move,
        "click": physical_click,
        "drag": physical_drag,
        "scroll": physical_scroll,
        "key": physical_key,
        "hotkey": physical_hotkey,
        "type_text": physical_type_text,
        "processes": processes,
        "launch": launch,
        "sleep": sleep,
        "browser": do_browser,
    }
    table.update(core_ext.handlers(context))
    return table


HANDLERS = build_handlers()


def handle(a):
    action = a.get("action")
    if not action:
        raise ActionError("Missing action", hint="start with {action: 'capabilities'}")
    spec = catalog.ACTIONS.get(action)
    handler = HANDLERS.get(action)
    if spec is None or handler is None:
        raise ActionError(
            f"Unknown action: {action}",
            hint="call capabilities for the full list",
            did_you_mean=catalog.suggest(action, catalog.ACTIONS),
        )
    missing = catalog.missing_params(spec, a)
    if missing:
        raise ActionError(
            f"{action}: missing required parameter(s): {', '.join(missing)}",
            hint=f"describe {{name: '{action}'}}",
            params=spec["params"],
        )
    if spec["effect"] == "physical":
        require_physical(a)
    return handler(a)


def error_payload(action, error):
    body = {
        "ok": False,
        "action": action,
        "error": str(error) or type(error).__name__,
        "type": type(error).__name__,
    }
    if isinstance(error, KeyError):
        body["error"] = f"missing parameter: {error.args[0] if error.args else error}"
        body["hint"] = f"describe {{name: '{action}'}}"
    if isinstance(error, ActionError):
        if error.hint:
            body["hint"] = error.hint
        body.update(core_ext.json_safe(error.extra))
    return body


class Handler(BaseHTTPRequestHandler):
    server_version = "RdcHarness/0.1"
    sys_version = ""
    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path != "/health":
            self._send(404, {"ok": False, "error": "Not found"})
            return
        self._send(200, {
            "ok": True,
            "ready": True,
            "pid": os.getpid(),
            "port": PORT,
        })

    def do_POST(self):
        if self.path != "/action":
            self._send(404, {"ok": False, "error": "Not found"})
            return
        supplied = self.headers.get("X-Desktop-Agent-Token", "")
        if not secrets.compare_digest(supplied, AGENT_TOKEN):
            self._send(403, {"ok": False, "error": "Invalid token"})
            return
        action = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 2 * 1024 * 1024:
                raise ActionError("Payload too large")
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ActionError("request must be a JSON object")
            action = payload.get("action")
            result = handle(payload)
            self._send(200, {"ok": True, "result": result})
        except Exception as e:
            if isinstance(e, PermissionError):
                status = 403
            elif isinstance(e, (ActionError, KeyError, json.JSONDecodeError)):
                status = 400
            else:
                status = 500
            self._send(status, error_payload(action, e))


def main():
    # DESKTOP_AGENT_THREADED=0 serves requests one at a time on the main thread:
    # slower under parallel calls, but every COM call then runs on one STA thread.
    threaded = os.getenv("DESKTOP_AGENT_THREADED", "1") != "0"
    server = (ThreadingHTTPServer if threaded else HTTPServer)((HOST, PORT), Handler)
    print(json.dumps({
        "ready": True,
        "pid": os.getpid(),
        "host": HOST,
        "port": PORT,
        "threaded": threaded,
        "artifact_dir": str(ARTIFACT_DIR),
    }, ensure_ascii=True), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
