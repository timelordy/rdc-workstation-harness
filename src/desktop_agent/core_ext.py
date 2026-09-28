import ctypes
import hashlib
import importlib.util
import mimetypes
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import psutil
import win32api
import win32com.client
import win32gui
import win32process
import win32ui
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = BASE_DIR / "adapters"
OUTBOX_DIR = BASE_DIR / "outbox"
ADAPTER_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
OBJECTS = {}
_ADAPTER_CACHE = {}  # name -> (mtime_ns, module)

_HOME = Path(os.getenv("USERPROFILE", str(Path.home())))
# Files under these directories must never leave the machine through file_handoff.
PROTECTED_DIRS = [
    Path(os.getenv("RDC_HARNESS_TOKEN_DIR", str(_HOME / ".chatgpt-desktop-agent"))),
    _HOME / ".desktop-commander-device",
    Path(os.getenv("PW_PROFILE_DIR", str(_HOME / ".rdc-workstation-harness" / "browser-profile"))),
]
PROTECTED_SUFFIXES = {".token"}


class ActionError(ValueError):
    """A request error the caller can fix; `hint` tells the model how."""

    def __init__(self, message, hint=None, **extra):
        super().__init__(message)
        self.hint = hint
        self.extra = extra


def json_safe(value, depth=0):
    if depth > 5:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_safe(x, depth + 1) for x in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v, depth + 1) for k, v in value.items()}
    try:
        return list(value)
    except Exception:
        return repr(value)


def run_command(a):
    command = a.get("command")
    if command is None:
        raise ActionError("command required")
    timeout = max(1, min(float(a.get("timeout", 120)), 3600))
    cwd = a.get("cwd") or None
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in (a.get("env") or {}).items()})
    shell = bool(a.get("shell", isinstance(command, str)))
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        shell=shell,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout[-200000:],
        "stderr": completed.stderr[-200000:],
    }


def run_python(a):
    code = a.get("code")
    if code is None:
        raise ActionError("code required")
    payload = dict(a)
    payload["command"] = [sys.executable, "-c", str(code)]
    payload["shell"] = False
    return run_command(payload)


def run_powershell(a):
    code = a.get("code")
    if code is None:
        raise ActionError("code required")
    payload = dict(a)
    payload["command"] = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", str(code),
    ]
    payload["shell"] = False
    return run_command(payload)


# --- adapters ------------------------------------------------------------

def _check_adapter_name(name):
    if not name or not str(name).replace("_", "").replace("-", "").isalnum():
        raise ActionError("invalid adapter name", hint="use letters, digits, '_' or '-'")


def load_adapter(name):
    _check_adapter_name(name)
    path = ADAPTER_DIR / f"{name}.py"
    if not path.exists():
        names = [item["name"] for item in list_adapters(with_manifest=False)]
        raise ActionError(f"adapter not found: {name}", hint="see adapter_list", adapters=names)
    mtime = path.stat().st_mtime_ns
    cached = _ADAPTER_CACHE.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    module_name = f"desktop_agent_adapter_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load adapter {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _ADAPTER_CACHE[name] = (mtime, module)
    return module


def adapter_manifest(name, module):
    actions = getattr(module, "ACTIONS", None)
    description = getattr(module, "DESCRIPTION", None)
    return {
        "name": name,
        "description": description,
        "actions": json_safe(actions) if isinstance(actions, dict) else None,
        "manifest": isinstance(actions, dict) and isinstance(description, str),
    }


