"""Desktop-control backend abstraction.

One backend per OS implements the hands.mcp.v1 tool set. Each method returns a
short human/agent-readable string (handed straight back as the tool result).
The surface (hands/surface.py) wraps returns into the contract's {ok,result,error}
shape and applies §9 trust tiers — backends themselves are unaware of tiers.

A backend that cannot implement a tool on its platform raises
`UnsupportedTool`; the surface maps that to ok:false, error:"unsupported" so the
tool never disappears from the list (hands.mcp.v1 platform_note).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

# The frozen hands.mcp.v1 tool names, in contract order.
TOOL_NAMES = (
    "open_app", "web_search", "open_url", "type_text", "press_keys",
    "click_element", "mouse_click", "scroll", "read_screen", "list_apps",
    "screenshot", "run_shell",
)


class UnsupportedTool(NotImplementedError):
    """This backend does not implement this tool on this platform."""


class Mechanism:
    """One concrete way to perform an action (e.g. 'xdotool type', 'ydotool
    type', 'wtype'). The redundancy primitive behind the Swiss-army-knife: a
    backend lists several mechanisms per action in preference order and
    `run_chain` tries each until one seats.

    available(): is this mechanism present on THIS box at all (skip if not).
    run(): perform the action; raise on failure (so the chain moves on).
    """

    __slots__ = ("name", "_available", "_run")

    def __init__(self, name: str, available: Callable[[], bool] | bool,
                 run: Callable[[], Any]) -> None:
        self.name = name
        self._available = available
        self._run = run

    def available(self) -> bool:
        return bool(self._available() if callable(self._available) else self._available)

    def run(self) -> Any:
        return self._run()


def run_chain(mechanisms: list[Mechanism], action: str):
    """Try each AVAILABLE mechanism in order; the first that doesn't raise wins.

    If every available mechanism fails (or none is present), raise
    UnsupportedTool — the honest 'no working way to do this on this box' that the
    hands.mcp.v1 tri-state expects, instead of a raw first-choice exception. So a
    dead primary (e.g. ydotool's socket down) pivots to the next prong (xdotool /
    wtype) instead of stranding the user. Returns (result, mechanism_name).
    """
    attempted: list[str] = []
    for m in mechanisms:
        try:
            present = m.available()
        except Exception:  # noqa: BLE001 — a broken probe just means "skip this prong"
            present = False
        if not present:
            continue
        try:
            return m.run(), m.name
        except Exception as e:  # noqa: BLE001 — a failed prong -> try the next
            attempted.append(f"{m.name}({type(e).__name__})")
    detail = ", ".join(attempted) if attempted else "no mechanism available"
    raise UnsupportedTool(f"{action}: no working mechanism [{detail}]")


class HandsBackend(ABC):
    name: str = "base"

    @abstractmethod
    def open_app(self, name: str) -> str: ...
    @abstractmethod
    def web_search(self, query: str) -> str: ...
    @abstractmethod
    def open_url(self, url: str) -> str: ...
    @abstractmethod
    def type_text(self, text: str) -> str: ...
    @abstractmethod
    def press_keys(self, combo: str) -> str: ...
    @abstractmethod
    def click_element(self, label: str) -> str: ...
    @abstractmethod
    def mouse_click(self, x: int, y: int, button: str = "left") -> str: ...
    @abstractmethod
    def scroll(self, amount: int) -> str: ...
    @abstractmethod
    def read_screen(self) -> str: ...
    @abstractmethod
    def list_apps(self) -> str: ...
    @abstractmethod
    def screenshot(self, path: str | None = None) -> str: ...
    @abstractmethod
    def run_shell(self, command: str) -> str: ...

    def capabilities(self) -> dict[str, bool]:
        """Which of the 12 tools this backend can actually do ON THIS MACHINE.

        The Swiss-army-knife knowing which blades it has: a tool whose required
        primitive is missing (e.g. cliclick absent on a Mac) reports False, so the
        surface/agent degrade gracefully instead of failing blindly. Default: all
        supported; OS backends override to reflect what's actually installed."""
        return {t: True for t in TOOL_NAMES}
