"""Action catalog: the single description of every Desktop Agent action.

The dispatcher, `capabilities` and `describe` are all driven from this table,
so what the model sees can never drift from what the agent actually executes.
This module has no third-party imports so tests and tools can load it anywhere.

Effects:
  read      observes state only
  write     changes application / OS / browser state
  exec      runs arbitrary code or programs
  physical  moves the real mouse or keyboard; needs allow_physical=true
"""

import difflib

SELECTOR = (
    "UIA selector object: title, title_re, control_type, automation_id, "
    "class_name, pid, index (at least one field)"
)
LOCATOR = (
    "one of: selector (CSS), role + name, label, placeholder, testid, text; "
    "optional exact=true; matches must be unique unless nth (zero-based) is explicit"
)
BROWSER_TARGET = {
    "tab_id": "stable live tab ID from tabs/newTab; omit to use current tab",
    "frame_id": "live frame ID from frames (alternative to frame_selector)",
    "frame_selector": "iframe CSS selector or list of selectors for nested frames",
    "nth": "zero-based locator match; omit to require exactly one match",
    "timeout": "operation timeout in milliseconds",
}

WORKFLOW = [
    "1. capabilities -> pick a group; describe {name} -> exact parameters.",
    "2. Universal desktop: app_catalog -> app_observe {hwnd} -> app_act {ref,operation,request_id} -> app_wait.",
    "3. Choose a supported interface: browser (DOM/CDP), UIA, Win32, CLI or COM. App-specific adapters are optional.",
    "   Browser: tabs -> explicit tab_id -> snapshot/frames -> action -> inspect expected state.",
    "   Desktop: windows -> uia_tree -> uia_inspect supported patterns and state -> action -> verify.",
    "   Use look (desktop_look) to see a window when UIA/DOM do not explain its state.",
    "4. Physical input (move, click, key, ...) is last resort and needs allow_physical=true.",
    "5. Externally visible browser clicks (send, pay, delete, ...) need commit=true after user confirmation.",
]

UIA_TARGET = {
    "window": SELECTOR + " for the top-level window",
    "control": SELECTOR + " for a descendant of window (optional)",
    "...": "or pass selector fields at top level to target a window directly",
}


def _spec(group, effect, summary, params=None, required=(), example=None):
    return {
        "group": group,
        "effect": effect,
        "summary": summary,
        "params": params or {},
        "required": list(required),
        "example": example,
    }


