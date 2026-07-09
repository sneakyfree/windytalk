"""Desktop-control backends. Linux now; macOS/Windows are Phase 3."""
from __future__ import annotations

import sys

from .base import HandsBackend, UnsupportedTool


def get_backend(name: str | None = None) -> HandsBackend:
    name = name or _detect()
    if name == "linux":
        from .linux import LinuxBackend
        return LinuxBackend()
    raise UnsupportedTool(f"no hands backend for platform {name!r} yet (Phase 3)")


def _detect() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return sys.platform


__all__ = ["get_backend", "HandsBackend", "UnsupportedTool"]