def list_adapters(with_manifest=True):
    items = []
    for path in sorted(ADAPTER_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        item = {"name": path.stem}
        if with_manifest:
            try:
                item.update(adapter_manifest(path.stem, load_adapter(path.stem)))
                if item["actions"]:
                    item["actions"] = sorted(item["actions"])
            except Exception as e:
                item.update({"manifest": False, "load_error": f"{type(e).__name__}: {e}"})
        items.append(item)
    return items


def describe_adapter(name):
    info = adapter_manifest(name, load_adapter(name))
    info["usage"] = {"action": "adapter_call", "name": name, "payload": {"action": "<adapter action>"}}
    return info


def call_adapter(a, context):
    name = a.get("name")
    module = load_adapter(name)
    handler = getattr(module, "handle", None)
    if not callable(handler):
        raise ActionError(f"adapter {name} must define handle(payload, context)")
    payload = a.get("payload") or {}
    actions = getattr(module, "ACTIONS", None)
    if isinstance(actions, dict) and payload.get("action") is not None and payload["action"] not in actions:
        raise ActionError(
            f"unknown {name} adapter action: {payload['action']}",
            hint=f"describe {{adapter: '{name}'}}",
            valid_actions=sorted(actions),
        )
    return json_safe(handler(payload, context))


def install_adapter(a):
    name = a.get("name")
    code = a.get("code")
    _check_adapter_name(name)
    if not isinstance(code, str) or not code.strip():
        raise ActionError("adapter code required")
    path = ADAPTER_DIR / f"{name}.py"
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    path.write_text(code, encoding="utf-8")
    try:
        module = load_adapter(name)
        if not callable(getattr(module, "handle", None)):
            raise ActionError("adapter must define handle(payload, context)")
        manifest = adapter_manifest(name, module)
        if not manifest["manifest"]:
            raise ActionError(
                "adapter must define DESCRIPTION (str) and ACTIONS (dict)",
                hint="ACTIONS = {'status': {'effect': 'read', 'summary': '...', 'params': {}}}",
            )
    except Exception:
        _ADAPTER_CACHE.pop(name, None)
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous, encoding="utf-8")
        raise
    return dict(manifest, path=str(path), loaded=True)


# --- COM -----------------------------------------------------------------

def object_put(obj, kind, label=None):
    handle = f"{kind}:{uuid.uuid4().hex}"
    OBJECTS[handle] = (obj, label)
    return handle


def object_get(handle):
    if handle not in OBJECTS:
        raise ActionError(f"unknown object handle: {handle}", hint="see com_list", handles=list(OBJECTS))
    return OBJECTS[handle][0]


def resolve_attr(obj, path):
    current = obj
    for part in (path or "").split("."):
        if not part:
            continue
        current = getattr(current, part)
    return current


def com_create(a):
    progid = a.get("progid")
    if not progid:
        raise ActionError("progid required")
    obj = win32com.client.Dispatch(progid)
    return {"handle": object_put(obj, "com", progid), "progid": progid}


def com_active(a):
    progid = a.get("progid")
    if not progid:
        raise ActionError("progid required")
    obj = win32com.client.GetActiveObject(progid)
    return {"handle": object_put(obj, "com_active", progid), "progid": progid}


def com_get(a):
    obj = object_get(a["handle"])
    value = resolve_attr(obj, a.get("path", ""))
    if hasattr(value, "__call__") and a.get("call_if_callable", False):
        value = value()
    return {"value": json_safe(value)}


def com_set(a):
    obj = object_get(a["handle"])
    path = a.get("path")
    if not path:
        raise ActionError("path required")
    parent_path, _, leaf = path.rpartition(".")
    setattr(resolve_attr(obj, parent_path), leaf, a.get("value"))
    return {"ok": True}


def com_call(a):
    obj = object_get(a["handle"])
    fn = resolve_attr(obj, a.get("path", ""))
    result = fn(*(a.get("args") or []), **(a.get("kwargs") or {}))
    if a.get("store_result") and result is not None:
        return {"handle": object_put(result, "com_result", a.get("path"))}
    return {"value": json_safe(result)}


def com_list(a):
    return {"items": [{"handle": h, "source": label} for h, (_, label) in OBJECTS.items()]}


def com_release(a):
    handle = a.get("handle")
    return {"ok": True, "released": OBJECTS.pop(handle, None) is not None}


# --- win32 / processes ---------------------------------------------------