ACTIONS = {
    # --- discovery -------------------------------------------------------
    "capabilities": _spec(
        "discovery", "read",
        "Compact list of all actions, browser actions and adapters.",
    ),
    "describe": _spec(
        "discovery", "read",
        "Full parameters of one action, browser action or adapter.",
        {
            "name": "action name, e.g. uia_click",
            "browser": "browser action name, e.g. click",
            "adapter": "adapter name, e.g. autocad",
        },
        example={"action": "describe", "name": "uia_click"},
    ),
    "selftest": _spec("discovery", "read", "Check browser, UIA, COM, adapters, file handoff and security settings."),
    "status": _spec("discovery", "read", "Agent pid, screen, cursor, active window and browser status."),
    # --- application-independent semantic control ------------------------
    "app_catalog": _spec("app", "read", "Discover running windows and registered App Paths executables without launching applications.",
                         {"query": "substring in name/title/path", "limit": "1..300 per section; default 100"}),
    "app_open": _spec("app", "exec", "Launch an explicit executable without shell, optionally observe its windows; not proof of document readiness.",
                      {"executable": "existing absolute .exe path", "args": "array of strings", "cwd": "absolute working directory",
                       "timeout": "seconds to observe a window, 0..30, default 0", "dry_run": "report plan without launching"},
                      required=("executable",)),
    "app_observe": _spec("app", "read", "Observe any accessible Windows application; negotiate UIA/Win32 and return short-lived control refs and supported operations.",
                         {"hwnd": "exact window handle from app_catalog", "pid": "optional expected process ID",
                          "backend": "auto | uia | win32", "depth": "0..8", "limit": "1..300 nodes", "max_text": "0..4000 characters per field"},
                         required=("hwnd",), example={"action": "app_observe", "hwnd": 1234}),
    "app_act": _spec("app", "write", "Apply one semantic operation to an observed ref; optional precondition, dry run, read-back and bounded request replay. Never physical fallback.",
                     {"ref": "live ref from app_observe", "operation": "invoke | set_value | set_checked | select | expand | collapse | scroll_into_view",
                      "request_id": "unique attempt ID; same ID/arguments retrieves cached result for 120 seconds",
                      "value": "text or number for set_value", "checked": "boolean for set_checked", "expected": "expected current state object",
                      "dry_run": "validate without writing", "commit": "literal true required for invoke after user authorization"},
                     required=("ref", "operation", "request_id")),
    "app_wait": _spec("app", "read", "Wait for an observed control to match a state predicate; no write retries.",
                      {"ref": "live ref", "expected": "nonempty value/checked/selected/expanded/enabled/visible object", "timeout": "seconds, 0..30"},
                      required=("ref", "expected")),
    # --- windows / UIA ---------------------------------------------------
    "windows": _spec(
        "uia", "read", "List top-level windows.",
        {"title_re": "regex filter on title (case-insensitive)", "limit": "max items, <= 300"},
    ),
    "active_window": _spec("uia", "read", "Foreground window: hwnd, title, pid, process, rect."),
    "uia_info": _spec("uia", "read", "Properties of one window/control.", UIA_TARGET),
    "uia_inspect": _spec(
        "uia", "read", "Inspect supported UIA patterns and current value, text, toggle, selection and expansion state.",
        dict(UIA_TARGET, max_text="0..20000 characters, default 4000; password values/text are redacted"),
        example={"action": "uia_inspect", "window": {"title_re": "Notepad"},
                 "control": {"control_type": "Edit"}},
    ),
    "uia_tree": _spec(
        "uia", "read", "Control tree under a window.",
        dict(UIA_TARGET, depth="0..8, default 3", limit="1..1000, default 200"),
        example={"action": "uia_tree", "window": {"title_re": "Notepad"}, "depth": 2},
    ),
    "uia_find": _spec(
        "uia", "read", "Find descendant controls matching a selector.",
        dict(UIA_TARGET, control=SELECTOR + " (required)", limit="1..300, default 50"),
        required=("control",),
    ),
    "uia_wait": _spec(
        "uia", "read", "Wait for a control state.",
        dict(UIA_TARGET, state="exists | visible | enabled | ready | active (space separated); absent/not_exists/hidden/disabled/inactive",
             timeout="seconds, 0..120, default 10", retry_interval="seconds between checks, 0.01..2, default 0.2"),
    ),
    "uia_click": _spec(
        "uia", "write", "Invoke a control semantically; falls back to a physical click only with allow_physical.",
        dict(UIA_TARGET, method="auto | invoke | click_input", button="left | right"),
        example={"action": "uia_click", "window": {"title_re": "Notepad"},
                 "control": {"control_type": "MenuItem", "title": "File"}},
    ),
    "uia_set_text": _spec(
        "uia", "write", "Set text of an edit control; clipboard-paste fallback needs allow_physical.",
        dict(UIA_TARGET, value="text"),
        required=("value",),
    ),
    "uia_call": _spec(
        "uia", "write", "Call any public pywinauto wrapper method on a control.",
        dict(UIA_TARGET, method="method name, e.g. select", args="list", kwargs="object"),
        required=("method",),
    ),
    "uia_type_input": _spec(
        "physical", "physical", "Physically click a control and paste text.",
        dict(UIA_TARGET, value="text", replace="select all first, default true"),
        required=("value",),
    ),
    "window": _spec(
        "uia", "write", "Window operation.",
        dict(UIA_TARGET, op="focus | minimize | maximize | restore | close"),
        required=("op",),
    ),
    "win32_message": _spec(
        "win32", "write", "SendMessage/PostMessage to a window handle.",
        {"hwnd": "int", "msg": "int or '0x..' string", "wparam": "int", "lparam": "int",
         "post": "true = PostMessage"},
        required=("hwnd", "msg"),
    ),
    "window_capture": _spec(
        "win32", "read", "Capture one window in the background (PrintWindow) to PNG.",
        {"hwnd": "int", "path": "optional output path", "flags": "PrintWindow flags, default 2"},
        required=("hwnd",),
    ),
    # --- screen / clipboard ---------------------------------------------
    "look": _spec(
        "screen", "read",
        "See a window, region or monitor as an image (MCP tool desktop_look). "
        "Window capture works in the background; GPU windows fall back to screen pixels.",
        {"window": SELECTOR + " for the window to look at", "hwnd": "int, alternative to window",
         "region": "{left, top, width, height} in screen pixels", "monitor": "index, 0 = all (default)",
         "max_side": "longest image side, 200..4096, default 1568", "quality": "JPEG 30..95, default 80",
         "save": "true: write the JPEG to <artifacts>/chat and return its path instead of base64 "
                 "(for terminal clients such as ChatGPT via Remote Desktop Commander; open it with read_file)"},
        example={"action": "look", "window": {"title_re": "AutoCAD"}},
    ),
    "screen_info": _spec("screen", "read", "Monitor geometry."),
    "screenshot": _spec(
        "screen", "read", "Screenshot of a monitor or region to PNG.",
        {"monitor": "index, 0 = all", "region": "{left, top, width, height}", "path": "optional"},
    ),
    "cursor": _spec("screen", "read", "Current cursor position."),
    "clipboard": _spec(
        "screen", "write", "Read clipboard, or set it with set=...",
        {"set": "text to put on the clipboard (omit to read)"},
    ),
    # --- physical input --------------------------------------------------
    "move": _spec("physical", "physical", "Move the mouse.", {"x": "int", "y": "int", "duration": "s"}, ("x", "y")),
    "click": _spec(
        "physical", "physical", "Physical mouse click at screen coordinates.",
        {"x": "int", "y": "int", "clicks": "int", "button": "left | right | middle"}, ("x", "y"),
    ),
    "drag": _spec(
        "physical", "physical", "Physical drag.",
        {"from_x": "int", "from_y": "int", "to_x": "int", "to_y": "int"},
        ("from_x", "from_y", "to_x", "to_y"),
    ),
    "scroll": _spec("physical", "physical", "Mouse wheel.", {"clicks": "int, negative = down", "x": "int", "y": "int"}),
    "key": _spec("physical", "physical", "Press a key.", {"key": "pyautogui key name", "presses": "int"}, ("key",)),
    "hotkey": _spec("physical", "physical", "Press a key chord.", {"keys": "list, e.g. ['ctrl','s']"}, ("keys",)),
    "type_text": _spec(
        "physical", "physical", "Type text into the focused control.",
        {"text": "text", "mode": "clipboard | keys"}, ("text",),
    ),
    # --- processes / code ------------------------------------------------
    "processes": _spec("process", "read", "List processes.", {"name_re": "regex", "limit": "<= 1000"}),
    "discover_process": _spec("process", "read", "Basic info of one process.", {"pid": "int"}, ("pid",)),
    "probe_target": _spec(
        "process", "read", "Deep probe of a process: version, windows, modules, sockets, children.",
        {"pid": "int", "module_limit": "int", "socket_limit": "int",
         "include_env": "bool", "env_keys": "list"},
        ("pid",),
    ),
    "launch": _spec(
        "process", "exec", "Start a program without waiting.",
        {"executable": "path", "args": "list", "cwd": "dir"}, ("executable",),
    ),
    "exec": _spec(
        "process", "exec", "Run a command and wait for it.",
        {"command": "string (shell) or list", "cwd": "dir", "env": "object",
         "timeout": "seconds, 1..3600, default 120", "shell": "bool"},
        ("command",),
    ),
    "python": _spec(
        "process", "exec", "Run Python code in the agent's venv (separate process).",
        {"code": "source", "timeout": "seconds"}, ("code",),
    ),
    "powershell": _spec(
        "process", "exec", "Run PowerShell code (separate process).",
        {"code": "source", "timeout": "seconds"}, ("code",),
    ),
    "sleep": _spec("process", "read", "Wait up to 60 s.", {"seconds": "float"}),
    # --- COM -------------------------------------------------------------
    "com_create": _spec(
        "com", "exec", "Create a COM object; returns a handle.", {"progid": "e.g. Excel.Application"}, ("progid",),
    ),
    "com_active": _spec(
        "com", "read", "Attach to a running COM object; returns a handle.", {"progid": "str"}, ("progid",),
    ),
    "com_get": _spec(
        "com", "read", "Read an attribute path from a handle.",
        {"handle": "str", "path": "dotted path, e.g. ActiveDocument.Name", "call_if_callable": "bool"},
        ("handle",),
    ),
    "com_set": _spec(
        "com", "write", "Set an attribute path on a handle.",
        {"handle": "str", "path": "dotted path", "value": "any"}, ("handle", "path"),
    ),
    "com_call": _spec(
        "com", "write", "Call a method path on a handle.",
        {"handle": "str", "path": "dotted method path", "args": "list", "kwargs": "object",
         "store_result": "bool: keep result as a new handle"},
        ("handle",),
        example={"action": "com_call", "handle": "com:...", "path": "Workbooks.Open", "args": ["C:/x.xlsx"]},
    ),
    "com_list": _spec("com", "read", "List live COM handles."),
    "com_release": _spec("com", "write", "Drop a COM handle.", {"handle": "str"}, ("handle",)),
    # --- adapters --------------------------------------------------------
    "adapter_list": _spec("adapter", "read", "Installed app adapters with their description and actions."),
    "adapter_call": _spec(
        "adapter", "write", "Call an adapter; see describe {adapter} for its actions.",
        {"name": "adapter name", "payload": "object with the adapter's action and params"},
        ("name",),
        example={"action": "adapter_call", "name": "autocad", "payload": {"action": "status"}},
    ),
    "adapter_install": _spec(
        "adapter", "exec", "Install or replace an adapter module (hot-loaded).",
        {"name": "identifier", "code": "Python source defining handle(payload, context), "
                                       "DESCRIPTION and ACTIONS"},
        ("name", "code"),
    ),
    # --- browser / files -------------------------------------------------
    "browser": _spec(
        "browser", "write", "Forward one action to the Playwright Browser Bridge.",
        {"payload": "browser request, e.g. {action:'open', url:'...'}; see describe {browser}",
         "browser_action": "shortcut for payload.action",
         "commit": "true to allow an externally visible click (send, pay, delete, ...)"},
        example={"action": "browser", "payload": {"action": "snapshot"}},
    ),
    "file_handoff": _spec(
        "files", "read", "Stage a local file for delivery to the chat client.",
        {"path": "absolute path", "name": "display name", "max_bytes": "int", "mime_type": "str"},
        ("path",),
    ),
}

