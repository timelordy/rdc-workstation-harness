import json
import subprocess
import sys
import uuid
from pathlib import Path

import win32com.client

DESCRIPTION = (
    "AutoCAD via COM. Attaches to an already running AutoCAD; never starts one. "
    "Call status first to see open documents."
)
ACTIONS = {
    "status": {
        "effect": "read",
        "summary": "AutoCAD version, visibility, active and open documents.",
        "params": {},
    },
    "python": {
        "effect": "write",
        "summary": (
            "Run Python in a separate process with app, doc, modelspace and args "
            "pre-bound; assign the return value to `result`. Can modify the drawing."
        ),
        "params": {
            "code": "Python source (required)",
            "args": "JSON object available as `args`",
            "timeout": "seconds, 1..1800, default 120",
        },
        "example": {"action": "python", "code": "result = modelspace.Count"},
    },
}

PROGIDS = [
    "AutoCAD.Application.24.1",
    "AutoCAD.Application",
]


def attach():
    errors = []
    for progid in PROGIDS:
        try:
            return win32com.client.GetActiveObject(progid), progid
        except Exception as e:
            errors.append("%s: %s" % (progid, e))
    raise RuntimeError("No running AutoCAD COM instance: " + " | ".join(errors))


def status():
    app, progid = attach()
    docs = []
    try:
        for i in range(app.Documents.Count):
            d = app.Documents.Item(i)
            docs.append({
                "name": getattr(d, "Name", None),
                "full_name": getattr(d, "FullName", None),
                "read_only": getattr(d, "ReadOnly", None),
            })
    except Exception:
        pass

    active = None
    try:
        if app.Documents.Count:
            d = app.ActiveDocument
            active = {
                "name": getattr(d, "Name", None),
                "full_name": getattr(d, "FullName", None),
            }
    except Exception:
        pass

    return {
        "ok": True,
        "progid": progid,
        "name": getattr(app, "Name", "AutoCAD"),
        "version": getattr(app, "Version", None),
        "visible": bool(getattr(app, "Visible", True)),
        "active_document": active,
        "documents": docs,
    }
def run_python(payload, context):
    body = payload.get("code")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("code is required")

    args = payload.get("args") or {}
    timeout = max(1.0, min(float(payload.get("timeout", 120)), 1800.0))
    artifact_dir = Path(context["artifact_dir"])
    work_dir = artifact_dir / "autocad-runtime"
    work_dir.mkdir(parents=True, exist_ok=True)
    script_path = work_dir / ("run-" + uuid.uuid4().hex + ".py")

    wrapper = """import json
import win32com.client

PROGIDS = ["AutoCAD.Application.24.1", "AutoCAD.Application"]
app = None
last_error = None
for _p in PROGIDS:
    try:
        app = win32com.client.GetActiveObject(_p)
        break
    except Exception as _e:
        last_error = _e
if app is None:
    raise RuntimeError("Could not attach AutoCAD: %s" % last_error)

doc = None
modelspace = None
try:
    if app.Documents.Count:
        doc = app.ActiveDocument
        modelspace = doc.ModelSpace
except Exception:
    pass

args = json.loads(__ARGS_JSON__)
result = None

__USER_CODE__

print(json.dumps({"ok": True, "result": result}, ensure_ascii=True, default=str))
"""
    wrapper = wrapper.replace(
        "__ARGS_JSON__",
        repr(json.dumps(args, ensure_ascii=True)),
    ).replace(
        "__USER_CODE__",
        body,
    )

    script_path.write_text(wrapper, encoding="utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        stdout = completed.stdout.strip()
        if completed.returncode != 0:
            return {
                "ok": False,
                "returncode": completed.returncode,
                "stdout": stdout[-50000:],
                "stderr": completed.stderr[-50000:],
            }
        if not stdout:
            return {"ok": True, "result": None}
        try:
            return json.loads(stdout.splitlines()[-1])
        except Exception:
            return {"ok": True, "stdout": stdout[-50000:]}
    finally:
        try:
            script_path.unlink()
        except Exception:
            pass
def handle(payload, context):
    action = payload.get("action", "status")
    if action == "status":
        return status()
    if action == "python":
        return run_python(payload, context)
    raise ValueError(
        "unknown autocad adapter action: %s (valid: %s)" % (action, ", ".join(sorted(ACTIONS)))
    )
