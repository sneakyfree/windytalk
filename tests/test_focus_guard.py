"""Phase 0 of docs/GAP_CLOSING_PLAN.md — the safety + honesty foundation.

1. type_text focus-guard + terminal-refuse: keystrokes are only injected after
   the focused window is resolved and verified. The live catastrophe class this
   kills: a mis-focused type_text submitted its text as a prompt into ANOTHER
   live Claude Code terminal (memory windytalk-live-stress-2026-07-12) — a
   shell command would have executed. The invariant under test everywhere:
   refusal happens BEFORE any mechanism runs; a refusal types NOTHING.

2. Capabilities report FUNCTION, not binary presence: the Linux AT-SPI and
   screenshot capabilities come from one real cached probe (grim was present on
   GNOME but dead; capabilities still said screenshot:true).

No test here touches a live desktop: guard decisions are pure, backends get
injected FocusInfo/mechanisms, and probes run against fakes. The module
ORIGINALS (captured at import, before the conftest autouse stubs apply) are
used where the real logic is the thing under test.
"""
from __future__ import annotations

import pytest

from hands import HandsSurface, TierPolicy
from hands.backends import linux as lx
from hands.backends import macos as mac
from hands.backends import windows as win
from hands.backends.base import (
    FocusInfo,
    GuardRefused,
    UnsupportedTool,
    focus_guard,
    is_terminal_focus,
)
from hands.tiers import deny_all

# Originals captured at import time — pytest imports test modules during
# collection, BEFORE the conftest autouse stubs monkeypatch these seams.
_REAL_ATSPI_PROBE = lx._atspi_probe
_REAL_SCREENSHOT_PROBE = lx._screenshot_probe
_REAL_WIN_FOCUSED = win._focused_window
_REAL_MAC_FOCUSED = mac._focused_window


# ============ the guard decision (pure — no desktop, no mocks) ====================

@pytest.mark.parametrize("app", [
    "ptyxis", "org.gnome.Ptyxis", "gnome-terminal-server", "Konsole", "kitty",
    "alacritty", "foot", "st",                       # Linux
    "Terminal", "iTerm2", "Warp", "ghostty",          # macOS
    "WindowsTerminal", "cmd", "conhost", "pwsh",      # Windows
    "xterm", "wezterm-gui", "qterminal", "xfce4-terminal", "terminator",  # 'term' substring
])
def test_terminal_apps_refuse(app):
    with pytest.raises(GuardRefused) as ei:
        focus_guard(FocusInfo(app=app))
    assert "terminal" in str(ei.value)
    assert "run_shell" in str(ei.value), "the refusal must point at the sanctioned shell path"


def test_terminal_ROLE_refuses_even_inside_a_non_terminal_app():
    # An IDE (e.g. VS Code) with its integrated terminal pane focused: the app
    # name is innocent, the focused element's AT-SPI role says 'terminal'.
    with pytest.raises(GuardRefused):
        focus_guard(FocusInfo(app="code", title="myproject", role="terminal"))


def test_terminal_in_window_TITLE_does_not_refuse():
    # A browser tab titled 'terminal emulators compared' is not a terminal —
    # only the app name / focused role decide, never the title.
    assert focus_guard(FocusInfo(app="firefox", title="terminal emulators compared")) == "firefox"


def test_unresolvable_focus_refuses_never_types_blind():
    for focus in (None, FocusInfo(), FocusInfo(app="", title="")):
        with pytest.raises(GuardRefused) as ei:
            focus_guard(focus)
        assert "resolve" in str(ei.value)


def test_target_mismatch_refuses():
    with pytest.raises(GuardRefused) as ei:
        focus_guard(FocusInfo(app="firefox", title="WagyuRanch"), target="gmail")
    msg = str(ei.value)
    assert "'gmail'" in msg and "firefox" in msg, "the refusal names both sides"


