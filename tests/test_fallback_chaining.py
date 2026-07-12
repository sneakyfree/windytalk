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
