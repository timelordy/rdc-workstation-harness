"""Terminal entry point to the Desktop Agent.

Built for clients that only have a shell on this PC, such as ChatGPT through
Remote Desktop Commander. Output is compact JSON on stdout and never contains
base64: images are saved to disk and returned as a path to open with read_file.

  pc-agent health
  pc-agent capabilities
  pc-agent describe uia_click
  pc-agent look                                   whole screen
  pc-agent look --window AutoCAD                  window title regex
  pc-agent look --hwnd 133850 --max-side 1024
  pc-agent '{"action": "windows", "title_re": "Excel"}'
  pc-agent @request.json
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.getenv("USERPROFILE", str(Path.home())))
TOKEN_PATH = Path(os.getenv("RDC_HARNESS_TOKEN_DIR", str(HOME / ".chatgpt-desktop-agent"))) / "desktop.token"
PORT = int(os.getenv("DESKTOP_AGENT_PORT", "17322"))
BASE = f"http://127.0.0.1:{PORT}"


def request(method, route, payload=None):
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["X-Desktop-Agent-Token"] = TOKEN_PATH.read_text(encoding="utf-8").strip()
    seconds = float(payload.get("timeout", 0) or 0) if isinstance(payload, dict) else 0
    req = urllib.request.Request(BASE + route, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=max(130.0, seconds + 15)) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            result = json.loads(body)
        except ValueError:
            result = {"ok": False, "error": body or str(e)}
        result["http_status"] = e.code
        return result
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"desktop agent is not reachable on {BASE}: {e.reason}",
                "hint": "check the 'RDC Harness - Desktop Agent' scheduled task or run scripts/diagnose.ps1"}


def ascii_path(path):
    """8.3 short form of a non-ASCII path (e.g. C:\\Users\\Иван -> C:\\Users\\D0A5~1).

    Terminals between this CLI and the model (PowerShell 5.1 on an OEM code page)
    mangle Cyrillic, and read_file then fails on the garbled path. The short form
    is pure ASCII. Falls back to the original path if short names are disabled.
    """
    if path.isascii() or os.name != "nt":
        return path
    import ctypes
    # Shorten only the directory: the file name we generate is already ASCII and
    # must keep its real name and extension (read_file picks the type by extension).
    folder, name = os.path.split(path)
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(folder, buf, len(buf)) and buf.value.isascii() and name.isascii():
        return os.path.join(buf.value, name)
    return path


def strip_images(value):
    """Safety net: a terminal must never receive base64 blobs."""
    if isinstance(value, dict):
        if isinstance(value.get("data"), str) and len(value["data"]) > 2000:
            return dict(value, data=f"<{len(value['data'])} base64 chars omitted; use look --save>")
        return {k: strip_images(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_images(v) for v in value]
    return value


def look_payload(argv):
    ap = argparse.ArgumentParser(prog="pc-agent look", description="Save a screenshot for read_file.")
    ap.add_argument("--window", help="window title regex")
    ap.add_argument("--hwnd", type=int)
    ap.add_argument("--region", help="left,top,width,height")
    ap.add_argument("--monitor", type=int)
    ap.add_argument("--max-side", type=int)
    args = ap.parse_args(argv)
    payload = {"action": "look", "save": True}
    if args.window:
        payload["window"] = {"title_re": args.window}
    if args.hwnd is not None:
        payload["hwnd"] = args.hwnd
    if args.region:
        left, top, width, height = (int(x) for x in args.region.split(","))
        payload["region"] = {"left": left, "top": top, "width": width, "height": height}
    if args.monitor is not None:
        payload["monitor"] = args.monitor
    if args.max_side:
        payload["max_side"] = args.max_side
    return payload


def parse(argv):
    if not argv:
        raw = sys.stdin.read()
        if not raw.strip():
            raise SystemExit(__doc__)
        return json.loads(raw)
    head = argv[0]
    if head == "look":
        return look_payload(argv[1:])
    if head == "describe" and len(argv) == 2:
        return {"action": "describe", "name": argv[1]}
    if head.startswith("@"):
        return json.loads(Path(head[1:]).read_text(encoding="utf-8"))
    if head.startswith("{"):
        return json.loads(" ".join(argv))
    if len(argv) == 1:
        return {"action": head}
    raise SystemExit(__doc__)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if argv == ["health"]:
        result = request("GET", "/health")
    else:
        payload = parse(argv)
        is_look = payload.get("action") == "look"
        if is_look:
            payload["save"] = True
        result = request("POST", "/action", payload)
        if is_look and result.get("ok"):
            result["result"]["path"] = ascii_path(result["result"]["path"])
            result["next"] = "open result.path with read_file to see the image"
    result = strip_images(result)
    # ASCII-escaped JSON survives any console code page; valid JSON either way.
    sys.stdout.write(json.dumps(result, ensure_ascii=True, indent=1) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