def win32_message(a):
    hwnd = int(a["hwnd"])
    msg = int(a["msg"], 0) if isinstance(a["msg"], str) else int(a["msg"])
    wparam = int(a.get("wparam", 0))
    lparam = int(a.get("lparam", 0))
    if a.get("post", False):
        win32gui.PostMessage(hwnd, msg, wparam, lparam)
        return {"posted": True}
    result = win32gui.SendMessage(hwnd, msg, wparam, lparam)
    return {"result": int(result) if isinstance(result, int) else repr(result)}


def discover_process(a):
    pid = int(a["pid"])
    p = psutil.Process(pid)
    info = {
        "pid": pid,
        "name": p.name(),
        "exe": p.exe(),
        "cmdline": p.cmdline(),
        "cwd": None,
        "username": None,
    }
    try:
        info["cwd"] = p.cwd()
    except Exception:
        pass
    try:
        info["username"] = p.username()
    except Exception:
        pass
    return info


def probe_target(a):
    pid = int(a["pid"])
    p = psutil.Process(pid)
    exe = None
    try:
        exe = p.exe()
    except Exception:
        pass

    version = {}
    if exe:
        try:
            fixed = win32api.GetFileVersionInfo(exe, "\\")
            version = {
                "file_version_ms": fixed.get("FileVersionMS"),
                "file_version_ls": fixed.get("FileVersionLS"),
                "product_version_ms": fixed.get("ProductVersionMS"),
                "product_version_ls": fixed.get("ProductVersionLS"),
            }
            try:
                lang, codepage = win32api.GetFileVersionInfo(exe, r"\VarFileInfo\Translation")[0]
                prefix = "\\StringFileInfo\\%04x%04x\\" % (lang, codepage)
                for key in ("FileDescription", "ProductName", "ProductVersion", "CompanyName"):
                    try:
                        version[key] = win32api.GetFileVersionInfo(exe, prefix + key)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    windows = []
    def enum_cb(hwnd, extra):
        try:
            _, owner = win32process.GetWindowThreadProcessId(hwnd)
            if owner != pid:
                return True
            rect = win32gui.GetWindowRect(hwnd)
            windows.append({
                "hwnd": int(hwnd),
                "title": win32gui.GetWindowText(hwnd),
                "class_name": win32gui.GetClassName(hwnd),
                "visible": bool(win32gui.IsWindowVisible(hwnd)),
                "rect": {
                    "left": rect[0], "top": rect[1],
                    "right": rect[2], "bottom": rect[3],
                },
            })
        except Exception:
            pass
        return True
    win32gui.EnumWindows(enum_cb, None)

    modules = []
    try:
        for item in p.memory_maps(grouped=False):
            path = getattr(item, "path", None)
            if path and path not in modules:
                modules.append(path)
            if len(modules) >= int(a.get("module_limit", 200)):
                break
    except Exception:
        pass

    sockets = []
    try:
        for c in p.net_connections(kind="inet"):
            sockets.append({
                "fd": c.fd,
                "family": int(c.family),
                "type": int(c.type),
                "laddr": list(c.laddr) if c.laddr else None,
                "raddr": list(c.raddr) if c.raddr else None,
                "status": c.status,
            })
            if len(sockets) >= int(a.get("socket_limit", 100)):
                break
    except Exception:
        pass

    children = []
    try:
        children = [{"pid": c.pid, "name": c.name()} for c in p.children(recursive=False)]
    except Exception:
        pass

    env = {}
    if a.get("include_env", False):
        try:
            raw = p.environ()
            allow = a.get("env_keys") or ["PATH", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"]
            env = {k: raw.get(k) for k in allow if k in raw}
        except Exception:
            pass

    return {
        "process": {
            "pid": pid,
            "name": p.name(),
            "exe": exe,
            "cmdline": p.cmdline(),
            "ppid": p.ppid(),
            "username": p.username() if hasattr(p, "username") else None,
        },
        "version": version,
        "windows": windows,
        "children": children,
        "modules": modules,
        "sockets": sockets,
        "environment": env,
    }


def grab_window(hwnd, flags=2):
    """Background capture of one window via PrintWindow.

    Returns (PIL image, rect, rendered). GPU-drawn windows may come back black;
    callers that need pixels should check `image.getbbox()`.
    """
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = int(right - left)
    height = int(bottom - top)
    if width <= 0 or height <= 0:
        raise ActionError("window has no drawable size (minimized?)", hint="window {op: 'restore'} first")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    mem_dc = src_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(src_dc, width, height)
    mem_dc.SelectObject(bitmap)

    try:
        rendered = ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), int(flags))
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        ).copy()
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        mem_dc.DeleteDC()
        src_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
    return image, (left, top, right, bottom), bool(rendered)


