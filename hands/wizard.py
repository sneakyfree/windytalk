"""The first-run wizard engine (GAP_CLOSING_PLAN Phase 4 #8).

Per-OS one-time grants, each PROVEN by a live self-test — never assumed. The
engine is headless and client-agnostic: the desktop app (or a terminal, or the
Phase-5 gauntlet) drives it through JSON:

    python -m hands.wizard list            # every step + live state, NO UI ever
    python -m hands.wizard run <step-id>   # execute ONE step (may pop the one
                                           # sanctioned grant dialog / sudo
                                           # prompt for that step), re-verify

Design rules (the Phase 0/3 honesty doctrine extended to onboarding):
  - probe() answers "is this already satisfied?" with reads only — property
    reads, store lookups, socket stats. A probe must NEVER pop UI. `list` is
    therefore always safe to call, any time, on any box.
  - run() may interact (grant dialog, pkexec prompt) — that interaction IS the
    step. It finishes by re-probing: a step only reports done when the live
    verify passes, so a dismissed dialog or a lying tool reads as not-done.
  - Steps the platform/session doesn't need report state "not-needed" (X11
    needs no portal grants; Windows needs nothing beyond install) — the client
    shows them checked without ever running anything.
  - A run() that needs conditions we can't create (e.g. GNOME 46 refusing
    portal pointer devices) fails HONESTLY with the live error message, never
    a phantom success.

macOS TCC notes: Accessibility and Screen Recording are probed via AppleScript
/ Quartz preflight; run() triggers the OS's own one-time prompt and opens the
right Settings pane. Windows/X11 get selftest-only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# States a step can report from probe/list.
SATISFIED = "satisfied"      # live-verified done
NEEDED = "needed"            # applies here and is not satisfied yet
NOT_NEEDED = "not-needed"    # doesn't apply on this platform/session
UNKNOWN = "unknown"          # probe couldn't tell (reported, never guessed)


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    why: str
    needs_sudo: bool
    probe: Callable[[], str]                      # -> a state string, NO UI
    run: Callable[[], tuple[bool, str]] | None    # -> (ok, detail); None = probe-only


def _b(cond: bool) -> str:
    return SATISFIED if cond else NEEDED


# ---- Linux steps -------------------------------------------------------------------


def _linux_session() -> str:
    from .backends import linux as lx
    return "x11" if lx._on_x11() else "wayland"


def _probe_atspi() -> str:
    from .backends import linux as lx
    return _b(lx._atspi_probe())


def _run_atspi() -> tuple[bool, str]:
    # GNOME gates the accessibility bus behind toolkit-accessibility; flipping
    # it is user-level and takes effect for newly-launched apps.
    gsettings = shutil.which("gsettings")
    if gsettings:
        subprocess.run([gsettings, "set", "org.gnome.desktop.interface",
                        "toolkit-accessibility", "true"],
                       capture_output=True, timeout=10, check=False)
    ok = _probe_atspi() == SATISFIED
    return ok, ("accessibility bus alive" if ok else
                "accessibility bus still dead after enabling "
                "toolkit-accessibility — a session restart may be needed")


def _probe_uinput() -> str:
    from .backends import linux as lx
    return _b(lx._ydotool_available())


def _run_uinput() -> tuple[bool, str]:
    """The wizard's ONE sudo step: udev rule + the bundled ydotoold service
    (packaging/linux/firstrun-linux.sh). Prefers pkexec (graphical prompt);
    reports the manual command when no elevation path exists."""
    script = _firstrun_script()
    if script is None:
        return False, "firstrun-linux.sh not found in payload or repo"
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    cmd = ["bash", str(script), "--user", user]
    ydotoold = _bundled_ydotoold()
    if ydotoold:
        cmd += ["--ydotoold-bin", str(ydotoold)]
    if shutil.which("pkexec"):
        r = subprocess.run(["pkexec", *cmd], capture_output=True, text=True,
                           timeout=300)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip()[-300:]
            return False, f"firstrun script failed: {tail or 'pkexec denied'}"
        ok = _probe_uinput() == SATISFIED
        return ok, "ydotoold socket alive" if ok else \
            "script succeeded but the ydotool socket is still absent"
    return False, "no pkexec on this box — run manually: sudo " + " ".join(cmd)


def _firstrun_script() -> Path | None:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "packaging" / "linux" / "firstrun-linux.sh",  # repo
        here.parent.parent.parent / "firstrun" / "firstrun-linux.sh",      # payload
    ]
    return next((c for c in candidates if c.is_file()), None)


def _bundled_ydotoold() -> Path | None:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent / "tools" / "ydotoold",                  # payload
        here.parent.parent / "packaging" / "linux" / "out" / "ydotoold",   # repo
    ]
    return next((c for c in candidates if c.is_file()), None)


def _probe_pointer_grant() -> str:
    from .backends import portal as pt
    from .backends.portal import _token_file
    if not pt.PortalPointer.available():
        return NEEDED  # portal missing entirely — run() will say so honestly
    return _b(_token_file().is_file())


def _run_pointer_grant() -> tuple[bool, str]:
    """Interactive RemoteDesktop grant: ensure_session pops the compositor's
    'allow remote control' dialog once; persist_mode=2 + the saved restore
    token make every later session silent. devices=0 (GNOME 46 / g-r-d 46.3)
    fails here honestly — see the Phase 1 finding."""
    from .backends import portal as pt
    if not pt.PortalPointer.available():
        return False, "no RemoteDesktop portal on the session bus"
    try:
        pointer = pt.PortalPointer()
        pointer.ensure_session()
    except pt.PortalError as e:
        return False, str(e)
    ok = _probe_pointer_grant() == SATISFIED
    return ok, ("remote-control grant remembered (restore token saved)"
                if ok else "grant completed but no restore token was returned")


def _probe_screenshot_grant() -> str:
    from .backends import linux as lx
    return _b(lx._portal_shot_usable())


def _run_screenshot_grant() -> tuple[bool, str]:
    """Interactive Screenshot request — the desktop shows its own dialog and
    seeds the permission store; from then on the capture chain's portal rung
    (Phase 3) is silent. Verified by the store AND a real capture."""
    from .backends import linux as lx
    from .backends import portal as pt
    if not pt.PortalScreenshot.available():
        return False, "no Screenshot portal on the session bus"
    if not pt.PortalScreenshot().grant():
        return False, "grant dialog denied, dismissed, or timed out"
    if _probe_screenshot_grant() != SATISFIED:
        return False, ("grant accepted but the permission store still has no "
                       "non-sandboxed 'yes' — compositor didn't persist it")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="windytalk-wizard-") as td:
        if lx._capture(str(Path(td) / "verify.png")) is None:
            return False, "grant recorded but a real capture still failed"
    return True, "screenshot grant recorded; live capture verified"


# ---- macOS steps -------------------------------------------------------------------


def _probe_mac_accessibility() -> str:
    # The EXACT operation the backend needs (its _focused_window query), so
    # 'satisfied' means the product works — not that some weaker call worked.
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to get name of first process '
         "whose frontmost is true"],
        capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        return SATISFIED
    err = (r.stderr or "").strip()
    # the backend's live-derived denial signatures (hands/backends/macos.py)
    if "not allowed assistive access" in err or "1002" in err or "-25211" in err:
        return NEEDED
    return UNKNOWN  # some other failure (no System Events?) — never guess


def _run_mac_accessibility() -> tuple[bool, str]:
    # The failed System Events call above triggers macOS's own one-time prompt;
    # deep-link straight to the right pane for the manual grant.
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference"
                    ".security?Privacy_Accessibility"],
                   capture_output=True, timeout=10, check=False)
    ok = _probe_mac_accessibility() == SATISFIED
    return ok, ("accessibility granted" if ok else
                "grant Windy Talk under Privacy & Security → Accessibility, "
                "then run this step again")


def _probe_mac_screen() -> str:
    code = ("import Quartz, sys;"
            "sys.exit(0 if Quartz.CGPreflightScreenCaptureAccess() else 1)")
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, timeout=15)
    if r.returncode not in (0, 1):
        return UNKNOWN  # no pyobjc in this python — report, never guess
    return _b(r.returncode == 0)


def _run_mac_screen() -> tuple[bool, str]:
    code = "import Quartz; Quartz.CGRequestScreenCaptureAccess()"
    subprocess.run([sys.executable, "-c", code], capture_output=True,
                   timeout=15, check=False)
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference"
                    ".security?Privacy_ScreenCapture"],
                   capture_output=True, timeout=10, check=False)
    ok = _probe_mac_screen() == SATISFIED
    return ok, ("screen recording granted" if ok else
                "grant Windy Talk under Privacy & Security → Screen Recording "
                "(macOS requires an app restart after granting), then re-run")


# ---- the universal self-test -------------------------------------------------------


def _selftest() -> tuple[bool, str]:
    """The live proof: real capabilities + a real capture on THIS box. This is
    what makes every earlier step's 'done' mean something."""
    from .backends import get_backend
    b = get_backend()
    caps = b.capabilities()
    lines = [f"{'PASS' if ok else 'FAIL'}  {tool}" for tool, ok in sorted(caps.items())]
    try:
        msg = b.screenshot("windytalk-wizard-selftest.png")
        path = msg.rsplit("Saved screenshot to ", 1)[-1].strip()
        size = Path(path).stat().st_size
        lines.append(f"PASS  screenshot file ({size:,} bytes)")
        shot_ok = size > 0
    except Exception as e:  # noqa: BLE001 — the selftest reports, never raises
        lines.append(f"FAIL  screenshot file ({e})")
        shot_ok = False
    ok = shot_ok and all(caps.get(t, False) for t in ("screenshot", "run_shell"))
    return ok, "\n".join(lines)