BROWSER_ACTIONS = {
    "attach": _spec("browser", "write", "Attach an explicitly exposed local Chromium/Electron CDP session without copying credentials.",
                    {"endpoint": "ws://127.0.0.1:<port>/devtools/browser/<id> or IPv6 loopback", "timeout": "milliseconds, <=30000"}, required=("endpoint",)),
    "detach": _spec("browser", "write", "Disconnect a borrowed CDP session without closing its application or saving its state."),
    "status": ("read", "Bridge state.", {}),
    "restart": ("write", "Restart the browser context.", {"headed": "bool, default: keep current"}),
    "open": ("write", "Navigate to url (alias goto).", {"url": "str", "waitUntil": "str", "timeout": "ms"}),
    "goto": ("write", "Navigate to url.", {"url": "str", "waitUntil": "str", "timeout": "ms"}),
    "snapshot": ("read", "ARIA snapshot of the target page or frame: the primary way to see it.", {}),
    "frames": ("read", "Discover live frames of the target tab for embedded and nested forms.",
               {"max_frames": "1..200, default 100"}),
    "inspect": ("read", "Inspect one element's bounded state, value, checked state and select options; passwords redacted.",
                {"locator": LOCATOR, "max_length": "1..4000 characters per field, default 500",
                 "max_options": "1..200 options, default 50"}),
    "click": ("write", "Click a locator.", {"locator": LOCATOR, "timeout": "ms"}),
    "fill": ("write", "Fill an input.", {"locator": LOCATOR, "value": "str"}),
    "type": ("write", "Type key by key.", {"locator": LOCATOR, "value": "str", "delay": "ms"}),
    "selectOption": ("write", "Select native dropdown options and return selected values.",
                     {"locator": LOCATOR, "value": "option value string or list of values"}),
    "setChecked": ("write", "Set a checkbox/radio to an explicit state and verify it.",
                   {"locator": LOCATOR, "checked": "bool"}),
    "hover": ("write", "Hover over a DOM element to expose menus without moving the OS pointer.", {"locator": LOCATOR}),
    "scrollIntoView": ("write", "Scroll an element into view in the target page/frame.", {"locator": LOCATOR}),
    "press": ("write", "Press a key on a locator or the page.", {"locator": LOCATOR + " (optional)", "key": "str"}),
    "wait": ("read", "Wait ms or for a locator state.", {"ms": "int", "locator": LOCATOR, "state": "str"}),
    "text": ("read", "innerText of a locator.", {"locator": LOCATOR}),
    "html": ("read", "innerHTML of a locator.", {"locator": LOCATOR}),
    "url": ("read", "Current url and title.", {}),
    "screenshot": ("read", "Page screenshot.", {"path": "optional", "fullPage": "bool"}),
    "upload": ("write", "Set input files.", {"locator": LOCATOR, "files": "list"}),
    "download": ("write", "Click and save a download.", {"locator": LOCATOR, "path": "optional"}),
    "evaluate": ("exec", "Evaluate a JS expression in the page.", {"expression": "str", "arg": "any"}),
    "cookies": ("read", "Read cookies, or clear=true.", {"urls": "list", "clear": "bool"}),
    "storage": ("read", "Read/set/clear local or session storage.", {"kind": "local | session",
                                                                     "set": "object", "clear": "bool"}),
    "tabs": ("read", "List tabs with stable tab_id values and legacy indexes.", {}),
    "newTab": ("write", "Open a tab.", {"url": "optional"}),
    "useTab": ("write", "Switch tab.", {"tab_id": "live tab ID", "index": "legacy index, alternative to tab_id"}),
    "closeTab": ("write", "Close a tab.", {"tab_id": "live tab ID", "index": "legacy index, default current"}),
    "console": ("read", "Recent console messages.", {"limit": "int", "clear": "bool"}),
    "errors": ("read", "Recent page errors.", {"limit": "int", "clear": "bool"}),
    "back": ("write", "History back.", {}),
    "forward": ("write", "History forward.", {}),
    "reload": ("write", "Reload page.", {}),
    "close": ("write", "Close the browser context.", {}),
}

