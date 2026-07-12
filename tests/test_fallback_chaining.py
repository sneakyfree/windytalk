"""Fallback-chaining inside the OS adapters (plan step #4).

The durability promise: 'if the first way to type/click doesn't seat, try the
next one, then the next — only report unsupported when EVERY prong fails.' These
tests exercise the reusable chain primitive (base.run_chain), the session-aware
ordering, and the Linux backend end-to-end with injected mechanisms — no real
desktop needed.
"""
from __future__ import annotations

import pytest

from hands.backends import linux as lx
from hands.backends.base import Mechanism, UnsupportedTool, run_chain

# -- the reusable chain primitive --------------------------------------------------

def _mech(name, available, run):
    return Mechanism(name, available, run)


def test_chain_first_available_success_wins():
    calls = []
    mechs = [
        _mech("a", True, lambda: calls.append("a")),
        _mech("b", True, lambda: calls.append("b")),
    ]
    result, used = run_chain(mechs, "act")
    assert used == "a"
    assert calls == ["a"], "the second mechanism must not run once the first succeeds"


def test_chain_skips_unavailable_then_runs_next():
    calls = []
    mechs = [
        _mech("absent", False, lambda: calls.append("absent")),
        _mech("present", True, lambda: calls.append("present")),
    ]
    _, used = run_chain(mechs, "act")
    assert used == "present"
    assert calls == ["present"], "an unavailable prong is skipped, not run"


def test_chain_primary_FAILS_then_pivots_to_secondary():
    calls = []

    def boom():
        calls.append("primary")
        raise RuntimeError("socket dead")

    mechs = [
        _mech("primary", True, boom),           # available but throws (e.g. ydotool socket down)
        _mech("secondary", True, lambda: calls.append("secondary")),
    ]
    _, used = run_chain(mechs, "type_text")
    assert used == "secondary", "a dead primary must pivot to the next prong"
    assert calls == ["primary", "secondary"]


def test_chain_tries_all_three_in_order():
    order = []

    def fail(tag):
        def run():
            order.append(tag)
            raise OSError(tag)
        return run

    mechs = [
        _mech("m1", True, fail("m1")),
        _mech("m2", True, fail("m2")),
        _mech("m3", True, lambda: order.append("m3")),  # third one finally works
    ]
    _, used = run_chain(mechs, "act")
    assert used == "m3"
    assert order == ["m1", "m2", "m3"], "every prong is tried in order until one seats"


def test_chain_all_fail_raises_UnsupportedTool_honestly():
    mechs = [
        _mech("m1", True, lambda: (_ for _ in ()).throw(RuntimeError("x"))),
        _mech("m2", False, lambda: None),  # not even present
    ]
    with pytest.raises(UnsupportedTool) as ei:
        run_chain(mechs, "type_text")
    # the honest 'no working mechanism' — the surface maps this to error:'unsupported'
    assert "type_text" in str(ei.value) and "m1" in str(ei.value)


def test_chain_none_available_raises_UnsupportedTool():
    mechs = [_mech("m1", False, lambda: None), _mech("m2", False, lambda: None)]
    with pytest.raises(UnsupportedTool):
        run_chain(mechs, "act")


def test_chain_broken_availability_probe_is_treated_as_absent():
    def bad_probe():
        raise RuntimeError("which blew up")
    mechs = [
        _mech("flaky", bad_probe, lambda: "should not run"),
        _mech("good", True, lambda: "ok"),
    ]
    _, used = run_chain(mechs, "act")
    assert used == "good"


# -- session-aware ordering --------------------------------------------------------