# ---- the per-OS step tables --------------------------------------------------------


def steps() -> list[Step]:
    if sys.platform.startswith("linux"):
        wayland = _linux_session() == "wayland"
        return [
            Step("accessibility-bus", "Turn on the accessibility bus",
                 "read_screen/click_element/type_text sense windows through "
                 "AT-SPI; GNOME ships with it gated off",
                 False, _probe_atspi, _run_atspi),
            Step("input-uinput", "Install the typing service (one sudo prompt)",
                 "keyboard input on Wayland needs the bundled ydotoold uinput "
                 "daemon — apt ships no daemon at all",
                 True, _probe_uinput, _run_uinput),
            Step("pointer-grant", "Allow remote control (mouse)",
                 "GNOME-Wayland's only honest pointer is the RemoteDesktop "
                 "portal; one Share click, remembered forever",
                 False,
                 _probe_pointer_grant if wayland else (lambda: NOT_NEEDED),
                 _run_pointer_grant if wayland else None),
            Step("screenshot-grant", "Allow screenshots",
                 "the capture chain's silent portal rung needs one recorded "
                 "grant; until then screenshots fall to slower tools",
                 False,
                 _probe_screenshot_grant if wayland else (lambda: NOT_NEEDED),
                 _run_screenshot_grant if wayland else None),
            Step("selftest", "Prove it all works",
                 "live capabilities + a real capture — done means verified",
                 False, lambda: NEEDED, _selftest),
        ]
    if sys.platform == "darwin":
        return [
            Step("accessibility", "Grant Accessibility (TCC)",
                 "clicking and reading windows needs the Accessibility "
                 "permission", False, _probe_mac_accessibility,
                 _run_mac_accessibility),
            Step("screen-recording", "Grant Screen Recording (TCC)",
                 "screenshots (and the vision spine) need the Screen "
                 "Recording permission", False, _probe_mac_screen,
                 _run_mac_screen),
            Step("selftest", "Prove it all works",
                 "live capabilities + a real capture — done means verified",
                 False, lambda: NEEDED, _selftest),
        ]
    # Windows: SendKeys/UIA/.NET capture all work in an active session with no
    # grants at all — the wizard is just the proof.
    return [
        Step("selftest", "Prove it all works",
             "live capabilities + a real capture — done means verified",
             False, lambda: NEEDED, _selftest),
    ]


