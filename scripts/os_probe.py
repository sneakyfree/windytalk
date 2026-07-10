"""Cross-OS capability probe for the Windy Talk hands layer.

Run on any machine (needs only Python 3 + the windytalk `hands/` package on the
path). Reports, as JSON:
  - OS + toolchain (node/python present → can the Electron client run here?)
  - the selected backend + its capability map (which of the 12 tools this machine
    can do — the Swiss-army-knife's blade list)
  - safe live probes: which read-only/benign tools actually execute here (over
    SSH, GUI tools may be permission- or display-limited — that's reported, not hidden)

    python3 scripts/os_probe.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def toolchain() -> dict:
    return {
        "os": sys.platform,
        "python": sys.version.split()[0],
        "node": _ver("node", "-v"),
        "display": os.environ.get("XDG_SESSION_TYPE") or os.environ.get("DISPLAY") or
                   ("aqua" if sys.platform == "darwin" else
                    "windows" if sys.platform.startswith("win") else "headless"),
    }


def _ver(cmd: str, flag: str) -> str:
    if shutil.which(cmd) is None:
        return "MISSING"
    try:
        import subprocess
        return subprocess.run([cmd, flag], capture_output=True, text=True,
                              timeout=8).stdout.strip() or "present"
    except Exception:
        return "present"


def backend_report() -> dict:
    from hands.backends import get_backend
    try:
        b = get_backend()
    except Exception as e:
        return {"error": f"no backend: {e}"}
    caps = b.capabilities()
    # safe live probes — read-only or benign, no destructive side effects
    live = {}
    for tool in ("list_apps", "read_screen", "screenshot"):
        if not caps.get(tool):
            live[tool] = "unsupported"
            continue
        try:
            out = getattr(b, tool)()
            live[tool] = "OK: " + (out.replace("\n", " ")[:70])
        except Exception as e:
            live[tool] = f"{type(e).__name__}: {str(e)[:60]}"
    return {"backend": b.name, "capabilities": caps, "live_probe": live}


def main() -> None:
    report = {"toolchain": toolchain(), "hands": backend_report()}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
