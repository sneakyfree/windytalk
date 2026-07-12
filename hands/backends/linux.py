"""Linux desktop-control backend (ported verbatim-in-spirit from
reference/hands.py — the proven Fedora/GNOME/Wayland layer).

Drives the real desktop via ydotool/xdotool (keyboard+mouse), gtk-launch/xdg
(apps+web), AT-SPI2 (read+click semantically), flameshot/scrot (screenshots).
X11 vs Wayland is auto-detected (WINDYTALK_INPUT overrides).

Changed from the prototype per the §3 ledger: `run_shell` no longer consults a
denylist — the §9 `always_confirm` tier (hands/tiers.py) carries the safety now,
so the surface prompts before every shell command. The backend just runs it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote_plus

from ..coords import geometry_for
from .base import FocusInfo, HandsBackend, Mechanism, UnsupportedTool, focus_guard, run_chain


def _ydotool_socket() -> str:
    """The ydotoold socket path. Defaults to the CURRENT user's runtime dir —
    never a hardcoded uid 1000 (wrong on any box where the login user isn't
    1000, which silently broke ydotool there)."""
    env = os.environ.get("YDOTOOL_SOCKET")
    if env:
        return env
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    return f"/run/user/{uid}/.ydotool_socket"


def _ydotool_available() -> bool:
    """ydotool works ONLY with a running ydotoold (socket present) OR direct
    /dev/uinput access. Without either it crashes ~1.5 s in ('failed to open
    uinput device'), so treat it as absent and pivot instantly to wtype/xdotool
    instead of eating the latency + a scary error on every keystroke (found on a
    real Ubuntu box with no ydotoold)."""
    if _which("ydotool") is None:
        return False
    return os.path.exists(_ydotool_socket()) or os.access("/dev/uinput", os.W_OK)

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
_XDO = {"return": "Return", "enter": "Return", "tab": "Tab", "escape": "Escape",
        "esc": "Escape", "space": "space", "backspace": "BackSpace",
        "delete": "Delete", "del": "Delete", "up": "Up", "down": "Down",
        "left": "Left", "right": "Right", "home": "Home", "end": "End",
        "pageup": "Page_Up", "pgup": "Page_Up", "pagedown": "Page_Down",
        "pgdn": "Page_Down", "super": "super", "meta": "super", "win": "super",
        "cmd": "super", "control": "ctrl"}
_APP_ALIASES = {
    "browser": "firefox", "web browser": "firefox", "chrome": "google-chrome",
    "terminal": "ptyxis", "console": "ptyxis", "files": "org.gnome.Nautilus",
    "file manager": "org.gnome.Nautilus", "nautilus": "org.gnome.Nautilus",
    "settings": "org.gnome.Settings", "text editor": "org.gnome.TextEditor",
    "editor": "org.gnome.TextEditor", "calculator": "org.gnome.Calculator",
    "code": "code", "vscode": "code", "cursor": "cursor",
}
_TEXT_ROLES = ("text", "entry", "document text", "document web", "document frame",
               "paragraph", "terminal", "password text", "heading", "static")
_SKIP_ROLES = ("filler", "panel", "section", "scroll pane", "scroll bar", "separator")


def _on_x11() -> bool:
    """Pure session detection (X11 vs Wayland) — SEPARATE from whether a given
    input tool is installed. Ordering uses this; availability is checked per
    mechanism, so a chain can still fall back to a non-native tool (xdotool on
    XWayland, ydotool via uinput on X11) when the native one isn't seated."""
    return bool(os.environ.get("XDG_SESSION_TYPE") == "x11"
                or (os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")))


def _input_order() -> list[str]:
    """Preferred order of KEYBOARD mechanisms for THIS session. WINDYTALK_INPUT
    forces one first. Every mechanism still gets a turn if the preferred one is
    absent or fails — the whole point of the chain."""
    order = ["xdotool", "ydotool", "wtype"]
    pref = os.environ.get("WINDYTALK_INPUT")
    if pref in order:
        return [pref] + [m for m in order if m != pref]
    if _on_x11():
        return ["xdotool", "ydotool", "wtype"]  # X11-native first, then uinput, then virtual-kbd
    return ["ydotool", "wtype", "xdotool"]  # Wayland: uinput, virtual-kbd, then XWayland


def _is_gnome() -> bool:
    return "gnome" in (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()


def _pointer_order() -> list[str]:
    """Preferred order of POINTER mechanisms — deliberately NOT _input_order.
    The live finding this encodes (Phase 1): Mutter honors ydotool's virtual
    KEYBOARD but silently ignores its virtual POINTER on every GNOME-Wayland
    box (any DPI) — the click 'succeeds' while the cursor never moves. A
    phantom prong is worse than a missing one (the chain can't detect a lying
    success), so on GNOME-Wayland the pointer is portal-ONLY; other Wayland
    compositors (wlroots honors virtual pointers) keep ydotool after the
    portal. WINDYTALK_POINTER forces one first."""
    if _on_x11():
        order = ["xdotool", "ydotool", "portal"]
    elif _is_gnome():
        order = ["portal"]
    else:
        order = ["portal", "ydotool", "xdotool"]
    pref = os.environ.get("WINDYTALK_POINTER")
    if pref in ("xdotool", "ydotool", "portal"):
        return [pref] + [m for m in order if m != pref]
    return order


def _portal_available() -> bool:
    """Is the RemoteDesktop portal (with pointer support) on the session bus?
    A real property read — never pops the grant dialog."""
    try:
        from .portal import PortalPointer
        return PortalPointer.available()
    except Exception:  # noqa: BLE001 — no gi / no bus ⇒ no portal
        return False


def _vision_configured() -> bool:
    """Is the vision-spine locator configured (WINDYTALK_VISION_URL set)?"""
    from ..vision import VisionLocator
    return VisionLocator.configured()


def _ydotool(*args: str, timeout: float = 10) -> None:
    env = {**os.environ, "YDOTOOL_SOCKET": _ydotool_socket()}
    subprocess.run(["ydotool", *args], env=env, check=True,
                   capture_output=True, timeout=timeout)


def _xdotool(*args, timeout: float = 10) -> None:
    subprocess.run(["xdotool", *args], check=True, capture_output=True, timeout=timeout)


def _wtype(*args: str, timeout: float = 10) -> None:
    subprocess.run(["wtype", *args], check=True, capture_output=True, timeout=timeout)


def _which(tool: str):
    # Wrapped so tests can monkeypatch a single seam for availability.
    return shutil.which(tool)


def _xdo_name(p: str) -> str:
    if len(p) >= 2 and p[0] == "f" and p[1:].isdigit():
        return p.upper()
    return _XDO.get(p, p)


def _atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    Atspi.init()
    return Atspi


# -- functional capability probes (Phase 0 #2) --------------------------------
# Presence lied on real machines: grim installed on GNOME but the compositor
# refuses it; gi importable but the accessibility bus can be dead (GNOME needs
# toolkit-accessibility=true). One real probe each, cached per backend instance.

def _atspi_probe() -> bool:
    """A real AT-SPI round-trip (init + desktop query), not 'is gi importable'."""
    try:
        A = _atspi()
        A.get_desktop(0).get_child_count()
        return True
    except Exception:  # noqa: BLE001 — any failure means the sense is not functional
        return False


def _screenshot_probe() -> bool:
    """One real throwaway capture through the same chain screenshot() uses —
    the only honest answer to 'can this box screenshot'. The probe file never
    leaves a temp dir."""
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="windytalk-probe-") as td:
            return _capture(str(Path(td) / "probe.png")) is not None
    except Exception:  # noqa: BLE001
        return False


def _capture(dest: str) -> str | None:
    """Session-AGNOSTIC capture chain: try every tool present, in a broad order
    that covers Wayland (grim), GNOME (gnome-screenshot, both sessions), KDE
    (spectacle), and X11 (scrot/import), with flameshot raw-stdout as the last
    rung. Each is verified by a real non-empty file before we accept it, so a
    tool that runs on the wrong session (writes nothing) transparently pivots to
    the next. Returns the name of the tool that seated, or None."""
    for cmd in (["grim", dest],
                ["gnome-screenshot", "-f", dest],
                ["spectacle", "-b", "-n", "-o", dest],
                ["scrot", "-o", dest],
                ["import", "-window", "root", dest]):
        if not _which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=15)
            if Path(dest).exists() and Path(dest).stat().st_size > 0:
                return cmd[0]
        except Exception:  # noqa: BLE001 — a failed rung -> try the next
            pass
    if _which("flameshot"):  # last resort: raw bytes to stdout
        try:
            data = subprocess.run(["flameshot", "full", "--raw"],
                                  capture_output=True, timeout=15).stdout
            if data:
                Path(dest).write_bytes(data)
                return "flameshot"
        except Exception:  # noqa: BLE001
            pass
    return None


class LinuxBackend(HandsBackend):
    name = "linux"

    def capabilities(self) -> dict[str, bool]:
        # Honest per-tool report: what this box can actually DO, so the agent
        # gets a graceful `unsupported` instead of a raw exception (PORTABILITY.md /
        # GET /capabilities promise this reflects reality, not an assumption).
        # AT-SPI and screenshot are FUNCTION-probed (once, cached) — binary
        # presence proved dishonest live: grim present on GNOME but refused by
        # the compositor; gi importable with the accessibility bus dead.
        input_ok = _which("xdotool") is not None or _which("wtype") is not None or _ydotool_available()
        atspi_ok = self._probed("atspi", _atspi_probe)
        shot_ok = self._probed("screenshot", _screenshot_probe)
        # Pointer honesty (Phase 1): wtype has no pointer; ydotool's pointer is
        # a PHANTOM on GNOME-Wayland (Mutter ignores it while it reports
        # success) so it must not satisfy the capability there — the portal is
        # the GNOME-Wayland pointer, probed for real presence on the bus.
        if _on_x11():
            pointer_ok = _which("xdotool") is not None or _ydotool_available()
        elif _is_gnome():
            pointer_ok = self._probed("portal", _portal_available)
        else:
            pointer_ok = self._probed("portal", _portal_available) or _ydotool_available()
        has_launch = shutil.which("gtk-launch") is not None
        has_xdg = shutil.which("xdg-open") is not None
        return {
            "open_app": has_launch or has_xdg, "open_url": has_xdg, "web_search": has_xdg,
            # type_text needs AT-SPI too: the focus-guard fails closed when it
            # can't resolve where keystrokes would land (unverifiable = unsafe).
            "type_text": input_ok and atspi_ok, "press_keys": input_ok,
            "mouse_click": pointer_ok, "scroll": pointer_ok,
            # click_element's do_action path needs no pointer; the vision rung
            # (Phase 2) also makes it work WITHOUT AT-SPI when a vision model,
            # a working capture, and a pointer are all present.
            "click_element": atspi_ok or (_vision_configured() and shot_ok and pointer_ok),
            "read_screen": atspi_ok,
            "list_apps": atspi_ok, "screenshot": shot_ok, "run_shell": True,
        }

    # -- keyboard / mouse (fallback-chained: try every prong before giving up) --

    def _type_mechs(self, text: str) -> list[Mechanism]:
        builders = {
            "xdotool": Mechanism("xdotool", lambda: _which("xdotool"),
                                 lambda: _xdotool("type", "--clearmodifiers", "--", text)),
            "ydotool": Mechanism("ydotool", _ydotool_available,
                                 lambda: _ydotool("type", "--", text)),
            # wtype: the Wayland virtual-keyboard typer (wlroots compositors).
            "wtype": Mechanism("wtype", lambda: _which("wtype"), lambda: _wtype(text)),
        }
        return [builders[k] for k in _input_order() if k in builders]

    def _key_mechs(self, combo: str) -> list[Mechanism]:
        parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]

        def xdotool_run():
            _xdotool("key", "+".join(_xdo_name(p) for p in parts))

        def ydotool_run():
            codes = [_KEYCODES[_ALIAS.get(p, p)] for p in parts]  # KeyError -> chain moves on
            seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
            _ydotool("key", *seq)

        # wtype handles named keys/modifiers too (-M/-m + -k); include it as a
        # third prong for Wayland compositors without a working uinput/X11 path.
        def wtype_run():
            args: list[str] = []
            mods = [p for p in parts if p in ("ctrl", "control", "alt", "shift", "super", "meta", "win", "cmd")]
            keys = [p for p in parts if p not in mods]
            for m in mods:
                args += ["-M", {"control": "ctrl", "meta": "logo", "super": "logo",
                                "win": "logo", "cmd": "logo"}.get(m, m)]
            for k in keys:
                args += ["-k", _xdo_name(k)]
            for m in reversed(mods):
                args += ["-m", {"control": "ctrl", "meta": "logo", "super": "logo",
                                "win": "logo", "cmd": "logo"}.get(m, m)]
            _wtype(*args)

        builders = {
            "xdotool": Mechanism("xdotool", lambda: _which("xdotool"), xdotool_run),
            "ydotool": Mechanism("ydotool", _ydotool_available, ydotool_run),
            "wtype": Mechanism("wtype", lambda: _which("wtype"), wtype_run),
        }
        return [builders[k] for k in _input_order() if k in builders]

    def _portal(self):
        """The lazily-created, session-remembering portal pointer (one per
        backend — its RemoteDesktop session persists across calls)."""
        if getattr(self, "_portal_obj", None) is None:
            from .portal import PortalPointer
            self._portal_obj = PortalPointer()
        return self._portal_obj

    def _click_mechs(self, x, y, button: str) -> list[Mechanism]:
        # x/y here are LOGICAL (pointer-space) coordinates — mouse_click maps
        # capture px → logical BEFORE building the chain.
        def xdotool_run():
            if x is not None and y is not None:
                _xdotool("mousemove", str(int(x)), str(int(y)))
            _xdotool("click", {"left": "1", "middle": "2", "right": "3"}.get(button, "1"))

        def ydotool_run():
            if x is not None and y is not None:
                _ydotool("mousemove", "-a", "-x", str(int(x)), "-y", str(int(y)))
            code = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}.get(button, "0xC0")
            _ydotool("click", code)

        # wtype has no pointer control; pointer chains never include it.
        builders = {
            "xdotool": Mechanism("xdotool", lambda: _which("xdotool"), xdotool_run),
            "ydotool": Mechanism("ydotool", _ydotool_available, ydotool_run),
            "portal": Mechanism("portal", _portal_available,
                                lambda: self._portal().click(x, y, button)),
        }
        return [builders[k] for k in _pointer_order() if k in builders]

    def _scroll_mechs(self, amount: int) -> list[Mechanism]:
        def xdotool_run():
            btn = "5" if amount < 0 else "4"
            for _ in range(abs(int(amount)) or 1):
                _xdotool("click", btn)

        def ydotool_run():
            _ydotool("mousemove", "-w", "-x", "0", "-y", str(int(amount)))

        builders = {
            "xdotool": Mechanism("xdotool", lambda: _which("xdotool"), xdotool_run),
            "ydotool": Mechanism("ydotool", _ydotool_available, ydotool_run),
            "portal": Mechanism("portal", _portal_available,
                                lambda: self._portal().scroll(int(amount))),
        }
        return [builders[k] for k in _pointer_order() if k in builders]

    def type_text(self, text: str, target: str | None = None) -> str:
        # Focus-guard BEFORE any keystroke leaves (Phase 0 #1): resolve where the
        # keys would actually land, refuse terminals/unknown/mismatched targets.
        where = focus_guard(self._focused_window(), target)
        run_chain(self._type_mechs(text), "type_text")  # raises UnsupportedTool if all fail
        n = len(text)
        return f"Typed {n} character{'s' if n != 1 else ''} into {where}"

    def press_keys(self, combo: str) -> str:
        run_chain(self._key_mechs(combo), "press_keys")
        return f"Pressed {combo}"

    def mouse_click(self, x: int, y: int, button: str = "left") -> str:
        lx, ly = self._map_capture_point(x, y)  # capture px → logical points
        self._click_logical(lx, ly, button)
        return f"{button.capitalize()}-clicked at ({x}, {y})"

    def _click_logical(self, x, y, button: str = "left") -> None:
        """Click at coordinates ALREADY in pointer (logical) space — the entry
        point for internally-derived coords (AT-SPI extents), which must not go
        through the capture mapping twice."""
        run_chain(self._click_mechs(x, y, button), "mouse_click")

    def scroll(self, amount: int) -> str:
        run_chain(self._scroll_mechs(amount), "scroll")
        return f"Scrolled {'down' if amount < 0 else 'up'} {abs(amount)}"

    # -- apps / web ------------------------------------------------------------

    def _desktop_ids(self) -> dict[str, str]:
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

    def open_app(self, name: str) -> str:
        key = name.strip().lower()
        target = _APP_ALIASES.get(key, key)
        ids = self._desktop_ids()
        if target.lower() in ids:
            subprocess.Popen(["gtk-launch", ids[target.lower()]],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opening {name}"
        for stem_lower, stem in ids.items():
            if key in stem_lower:
                subprocess.Popen(["gtk-launch", stem],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"Opening {stem}"
        if shutil.which(target):
            subprocess.Popen([target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opening {name}"
        return f"Couldn't find an app called {name!r}."

    def open_url(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Opening {url}"

    def web_search(self, query: str) -> str:
        subprocess.Popen(["xdg-open", f"https://www.google.com/search?q={quote_plus(query)}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Searching the web for {query!r}"

    # -- AT-SPI: read + click --------------------------------------------------

    def list_apps(self) -> str:
        A = _atspi()
        desk = A.get_desktop(0)
        names = []
        for i in range(desk.get_child_count()):
            try:
                names.append(desk.get_child_at_index(i).get_name() or "?")
            except Exception:
                pass
        return "Open apps: " + ", ".join(names) if names else "No accessible apps found."

    def _active_app(self, A):
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
                    if frame.get_state_set().contains(A.StateType.ACTIVE):
                        return app
                    best = app
            except Exception:
                continue
        return best

    def _focused_window(self) -> FocusInfo | None:
        """Resolve where keystrokes would ACTUALLY land, for the type_text
        focus-guard. Unlike _active_app there is NO best-guess fallback — the
        guard needs certainty, and 'no frame claims ACTIVE' honestly means
        'unresolvable' (the guard then refuses rather than typing blind)."""
        try:
            A = _atspi()
            desk = A.get_desktop(0)
            actives = []
            for i in range(desk.get_child_count()):
                try:
                    app = desk.get_child_at_index(i)
                    for j in range(app.get_child_count()):
                        frame = app.get_child_at_index(j)
                        if frame.get_state_set().contains(A.StateType.ACTIVE):
                            actives.append((app, frame))
                except Exception:
                    continue
            if not actives:
                return None
            # Shell chrome (gnome-shell etc.) can hold an ACTIVE frame alongside
            # the real app's — prefer the real app when both claim it. (When ONLY
            # gnome-shell is active — the overview search — that IS the focus,
            # and typing there is legitimate.)
            shell = ("gnome-shell", "mutter-x11-frames", "ibus-extension-gtk3")
            actives.sort(key=lambda af: (af[0].get_name() or "").lower() in shell)
            app, frame = actives[0]
            return FocusInfo(app=app.get_name() or None, title=frame.get_name() or None,
                             role=self._focused_role(A, frame))
        except Exception:
            return None

    def _focused_role(self, A, frame, budget: int = 400, max_depth: int = 15):
        """Accessibility role of the FOCUSED element inside the active frame
        (bounded walk). This is what catches a terminal PANE inside an app that
        isn't itself a terminal (VTE widgets expose role 'terminal')."""
        stack = [(frame, 0)]
        while stack and budget > 0:
            node, depth = stack.pop()
            budget -= 1
            try:
                if node.get_state_set().contains(A.StateType.FOCUSED):
                    return node.get_role_name()
                if depth < max_depth:
                    for i in range(min(node.get_child_count(), 100)):
                        stack.append((node.get_child_at_index(i), depth + 1))
            except Exception:
                continue
        return None

    def _collect_text(self, node, A, out, budget, depth=0, limit=200):
        if len(out) >= limit or depth > 30 or budget[0] <= 0:
            return
        budget[0] -= 1
        try:
            role = node.get_role_name()
            name = node.get_name()
            content = ""
            if role in _TEXT_ROLES:
                try:
                    txt = node.get_text(0, -1)
                    if txt and txt.strip():
                        content = txt.strip()
                except Exception:
                    pass
            label = content or name
            if label and role not in _SKIP_ROLES:
                out.append(f"[{role}] {label[:300]}")
            for i in range(min(node.get_child_count(), 200)):
                if budget[0] <= 0:
                    break
                self._collect_text(node.get_child_at_index(i), A, out, budget, depth + 1, limit)
        except Exception:
            return

    def read_screen(self) -> str:
        A = _atspi()
        app = self._active_app(A)
        if app is None:
            return "Couldn't find an active accessible window."
        out: list[str] = []
        self._collect_text(app, A, out, budget=[600])
        if not out:
            return (f"The active app ({app.get_name()}) exposes no accessible text. "
                    "Electron/Chromium apps need ACCESSIBILITY_ENABLED=1.")
        return f"On screen in {app.get_name()}:\n" + "\n".join(out[:120])

    def click_element(self, label: str) -> str:
        """The Phase-2 click ladder. AT-SPI is the FAST LANE (native, Firefox,
        Electron — coord-free, ~free); the vision spine is the universal rung
        for what accessibility can't see (Chrome is absent from the AT-SPI tree
        and can't be woken at runtime):

          1. AT-SPI element found → its named action (click|jump|activate…)
          2. found but not actionable → its extents → coordinate click
          3. not found / extents bogus → screenshot → vision model → click
        """
        try:
            A = _atspi()
            app = self._active_app(A)
        except Exception:  # noqa: BLE001 — no AT-SPI at all: straight to the vision rung
            A = app = None
        if app is None:
            return (self._click_visual(label)
                    or f"Couldn't find a clickable element named {label!r}.")
        want = label.strip().lower()
        exact, partial = [], []
        budget = [4000]  # web documents are big (a live Firefox tree walked 1170 nodes)

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
                    actionable = False
                    try:
                        a = node.get_action_iface()
                        actionable = bool(a and a.get_n_actions() > 0)
                    except Exception:
                        pass
                    if interactive or actionable:
                        (exact if name == want else partial).append(node)
                for i in range(min(node.get_child_count(), 200)):
                    if budget[0] <= 0:
                        break
                    walk(node.get_child_at_index(i), depth + 1)
            except Exception:
                return

        walk(app)
        candidates = exact or partial
        if not candidates:
            # AT-SPI is blind to it (Chrome, canvas UI, div-button web apps) —
            # the vision spine is exactly for this.
            return (self._click_visual(label)
                    or f"Couldn't find a clickable element named {label!r}.")
        match = candidates[0]
        if self._do_preferred_action(match):
            return f"Clicked {match.get_name()!r}"
        # Found but not actionable: its box → coordinate click (plan item #6).
        try:
            pt = match.get_position(A.CoordType.SCREEN)
            sz = match.get_size()
            # Extents sanity: GTK4-on-Wayland apps can report window-RELATIVE
            # positions (a live calculator button claimed 0,0) — clicking that
            # would hit the screen corner. Reject the bogus shape and let the
            # vision rung take it rather than click a wrong place.
            if sz.width > 0 and sz.height > 0 and (pt.x, pt.y) != (0, 0):
                # AT-SPI extents are ALREADY logical — bypass the capture mapping.
                self._click_logical(pt.x + sz.width // 2, pt.y + sz.height // 2)
                return f"Clicked {match.get_name()!r}"
        except Exception:
            pass
        return (self._click_visual(label)
                or f"Found {label!r} but couldn't click it (no action, unusable extents).")

    def _do_preferred_action(self, node) -> bool:
        """Run the node's most click-like AT-SPI action. Action NAMES vary by
        toolkit (live-measured: links say 'jump', entries 'activate', buttons
        'click'/'press') — prefer a recognized name, else fall back to action 0
        as before."""
        try:
            a = node.get_action_iface()
            if not a or a.get_n_actions() <= 0:
                return False
            names = []
            for i in range(a.get_n_actions()):
                try:
                    names.append((a.get_action_name(i) or "").strip().lower())
                except Exception:
                    names.append("")
            for want in ("click", "press", "jump", "activate", "activate-item", "default"):
                if want in names:
                    a.do_action(names.index(want))
                    return True
            a.do_action(0)
            return True
        except Exception:  # noqa: BLE001 — a failed action just moves the ladder on
            return False

    # -- screenshot / shell ----------------------------------------------------

    def screenshot(self, path: str | None = None) -> str:
        # Confine output to a screenshots dir — `path` is a filename only, never an
        # absolute/traversal path (else `screenshot` becomes an arbitrary-file
        # overwrite primitive at auto_allow tier).
        shots_dir = Path.home() / ".windytalk" / "screenshots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        name = Path(path).name if path else "windytalk_shot.png"
        if not name.lower().endswith(".png"):
            name += ".png"
        path = str(shots_dir / name)
        if _capture(path) is not None:  # the shared session-agnostic chain
            # Remember this capture's geometry: subsequent mouse_click coords
            # are pixels of THIS image and get mapped to logical points.
            self._last_capture = geometry_for(path, self._logical_size())
            return f"Saved screenshot to {path}"
        raise UnsupportedTool("screenshot: no working capture backend on this box")

    def _logical_size(self) -> tuple[int, int] | None:
        """Best-effort logical (pointer-space) screen size. The portal stream
        is authoritative when a session exists (Wayland); X11 asks xdotool.
        None → the capture is assumed to BE logical (exact on X11/identity
        boxes; matches flameshot/portal captures on GNOME)."""
        p = getattr(self, "_portal_obj", None)
        if p is not None and p.stream_size:
            return p.stream_size
        if _on_x11() and _which("xdotool"):
            try:
                out = subprocess.run(["xdotool", "getdisplaygeometry"], check=True,
                                     capture_output=True, text=True, timeout=5).stdout.split()
                return int(out[0]), int(out[1])
            except Exception:  # noqa: BLE001 — unknown size just means identity mapping
                return None
        return None

    def run_shell(self, command: str) -> str:
        # Safety is the surface's §9 always_confirm gate now, not a denylist (§3 ledger).
        try:
            r = subprocess.run(["bash", "-lc", command], capture_output=True,
                               text=True, timeout=30)
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            tail = out[-1500:] if out else (err[-1500:] if err else "(no output)")
            return f"exit {r.returncode}\n{tail}"
        except subprocess.TimeoutExpired:
            return "Command timed out after 30s."
