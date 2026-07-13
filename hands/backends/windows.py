"""Windows desktop-control backend (the Windows peer of linux.py).

Drives the real desktop by shelling out to PowerShell:
  - UIAutomation (System.Windows.Automation)  → read the screen + click elements
    semantically (the accessibility tree — no screenshots)
  - System.Windows.Forms.SendKeys             → type text + key combos
  - user32 (SetCursorPos / mouse_event)       → mouse move/click/scroll
  - System.Drawing Graphics.CopyFromScreen    → screenshots
  - Start-Process                             → launch apps, open URLs, web search

Each PowerShell snippet is passed base64-encoded (-EncodedCommand, UTF-16LE) so
there is zero shell-escaping ambiguity. Imports cleanly on any OS (it only shells
out at call time), so the ABC + tests load everywhere; it runs for real on Windows.

Verified primitives present on the GrantW laptop (Windows 11 Pro, PowerShell 5.1):
UIAutomationClient + System.Windows.Forms both load.
"""
from __future__ import annotations

import base64
import shutil
import subprocess

from ..coords import geometry_for
from .base import FocusInfo, HandsBackend, UnsupportedTool, focus_guard

# Interpreter chain: Windows PowerShell 5.1 (`powershell`) is the historical
# default, but a modern / Windows-11-lean box may ship ONLY PowerShell 7
# (`pwsh`). Pick the first that's actually on PATH — the same in-box .NET
# assemblies drive every tool regardless of which one runs them, so this one
# fix makes every tool work on a pwsh-only machine.
_PS_BINARIES = ("powershell", "pwsh")

# Make the PowerShell process system-DPI-aware BEFORE any screen API runs, so
# CopyFromScreen captures true PHYSICAL pixels and SetCursorPos speaks the same
# physical space. Without this, a DPI-unaware process at 125-150% scaling (most
# consumer laptops) gets VIRTUALIZED coordinates: CopyFromScreen can crop the
# screen to the top-left and any vision-located click lands offset by the scale
# factor. Both screenshot and mouse must share this so their pixel spaces agree
# (identity mapping). No-op at 100% scaling. Must precede loading Forms/Drawing.
_DPI_AWARE = (
    "Add-Type @'\nusing System;using System.Runtime.InteropServices;\n"
    "public class WtDpi{[DllImport(\"user32.dll\")]public static extern bool "
    "SetProcessDPIAware();}\n'@;[WtDpi]::SetProcessDPIAware() | Out-Null;")

_APP_ALIASES = {
    "browser": "msedge", "web browser": "msedge", "chrome": "chrome",
    "google chrome": "chrome", "firefox": "firefox",
    "edge": "msedge", "microsoft edge": "msedge",
    "terminal": "wt", "console": "cmd", "command prompt": "cmd", "cmd": "cmd",
    "powershell": "powershell", "files": "explorer",
    "file manager": "explorer", "explorer": "explorer", "file explorer": "explorer",
    "settings": "ms-settings:",
    "text editor": "notepad", "editor": "notepad", "notepad": "notepad",
    "calculator": "calc", "calc": "calc", "code": "code", "vscode": "code",
    "paint": "mspaint",
    # the apps grandmas actually name (Office desktop + common consumer apps)
    "word": "winword", "microsoft word": "winword", "ms word": "winword",
    "excel": "excel", "microsoft excel": "excel", "ms excel": "excel",
    "powerpoint": "powerpnt", "power point": "powerpnt",
    "outlook": "outlook", "email": "outlook", "mail": "outlook",
    "teams": "ms-teams:", "microsoft teams": "ms-teams:",
    "zoom": "zoom", "photos": "ms-photos:", "photo": "ms-photos:",
    "camera": "microsoft.windows.camera:", "maps": "bingmaps:",
    "store": "ms-windows-store:", "microsoft store": "ms-windows-store:",
    "spotify": "spotify", "whatsapp": "whatsapp", "telegram": "telegram",
    "solitaire": "xboxliveapp-1297287741:", "clock": "ms-clock:",
    "task manager": "taskmgr", "control panel": "control", "notepad++": "notepad++",
}

