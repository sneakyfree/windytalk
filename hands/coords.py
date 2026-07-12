"""Coordinate-space reconciliation (GAP_CLOSING_PLAN Phase 1 #3).

There are three coordinate spaces in play and they are NOT the same:

  - capture px   — pixels of a saved screenshot PNG (what a vision model or a
                   human looking at the image reasons in)
  - logical pts  — what the pointer primitives speak (AT-SPI extents, the
                   RemoteDesktop portal stream, cliclick, xdotool)
  - physical px  — the panel; only ever seen through a capture tool that grabs
                   at physical resolution

Live-measured examples that make this a real bug, not a theory:
  Windy 0 (GNOME 5K):  physical 5120x2880, logical 3840x2160, flameshot 3840x2160
  OC5 (macOS Retina):  cliclick speaks POINTS, screencapture writes 2x pixels
  OC2 (X11 stock DPI): scrot px == xdotool px, 1:1

The rule: `mouse_click` coordinates are CAPTURE-space when a screenshot has
been taken (the agent is pointing at something it saw), mapped here to the
pointer's logical space; with no capture on record they pass through 1:1.
Internally-derived coordinates (AT-SPI extents) are ALREADY logical and must
bypass this mapping — backends use `_click_logical` for those.

Single-monitor assumption for v1 (matches the whole validated fleet); the
mapper takes full-screen sizes, not per-output layouts.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CaptureGeometry:
    """The size of the last screenshot and of the logical screen it captured."""
    capture_w: int
    capture_h: int
    logical_w: int
    logical_h: int

    def to_logical(self, x: float, y: float) -> tuple[int, int]:
        """Map a capture-space point to logical (pointer) space, clamped onto
        the screen — a vision model's box edge must never fling the pointer
        off-screen or onto a phantom negative coordinate."""
        lx = x * self.logical_w / self.capture_w if self.capture_w else x
        ly = y * self.logical_h / self.capture_h if self.capture_h else y
        lx = min(max(round(lx), 0), max(self.logical_w - 1, 0))
        ly = min(max(round(ly), 0), max(self.logical_h - 1, 0))
        return int(lx), int(ly)

    @property
    def identity(self) -> bool:
        return (self.capture_w, self.capture_h) == (self.logical_w, self.logical_h)


def png_size(path: str | Path) -> tuple[int, int] | None:
    """Width/height straight out of a PNG's IHDR — 24 bytes of file, no
    imaging dependency. Returns None for anything that isn't a readable PNG
    (the caller then falls back to identity mapping, never crashes)."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
            return None
        w, h = struct.unpack(">II", head[16:24])
        return (int(w), int(h)) if w > 0 and h > 0 else None
    except OSError:
        return None


def geometry_for(capture_path: str | Path,
                 logical: tuple[int, int] | None) -> CaptureGeometry | None:
    """Build the capture geometry for a just-saved screenshot. Unknown logical
    size (headless probe, tool couldn't say) → assume the capture IS logical
    (identity), which is exactly right on X11/Windows and the safe default
    elsewhere. Unreadable PNG → None (no mapping on record)."""
    size = png_size(capture_path)
    if size is None:
        return None
    lw, lh = logical if logical else size
    return CaptureGeometry(capture_w=size[0], capture_h=size[1],
                           logical_w=int(lw), logical_h=int(lh))
