"""Shared test guardrails: NEVER touch the live desktop.

pytest routinely runs on real, in-use machines (including Grant's active
desktop), so no test may ever capture the actual screen, query real window
focus, or inject anything. These autouse stubs pin the live seams the Phase 0
work introduced:

  - the Linux FUNCTIONAL capability probes (a real AT-SPI query / a real
    throwaway screen capture) → replaced with presence-based stand-ins, so a
    plain `capabilities()` call in a test can't spawn flameshot against the
    developer's screen;
  - `_focused_window` on all three backends → a benign fake window, so
    `type_text` tests exercise the guard without an accessibility round-trip.

A test that exercises the REAL probe/guard logic monkeypatches these seams
itself (a test-level monkeypatch overrides the autouse stub) or calls the
module originals captured at import time — see tests/test_focus_guard.py.
"""
from __future__ import annotations

import pytest

from hands.backends import linux as _lx
from hands.backends import macos as _mac
from hands.backends import windows as _win
from hands.backends.base import FocusInfo

_FAKE_FOCUS = FocusInfo(app="TestApp", title="Test Window")


@pytest.fixture(autouse=True)
def _no_live_desktop(monkeypatch):
    # Presence-based stand-ins for the functional probes (reads _lx._which at
    # call time, so tests that monkeypatch _which keep their semantics).
    monkeypatch.setattr(_lx, "_atspi_probe", lambda: True)
    monkeypatch.setattr(
        _lx, "_screenshot_probe",
        lambda: any(_lx._which(c) for c in
                    ("grim", "gnome-screenshot", "spectacle", "scrot", "import", "flameshot")))
    monkeypatch.setattr(_lx.LinuxBackend, "_focused_window", lambda self: _FAKE_FOCUS)
    monkeypatch.setattr(_mac, "_focused_window", lambda: _FAKE_FOCUS)
    monkeypatch.setattr(_win, "_focused_window", lambda: _FAKE_FOCUS)
    # The portal probes are REAL session-bus property/store reads — stub them
    # False so a capabilities() or _capture() call in a test never touches the
    # developer's live portal (the screenshot rung WOULD silently capture the
    # dev's screen on a granted box like Windy 0).
    monkeypatch.setattr(_lx, "_portal_available", lambda: False)
    monkeypatch.setattr(_lx, "_portal_shot_usable", lambda: False)
    # The vision lane must never be live in tests, whatever the dev box's env
    # says — a stray WINDYTALK_VISION_URL would turn click tests into real
    # screenshots + model calls.
    monkeypatch.delenv("WINDYTALK_VISION_URL", raising=False)
