"""Phase 1 of docs/GAP_CLOSING_PLAN.md — the pointer engine.

Three live-proven facts drive everything here (memory
windytalk-live-stress-2026-07-12):

  1. Mutter ignores ydotool's virtual POINTER on every GNOME-Wayland box while
     the click reports success — a PHANTOM prong. The GNOME-Wayland pointer is
     the RemoteDesktop portal, and ydotool must not appear in that chain or
     satisfy the pointer capability there.
  2. The portal dance: handle_token-matched Request/Response, SelectDevices
     with persist_mode=2 + single-use restore tokens, absolute motion via a
     LINKED ScreenCast stream.
  3. Coordinate spaces differ for real (macOS points vs 2x screencapture px;
     5K GNOME logical vs physical) — mouse_click coords are capture px mapped
     to logical; AT-SPI-derived coords are already logical and bypass mapping.

No test touches a live desktop or bus: the portal's D-Bus seams are faked, the
mechanisms are injected, and geometry comes from hand-written PNGs.
"""
from __future__ import annotations

import struct
import zlib

import pytest

from hands.backends import linux as lx
from hands.backends import macos as mac
from hands.backends import portal as pt
from hands.backends import windows as win
from hands.coords import CaptureGeometry, geometry_for, png_size

# ============ hands.coords: the mapper ==========================================

