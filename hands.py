"""
Windy Jarvis — the "hands": a Linux (Fedora/GNOME/Wayland) desktop-control layer.

This is the Linux equivalent of the macOS `agent-desktop` package the viral GPT
Realtime 2 demos used. It drives the real desktop via:
  - ydotool (uinput)        -> keyboard + mouse injection that works under Wayland
  - gtk-launch / gio / xdg  -> launch apps, open URLs, web search
  - AT-SPI2 accessibility   -> read on-screen text + click UI elements semantically
  - flameshot / XDG portal  -> screenshots (grim does not work on GNOME)

Every public function returns a short human/agent-readable string describing the
result, so it can be handed straight back to the voice model as a tool result.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import config

# ---------------------------------------------------------------------------
# ydotool plumbing
# ---------------------------------------------------------------------------
_YENV = {**os.environ, "YDOTOOL_SOCKET": config.YDOTOOL_SOCKET}


def _ydotool(*args: str, timeout: float = 10) -> None:
    subprocess.run(["ydotool", *args], env=_YENV, check=True,
                   capture_output=True, timeout=timeout)


# evdev keycodes (linux/input-event-codes.h) for named keys -> `ydotool key`
_KEYCODES = {
    "esc": 1, "escape": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7,
    "7": 8, "8": 9, "9": 10, "0": 11, "minus": 12, "equal": 13,
    "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23,
    "o": 24, "p": 25, "leftbrace": 26, "rightbrace": 27, "enter": 28,
    "return": 28, "ctrl": 29, "control": 29, "leftctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37,
    "l": 38, "semicolon": 39, "apostrophe": 40, "grave": 41,
    "shift": 42, "leftshift": 42, "backslash": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
    "comma": 51, "dot": 52, "period": 52, "slash": 53, "rightshift": 54,
    "kpasterisk": 55, "alt": 56, "leftalt": 56, "space": 57, "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65,
    "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "home": 102, "up": 103, "pageup": 104, "left": 105, "right": 106,
    "end": 107, "down": 108, "pagedown": 109, "insert": 110, "delete": 111,
    "del": 111, "super": 125, "meta": 125, "win": 125, "cmd": 125,
    "leftmeta": 125, "rightmeta": 126,
}
_ALIAS = {"plus": "equal", "esc": "escape", "pgup": "pageup", "pgdn": "pagedown"}


def press_keys(combo: str) -> str:
    """Press a key or chord like 'ctrl+c', 'alt+Tab', 'super', 'Return'."""
    parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
    parts = [_ALIAS.get(p, p) for p in parts]
    try:
        codes = [_KEYCODES[p] for p in parts]
    except KeyError as e:
        return f"Unknown key: {e.args[0]!r}. Known modifiers: ctrl, alt, shift, super."
    seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
    _ydotool("key", *seq)
    return f"Pressed {'+'.join(parts)}"


def type_text(text: str) -> str:
    """Type a string into the focused field (via uinput, layout = US ASCII)."""
    _ydotool("type", "--", text)
    n = len(text)
    return f"Typed {n} character{'s' if n != 1 else ''}"


def mouse_move(x: int, y: int) -> str:
    _ydotool("mousemove", "-a", "-x", str(int(x)), "-y", str(int(y)))
    return f"Moved pointer to ({x}, {y})"


def mouse_click(x: int | None = None, y: int | None = None, button: str = "left") -> str:
    """Click at absolute (x, y); if omitted, click at the current pointer position."""
    if x is not None and y is not None:
        mouse_move(x, y)
    code = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}.get(button, "0xC0")
    _ydotool("click", code)
    where = f" at ({x}, {y})" if x is not None else ""
    return f"{button.capitalize()}-clicked{where}"


def scroll(amount: int = -3) -> str:
    """Scroll the wheel; negative = down, positive = up (in wheel clicks)."""
    _ydotool("mousemove", "-w", "-x", "0", "-y", str(int(amount)))
    return f"Scrolled {'down' if amount < 0 else 'up'} {abs(amount)}"


# ---------------------------------------------------------------------------
# App launching / web
# ---------------------------------------------------------------------------
_APP_ALIASES = {
    "browser": "firefox", "web browser": "firefox", "chrome": "google-chrome",
    "terminal": "ptyxis", "console": "ptyxis", "files": "org.gnome.Nautilus",
    "file manager": "org.gnome.Nautilus", "nautilus": "org.gnome.Nautilus",
    "settings": "org.gnome.Settings", "text editor": "org.gnome.TextEditor",
    "editor": "org.gnome.TextEditor", "calculator": "org.gnome.Calculator",
    "code": "code", "vscode": "code", "cursor": "cursor",
}


def _desktop_ids() -> dict[str, str]:
    ids: dict[str, str] = {}
    dirs = ["/usr/share/applications", "/usr/local/share/applications",
            str(Path.home() / ".local/share/applications"),
            "/var/lib/flatpak/exports/share/applications"]
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in p.glob("*.desktop"):
            ids[f.stem.lower()] = f.stem
    return ids


def open_app(name: str) -> str:
    """Launch an application by friendly name, .desktop id, or binary."""
    key = name.strip().lower()
    target = _APP_ALIASES.get(key, key)

    # 1) direct gtk-launch on an exact .desktop id
    ids = _desktop_ids()
    if target.lower() in ids:
        subprocess.Popen(["gtk-launch", ids[target.lower()]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Opening {name}"
    # 2) fuzzy match against .desktop names
    for stem_lower, stem in ids.items():
        if key in stem_lower:
            subprocess.Popen(["gtk-launch", stem],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opening {stem}"
    # 3) fall back to a bare binary on PATH
    if shutil.which(target):
        subprocess.Popen([target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Opening {name}"
    return f"Couldn't find an app called {name!r}."


def open_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Opening {url}"


def web_search(query: str) -> str:
    from urllib.parse import quote_plus
    subprocess.Popen(["xdg-open", f"https://www.google.com/search?q={quote_plus(query)}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Searching the web for {query!r}"


# ---------------------------------------------------------------------------
# AT-SPI: read the screen + click elements semantically (no screenshots)
# ---------------------------------------------------------------------------
def _atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    Atspi.init()
    return Atspi


def list_apps() -> str:
    """List currently running, accessible GUI applications."""
    A = _atspi()
    desk = A.get_desktop(0)
    names = []
    for i in range(desk.get_child_count()):
        try:
            names.append(desk.get_child_at_index(i).get_name() or "?")
        except Exception:
            pass
    return "Open apps: " + ", ".join(names) if names else "No accessible apps found."


def _active_app(A):
    """Best-effort: the app whose top window is active, else the last app."""
    desk = A.get_desktop(0)
    best = None
    for i in range(desk.get_child_count()):
        try:
            app = desk.get_child_at_index(i)
            name = (app.get_name() or "").lower()
            if name in ("gnome-shell", "mutter-x11-frames", "ibus-extension-gtk3"):
                continue
            for j in range(app.get_child_count()):
                frame = app.get_child_at_index(j)
                states = frame.get_state_set()
                if states.contains(A.StateType.ACTIVE):
                    return app
                best = app
        except Exception:
            continue
    return best


_TEXT_ROLES = ("text", "entry", "document text", "document web", "document frame",
               "paragraph", "terminal", "password text", "heading", "static")
_SKIP_ROLES = ("filler", "panel", "section", "scroll pane", "scroll bar", "separator")


def _collect_text(node, A, out, budget, depth=0, limit=200):
    # budget = [nodes_remaining]; hard cap on TOTAL nodes visited so a huge or
    # unresponsive accessibility tree (e.g. a big browser page) can never hang us.
    if len(out) >= limit or depth > 30 or budget[0] <= 0:
        return
    budget[0] -= 1
    try:
        role = node.get_role_name()
        name = node.get_name()
        content = ""
        if role in _TEXT_ROLES:  # pull real content via the AT-SPI Text interface
            try:
                txt = node.get_text(0, -1)
                if txt and txt.strip():
                    content = txt.strip()
            except Exception:
                pass
        label = content or name
        if label and role not in _SKIP_ROLES:
            out.append(f"[{role}] {label[:300]}")
        n = min(node.get_child_count(), 200)
        for i in range(n):
            if budget[0] <= 0:
                break
            _collect_text(node.get_child_at_index(i), A, out, budget, depth + 1, limit)
    except Exception:
        return


def read_screen() -> str:
    """Read visible labels/text from the active window via the accessibility tree."""
    A = _atspi()
    app = _active_app(A)
    if app is None:
        return "Couldn't find an active accessible window."
    out: list[str] = []
    _collect_text(app, A, out, budget=[600])
    if not out:
        return (f"The active app ({app.get_name()}) exposes no accessible text. "
                "Electron/Chromium apps need ACCESSIBILITY_ENABLED=1.")
    return f"On screen in {app.get_name()}:\n" + "\n".join(out[:120])


def click_element(label: str) -> str:
    """Find a button/link/menu item whose name contains `label` and activate it."""
    A = _atspi()
    app = _active_app(A)
    if app is None:
        return "No active window to click in."
    want = label.strip().lower()
    exact, partial = [], []
    budget = [800]

    def walk(node, depth=0):
        if budget[0] <= 0 or depth > 30:
            return
        budget[0] -= 1
        try:
            name = (node.get_name() or "").lower()
            if want and want in name:
                role = node.get_role_name()
                interactive = ("button" in role or role in (
                    "link", "menu item", "check box", "radio button",
                    "list item", "tab", "page tab", "combo box", "entry"))
                # prefer things we can actually act on
                actionable = False
                try:
                    a = node.get_action_iface()
                    actionable = bool(a and a.get_n_actions() > 0)
                except Exception:
                    pass
                if interactive or actionable:
                    (exact if name == want else partial).append(node)
            n = min(node.get_child_count(), 200)
            for i in range(n):
                if budget[0] <= 0:
                    break
                walk(node.get_child_at_index(i), depth + 1)
        except Exception:
            return

    walk(app)
    candidates = exact or partial
    if not candidates:
        return f"Couldn't find a clickable element named {label!r}."
    match = candidates[0]
    # Prefer the accessible action; fall back to a real click at its center.
    try:
        action = match.get_action_iface()
        if action and action.get_n_actions() > 0:
            action.do_action(0)
            return f"Clicked {match.get_name()!r}"
    except Exception:
        pass
    try:
        pt = match.get_position(A.CoordType.SCREEN)
        sz = match.get_size()
        return mouse_click(pt.x + sz.width // 2, pt.y + sz.height // 2)
    except Exception as e:
        return f"Found {label!r} but couldn't click it: {e}"


# ---------------------------------------------------------------------------
# Screenshots (grim is unsupported on GNOME; use flameshot raw, then portal)
# ---------------------------------------------------------------------------
def screenshot(path: str = "/tmp/jarvis_shot.png") -> str:
    if shutil.which("flameshot"):
        try:
            data = subprocess.run(["flameshot", "full", "--raw"],
                                  capture_output=True, timeout=15).stdout
            if data:
                Path(path).write_bytes(data)
                return f"Saved screenshot to {path}"
        except Exception:
            pass
    return "Screenshot failed (no working Wayland capture backend)."


# ---------------------------------------------------------------------------
# Guarded shell
# ---------------------------------------------------------------------------
def run_shell(command: str) -> str:
    low = command.lower()
    if not config.ALLOW_DANGEROUS:
        for bad in config.SHELL_DENYLIST:
            if bad in low:
                return f"Refused: command contains blocked pattern {bad!r}."
    try:
        r = subprocess.run(["bash", "-lc", command], capture_output=True,
                           text=True, timeout=30)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        tail = out[-1500:] if out else (err[-1500:] if err else "(no output)")
        return f"exit {r.returncode}\n{tail}"
    except subprocess.TimeoutExpired:
        return "Command timed out after 30s."