# friendly key names → SendKeys tokens. Modifiers: ^ ctrl, % alt, + shift.
# SendKeys has NO token for the Windows/Start key — super/win/meta are dropped
# (a no-op) rather than silently mismapped to Ctrl, which would fire the wrong chord.
_SK_MODS = {"ctrl": "^", "control": "^", "alt": "%", "option": "%",
            "shift": "+", "cmd": "^"}
_SK_DROP = {"super", "win", "meta"}  # unreachable via SendKeys; ignored, not mismapped
_SK_KEYS = {
    "return": "{ENTER}", "enter": "{ENTER}", "tab": "{TAB}", "escape": "{ESC}",
    "esc": "{ESC}", "space": " ", "delete": "{DEL}", "del": "{DEL}",
    "backspace": "{BACKSPACE}", "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}",
    "right": "{RIGHT}", "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}",
    "pagedown": "{PGDN}", "f1": "{F1}", "f2": "{F2}", "f3": "{F3}", "f4": "{F4}",
    "f5": "{F5}", "f6": "{F6}", "f7": "{F7}", "f8": "{F8}", "f9": "{F9}",
    "f10": "{F10}", "f11": "{F11}", "f12": "{F12}",
}


def _ps_binary():
    """The first PowerShell interpreter on PATH, or None. Wrapped so tests can
    monkeypatch a single seam."""
    for binary in _PS_BINARIES:
        if shutil.which(binary) is not None:
            return binary
    return None


def _ps(script: str, timeout: float = 20) -> str:
    """Run a PowerShell snippet via -EncodedCommand on the first available
    interpreter (powershell, else pwsh); return stdout. The interpreter choice is
    the ONLY fallback — a launched-but-failed script is a real error surfaced as
    such (never re-run on the other interpreter, which could double a side effect
    like a half-typed SendKeys). No interpreter at all -> UnsupportedTool."""
    binary = _ps_binary()
    if binary is None:
        raise UnsupportedTool("no PowerShell interpreter (powershell / pwsh) on PATH")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    # CREATE_NO_WINDOW: when the app's python runs windowless (pythonw, or an
    # Electron child with no console), Windows otherwise allocates a NEW visible
    # console for each PowerShell child — so every hands action (screenshot,
    # click, type) flashes a black box on the user's screen, AND that console can
    # sit on top of the very screenshot the action is capturing (live-caught: the
    # vision spine "couldn't see" Chrome because the _ps console occluded it).
    # This flag runs PowerShell with no window, ever.
    r = subprocess.run(
        [binary, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "powershell failed").strip()[:400])
    return (r.stdout or "").strip()


def _sq(s: str) -> str:
    """Escape a string for a PowerShell single-quoted literal ('' == one quote).
    Without this, a quote in an agent-supplied app name / URL breaks out of the
    literal into arbitrary PowerShell (RCE) at auto_allow tier."""
    return s.replace("'", "''")


def _focused_window() -> FocusInfo | None:
    """Owning process + window title of the UIAutomation FocusedElement, for
    the type_text focus-guard. Any failure (Session-0, no focus, PS error)
    returns None, and the guard fails closed instead of typing blind."""
    script = (
        "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
        "$foc=[System.Windows.Automation.AutomationElement]::FocusedElement;"
        "if($foc -eq $null){exit 0};"
        "$win=$foc;while($win -ne $null -and $win.Current.ControlType.ProgrammaticName -ne 'ControlType.Window'){$win=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($win)};"
        "if($win -eq $null){$win=$foc};"
        "$p=Get-Process -Id $win.Current.ProcessId -ErrorAction SilentlyContinue;"
        "Write-Output ([string]$p.ProcessName);Write-Output ([string]$win.Current.Name)")
    try:
        out = _ps(script, timeout=12)
    except Exception:  # noqa: BLE001 — unresolvable focus is the guard's business
        return None
    lines = out.splitlines()
    app = lines[0].strip() if lines else ""
    title = lines[1].strip() if len(lines) > 1 else ""
    if not app and not title:
        return None
    return FocusInfo(app=app or None, title=title or None)


