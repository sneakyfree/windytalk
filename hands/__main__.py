"""Run the hands control surface locally: `python -m hands` (default :8781).

Confirmer for the trust tiers (§9): auto_allow runs; ask_first / always_confirm
prompt on the console (y/N). Set WINDYTALK_HANDS_AUTOAPPROVE=1 to auto-approve
everything (convenient for a solo audio test — do NOT use unattended).
"""
from __future__ import annotations

import os

from hands import HandsSurface, TierPolicy


def _console_confirmer(tool: str, args: dict, tier: str) -> bool:
    if os.environ.get("WINDYTALK_HANDS_AUTOAPPROVE") == "1":
        print(f"[hands] auto-approved {tier} {tool} {args}")
        return True
    try:
        ans = input(f"[hands] {tier.upper()} — allow {tool}({args})? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def main() -> None:
    host = os.environ.get("WINDYTALK_HANDS_HOST", "127.0.0.1")
    port = int(os.environ.get("WINDYTALK_HANDS_PORT", "8781"))
    surface = HandsSurface(policy=TierPolicy(confirmer=_console_confirmer))
    h, p = surface.serve(host, port)
    print(f"[hands] control surface on http://{h}:{p}  (backend: {surface.backend.name})")
    print("[hands] Ctrl-C to stop.")
    try:
        import threading
        threading.Event().wait()
    except KeyboardInterrupt:
        surface.stop()


if __name__ == "__main__":
    main()