def test_target_matches_app_name_case_insensitive():
    assert focus_guard(FocusInfo(app="Firefox"), target="firefox") == "Firefox"


def test_target_matches_window_title_fragment():
    # The browser case: app is 'firefox', intent is the Gmail tab.
    assert focus_guard(FocusInfo(app="firefox", title="Inbox — Gmail"), target="gmail") == "firefox"


def test_no_target_non_terminal_passes_and_reports_where():
    assert focus_guard(FocusInfo(app="gnome-shell")) == "gnome-shell", \
        "typing into the GNOME overview search is legitimate (proven live)"


def test_blank_target_is_ignored():
    assert focus_guard(FocusInfo(app="firefox"), target="   ") == "firefox"


def test_env_escape_hatch_disables_guard(monkeypatch):
    monkeypatch.setenv("WINDYTALK_TYPE_GUARD", "off")
    # even a terminal passes when the dev hatch is open — that's the point of a hatch
    assert focus_guard(FocusInfo(app="ptyxis")) == "ptyxis"
    assert "unknown" in focus_guard(None)


def test_is_terminal_focus_negative_cases():
    for app in ("firefox", "org.gnome.Calculator", "gnome-shell", "Windy Word", "notepad"):
        assert is_terminal_focus(FocusInfo(app=app)) is False, app


# ============ backends: refusal happens BEFORE any keystroke =====================