def _sk_escape(text: str) -> str:
    # SendKeys treats + ^ % ~ ( ) { } [ ] specially → wrap them in braces.
    out = []
    for ch in text:
        out.append("{" + ch + "}" if ch in "+^%~(){}[]" else ch)
    return "".join(out)


class WindowsBackend(HandsBackend):
    name = "windows"

    def capabilities(self) -> dict[str, bool]:
        # Every primitive is built on PowerShell + in-box .NET assemblies, so the
        # one real dependency is that SOME PowerShell (Windows 5.1 `powershell` OR
        # 7 `pwsh`) is on PATH. If neither is, be honest and report nothing works
        # rather than assume-True.
        from .base import TOOL_NAMES
        has_ps = _ps_binary() is not None
        return {t: has_ps for t in TOOL_NAMES}

    # -- apps / web ------------------------------------------------------------

    def open_app(self, name: str) -> str:
        target = _APP_ALIASES.get(name.strip().lower(), name)
        try:
            _ps("Start-Process '" + _sq(target) + "'")  # launch — kept simple/reliable
        except Exception:
            return f"Couldn't find an app called {name!r}."
        # Then, as a SEPARATE best-effort step that can NEVER fail the launch,
        # bring the app's window to the FRONT so the next read_screen/click/type
        # targets IT. Windows' foreground lock otherwise leaves the new window
        # behind whatever was focused, and the agent acts on the wrong window —
        # live-confirmed: with the app un-fronted, a following type_text leaked
        # its text into the still-focused browser.
        try:
            self._bring_to_front(target)
        except Exception:  # noqa: BLE001 — foregrounding is best-effort, never fatal
            pass
        return f"Opening {name}"

    def _bring_to_front(self, target: str) -> None:
        """Foreground the newest visible window of the process matching `target`.
        Zeroing the foreground-lock timeout + a synthetic Alt keypress is what
        lets a background caller's SetForegroundWindow be honored. URI/UWP
        targets expose no matching process — they self-activate, so no-match is
        fine."""
        base = target.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        if base.lower().endswith(".exe"):
            base = base[:-4]
        if not base or base.endswith(":"):
            return  # protocol/URI launcher — nothing to match; it self-activates
        # NB: do NOT nudge with a synthetic Alt keypress to unlock the
        # foreground — Alt activates the target app's MENU BAR, so the following
        # type_text/press_keys goes to the menu instead of the content (live-
        # caught: a Ctrl+Z landed on Notepad's File menu). Attach this thread's
        # input to the current foreground thread instead, which lets
        # SetForegroundWindow through with no key side-effects.
        script = (
            "Add-Type @'\nusing System;using System.Runtime.InteropServices;\n"
            "public class Fg{"
            "[DllImport(\"user32.dll\")]public static extern bool SetForegroundWindow(IntPtr h);"
            "[DllImport(\"user32.dll\")]public static extern bool BringWindowToTop(IntPtr h);"
            "[DllImport(\"user32.dll\")]public static extern bool ShowWindow(IntPtr h,int c);"
            "[DllImport(\"user32.dll\")]public static extern IntPtr GetForegroundWindow();"
            "[DllImport(\"user32.dll\")]public static extern uint GetWindowThreadProcessId(IntPtr h,IntPtr p);"
            "[DllImport(\"user32.dll\")]public static extern bool AttachThreadInput(uint a,uint b,bool f);"
            "[DllImport(\"kernel32.dll\")]public static extern uint GetCurrentThreadId();"
            "[DllImport(\"user32.dll\")]public static extern bool SystemParametersInfo(uint a,uint p,IntPtr v,uint f);}\n'@;"
            "[Fg]::SystemParametersInfo(0x2001,0,[IntPtr]::Zero,0) | Out-Null;"
            "Start-Sleep -Milliseconds 900;"
            "$w=Get-Process -ErrorAction SilentlyContinue | "
            "Where-Object {$_.MainWindowHandle -ne 0 -and $_.ProcessName -like '*" + _sq(base) + "*'} | "
            "Sort-Object StartTime -Descending | Select-Object -First 1;"
            "if($w -ne $null){$h=$w.MainWindowHandle;"
            "$cur=[Fg]::GetCurrentThreadId();"
            "$fgt=[Fg]::GetWindowThreadProcessId([Fg]::GetForegroundWindow(),[IntPtr]::Zero);"
            "[Fg]::AttachThreadInput($cur,$fgt,$true)|Out-Null;"
            "[Fg]::ShowWindow($h,9)|Out-Null;[Fg]::BringWindowToTop($h)|Out-Null;"
            "[Fg]::SetForegroundWindow($h)|Out-Null;"
            "[Fg]::AttachThreadInput($cur,$fgt,$false)|Out-Null}")
        _ps(script, timeout=8)

    def open_url(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        _ps(f"Start-Process '{_sq(url)}'")
        return f"Opening {url}"

    def web_search(self, query: str) -> str:
        from urllib.parse import quote_plus
        _ps(f"Start-Process 'https://www.google.com/search?q={quote_plus(query)}'")
        return f"Searching the web for {query!r}"

    # -- keyboard / mouse ------------------------------------------------------

    def type_text(self, text: str, target: str | None = None) -> str:
        # Focus-guard BEFORE any keystroke leaves (Phase 0 #1): resolve where the
        # keys would actually land, refuse terminals/unknown/mismatched targets.
        where = focus_guard(_focused_window(), target)
        esc = _sk_escape(text).replace("'", "''")
        _ps("Add-Type -AssemblyName System.Windows.Forms; "
            f"[System.Windows.Forms.SendKeys]::SendWait('{esc}')")
        n = len(text)
        return f"Typed {n} character{'s' if n != 1 else ''} into {where}"

    def press_keys(self, combo: str) -> str:
        parts = [p.strip().lower() for p in combo.replace(" ", "").split("+")
                 if p.strip() and p.strip().lower() not in _SK_DROP]
        mods = "".join(_SK_MODS[p] for p in parts if p in _SK_MODS)
        keys = "".join(_SK_KEYS.get(p, p) for p in parts if p not in _SK_MODS)
        seq = (mods + "(" + keys + ")") if mods else keys
        seq = seq.replace("'", "''")
        _ps("Add-Type -AssemblyName System.Windows.Forms; "
            f"[System.Windows.Forms.SendKeys]::SendWait('{seq}')")
        return f"Pressed {combo}"

    def mouse_click(self, x: int, y: int, button: str = "left") -> str:
        # CopyFromScreen captures the same px space SetCursorPos speaks, so this
        # is identity today — wired through the shared mapper anyway so a future
        # DPI-virtualized capture can't silently reintroduce the offset bug.
        lx, ly = self._map_capture_point(x, y)
        down, up = ("0x0008", "0x0010") if button == "right" else ("0x0002", "0x0004")
        _ps(
            _DPI_AWARE +
            "Add-Type @'\nusing System;using System.Runtime.InteropServices;\n"
            "public class M{[DllImport(\"user32.dll\")]public static extern bool SetCursorPos(int x,int y);"
            "[DllImport(\"user32.dll\")]public static extern void mouse_event(uint f,uint dx,uint dy,uint d,int e);}\n'@;"
            f"[M]::SetCursorPos({int(lx)},{int(ly)});"
            f"[M]::mouse_event({down},0,0,0,0);[M]::mouse_event({up},0,0,0,0)")
        return f"{button.capitalize()}-clicked at ({x}, {y})"

    def scroll(self, amount: int) -> str:
        delta = 120 * int(amount)  # WHEEL_DELTA per notch; +up / -down
        _ps(
            "Add-Type @'\nusing System;using System.Runtime.InteropServices;\n"
            "public class W{[DllImport(\"user32.dll\")]public static extern void mouse_event(uint f,uint dx,uint dy,int d,int e);}\n'@;"
            f"[W]::mouse_event(0x0800,0,0,{delta},0)")
        return f"Scrolled {'down' if amount < 0 else 'up'} {abs(amount)}"

    # -- UIAutomation: read + click --------------------------------------------

    def list_apps(self) -> str:
        out = _ps("Get-Process | Where-Object {$_.MainWindowTitle} | "
                  "Select-Object -ExpandProperty MainWindowTitle -Unique")
        names = [n.strip() for n in out.splitlines() if n.strip()][:40]
        return "Open windows: " + ", ".join(names) if names else "No windows with titles found."

    def read_screen(self) -> str:
        script = (
            "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
            "$root=[System.Windows.Automation.AutomationElement]::RootElement;"
            "$foc=[System.Windows.Automation.AutomationElement]::FocusedElement;"
            "$win=$foc;while($win -ne $null -and $win.Current.ControlType.ProgrammaticName -ne 'ControlType.Window'){$win=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($win)};"
            "if($win -eq $null){$win=$foc};"
            "$cond=[System.Windows.Automation.Condition]::TrueCondition;"
            "$els=$win.FindAll([System.Windows.Automation.TreeScope]::Descendants,$cond);"
            "$out=@();foreach($e in $els){$n=$e.Current.Name;$c=$e.Current.ControlType.ProgrammaticName;"
            "if($n){$out+=('['+$c.Replace('ControlType.','')+'] '+$n)}};"
            "$out | Select-Object -First 120 | ForEach-Object {$_}")
        try:
            out = _ps(script, timeout=18)
        except Exception:
            return "Couldn't read the active window's accessibility content."
        lines = [ln for ln in out.splitlines() if ln.strip()][:120]
        return "On screen:\n" + "\n".join(lines) if lines else "The active window exposes no accessible text."

    def click_element(self, label: str) -> str:
        want = label.strip().replace("'", "''")
        script = (
            "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
            "$foc=[System.Windows.Automation.AutomationElement]::FocusedElement;"
            "$win=$foc;while($win -ne $null -and $win.Current.ControlType.ProgrammaticName -ne 'ControlType.Window'){$win=[System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($win)};"
            "if($win -eq $null){$win=[System.Windows.Automation.AutomationElement]::RootElement};"
            f"$cond=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,'{want}');"
            "$el=$win.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond);"
            "if($el -eq $null){'notfound'}else{"
            "try{$ip=$el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern);$ip.Invoke();'clicked'}"
            "catch{try{$sp=$el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern);$sp.Select();'clicked'}catch{'notfound'}}}")
        try:
            r = _ps(script, timeout=15)
        except Exception:
            r = "notfound"
        if "clicked" in r:
            return f"Clicked {label!r}"
        # UIA couldn't see it (Chrome, canvas UI) → the vision spine (Phase 2).
        return (self._click_visual(label)
                or f"Couldn't find a clickable element named {label!r}.")

    # -- screenshot / shell ----------------------------------------------------

    def screenshot(self, path: str | None = None) -> str:
        from pathlib import Path
        shots = Path.home() / ".windytalk" / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        name = Path(path).name if path else "windytalk_shot.png"
        if not name.lower().endswith(".png"):
            name += ".png"
        dest = str(shots / name).replace("\\", "\\\\").replace("'", "''")
        _ps(
            _DPI_AWARE +
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
            "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height);"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
            f"$bmp.Save('{dest}');$g.Dispose();$bmp.Dispose()", timeout=20)
        # VirtualScreen capture px == SetCursorPos px → logical omitted (identity).
        self._last_capture = geometry_for(shots / name, None)
        return f"Saved screenshot to {shots / name}"

    def run_shell(self, command: str) -> str:
        # Safety is the surface's §9 always_confirm gate, not a denylist.
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                "-Command", command], capture_output=True, text=True, timeout=30)
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            tail = out[-1500:] if out else (err[-1500:] if err else "(no output)")
            return f"exit {r.returncode}\n{tail}"
        except subprocess.TimeoutExpired:
            return "Command timed out after 30s."
