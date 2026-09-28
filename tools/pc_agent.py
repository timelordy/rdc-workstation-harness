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
  pc-agent look --share                           also publish, print a link
  pc-agent share <file>                           publish any file, print a link
  pc-agent '{"action": "windows", "title_re": "Excel"}'
  pc-agent @request.json

Sharing: some chat clients (ChatGPT web via Remote Desktop Commander) drop
images returned by read_file. --share hands the file to a command you choose,
set in RDC_HARNESS_SHARE_CMD, e.g. an uploader to your own storage. It gets
the file path as its last argument and must print the URL as the last line of
stdout. Nothing is uploaded unless that variable is set.
"""
import argparse
import json
import os
import shlex
import subprocess
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
    """8.3 short form of a non-ASCII path (e.g. C:\\Users\\Анвар -> C:\\Users\\2AEA~1).

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


SHARE_ENV = "RDC_HARNESS_SHARE_CMD"


def share_command():
    """The configured uploader, from the environment or the user registry."""
    raw = os.getenv(SHARE_ENV)
    if not raw and os.name == "nt":
        # Read the persisted user value too: a long-running parent (e.g. the
        # Remote Commander) may have started before the variable was set.
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                raw = winreg.QueryValueEx(key, SHARE_ENV)[0]
        except OSError:
            raw = None
    return raw or None


def share(path, timeout=120):
    """Run the configured uploader for one file; returns a result dict."""
    raw = share_command()
    if not raw:
        return {"ok": False, "error": f"sharing is not configured: set {SHARE_ENV}",
                "hint": "a command that takes a file path and prints a URL; see docs/CHATGPT_RU.md"}
    if not Path(path).is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    cmd = shlex.split(raw, posix=False) + [str(path)]
    cmd = [c[1:-1] if len(c) > 1 and c[0] == c[-1] == '"' else c for c in cmd]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"share command failed: {e}"}
    lines = [line.strip() for line in run.stdout.splitlines() if line.strip()]
    url = lines[-1] if lines else ""
    if run.returncode != 0 or not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "share command did not print a URL",
                "returncode": run.returncode, "stderr": run.stderr[-2000:]}
    return {"ok": True, "url": url}


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
    ap.add_argument("--share", action="store_true", help=f"publish via {SHARE_ENV} and print the URL")
    args = ap.parse_args(argv)
    payload = {"action": "look", "save": True}
    if args.share:
        payload["_share"] = True
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
    elif argv[:1] == ["share"]:
        if len(argv) != 2:
            raise SystemExit("usage: pc-agent share <file>")
        result = share(argv[1])
    else:
        payload = parse(argv)
        is_look = payload.get("action") == "look"
        want_share = bool(payload.pop("_share", False))
        if is_look:
            payload["save"] = True
        result = request("POST", "/action", payload)
        if is_look and result.get("ok"):
            result["result"]["path"] = ascii_path(result["result"]["path"])
            result["next"] = "open result.path with read_file to see the image"
            if want_share:
                shared = share(result["result"]["path"])
                if shared["ok"]:
                    result["result"]["url"] = shared["url"]
                    result["next"] = "give the user result.url (a link to the image)"
                else:
                    result["share_error"] = shared
    result = strip_images(result)
    # ASCII-escaped JSON survives any console code page; valid JSON either way.
    sys.stdout.write(json.dumps(result, ensure_ascii=True, indent=1) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
