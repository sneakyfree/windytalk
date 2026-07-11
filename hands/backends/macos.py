"""macOS desktop-control backend (the macOS peer of linux.py).

Drives the real desktop via:
  - osascript / AppleScript System Events  → app launch, read UI, click elements
    semantically (the AXUIElement accessibility tree, no screenshots)
  - cliclick                               → mouse move/click + keystrokes
  - screencapture                          → screenshots
  - open                                   → launch apps, open URLs, web search

Every method returns a short human/agent-readable string (handed back as the tool
result). Requires macOS Accessibility permission for the controlling process
(System Settings → Privacy & Security → Accessibility) — the read/click tools
return a clear message if it isn't granted yet.

Verified primitives present on OC5 (macOS 13): osascript, screencapture, cliclick,
open. cliclick installed via `brew install cliclick`.
"""
from __future__ import annotations

import shutil
import subprocess
from urllib.parse import quote_plus

from .base import HandsBackend, UnsupportedTool

_APP_ALIASES = {
    "browser": "Safari", "web browser": "Safari", "chrome": "Google Chrome",
    "terminal": "Terminal", "console": "Terminal", "files": "Finder",
    "file manager": "Finder", "finder": "Finder", "settings": "System Settings",
    "system preferences": "System Settings", "text editor": "TextEdit",
    "editor": "TextEdit", "calculator": "Calculator", "notes": "Notes",
    "mail": "Mail", "code": "Visual Studio Code", "vscode": "Visual Studio Code",
}

# friendly key names → macOS key codes for cliclick `kp:` (key-press) where possible,
# else cliclick `t:` types characters. Modifiers use cliclick `kd:`/`ku:`.
_CLICK_KEYS = {
    "return": "return", "enter": "return", "tab": "tab", "escape": "esc",
    "esc": "esc", "space": "space", "delete": "delete", "backspace": "delete",
    "up": "arrow-up", "down": "arrow-down", "left": "arrow-left",
    "right": "arrow-right", "home": "home", "end": "end", "pageup": "page-up",
    "pagedown": "page-down",
}
_MODS = {"cmd": "cmd", "command": "cmd", "ctrl": "ctrl", "control": "ctrl",
         "alt": "alt", "option": "alt", "opt": "alt", "shift": "shift",
         "super": "cmd", "win": "cmd", "meta": "cmd"}


def _osa(script: str, timeout: float = 12) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "not allowed assistive access" in err or "1002" in err or "-25211" in err:
            raise PermissionError("macOS Accessibility permission not granted")
        raise RuntimeError(err or "osascript failed")
    return (r.stdout or "").strip()


def _cliclick(*args: str, timeout: float = 10) -> None:
    subprocess.run(["cliclick", *args], check=True, capture_output=True, timeout=timeout)