def capture_window(a, context):
    hwnd = int(a["hwnd"])
    if is_hung(hwnd):
        raise ActionError("window is not responding; background capture would block",
                          hint="use look {hwnd} (falls back to screen pixels) or screenshot {region}")
    image, (left, top, right, bottom), rendered = grab_window(hwnd, a.get("flags", 2))
    width, height = image.size

    artifact_dir = Path(context["artifact_dir"])
    artifact_dir.mkdir(parents=True, exist_ok=True)
    output = Path(a.get("path") or artifact_dir / f"window-{hwnd}-{uuid.uuid4().hex[:8]}.png")
    image.save(output)

    return {
        "path": str(output),
        "hwnd": hwnd,
        "width": width,
        "height": height,
        "rendered": bool(rendered),
        "title": win32gui.GetWindowText(hwnd),
    }


# --- look: an image the model can see --------------------------------------

LOOK_MAX_SIDE = 1568      # longest side sent to the model
LOOK_MIN_SIDE = 200


def _grab_screen(bbox=None, monitor=0):
    import mss  # local: keep core_ext importable without a display stack
    with mss.mss() as sct:
        if bbox:
            left, top, right, bottom = bbox
            area = {"left": left, "top": top, "width": right - left, "height": bottom - top}
        else:
            area = sct.monitors[int(monitor)]
        shot = sct.grab(area)
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return image, (area["left"], area["top"], area["left"] + area["width"], area["top"] + area["height"])


def _find_hwnd(a, context):
    if a.get("hwnd") is not None:
        return int(a["hwnd"])
    if a.get("window"):
        wrapper = context["find_uia"]({"window": a["window"]})
        return int(wrapper.handle)
    return None


LOOK_KEEP_FILES = 50      # saved looks kept in <artifact_dir>/chat


def encode_for_model(image, max_side=LOOK_MAX_SIDE, quality=80):
    """Downscale and JPEG-encode; returns (jpeg bytes, scale, size)."""
    import io
    max_side = max(LOOK_MIN_SIDE, min(int(max_side), 4096))
    scale = min(1.0, max_side / max(image.size))
    if scale < 1.0:
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=max(30, min(int(quality), 95)), optimize=True)
    return buf.getvalue(), scale, image.size


