#!/usr/bin/env python3
"""Windy Talk Linux live self-test — the wizard's final 'prove it on THIS
machine' step (packaging doctrine rule 3: detection wires and VERIFIES).

Run as the desktop user, inside (or with the env of) the graphical session:

    python3 packaging/linux/selftest.py            # report-only (harmless)
    python3 packaging/linux/selftest.py --inject   # + real keystroke (escape)

Report-only exercises capabilities + a real screenshot (written under
~/.windytalk/screenshots/, checked non-trivial). --inject additionally presses
the harmless 'escape' key through the full fallback chain — the wizard runs
this focused on its own window. Exit 0 = green; exit 1 = failures (printed).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hands.backends import get_backend  # noqa: E402


def main() -> int:
    inject = "--inject" in sys.argv[1:]
    backend = get_backend()
    failures: list[str] = []

    caps = backend.capabilities()
    print("capabilities:")
    for tool, ok in sorted(caps.items()):
        print(f"  {'PASS' if ok else '----'}  {tool}")
    for critical in ("type_text", "press_keys", "screenshot"):
        if not caps.get(critical):
            failures.append(f"critical capability {critical} unavailable")

    try:
        msg = backend.screenshot("windytalk-selftest.png")
        shot = Path(msg.rsplit(" ", 1)[-1])
        size = shot.stat().st_size
        if size > 10_000:
            print(f"  PASS  screenshot ({size:,} bytes at {shot})")
        else:
            failures.append(f"screenshot suspiciously small ({size} bytes)")
        shot.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 — self-test reports, never crashes
        failures.append(f"screenshot failed: {exc}")

    if inject:
        try:
            print(f"  PASS  inject: {backend.press_keys('escape')}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"keystroke injection failed: {exc}")

    if failures:
        print("\nSELF-TEST FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nSELF-TEST GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
