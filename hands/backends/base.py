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

import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The frozen hands.mcp.v1 tool names, in contract order.
TOOL_NAMES = (
    "open_app", "web_search", "open_url", "type_text", "press_keys",
    "click_element", "mouse_click", "scroll", "read_screen", "list_apps",
    "screenshot", "run_shell",
)


class UnsupportedTool(NotImplementedError):
    """This backend does not implement this tool on this platform."""


class GuardRefused(Exception):
    """The focus-guard refused to inject keystrokes (nothing was typed).

    The surface maps this to ok:false, error:"refused: ..." so the agent knows
    the action did NOT happen and why — never a silent no-op, never a raw crash.
    """


@dataclass(frozen=True)
class FocusInfo:
    """What the backend could resolve about the currently focused window."""
    app: str | None = None    # owning application (process/AT-SPI app name)
    title: str | None = None  # window title, when the platform exposes one
    role: str | None = None   # accessibility role of the focused ELEMENT, if known


# Apps whose focused window must NEVER receive injected keystrokes. Proven
# catastrophe class (live stress 2026-07-12): a mis-focused type_text submitted
# its text as a prompt into another live Claude Code terminal — a shell command
# would have executed. Multiple live terminals per box is the fleet norm.
# Names are matched lowercase against the focused APP name (not the window
# title — a browser tab titled "terminal emulators" must not false-refuse).
TERMINAL_APPS = frozenset({
    # Linux (AT-SPI application names / binaries)
    "gnome-terminal", "gnome-terminal-server", "ptyxis", "org.gnome.ptyxis",
    "konsole", "org.kde.konsole", "xterm", "uxterm", "kitty", "alacritty",
    "foot", "footclient", "tilix", "urxvt", "rxvt", "st", "guake", "yakuake",
    "lxterminal", "sakura", "terminology", "cool-retro-term",
    # macOS (System Events frontmost process names)
    "terminal", "iterm", "iterm2", "warp", "hyper", "tabby", "ghostty", "rio",
    "termius",
    # Windows (process names)
    "windowsterminal", "wt", "cmd", "conhost", "openconsole", "powershell",
    "powershell_ise", "pwsh", "mintty", "putty", "conemu", "conemu64",
})


def is_terminal_focus(focus: FocusInfo) -> bool:
    """Is the focused window a terminal? Checks the focused ELEMENT's
    accessibility role first ('terminal' is the AT-SPI role of a VTE pane —
    this also catches a terminal embedded in a non-terminal app like an IDE),
    then the app name against the known-terminal set plus a 'term' substring
    (xterm, wezterm, qterminal, terminator, Windows Terminal, ...). Biased
    fail-closed: a rare false positive refuses a typing action; a false
    negative injects keystrokes into a live shell."""
    if focus.role and "terminal" in focus.role.lower():
        return True
    app = (focus.app or "").strip().lower()
    if not app:
        return False
    return app in TERMINAL_APPS or "term" in app


def _guard_disabled() -> bool:
    # Dev/chaos escape hatch only (mirrors WINDYTALK_ALLOW_FOREIGN_RELAUNCH):
    # never set it on a machine with live terminals you care about.
    return os.environ.get("WINDYTALK_TYPE_GUARD", "").lower() in ("off", "0", "disabled")


def focus_guard(focus: FocusInfo | None, target: str | None = None) -> str:
    """The type_text safety gate (GAP_CLOSING_PLAN Phase 0 #1). Decide whether
    injected keystrokes may proceed given where focus actually is. Returns the
    focused-window label to report typing into; raises GuardRefused otherwise.

    Rules (fail closed — keystrokes into the wrong window are unrecoverable):
      1. Focus unresolvable            → refuse: never type blind.
      2. Focused app is a terminal     → refuse: run_shell is the shell path.
      3. `target` given, focus doesn't match it (case-insensitive substring of
         app name or window title)     → refuse: wrong window frontmost.
    """
    if _guard_disabled():
        return (focus.app or focus.title) if focus else "unknown window (guard disabled)"
    if focus is None or not (focus.app or focus.title):
        raise GuardRefused(
            "can't resolve the focused window (accessibility unavailable or no "
            "active window) — refusing to type blind")
    label = focus.app or focus.title
    if is_terminal_focus(focus):
        raise GuardRefused(
            f"the focused window is a terminal ({label}) — type_text never injects "
            "keystrokes into terminals; use run_shell to run shell commands")
    if target and target.strip():
        want = target.strip().lower()
        haystack = " ".join(s for s in (focus.app, focus.title) if s).lower()
        if want not in haystack:
            shown = f"{label!r}" + (f" ({focus.title!r})" if focus.title and focus.title != label else "")
            raise GuardRefused(
                f"the focused window is {shown}, not the requested target "
                f"{target!r} — bring the target window to the front first")
    return label


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
    def type_text(self, text: str, target: str | None = None) -> str: ...
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

    def _map_capture_point(self, x: int, y: int) -> tuple[int, int]:
        """Map a mouse_click coordinate from capture space (pixels of the most
        recent screenshot — what a vision model reasons in) to the pointer's
        logical space. Set by screenshot(); identity when no capture is on
        record (the coords are then native screen coordinates). Coordinates the
        backend derived ITSELF from AT-SPI are already logical and go straight
        to _click_logical/the mechanisms, bypassing this."""
        geom = getattr(self, "_last_capture", None)
        if geom is None:
            return int(x), int(y)
        return geom.to_logical(x, y)

    def _probed(self, key: str, probe: Callable[[], bool]) -> bool:
        """Run a FUNCTIONAL capability probe once per backend instance and cache
        the verdict (GAP_CLOSING_PLAN Phase 0 #2). Presence of a binary is not
        function — grim exists on GNOME yet fails — so capabilities that matter
        get one real probe at first ask instead of an assumption. A probe that
        raises means 'not functional', never a crash."""
        cache = self.__dict__.setdefault("_probe_cache", {})
        if key not in cache:
            try:
                cache[key] = bool(probe())
            except Exception:  # noqa: BLE001 — a broken probe IS the answer: not functional
                cache[key] = False
        return cache[key]