# ---- CLI ---------------------------------------------------------------------------


def _state_of(step: Step) -> str:
    try:
        return step.probe()
    except Exception:  # noqa: BLE001 — a broken probe is reported, not raised
        return UNKNOWN


def list_steps() -> list[dict]:
    return [{"id": s.id, "title": s.title, "why": s.why,
             "needs_sudo": s.needs_sudo, "state": _state_of(s)}
            for s in steps()]


def run_step(step_id: str) -> dict:
    for s in steps():
        if s.id != step_id:
            continue
        state = _state_of(s)
        if s.run is None or state == NOT_NEEDED:
            return {"id": s.id, "ok": True, "state": NOT_NEEDED,
                    "detail": "nothing to do on this platform/session"}
        if state == SATISFIED and s.id != "selftest":
            # Idempotency: re-running a granted step must not pop its UI again.
            return {"id": s.id, "ok": True, "state": SATISFIED,
                    "detail": "already satisfied (live-verified) — nothing to do"}
        try:
            ok, detail = s.run()
        except Exception as e:  # noqa: BLE001 — a crashed step is an honest failure
            ok, detail = False, f"step crashed: {e}"
        return {"id": s.id, "ok": ok, "state": _state_of(s), "detail": detail}
    return {"id": step_id, "ok": False, "state": UNKNOWN,
            "detail": f"unknown step {step_id!r}"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["list"]:
        print(json.dumps({"steps": list_steps()}, indent=1))
        return 0
    if args[:1] == ["run"] and len(args) == 2:
        res = run_step(args[1])
        print(json.dumps(res, indent=1))
        return 0 if res["ok"] else 1
    print("usage: python -m hands.wizard list | run <step-id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