def _osa_str(s: str) -> str:
    """Escape a Python string for safe embedding inside an AppleScript "..." literal.
    Without this, a `"` in an agent-supplied app name / element label breaks out of
    the literal into arbitrary AppleScript (→ `do shell script` RCE) at auto_allow."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


class MacOSBackend(HandsBackend):
    name = "macos"

    def capabilities(self) -> dict[str, bool]:
        has_cliclick = shutil.which("cliclick") is not None
        has_osa = shutil.which("osascript") is not None
        has_open = shutil.which("open") is not None
        has_shot = shutil.which("screencapture") is not None
        return {
            "open_app": has_open or has_osa,
            "web_search": has_open, "open_url": has_open,
            "type_text": has_cliclick, "press_keys": has_cliclick,
            "mouse_click": has_cliclick, "scroll": has_cliclick,
            "click_element": has_osa, "read_screen": has_osa, "list_apps": has_osa,
            "screenshot": has_shot, "run_shell": True,
        }

    # -- apps / web ------------------------------------------------------------

    def open_app(self, name: str) -> str:
        app = _APP_ALIASES.get(name.strip().lower(), name)
        r = subprocess.run(["open", "-a", app], capture_output=True, text=True, timeout=12)
        if r.returncode == 0:
            return f"Opening {app}"
        # fall back to Spotlight-style launch via System Events
        try:
            _osa(f'tell application "{_osa_str(app)}" to activate')
            return f"Opening {app}"
        except Exception:
            return f"Couldn't find an app called {name!r}."

    def open_url(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        subprocess.run(["open", url], check=True, capture_output=True, timeout=10)
        return f"Opening {url}"

    def web_search(self, query: str) -> str:
        subprocess.run(["open", f"https://www.google.com/search?q={quote_plus(query)}"],
                       check=True, capture_output=True, timeout=10)
        return f"Searching the web for {query!r}"

    # -- keyboard / mouse (cliclick) -------------------------------------------

    def type_text(self, text: str) -> str:
        if shutil.which("cliclick") is None:
            raise UnsupportedTool("cliclick not installed (brew install cliclick)")
        _cliclick("t:" + text)
        n = len(text)
        return f"Typed {n} character{'s' if n != 1 else ''}"

    def press_keys(self, combo: str) -> str:
        if shutil.which("cliclick") is None:
            raise UnsupportedTool("cliclick not installed (brew install cliclick)")
        parts = [p.strip().lower() for p in combo.replace(" ", "").split("+") if p.strip()]
        mods = [_MODS[p] for p in parts if p in _MODS]
        keys = [p for p in parts if p not in _MODS]
        if mods:
            _cliclick("kd:" + ",".join(mods))
        try:
            for k in keys:
                if k in _CLICK_KEYS:
                    _cliclick("kp:" + _CLICK_KEYS[k])
                else:
                    _cliclick("t:" + k)
        finally:
            if mods:
                _cliclick("ku:" + ",".join(mods))
        return f"Pressed {combo}"

    def mouse_click(self, x: int, y: int, button: str = "left") -> str:
        if shutil.which("cliclick") is None:
            raise UnsupportedTool("cliclick not installed (brew install cliclick)")
        verb = "rc" if button == "right" else "c"  # right-click / left-click
        _cliclick(f"{verb}:{int(x)},{int(y)}")
        return f"{button.capitalize()}-clicked at ({x}, {y})"

    def scroll(self, amount: int) -> str:
        if shutil.which("cliclick") is None:
            raise UnsupportedTool("cliclick not installed")
        # cliclick has no wheel; emulate with repeated arrow keys (best-effort)
        key = "arrow-down" if amount < 0 else "arrow-up"
        for _ in range(min(abs(int(amount)) or 1, 20)):
            _cliclick("kp:" + key)
        return f"Scrolled {'down' if amount < 0 else 'up'} {abs(amount)}"

    # -- AT (System Events): read + click --------------------------------------

    def list_apps(self) -> str:
        try:
            out = _osa('tell application "System Events" to get name of '
                       '(every process whose background only is false)')
        except PermissionError as e:
            return str(e)
        names = [n.strip() for n in out.split(",") if n.strip()]
        return "Open apps: " + ", ".join(names) if names else "No accessible apps found."

    def read_screen(self) -> str:
        script = (
            'tell application "System Events"\n'
            ' set p to first process whose frontmost is true\n'
            ' set out to name of p & ":\n"\n'
            ' try\n'
            '  set els to entire contents of front window of p\n'
            '  repeat with e in els\n'
            '   try\n'
            '    set v to (value of e as text)\n'
            '    if v is not "" then set out to out & v & "\n"\n'
            '   end try\n'
            '   try\n'
            '    set d to (description of e as text)\n'
            '    if d is not "" then set out to out & "[" & d & "]\n"\n'
            '   end try\n'
            '  end repeat\n'
            ' end try\n'
            ' return out\n'
            'end tell')
        try:
            out = _osa(script, timeout=15)
        except PermissionError as e:
            return str(e)
        except Exception:
            return "Couldn't read the active window's accessibility content."
        lines = [ln for ln in out.splitlines() if ln.strip()][:120]
        return "On screen:\n" + "\n".join(lines) if lines else "The active app exposes no accessible text."

    def click_element(self, label: str) -> str:
        want = _osa_str(label.strip())
        script = (
            'tell application "System Events"\n'
            ' set p to first process whose frontmost is true\n'
            ' try\n'
            f'  click (first button of front window of p whose name is "{want}")\n'
            '  return "clicked"\n'
            ' end try\n'
            ' try\n'
            f'  click (first UI element of front window of p whose name is "{want}")\n'
            '  return "clicked"\n'
            ' end try\n'
            ' return "notfound"\n'
            'end tell')
        try:
            r = _osa(script, timeout=12)
        except PermissionError as e:
            return str(e)
        except Exception:
            r = "notfound"
        return f"Clicked {label!r}" if r == "clicked" else f"Couldn't find a clickable element named {label!r}."

    # -- screenshot / shell ----------------------------------------------------

    def screenshot(self, path: str | None = None) -> str:
        from pathlib import Path
        shots = Path.home() / ".windytalk" / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        name = Path(path).name if path else "windytalk_shot.png"
        if not name.lower().endswith(".png"):
            name += ".png"
        dest = str(shots / name)
        subprocess.run(["screencapture", "-x", dest], check=True, capture_output=True, timeout=15)
        return f"Saved screenshot to {dest}"

    def run_shell(self, command: str) -> str:
        # Safety is the surface's §9 always_confirm gate, not a denylist.
        try:
            r = subprocess.run(["/bin/zsh", "-lc", command], capture_output=True,
                               text=True, timeout=30)
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            tail = out[-1500:] if out else (err[-1500:] if err else "(no output)")
            return f"exit {r.returncode}\n{tail}"
        except subprocess.TimeoutExpired:
            return "Command timed out after 30s."