def test_linux_refuses_terminal_before_any_mechanism_runs(monkeypatch):
    monkeypatch.setattr(lx.LinuxBackend, "_focused_window",
                        lambda self: FocusInfo(app="ptyxis", role="terminal"))
    ran = []
    monkeypatch.setattr(lx, "_which", lambda t: t)  # every tool 'installed'
    monkeypatch.setattr(lx, "_xdotool", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(lx, "_ydotool", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(lx, "_wtype", lambda *a, **k: ran.append(a))
    with pytest.raises(GuardRefused):
        lx.LinuxBackend().type_text("mail.google.com")
    assert ran == [], "REFUSED means NOTHING was typed — no mechanism may have run"


def test_linux_refuses_when_focus_unresolvable(monkeypatch):
    monkeypatch.setattr(lx.LinuxBackend, "_focused_window", lambda self: None)
    ran = []
    monkeypatch.setattr(lx, "_which", lambda t: t)
    monkeypatch.setattr(lx, "_xdotool", lambda *a, **k: ran.append(a))
    with pytest.raises(GuardRefused):
        lx.LinuxBackend().type_text("hi")
    assert ran == []


def test_linux_types_and_reports_where(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WINDYTALK_INPUT", raising=False)
    monkeypatch.setattr(lx.LinuxBackend, "_focused_window",
                        lambda self: FocusInfo(app="firefox", title="Inbox — Gmail"))
    monkeypatch.setattr(lx, "_which", lambda t: t if t == "xdotool" else None)
    ran = []
    monkeypatch.setattr(lx, "_xdotool", lambda *a, **k: ran.append(a))
    out = lx.LinuxBackend().type_text("hello", target="gmail")
    assert out == "Typed 5 characters into firefox"
    assert ran, "the guarded happy path still types"


def test_macos_refuses_terminal_before_typing(monkeypatch):
    monkeypatch.setattr(mac, "_focused_window", lambda: FocusInfo(app="Terminal"))
    ran = []
    monkeypatch.setattr(mac, "_which", lambda t: t)
    monkeypatch.setattr(mac, "_cliclick", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(mac, "_osa", lambda *a, **k: ran.append(a) or "")
    with pytest.raises(GuardRefused):
        mac.MacOSBackend().type_text("rm -rf /")
    assert ran == []


def test_windows_refuses_terminal_before_typing(monkeypatch):
    monkeypatch.setattr(win, "_focused_window",
                        lambda: FocusInfo(app="WindowsTerminal", title="PowerShell"))
    ran = []
    monkeypatch.setattr(win, "_ps", lambda *a, **k: ran.append(a) or "")
    with pytest.raises(GuardRefused):
        win.WindowsBackend().type_text("format c:")
    assert ran == [], "SendKeys must never have been invoked"


def test_windows_target_mismatch_refuses(monkeypatch):
    monkeypatch.setattr(win, "_focused_window", lambda: FocusInfo(app="notepad", title="Untitled"))
    ran = []
    monkeypatch.setattr(win, "_ps", lambda *a, **k: ran.append(a) or "")
    with pytest.raises(GuardRefused):
        win.WindowsBackend().type_text("hi", target="chrome")
    assert ran == []


# ============ the per-OS focus resolvers (fed fake transport output) =============

def test_windows_focused_window_parses_process_and_title(monkeypatch):
    monkeypatch.setattr(win, "_ps", lambda script, timeout=12: "chrome\nInbox — Gmail")
    focus = _REAL_WIN_FOCUSED()
    assert focus == FocusInfo(app="chrome", title="Inbox — Gmail")


def test_windows_focused_window_none_on_ps_failure(monkeypatch):
    def boom(script, timeout=12):
        raise RuntimeError("no desktop (Session-0)")
    monkeypatch.setattr(win, "_ps", boom)
    assert _REAL_WIN_FOCUSED() is None, "unresolvable focus must be None (guard then refuses)"


def test_windows_focused_window_none_on_empty_output(monkeypatch):
    monkeypatch.setattr(win, "_ps", lambda script, timeout=12: "")
    assert _REAL_WIN_FOCUSED() is None


def test_macos_focused_window_none_without_accessibility(monkeypatch):
    def denied(script, timeout=12):
        raise PermissionError("macOS Accessibility permission not granted")
    monkeypatch.setattr(mac, "_osa", denied)
    assert _REAL_MAC_FOCUSED() is None


def test_macos_focused_window_app_and_title(monkeypatch):
    def fake_osa(script, timeout=12):
        return "Safari" if "frontmost is true" in script and "front window" not in script \
            else "Apple — Start Page"
    monkeypatch.setattr(mac, "_osa", fake_osa)
    assert _REAL_MAC_FOCUSED() == FocusInfo(app="Safari", title="Apple — Start Page")


# ============ surface: target plumbs through, refusal maps to the result shape ===

class _GuardBackend(lx.LinuxBackend):
    """Records what reaches type_text; never touches a desktop."""
    def __init__(self):
        self.seen = []

    def type_text(self, text, target=None):
        self.seen.append((text, target))
        if target == "terminal-sim":
            raise GuardRefused("the focused window is a terminal (sim)")
        return f"Typed {len(text)} characters into sim"


def _surface(backend):
    return HandsSurface(backend=backend, policy=TierPolicy(confirmer=deny_all))


def test_surface_passes_target_through_schema_filter():
    b = _GuardBackend()
    res = _surface(b).invoke("type_text", {"text": "hi", "target": "gmail"})
    assert res["ok"] is True
    assert b.seen == [("hi", "gmail")], "'target' must survive _filter_args (it's in the schema now)"


def test_surface_maps_guard_refusal_to_refused_error():
    b = _GuardBackend()
    res = _surface(b).invoke("type_text", {"text": "x", "target": "terminal-sim"})
    assert res["ok"] is False
    assert res["error"].startswith("refused: "), res
    assert "terminal" in res["error"]


def test_contract_target_is_optional_and_additive():
    from hands.surface import _load_tool_schemas
    schema = _load_tool_schemas()["type_text"]["inputSchema"]
    assert "target" in schema["properties"]
    assert schema["required"] == ["text"], "'target' must stay optional (additive rev)"


# ============ Phase 0 #2: capabilities report FUNCTION, not presence =============

def test_linux_type_text_capability_requires_working_atspi(monkeypatch):
    # Input tools installed, but the accessibility bus is dead → the guard could
    # never resolve focus → type_text is HONESTLY unsupported, not a runtime trap.
    monkeypatch.setattr(lx, "_which", lambda t: t if t == "xdotool" else None)
    monkeypatch.setattr(lx, "_atspi_probe", lambda: False)
    caps = lx.LinuxBackend().capabilities()
    assert caps["type_text"] is False
    assert caps["press_keys"] is True, "press_keys is not focus-guarded in Phase 0"
    assert caps["read_screen"] is False and caps["list_apps"] is False


def test_linux_screenshot_capability_is_the_probe_verdict(monkeypatch):
    # grim PRESENT but non-functional (the live GNOME finding): presence used to
    # report screenshot:true; the functional probe now decides.
    monkeypatch.setattr(lx, "_which", lambda t: t if t == "grim" else None)
    monkeypatch.setattr(lx, "_screenshot_probe", lambda: False)
    assert lx.LinuxBackend().capabilities()["screenshot"] is False
    monkeypatch.setattr(lx, "_screenshot_probe", lambda: True)
    assert lx.LinuxBackend().capabilities()["screenshot"] is True


def test_probes_run_once_per_instance_and_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(lx, "_atspi_probe", lambda: calls.append("atspi") or True)
    monkeypatch.setattr(lx, "_screenshot_probe", lambda: calls.append("shot") or True)
    b = lx.LinuxBackend()
    b.capabilities()
    b.capabilities()
    assert calls.count("atspi") == 1 and calls.count("shot") == 1, \
        "a probe is real work (a capture!) — once per instance, then cached"


def test_probe_exception_means_not_functional():
    b = lx.LinuxBackend()

    def boom():
        raise RuntimeError("bus exploded")
    assert b._probed("x", boom) is False
    assert b._probed("x", lambda: True) is False, "the failed verdict is cached, not retried"


def test_real_atspi_probe_false_when_atspi_unavailable(monkeypatch):
    def no_gi():
        raise ImportError("no module named gi")
    monkeypatch.setattr(lx, "_atspi", no_gi)
    assert _REAL_ATSPI_PROBE() is False


def test_real_atspi_probe_true_on_answering_bus(monkeypatch):
    class _Desk:
        def get_child_count(self):
            return 3

    class _A:
        @staticmethod
        def get_desktop(i):
            return _Desk()
    monkeypatch.setattr(lx, "_atspi", lambda: _A)
    assert _REAL_ATSPI_PROBE() is True


def test_real_screenshot_probe_false_when_no_tool_captures(monkeypatch):
    monkeypatch.setattr(lx, "_which", lambda t: None)  # nothing installed at all
    assert _REAL_SCREENSHOT_PROBE() is False


def test_real_screenshot_probe_true_when_a_rung_produces_bytes(monkeypatch):
    monkeypatch.setattr(lx, "_capture", lambda dest: "flameshot")
    assert _REAL_SCREENSHOT_PROBE() is True


def test_capture_dead_grim_pivots_to_flameshot_bytes(monkeypatch, tmp_path):
    # The exact live failure: grim present, runs, writes NOTHING (compositor
    # refuses) → the chain must fall through to flameshot's raw stdout.
    present = {"grim", "flameshot"}
    monkeypatch.setattr(lx, "_which", lambda t: t if t in present else None)

    class _R:
        stdout = b"\x89PNG fake bytes"

    def fake_run(cmd, **kw):
        return _R()  # grim 'succeeds' but writes no file; flameshot returns bytes
    monkeypatch.setattr(lx.subprocess, "run", fake_run)
    dest = tmp_path / "shot.png"
    assert lx._capture(str(dest)) == "flameshot"
    assert dest.read_bytes() == b"\x89PNG fake bytes"


def test_screenshot_tool_uses_shared_capture_chain(monkeypatch, tmp_path):
    monkeypatch.setattr(lx.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(lx, "_capture", lambda dest: None)
    with pytest.raises(UnsupportedTool):
        lx.LinuxBackend().screenshot()
