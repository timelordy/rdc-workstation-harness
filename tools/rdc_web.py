import argparse, json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
TOKEN_DIR = Path(os.getenv("RDC_HARNESS_TOKEN_DIR", str(HOME / ".chatgpt-desktop-agent")))
TOKEN = TOKEN_DIR / "desktop.token"
PORT = int(os.getenv("DESKTOP_AGENT_PORT", "17322"))
BASE = f"http://127.0.0.1:{PORT}/action"
LOG = Path(os.getenv("RDC_HARNESS_AUDIT_LOG", str(HOME / ".rdc-workstation-harness" / "logs" / "browser-actions.ndjson")))

# Share the side-effect rules with the agent: repo layout (src/), install layout
# (desktop-agent/ next to tools/) or a copy placed inside the agent directory.
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent / "src" / "desktop_agent", _HERE.parent / "desktop-agent", _HERE.parent):
    if (_candidate / "catalog.py").exists():
        sys.path.insert(0, str(_candidate))
        break
from catalog import browser_side_effect  # noqa: E402

def call_agent(payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "X-Desktop-Agent-Token": TOKEN.read_text(encoding="utf-8").strip(),
    })
    with urllib.request.urlopen(req, timeout=130) as r:
        return json.loads(r.read().decode("utf-8"))

def log_event(event):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def looks_side_effect(payload):
    return browser_side_effect(payload)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="allow externally visible side-effect click")
    ap.add_argument("json_payload", nargs="?", help="browser payload JSON; if omitted read stdin")
    args = ap.parse_args()

    raw = args.json_payload if args.json_payload is not None else sys.stdin.read()
    if not raw.strip():
        raise SystemExit("Provide browser payload JSON")
    browser_payload = json.loads(raw)

    side_effect = looks_side_effect(browser_payload)
    if side_effect and not args.commit:
        result = {
            "ok": False,
            "blocked": True,
            "reason": "side-effect click requires --commit",
            "browser_payload": browser_payload,
        }
        log_event({"status":"blocked","side_effect":True,"payload":browser_payload})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3

    # The agent enforces the same gate server-side; forward the explicit decision.
    payload = {"action": "browser", "payload": browser_payload, "commit": bool(args.commit)}
    try:
        result = call_agent(payload)
        log_event({"status":"executed","side_effect":side_effect,"payload":browser_payload,"ok":bool(result.get("ok"))})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log_event({"status":"http_error","side_effect":side_effect,"payload":browser_payload,"http_status":e.code})
        print(body)
        return 2
    except Exception as e:
        log_event({"status":"error","side_effect":side_effect,"payload":browser_payload,"error":str(e)})
        print(json.dumps({"ok":False,"error":str(e)}, ensure_ascii=False, indent=2))
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