def test_input_order_x11_prefers_xdotool(monkeypatch):
    monkeypatch.delenv("WINDYTALK_INPUT", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert lx._input_order()[0] == "xdotool"
    assert set(lx._input_order()) == {"xdotool", "ydotool", "wtype"}, "all prongs still present"


def test_input_order_wayland_prefers_ydotool(monkeypatch):
    monkeypatch.delenv("WINDYTALK_INPUT", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    order = lx._input_order()
    assert order[0] == "ydotool"
    assert "xdotool" in order, "XWayland fallback prong must still be available last"


def test_input_order_env_override_forces_choice(monkeypatch):
    monkeypatch.setenv("WINDYTALK_INPUT", "wtype")
    assert lx._input_order()[0] == "wtype"
    assert set(lx._input_order()) == {"xdotool", "ydotool", "wtype"}


# -- Linux backend end-to-end: injected mechanisms prove the pivot -----------------

def test_linux_type_text_pivots_when_primary_tool_dead(monkeypatch):
    # X11 session: order is xdotool, ydotool, wtype. Both xdotool and ydotool are
    # 'installed', but xdotool THROWS (dead) — type must pivot to ydotool.
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("WINDYTALK_INPUT", raising=False)
    present = {"xdotool", "ydotool"}  # wtype not installed
    monkeypatch.setattr(lx, "_which", lambda t: t if t in present else None)
    ran = []
    monkeypatch.setattr(lx, "_xdotool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no X")))
    monkeypatch.setattr(lx, "_ydotool", lambda *a, **k: ran.append(("ydotool", a)))

    out = lx.LinuxBackend().type_text("hello")
    assert out == "Typed 5 characters"
    assert ran and ran[0][0] == "ydotool", "must have pivoted from the dead xdotool to ydotool"


def test_linux_type_text_all_dead_is_unsupported(monkeypatch):
    monkeypatch.delenv("WINDYTALK_INPUT", raising=False)
    monkeypatch.setattr(lx, "_which", lambda t: None)  # nothing installed
    with pytest.raises(UnsupportedTool):
        lx.LinuxBackend().type_text("hi")