def save_look(jpeg, artifact_dir):
    """Write a look to <artifact_dir>/chat and keep only the newest files there."""
    import time
    chat_dir = Path(artifact_dir) / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = chat_dir / f"look-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.jpg"
    path.write_bytes(jpeg)
    old = sorted(chat_dir.glob("look-*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in old[LOOK_KEEP_FILES:]:
        stale.unlink(missing_ok=True)
    return path


def is_hung(hwnd):
    """True when the window's thread stopped pumping messages (IsHungAppWindow)."""
    return bool(ctypes.windll.user32.IsHungAppWindow(int(hwnd)))


def look(a, context):
    """Capture a window, region or monitor and return it as an image for the model."""
    hwnd = _find_hwnd(a, context)
    source = "screen"
    note = None
    if hwnd is not None:
        if win32gui.IsIconic(hwnd):
            raise ActionError("window is minimized and cannot be captured",
                              hint="window {op: 'restore'} first")
        if is_hung(hwnd):
            # PrintWindow sends WM_PRINT and would block on a hung window.
            rect = win32gui.GetWindowRect(hwnd)
            image, rect = _grab_screen(rect)
            source = "screen_region"
            note = "window is not responding; used visible screen pixels"
        else:
            image, rect, rendered = grab_window(hwnd)
            source = "window"
            # PrintWindow returns black for many GPU/DirectX surfaces; fall back
            # to the visible screen pixels of the same rectangle.
            if not rendered or image.convert("L").getextrema()[1] < 8:
                image, rect = _grab_screen(rect)
                source = "screen_region"
                note = ("background capture was blank (GPU surface); used visible screen pixels, "
                        "overlapping windows may show")
        title = win32gui.GetWindowText(hwnd)
    elif a.get("region"):
        r = a["region"]
        left, top = int(r["left"]), int(r["top"])
        image, rect = _grab_screen((left, top, left + int(r["width"]), top + int(r["height"])))
        title = None
    else:
        image, rect = _grab_screen(monitor=a.get("monitor", 0))
        title = None

    jpeg, scale, size = encode_for_model(image, a.get("max_side", LOOK_MAX_SIDE), a.get("quality", 80))
    result = {}
    if a.get("save"):
        # For clients that reach the agent through a terminal (e.g. ChatGPT via
        # Remote Desktop Commander): no base64 in stdout, the client opens the file.
        path = save_look(jpeg, context["artifact_dir"])
        result["path"] = str(path)
        result["bytes"] = len(jpeg)
    else:
        import base64
        result["image"] = {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode("ascii")}
    result.update({
        "source": source,
        "title": title,
        "hwnd": hwnd,
        "screen_rect": {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]},
        "image_size": {"width": size[0], "height": size[1]},
        "scale": round(scale, 6),
        "to_screen": "screen_x = left + image_x / scale; screen_y = top + image_y / scale",
    })
    if note:
        result["note"] = note
    return result


# --- file handoff --------------------------------------------------------

def is_protected(path):
    path = Path(path).resolve()
    if path.suffix.lower() in PROTECTED_SUFFIXES:
        return True
    for root in PROTECTED_DIRS:
        try:
            path.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def stage_file(a):
    raw_path = a.get("path")
    if not raw_path:
        raise ActionError("path is required")

    source = Path(raw_path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(str(source))
    if is_protected(source):
        raise PermissionError(
            "refusing to hand off credentials: tokens, device identity and browser profile never leave this PC"
        )

    size = source.stat().st_size
    max_bytes = int(a.get("max_bytes", 25 * 1024 * 1024))
    if max_bytes <= 0:
        raise ActionError("max_bytes must be positive")
    if size > max_bytes:
        raise ActionError(
            "file is too large for chat handoff: %d bytes > %d bytes"
            % (size, max_bytes)
        )

    requested_name = str(a.get("name") or source.name).strip()
    safe_name = Path(requested_name).name
    if not safe_name:
        safe_name = source.name

    item_id = uuid.uuid4().hex
    target = OUTBOX_DIR / ("%s-%s" % (item_id, safe_name))
    shutil.copy2(source, target)

    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    mime_type = a.get("mime_type") or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

    return {
        "id": item_id,
        "path": str(target),
        "source_path": str(source),
        "name": safe_name,
        "size": size,
        "mime_type": mime_type,
        "sha256": digest.hexdigest(),
        "uri": "desktop-file://%s/%s" % (item_id, safe_name),
    }


def handlers(context):
    """Actions implemented in this module, keyed by catalog name."""
    return {
        "exec": run_command,
        "python": run_python,
        "powershell": run_powershell,
        "adapter_list": lambda a: {"items": list_adapters()},
        "adapter_install": install_adapter,
        "adapter_call": lambda a: call_adapter(a, context),
        "com_create": com_create,
        "com_active": com_active,
        "com_get": com_get,
        "com_set": com_set,
        "com_call": com_call,
        "com_list": com_list,
        "com_release": com_release,
        "win32_message": win32_message,
        "window_capture": lambda a: capture_window(a, context),
        "look": lambda a: look(a, context),
        "discover_process": discover_process,
        "probe_target": probe_target,
        "file_handoff": stage_file,
    }
