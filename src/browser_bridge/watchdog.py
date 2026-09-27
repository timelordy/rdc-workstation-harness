import os
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
PORT = int(os.getenv("PW_BRIDGE_PORT", "17321"))
HEALTH = f"http://127.0.0.1:{PORT}/health"
LOG = Path(os.getenv("PW_BRIDGE_LOG", str(BASE / "bridge.log")))
NODE = os.getenv("NODE_EXE", "node")
RESTART_DELAY = float(os.getenv("RDC_WATCHDOG_DELAY", "2"))


def alive() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False


def main() -> int:
    if alive():
        return 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    while True:
        with LOG.open("a", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                [NODE, str(BASE / "bridge.js")],
                cwd=str(BASE), stdout=log, stderr=subprocess.STDOUT,
            )
            process.wait()
        time.sleep(RESTART_DELAY)
        if alive():
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
