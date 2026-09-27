import ctypes
import hashlib
import importlib.util
import json
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
import win32con
import win32gui
import win32ui
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = BASE_DIR / "adapters"
OUTBOX_DIR = BASE_DIR / "outbox"
ADAPTER_DIR.mkdir(parents=True, exist_ok=True)
OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
OBJECTS = {}


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
        raise ValueError("command required")
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
        raise ValueError("code required")
    payload = dict(a)
    payload["command"] = [sys.executable, "-c", str(code)]
    payload["shell"] = False
    return run_command(payload)


def run_powershell(a):
    code = a.get("code")
    if code is None:
        raise ValueError("code required")
    payload = dict(a)
    payload["command"] = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", str(code),
    ]
    payload["shell"] = False
    return run_command(payload)
def list_adapters():
    items = []
    for path in sorted(ADAPTER_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        items.append({
            "name": path.stem,
            "path": str(path),
            "mtime": path.stat().st_mtime,
        })
    return items


def load_adapter(name):
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("invalid adapter name")
    path = ADAPTER_DIR / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(str(path))
    module_name = f"desktop_agent_adapter_{name}_{path.stat().st_mtime_ns}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load adapter {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call_adapter(a, context):
    name = a.get("name")
    module = load_adapter(name)
    handler = getattr(module, "handle", None)
    if not callable(handler):
        raise ValueError(f"adapter {name} must define handle(payload, context)")
    result = handler(a.get("payload") or {}, context)
    return json_safe(result)
def object_put(obj, kind):
    handle = f"{kind}:{uuid.uuid4().hex}"
    OBJECTS[handle] = obj
    return handle


def object_get(handle):
    if handle not in OBJECTS:
        raise KeyError(f"unknown object handle: {handle}")
    return OBJECTS[handle]


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
        raise ValueError("progid required")
    obj = win32com.client.Dispatch(progid)
    return {"handle": object_put(obj, "com"), "progid": progid}


def com_active(a):
    progid = a.get("progid")
    if not progid:
        raise ValueError("progid required")
    obj = win32com.client.GetActiveObject(progid)
    return {"handle": object_put(obj, "com_active"), "progid": progid}


def com_get(a):
    obj = object_get(a["handle"])
    value = resolve_attr(obj, a.get("path", ""))
    if hasattr(value, "__call__") and a.get("call_if_callable", False):
        value = value()
    return {"value": json_safe(value)}


def com_set(a):
    obj = object_get(a["handle"])
    path = a.get("path")
    if not path or "." in path:
        parent_path, _, leaf = path.rpartition(".")
        parent = resolve_attr(obj, parent_path)
    else:
        parent, leaf = obj, path
    setattr(parent, leaf, a.get("value"))
    return {"ok": True}


def com_call(a):
    obj = object_get(a["handle"])
    fn = resolve_attr(obj, a.get("path", ""))
    result = fn(*(a.get("args") or []), **(a.get("kwargs") or {}))
    if a.get("store_result") and result is not None:
        return {"handle": object_put(result, "com_result")}
    return {"value": json_safe(result)}
def com_release(a):
    handle = a.get("handle")
    OBJECTS.pop(handle, None)
    return {"ok": True}


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


def capture_window(a, context):
    hwnd = int(a["hwnd"])
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = int(right - left)
    height = int(bottom - top)
    if width <= 0 or height <= 0:
        raise ValueError("window has no drawable size")

    artifact_dir = Path(context["artifact_dir"])
    artifact_dir.mkdir(parents=True, exist_ok=True)
    output = Path(a.get("path") or artifact_dir / f"window-{hwnd}-{uuid.uuid4().hex[:8]}.png")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    mem_dc = src_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(src_dc, width, height)
    mem_dc.SelectObject(bitmap)

    try:
        flags = int(a.get("flags", 2))
        rendered = ctypes.windll.user32.PrintWindow(hwnd, mem_dc.GetSafeHdc(), flags)
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
        )
        image.save(output)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        mem_dc.DeleteDC()
        src_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)

    return {
        "path": str(output),
        "hwnd": hwnd,
        "width": width,
        "height": height,
        "rendered": bool(rendered),
        "title": win32gui.GetWindowText(hwnd),
    }


def install_adapter(a):
    name = a.get("name")
    code = a.get("code")
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("invalid adapter name")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("adapter code required")
    path = ADAPTER_DIR / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    module = load_adapter(name)
    if not callable(getattr(module, "handle", None)):
        path.unlink(missing_ok=True)
        raise ValueError("adapter must define handle(payload, context)")
    return {"name": name, "path": str(path), "loaded": True}


def stage_file(a):
    raw_path = a.get("path")
    if not raw_path:
        raise ValueError("path is required")

    source = Path(raw_path).expanduser().resolve()
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(str(source))

    size = source.stat().st_size
    max_bytes = int(a.get("max_bytes", 25 * 1024 * 1024))
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if size > max_bytes:
        raise ValueError(
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


CAPABILITIES = {
    "routing": [
        "native/api/adapter",
        "browser/playwright",
        "com/ipc/cli",
        "uia semantic",
        "win32 messages",
        "background capture",
        "physical input only when explicitly allowed",
    ],
    "generic_actions": [
        "exec", "python", "powershell",
        "adapter_list", "adapter_install", "adapter_call",
        "com_create", "com_active", "com_get", "com_set", "com_call", "com_release",
        "win32_message", "window_capture", "discover_process", "probe_target",
        "file_handoff",
    ],
}
def handle(action, a, context):
    if action == "capabilities":
        return CAPABILITIES
    if action == "exec":
        return run_command(a)
    if action == "python":
        return run_python(a)
    if action == "powershell":
        return run_powershell(a)
    if action == "adapter_list":
        return {"items": list_adapters()}
    if action == "adapter_install":
        return install_adapter(a)
    if action == "adapter_call":
        return call_adapter(a, context)
    if action == "com_create":
        return com_create(a)
    if action == "com_active":
        return com_active(a)
    if action == "com_get":
        return com_get(a)
    if action == "com_set":
        return com_set(a)
    if action == "com_call":
        return com_call(a)
    if action == "com_release":
        return com_release(a)
    if action == "win32_message":
        return win32_message(a)
    if action == "window_capture":
        return capture_window(a, context)
    if action == "discover_process":
        return discover_process(a)
    if action == "probe_target":
        return probe_target(a)
    if action == "file_handoff":
        return stage_file(a)
    return None