def _png_bytes(w: int, h: int) -> bytes:
    """A minimal valid PNG of the given size (1-bit grayscale, empty IDAT)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 1, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b""))


def test_png_size_reads_ihdr(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(_png_bytes(3840, 2160))
    assert png_size(p) == (3840, 2160)


def test_png_size_rejects_non_png_and_truncated(tmp_path):
    bad = tmp_path / "not.png"
    bad.write_bytes(b"JFIF nope")
    assert png_size(bad) is None
    short = tmp_path / "short.png"
    short.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    assert png_size(short) is None
    assert png_size(tmp_path / "absent.png") is None


def test_capture_to_logical_scales_and_rounds():
    # The macOS Retina case: 2x screenshot px -> points.
    g = CaptureGeometry(capture_w=4096, capture_h=2304, logical_w=2048, logical_h=1152)
    assert g.to_logical(4096, 2304) == (2047, 1151)  # clamped inside the screen
    assert g.to_logical(1000, 500) == (500, 250)
    assert g.to_logical(0, 0) == (0, 0)


def test_capture_to_logical_clamps_offscreen():
    g = CaptureGeometry(100, 100, 100, 100)
    assert g.to_logical(-5, 250) == (0, 99), "a vision-box edge must never leave the screen"


def test_geometry_for_defaults_to_identity(tmp_path):
    p = tmp_path / "s.png"
    p.write_bytes(_png_bytes(1920, 1080))
    g = geometry_for(p, None)  # logical unknown -> capture IS logical
    assert g.identity and g.to_logical(15, 20) == (15, 20)


def test_geometry_for_none_when_png_unreadable(tmp_path):
    assert geometry_for(tmp_path / "missing.png", (1920, 1080)) is None


# ============ portal: protocol sequence against faked D-Bus seams ================

class _FakePortal(pt.PortalPointer):
    """Records the request/notify traffic; answers like a modern portal."""

    def __init__(self, fail_notifies_once: bool = False):
        super().__init__()
        self.requests: list[tuple] = []
        self.notifies: list[tuple] = []
        self._fail_once = fail_notifies_once
        self.start_count = 0

    def _request(self, iface, method, prefix, signature, options, timeout=8.0):
        self.requests.append((iface, method, prefix, options))
        if method == "CreateSession":
            return {"session_handle": f"/sess/{len(self.requests)}"}
        if method == "Start":
            self.start_count += 1
            return {"devices": 2,  # pointer granted
                    "streams": [(42, {"size": (3840, 2160)})],
                    "restore_token": f"tok-{self.start_count}"}
        return {}

    def _notify(self, method, signature, args):
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("session closed by compositor")
        self.notifies.append((method, args))


@pytest.fixture()
def token_file(tmp_path, monkeypatch):
    f = tmp_path / "portal_restore_token"
    monkeypatch.setenv("WINDYTALK_PORTAL_TOKEN_FILE", str(f))
    return f


def test_portal_session_dance_persist_mode_and_linked_stream(token_file):
    p = _FakePortal()
    p.ensure_session()
    seq = [(r[0], r[1]) for r in p.requests]
    assert seq == [("RemoteDesktop", "CreateSession"),
                   ("RemoteDesktop", "SelectDevices"),
                   ("ScreenCast", "SelectSources"),   # the LINKED stream for absolute motion
                   ("RemoteDesktop", "Start")]
    select_opts = p.requests[1][3]
    assert select_opts["persist_mode"] == ("u", 2), "the grant must be remembered (persist 2)"
    assert select_opts["types"] == ("u", 2), "pointer device"
    assert "restore_token" not in select_opts, "no token yet on a fresh box"
    assert p.stream_size == (3840, 2160)
    assert token_file.read_text() == "tok-1", "the fresh restore token is persisted at once"


def test_portal_restore_token_round_trip_single_use(token_file):
    token_file.write_text("tok-old")
    p = _FakePortal()
    p.ensure_session()
    assert p.requests[1][3]["restore_token"] == ("s", "tok-old"), "saved token offered on restore"
    assert token_file.read_text() == "tok-1", "Start's NEW token replaces it (single-use)"


def test_portal_click_moves_then_presses_then_releases(token_file):
    p = _FakePortal()
    p.click(100, 200, "left")
    assert [n[0] for n in p.notifies] == [
        "NotifyPointerMotionAbsolute", "NotifyPointerButton", "NotifyPointerButton"]
    move = p.notifies[0][1]
    assert move[2] == 42 and move[3] == 100.0 and move[4] == 200.0, "motion targets the stream node"
    down, up = p.notifies[1][1], p.notifies[2][1]
    assert down[2] == 0x110 and down[3] == 1, "BTN_LEFT press"
    assert up[2] == 0x110 and up[3] == 0, "BTN_LEFT release"


def test_portal_right_button_code_and_no_move_without_coords(token_file):
    p = _FakePortal()
    p.click(None, None, "right")
    assert [n[0] for n in p.notifies] == ["NotifyPointerButton", "NotifyPointerButton"]
    assert p.notifies[0][1][2] == 0x111, "BTN_RIGHT"


def test_portal_scroll_sign_flips_to_libinput_convention(token_file):
    p = _FakePortal()
    p.scroll(3)  # hands: positive = up
    method, args = p.notifies[0]
    assert method == "NotifyPointerAxisDiscrete"
    assert args[2] == 0 and args[3] == -3, "portal axis: positive steps = down, so up = -amount"


def test_portal_devices_zero_refused_crisply(token_file):
    # The live GNOME-46 finding: Start grants the stream but devices=0. The
    # session must fail IMMEDIATELY with a clear message, not limp on to a
    # per-notify "not allowed" later.
    class _NoDevices(_FakePortal):
        def _request(self, iface, method, prefix, signature, options, timeout=8.0):
            res = super()._request(iface, method, prefix, signature, options, timeout)
            if method == "Start":
                res = dict(res)
                res["devices"] = 0  # granted screencast, refused pointer
            return res
    p = _NoDevices()
    with pytest.raises(pt.PortalError) as ei:
        p.ensure_session()
    assert "devices=0" in str(ei.value)


def test_portal_reestablishes_dead_session_once(token_file):
    p = _FakePortal(fail_notifies_once=True)
    p.click(5, 5)
    assert p.start_count == 2, "a closed session is rebuilt transparently, once"
    assert [n[0] for n in p.notifies][-3:] == [
        "NotifyPointerMotionAbsolute", "NotifyPointerButton", "NotifyPointerButton"]


# ============ Linux: the pointer chain is session-aware and phantom-free =========

def _wayland_gnome(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.delenv("WINDYTALK_POINTER", raising=False)


def test_pointer_order_gnome_wayland_is_portal_only(monkeypatch):
    _wayland_gnome(monkeypatch)
    assert lx._pointer_order() == ["portal"], \
        "ydotool's pointer is a PHANTOM under Mutter — it must not be in the chain"


def test_pointer_order_x11_keeps_xdotool_first(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WINDYTALK_POINTER", raising=False)
    assert lx._pointer_order()[0] == "xdotool"


def test_pointer_order_other_wayland_portal_then_ydotool(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "sway")
    monkeypatch.delenv("WINDYTALK_POINTER", raising=False)
    assert lx._pointer_order()[:2] == ["portal", "ydotool"], \
        "wlroots honors virtual pointers — ydotool stays as a prong there"


def test_pointer_order_env_override(monkeypatch):
    _wayland_gnome(monkeypatch)
    monkeypatch.setenv("WINDYTALK_POINTER", "ydotool")
    assert lx._pointer_order()[0] == "ydotool"


def test_gnome_wayland_click_goes_through_portal(monkeypatch):
    _wayland_gnome(monkeypatch)
    monkeypatch.setattr(lx, "_portal_available", lambda: True)
    clicks = []

    class _P:
        stream_size = (3840, 2160)
        def click(self, x, y, button):
            clicks.append((x, y, button))
    b = lx.LinuxBackend()
    monkeypatch.setattr(lx.LinuxBackend, "_portal", lambda self: _P())
    out = b.mouse_click(10, 20, "left")
    assert "clicked at (10, 20)" in out
    assert clicks == [(10, 20, "left")]


def test_gnome_wayland_capability_needs_portal_not_ydotool(monkeypatch):
    _wayland_gnome(monkeypatch)
    # ydotool fully installed and seated — the GNOME phantom case.
    monkeypatch.setattr(lx, "_which", lambda t: t if t == "ydotool" else None)
    monkeypatch.setattr(lx, "_ydotool_available", lambda: True)
    monkeypatch.setattr(lx, "_portal_available", lambda: False)
    caps = lx.LinuxBackend().capabilities()
    assert caps["mouse_click"] is False and caps["scroll"] is False, \
        "a phantom pointer must not report a working mouse"
    assert caps["press_keys"] is True, "the KEYBOARD side of ydotool is real on GNOME"
    monkeypatch.setattr(lx, "_portal_available", lambda: True)
    caps = lx.LinuxBackend().capabilities()
    assert caps["mouse_click"] is True and caps["scroll"] is True


def test_x11_capability_ignores_portal(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(lx, "_which", lambda t: t if t == "xdotool" else None)
    monkeypatch.setattr(lx, "_portal_available", lambda: False)
    assert lx.LinuxBackend().capabilities()["mouse_click"] is True


# ============ capture-space mapping end to end ===================================

def test_linux_mouse_click_maps_capture_px_to_logical(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WINDYTALK_POINTER", raising=False)
    monkeypatch.setattr(lx, "_which", lambda t: t if t == "xdotool" else None)
    moves = []
    monkeypatch.setattr(lx, "_xdotool", lambda *a, **k: moves.append(a))
    b = lx.LinuxBackend()
    # a 2x capture (5K panel screenshotted physical) over a logical screen
    b._last_capture = CaptureGeometry(5120, 2880, 2560, 1440)
    out = b.mouse_click(1000, 500)
    assert ("mousemove", "500", "250") in moves, "capture px must be halved to logical"
    assert "clicked at (1000, 500)" in out, "the caller sees THEIR coordinates back"


def test_linux_click_logical_bypasses_capture_mapping(monkeypatch):
    # AT-SPI extents are already logical — click_element's fallback must not
    # be scaled a second time even with a capture on record.
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WINDYTALK_POINTER", raising=False)
    monkeypatch.setattr(lx, "_which", lambda t: t if t == "xdotool" else None)
    moves = []
    monkeypatch.setattr(lx, "_xdotool", lambda *a, **k: moves.append(a))
    b = lx.LinuxBackend()
    b._last_capture = CaptureGeometry(5120, 2880, 2560, 1440)
    b._click_logical(1000, 500)
    assert ("mousemove", "1000", "500") in moves


def test_linux_screenshot_records_capture_geometry(monkeypatch, tmp_path):
    monkeypatch.setattr(lx.Path, "home", classmethod(lambda cls: tmp_path))

    def fake_capture(dest):
        lx.Path(dest).write_bytes(_png_bytes(3840, 2160))
        return "flameshot"
    monkeypatch.setattr(lx, "_capture", fake_capture)
    monkeypatch.setattr(lx.LinuxBackend, "_logical_size", lambda self: (1920, 1080))
    b = lx.LinuxBackend()
    b.screenshot()
    assert b._last_capture == CaptureGeometry(3840, 2160, 1920, 1080)
    assert b._map_capture_point(3840, 2160) == (1919, 1079)


def test_macos_retina_capture_maps_to_points(monkeypatch):
    monkeypatch.setattr(mac, "_which", lambda t: t if t == "cliclick" else None)
    sent = []
    monkeypatch.setattr(mac, "_cliclick", lambda *a, **k: sent.append(a))
    b = mac.MacOSBackend()
    b._last_capture = CaptureGeometry(4096, 2304, 2048, 1152)  # 2x Retina capture
    out = b.mouse_click(800, 600)
    assert sent == [("c:400,300",)], "screenshot px must be halved to cliclick POINTS"
    assert "clicked at (800, 600)" in out


def test_macos_logical_size_parses_finder_bounds(monkeypatch):
    monkeypatch.setattr(mac, "_osa", lambda s, **k: "0, 0, 2048, 1152")
    assert mac._logical_size() == (2048, 1152)
    monkeypatch.setattr(mac, "_osa", lambda s, **k: (_ for _ in ()).throw(RuntimeError("no AX")))
    assert mac._logical_size() is None


def test_macos_screenshot_records_geometry(monkeypatch, tmp_path):
    monkeypatch.setattr(mac.subprocess, "run", lambda cmd, **k: (
        open(cmd[-1], "wb").write(_png_bytes(4096, 2304))))
    monkeypatch.setattr(mac, "_logical_size", lambda: (2048, 1152))
    import pathlib
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    b = mac.MacOSBackend()
    b.screenshot()
    assert b._last_capture == CaptureGeometry(4096, 2304, 2048, 1152)


def test_windows_click_identity_without_capture(monkeypatch):
    scripts = []
    monkeypatch.setattr(win, "_ps", lambda s, timeout=20: scripts.append(s) or "")
    win.WindowsBackend().mouse_click(300, 400)
    assert "SetCursorPos(300,400)" in scripts[-1], "no capture on record -> identity"


def test_no_capture_on_record_is_identity_mapping():
    b = lx.LinuxBackend()
    assert b._map_capture_point(123, 456) == (123, 456)
