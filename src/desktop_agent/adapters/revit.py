import json
import os
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(os.getenv("REVIT_BRIDGE_PORT", "17323"))
BASE = "http://127.0.0.1:%d" % PORT
HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
TOKEN_DIR = Path(os.getenv("RDC_HARNESS_TOKEN_DIR", str(HOME / ".chatgpt-desktop-agent")))
TOKEN_PATH = TOKEN_DIR / "revit.token"


def _health():
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "running": False, "error": str(e)}


def _token():
    if not TOKEN_PATH.exists():
        raise RuntimeError("Revit bridge token does not exist yet. Start Revit once after installing the bridge.")
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("Invalid Revit bridge token")
    return token


def _post(payload, timeout=125):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/action",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Desktop-Agent-Token": _token(),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def handle(payload, context):
    action = payload.get("action", "status")

    if action == "health":
        return _health()

    if action == "status":
        health = _health()
        if not health.get("ok"):
            return health
        return _post({"action": "status"})

    if action == "csharp":
        code = payload.get("code")
        if not code:
            raise ValueError("code is required")
        return _post({
            "action": "csharp",
            "code": code,
            "args": payload.get("args") or {},
        }, timeout=float(payload.get("timeout", 125)))

    if action == "raw":
        request = payload.get("request")
        if not isinstance(request, dict):
            raise ValueError("request dict is required")
        return _post(request, timeout=float(payload.get("timeout", 125)))

    if action == "installed":
        revit_year = os.getenv("REVIT_YEAR", "2022")
        install_root = Path(os.getenv(
            "RDC_HARNESS_INSTALL_ROOT",
            str(HOME / "AppData" / "Local" / "RdcWorkstationHarness"),
        ))
        manifest = HOME / "AppData" / "Roaming" / "Autodesk" / "Revit" / "Addins" / revit_year / "RdcHarness.RevitBridge.addin"
        dll = install_root / "revit-bridge" / "RdcHarness.RevitBridge.dll"
        return {
            "manifest": str(manifest),
            "manifest_exists": manifest.exists(),
            "dll": str(dll),
            "dll_exists": dll.exists(),
            "token_exists": TOKEN_PATH.exists(),
            "health": _health(),
        }

    raise ValueError("unknown revit adapter action: %s" % action)
