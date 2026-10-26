"""Read the product VERSION in a checkout or installed runtime."""
import re
from pathlib import Path


def read_version(base_dir=None):
    base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    for candidate in (base / "VERSION", base.parent.parent / "VERSION"):
        try:
            version = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if re.fullmatch(r"\d+\.\d+\.\d+", version):
            return version
    return "unknown"


VERSION = read_version()