def test_linux_mouse_click_falls_back_xdotool_to_ydotool(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WINDYTALK_INPUT", raising=False)
    monkeypatch.setattr(lx, "_which", lambda t: t if t in ("xdotool", "ydotool") else None)
    monkeypatch.setattr(lx, "_xdotool", lambda *a, **k: (_ for _ in ()).throw(OSError("dead")))
    ran = []
    monkeypatch.setattr(lx, "_ydotool", lambda *a, **k: ran.append(a))
    out = lx.LinuxBackend().mouse_click(10, 20, "left")
    assert "clicked at (10, 20)" in out
    assert ran, "mouse_click must have pivoted to ydotool"


def test_linux_capabilities_reflect_the_wider_mechanism_set(monkeypatch):
    # Only wtype present (no xdotool/ydotool) -> input tools still supported.
    monkeypatch.setattr(lx, "_which", lambda t: t if t in ("wtype", "grim", "xdg-open", "gtk-launch") else None)
    caps = lx.LinuxBackend().capabilities()
    assert caps["type_text"] is True, "wtype alone must satisfy the input capability"
    assert caps["press_keys"] is True
    assert caps["screenshot"] is True, "grim alone must satisfy the screenshot capability"


# ============================ macOS adapter =====================================

from hands.backends import macos as mac  # noqa: E402


def test_macos_type_pivots_from_cliclick_to_osascript(monkeypatch):
    # A stock Mac has NO cliclick; typing must pivot to the built-in osascript.
    monkeypatch.setattr(mac, "_which", lambda t: t if t == "osascript" else None)
    ran = []
    monkeypatch.setattr(mac, "_cliclick", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cliclick absent")))
    monkeypatch.setattr(mac, "_osa", lambda script, **k: ran.append(script) or "")
    out = mac.MacOSBackend().type_text("hi there")
    assert out == "Typed 8 characters"
    assert ran and "keystroke" in ran[0], "must have typed via osascript keystroke"


def test_macos_press_keys_uses_osascript_when_cliclick_absent(monkeypatch):
    monkeypatch.setattr(mac, "_which", lambda t: t if t == "osascript" else None)
    scripts = []
    monkeypatch.setattr(mac, "_osa", lambda script, **k: scripts.append(script) or "")
    out = mac.MacOSBackend().press_keys("cmd+c")
    assert out == "Pressed cmd+c"
    joined = "\n".join(scripts)
    assert "command down" in joined and 'keystroke "c"' in joined


def test_macos_all_input_mechanisms_absent_is_unsupported(monkeypatch):
    monkeypatch.setattr(mac, "_which", lambda t: None)
    monkeypatch.setattr(mac, "_quartz_available", lambda: False)
    with pytest.raises(UnsupportedTool):
        mac.MacOSBackend().type_text("x")
    with pytest.raises(UnsupportedTool):
        mac.MacOSBackend().mouse_click(1, 2)


def test_macos_capabilities_stock_mac_types_via_osascript(monkeypatch):
    # osascript + open + screencapture present; cliclick + Quartz absent.
    present = {"osascript", "open", "screencapture"}
    monkeypatch.setattr(mac, "_which", lambda t: t if t in present else None)
    monkeypatch.setattr(mac, "_quartz_available", lambda: False)
    caps = mac.MacOSBackend().capabilities()
    assert caps["type_text"] is True, "stock Mac types via osascript"
    assert caps["press_keys"] is True
    assert caps["mouse_click"] is False, "no cliclick and no Quartz -> honest false for pointer"


def test_macos_mouse_uses_quartz_when_cliclick_absent(monkeypatch):
    monkeypatch.setattr(mac, "_which", lambda t: None)  # no cliclick
    monkeypatch.setattr(mac, "_quartz_available", lambda: True)
    hit = []
    monkeypatch.setattr(mac, "_quartz_click", lambda x, y, b: hit.append((x, y, b)))
    out = mac.MacOSBackend().mouse_click(5, 6, "left")
    assert "clicked at (5, 6)" in out
    assert hit == [(5, 6, "left")], "must have clicked via Quartz"


# ============================ Windows adapter ===================================

from hands.backends import windows as win  # noqa: E402


def test_windows_ps_prefers_powershell_then_pwsh(monkeypatch):
    # Only pwsh present (a PowerShell-7-only box) -> _ps must use it.
    monkeypatch.setattr(win.shutil, "which", lambda t: t if t == "pwsh" else None)
    assert win._ps_binary() == "pwsh"
    # Both present -> Windows PowerShell 5.1 preferred.
    monkeypatch.setattr(win.shutil, "which", lambda t: t if t in ("powershell", "pwsh") else None)
    assert win._ps_binary() == "powershell"


def test_windows_ps_no_interpreter_is_unsupported(monkeypatch):
    monkeypatch.setattr(win.shutil, "which", lambda t: None)
    with pytest.raises(UnsupportedTool):
        win._ps("echo hi")


def test_windows_ps_runs_on_pwsh_only_box(monkeypatch):
    monkeypatch.setattr(win.shutil, "which", lambda t: t if t == "pwsh" else None)
    seen = {}

    class _R:
        returncode = 0
        stdout = "output"
        stderr = ""

    def fake_run(cmd, **k):
        seen["binary"] = cmd[0]
        return _R()

    monkeypatch.setattr(win.subprocess, "run", fake_run)
    out = win._ps("Get-Process")
    assert out == "output"
    assert seen["binary"] == "pwsh", "the tool ran on the only interpreter present"


def test_windows_capabilities_true_on_pwsh_only(monkeypatch):
    monkeypatch.setattr(win.shutil, "which", lambda t: t if t == "pwsh" else None)
    caps = win.WindowsBackend().capabilities()
    assert all(caps.values()), "a pwsh-only box must report the tools as supported"
    monkeypatch.setattr(win.shutil, "which", lambda t: None)
    caps2 = win.WindowsBackend().capabilities()
    assert not any(caps2.values()), "no PowerShell at all -> honest all-false"


# ===== real-machine stress-test findings (OC2 Ubuntu, OC5 Intel Mac) ==============

def test_ydotool_socket_uses_current_uid_not_hardcoded_1000(monkeypatch):
    # The default socket path must follow the ACTUAL login uid — hardcoding 1000
    # silently broke ydotool on any box where the user isn't uid 1000.
    monkeypatch.delenv("YDOTOOL_SOCKET", raising=False)
    monkeypatch.setattr(lx.os, "getuid", lambda: 1234)
    assert lx._ydotool_socket() == "/run/user/1234/.ydotool_socket"
    monkeypatch.setenv("YDOTOOL_SOCKET", "/custom/sock")
    assert lx._ydotool_socket() == "/custom/sock", "explicit env still wins"


def test_ydotool_unavailable_without_daemon_or_uinput(monkeypatch):
    # Found on OC2: no ydotoold + no /dev/uinput access -> ydotool crashes ~1.5s
    # in. It must be treated as ABSENT so the chain skips it (no wasted latency).
    monkeypatch.setattr(lx, "_which", lambda t: "/usr/bin/ydotool" if t == "ydotool" else None)
    monkeypatch.delenv("YDOTOOL_SOCKET", raising=False)
    monkeypatch.setattr(lx.os.path, "exists", lambda p: False)   # no socket
    monkeypatch.setattr(lx.os, "access", lambda p, m: False)     # no uinput
    assert lx._ydotool_available() is False


def test_ydotool_available_when_socket_present(monkeypatch):
    monkeypatch.setattr(lx, "_which", lambda t: "/usr/bin/ydotool" if t == "ydotool" else None)
    monkeypatch.setattr(lx.os.path, "exists", lambda p: True)    # ydotoold socket present
    assert lx._ydotool_available() is True


def test_ydotool_available_via_uinput_when_no_socket(monkeypatch):
    monkeypatch.setattr(lx, "_which", lambda t: "/usr/bin/ydotool" if t == "ydotool" else None)
    monkeypatch.setattr(lx.os.path, "exists", lambda p: False)   # no socket
    monkeypatch.setattr(lx.os, "access", lambda p, m: p == "/dev/uinput")  # uinput writable
    assert lx._ydotool_available() is True


def test_chain_skips_doomed_ydotool_and_never_runs_it(monkeypatch):
    # Wayland order is [ydotool, wtype, xdotool]. ydotool is installed but has no
    # daemon/uinput -> it must be SKIPPED (never executed), pivoting straight to
    # wtype with zero latency, not crashed-into.
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("WINDYTALK_INPUT", raising=False)
    monkeypatch.setattr(lx, "_which", lambda t: t if t in ("ydotool", "wtype") else None)
    monkeypatch.setattr(lx, "_ydotool_available", lambda: False)  # daemon/uinput down
    ydotool_ran = []
    wtype_ran = []
    monkeypatch.setattr(lx, "_ydotool", lambda *a, **k: ydotool_ran.append(a))
    monkeypatch.setattr(lx, "_wtype", lambda *a, **k: wtype_ran.append(a))
    out = lx.LinuxBackend().type_text("hi")
    assert out == "Typed 2 characters"
    assert ydotool_ran == [], "a doomed ydotool must be skipped, never executed"
    assert wtype_ran, "typing pivoted straight to wtype"


def test_capabilities_input_false_when_only_doomed_ydotool(monkeypatch):
    # GNOME-Wayland reality: only ydotool installed, but no daemon/uinput -> input
    # is honestly unsupported (not a false 'yes').
    monkeypatch.setattr(lx, "_which", lambda t: "/usr/bin/ydotool" if t == "ydotool" else None)
    monkeypatch.setattr(lx, "_ydotool_available", lambda: False)
    caps = lx.LinuxBackend().capabilities()
    assert caps["type_text"] is False, "doomed-only ydotool -> honest unsupported"
