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

# The frozen hands.mcp.v1 tool names, in contract order.
TOOL_NAMES = (
    "open_app", "web_search", "open_url", "type_text", "press_keys",
    "click_element", "mouse_click", "scroll", "read_screen", "list_apps",
    "screenshot", "run_shell",
)


class UnsupportedTool(NotImplementedError):
    """This backend does not implement this tool on this platform."""


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
