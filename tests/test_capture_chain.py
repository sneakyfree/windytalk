"""Phase 3 of docs/GAP_CLOSING_PLAN.md — screenshot & sensing.

Live-proven facts driving these tests (probed 2026-07-12 on OC3 GNOME 46 and
Windy 0 GNOME 50):

  1. org.freedesktop.portal.Screenshot answers a NON-interactive request
     silently and fast (0.14s @1080p / 1.03s @4K) when the permission store's
     non-sandboxed entry ('') says 'yes' — and it writes its own file under
     ~/Pictures, which the rung must copy out and remove.
  2. The portal rung must never be the thing that pops a first-use dialog:
     it runs only when the store already records the grant
     (_portal_shot_usable), and a request that does block on UI is Close()d
     at timeout.
  3. On Wayland the portal rung goes FIRST (sanctioned, no tool binary); on
     X11 the proven native grabbers stay first and the portal slots in just
     before flameshot.

No test touches a live desktop or bus: portal seams are faked, tool presence
is monkeypatched, and subprocess never runs a real grabber.
"""
from __future__ import annotations

import struct
import zlib

import pytest

from hands.backends import linux as lx
from hands.backends import portal as pt


def _png_bytes(w: int, h: int) -> bytes:
    """A minimal valid PNG of the given size (1-bit grayscale, empty IDAT)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 1, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b""))

# ============ PortalScreenshot.capture (at its _request seam) ===================


class _FakeShot(pt.PortalScreenshot):
    """Overrides the _request seam exactly like the pointer tests do."""

    def __init__(self, response: dict | Exception):
        self._response = response
        self.requests: list[dict] = []

    def _request(self, options, timeout):
        self.requests.append({"options": options, "timeout": timeout})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_capture_copies_portal_file_and_removes_original(tmp_path):
    src = tmp_path / "Screenshot-1.png"
    src.write_bytes(b"png-bytes")
    dest = tmp_path / "out.png"
    shot = _FakeShot({"uri": f"file://{src}"})
    assert shot.capture(str(dest)) is True
    assert dest.read_bytes() == b"png-bytes"
    assert not src.exists()  # never litter the user's Pictures
    # non-interactive ONLY — interactive grants belong to the wizard (Phase 4)
    assert shot.requests[0]["options"] == {"interactive": ("b", False)}


def test_capture_false_on_portal_error(tmp_path):
    shot = _FakeShot(pt.PortalError("Screenshot.Screenshot response code 2"))
    assert shot.capture(str(tmp_path / "out.png")) is False


@pytest.mark.parametrize("res", [
    {},                                # no uri at all
    {"uri": "clipboard:"},             # non-file uri
    {"uri": "file:///nonexistent/x"},  # dead path
])
def test_capture_false_on_unusable_uri(tmp_path, res):
    assert _FakeShot(res).capture(str(tmp_path / "out.png")) is False


def test_capture_false_on_empty_portal_file(tmp_path):
    src = tmp_path / "empty.png"
    src.write_bytes(b"")
    shot = _FakeShot({"uri": f"file://{src}"})
    assert shot.capture(str(tmp_path / "out.png")) is False


# ============ _portal_shot_usable: the no-UI gate ===============================


# The real gate, captured at import time — conftest's autouse fixture stubs the
# module attribute False for every test, so this is the only handle on it.
_REAL_GATE = lx._portal_shot_usable


def test_usable_needs_portal_AND_recorded_grant(monkeypatch):
    for available, granted, want in [(True, True, True), (True, False, False),
                                     (False, True, False), (False, False, False)]:
        monkeypatch.setattr(pt.PortalScreenshot, "available",
                            staticmethod(lambda a=available: a))
        monkeypatch.setattr(pt.PortalScreenshot, "permission_granted",
                            staticmethod(lambda g=granted: g))
        assert _REAL_GATE() is want


# ============ _capture rung order ===============================================


def _order_probe(monkeypatch, tmp_path, *, x11: bool, portal_usable: bool,
                 present: set[str], portal_ok: bool = False):
    """Run _capture with every rung faked to fail (unless told to succeed) and
    return the order in which rungs were attempted."""
    attempts: list[str] = []
    dest = tmp_path / "shot.png"

    monkeypatch.setattr(lx, "_on_x11", lambda: x11)
    monkeypatch.setattr(lx, "_portal_shot_usable", lambda: portal_usable)
    monkeypatch.setattr(lx, "_which", lambda t: t in present)

    def fake_portal_capture(d):
        attempts.append("portal")
        if portal_ok:
            lx.Path(d).write_bytes(b"png")
            return True
        return False
    monkeypatch.setattr(lx, "_portal_capture", fake_portal_capture)

    def fake_run(cmd, **kw):
        attempts.append(cmd[0])

        class R:
            stdout = b""
        return R()
    monkeypatch.setattr(lx.subprocess, "run", fake_run)

    winner = lx._capture(str(dest))
    return attempts, winner


ALL_TOOLS = {"grim", "gnome-screenshot", "spectacle", "scrot", "import", "flameshot"}


def test_wayland_portal_first_then_tools_then_flameshot(monkeypatch, tmp_path):
    attempts, winner = _order_probe(monkeypatch, tmp_path, x11=False,
                                    portal_usable=True, present=ALL_TOOLS)
    assert attempts == ["portal", "grim", "gnome-screenshot", "spectacle",
                        "scrot", "import", "flameshot"]
    assert winner is None  # everything failed -> honest None


def test_x11_native_first_portal_before_flameshot(monkeypatch, tmp_path):
    attempts, _ = _order_probe(monkeypatch, tmp_path, x11=True,
                               portal_usable=True, present=ALL_TOOLS)
    assert attempts == ["grim", "gnome-screenshot", "spectacle", "scrot",
                        "import", "portal", "flameshot"]


def test_portal_rung_absent_without_grant(monkeypatch, tmp_path):
    attempts, _ = _order_probe(monkeypatch, tmp_path, x11=False,
                               portal_usable=False, present=ALL_TOOLS)
    assert "portal" not in attempts


def test_portal_success_short_circuits(monkeypatch, tmp_path):
    attempts, winner = _order_probe(monkeypatch, tmp_path, x11=False,
                                    portal_usable=True, present=ALL_TOOLS,
                                    portal_ok=True)
    assert attempts == ["portal"]
    assert winner == "portal"


def test_portal_failure_pivots_to_tools(monkeypatch, tmp_path):
    """A dead portal rung (code 2, timeout, whatever) must not kill the chain."""
    attempts, winner = _order_probe(monkeypatch, tmp_path, x11=False,
                                    portal_usable=True, present=set())
    assert attempts == ["portal"]  # no tools present, flameshot absent
    assert winner is None


def test_screenshot_records_geometry_via_portal(monkeypatch, tmp_path):
    """The portal rung feeds the same geometry bookkeeping as every other rung
    (mouse_click maps capture px -> logical using it)."""
    b = lx.LinuxBackend()
    monkeypatch.setattr(lx, "_capture",
                        lambda d: (lx.Path(d).write_bytes(_png_bytes(1920, 1080)), "portal")[1])
    monkeypatch.setattr(lx.LinuxBackend, "_logical_size", lambda self: None)
    monkeypatch.setattr(lx.Path, "home", classmethod(lambda cls: tmp_path))
    out = b.screenshot("p3.png")
    assert "Saved screenshot to" in out
    assert b._last_capture is not None