SIDE_EFFECT_HINTS = (
    "send", "post", "publish", "submit", "confirm", "delete", "remove",
    "buy", "purchase", "pay", "order", "book", "reserve", "invite",
    "отправ", "опубликов", "подтверд", "удал", "куп", "оплат",
    "заказ", "заброниров", "приглас",
)
_LOCATOR_KEYS = ("name", "text", "label", "selector", "testid")


def browser_side_effect(payload):
    """True when a browser request looks like an externally visible commit."""
    action = payload.get("action")
    if action == "press":
        if str(payload.get("key", "")).lower() != "enter":
            return False
    elif action != "click":
        return False
    hay = " ".join(str(payload.get(k, "")) for k in _LOCATOR_KEYS).lower()
    return any(x in hay for x in SIDE_EFFECT_HINTS)


def suggest(name, choices):
    return difflib.get_close_matches(str(name), list(choices), n=3, cutoff=0.5)


def missing_params(spec, request):
    return [p for p in spec["required"] if request.get(p) in (None, "", [])]


def capabilities(adapters=()):
    groups = {}
    for name, spec in ACTIONS.items():
        groups.setdefault(spec["group"], []).append(
            {"name": name, "effect": spec["effect"], "summary": spec["summary"]}
        )
    return {
        "workflow": WORKFLOW,
        "actions": groups,
        "browser_actions": sorted(BROWSER_ACTIONS),
        "adapters": list(adapters),
        "next": "describe {name} | {browser} | {adapter} for parameters and examples",
    }


def describe_action(name):
    spec = ACTIONS[name]
    return dict(spec, name=name)


def describe_browser(name):
    effect, summary, params = BROWSER_ACTIONS[name]
    params = dict(params)
    if name not in {"status", "restart", "close", "tabs", "newTab", "cookies"}:
        params.setdefault("tab_id", BROWSER_TARGET["tab_id"])
    if name in {
        "open", "goto", "url", "snapshot", "click", "fill", "type", "press", "wait",
        "text", "html", "inspect", "selectOption", "setChecked", "hover",
        "scrollIntoView", "upload", "download", "evaluate", "storage",
    }:
        params.update({key: BROWSER_TARGET[key] for key in ("frame_id", "frame_selector")})
    if "locator" in params:
        params["nth"] = BROWSER_TARGET["nth"]
    if name not in {"status", "close", "tabs", "console", "errors", "frames", "cookies"}:
        params.setdefault("timeout", BROWSER_TARGET["timeout"])
    return {
        "name": name,
        "effect": effect,
        "summary": summary,
        "params": params,
        "usage": {"action": "browser", "payload": dict({"action": name})},
    }
