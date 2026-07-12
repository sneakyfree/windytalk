"""Phase 2 of docs/GAP_CLOSING_PLAN.md — the vision loop + AT-SPI fast lane.

The spine: screenshot → local vision model → capture-px point →
coordinate-click — the only rung that works on Chrome/Chromium (invisible to
AT-SPI, can't be woken at runtime). The fast lane: AT-SPI named actions, whose
names VARY by toolkit (live-measured: links 'jump', entries 'activate',
buttons 'click'/'press'), plus the found-but-not-actionable extents case and
the GTK4-on-Wayland bogus-extents guard.

No live desktop, no live model: the locator's transport is faked, AT-SPI nodes
are hand-built, and screenshots are hand-written PNGs.
"""
from __future__ import annotations

from test_pointer_engine import _png_bytes

from hands.backends import linux as lx
from hands.backends import macos as mac
from hands.backends import windows as win
from hands.coords import CaptureGeometry
from hands.vision import VisionLocator, _parse_point

# ============ the locator: parsing, payload, fault behavior ======================

SIZE = (1920, 1080)


def test_parse_strict_json():
    assert _parse_point('{"found": true, "x": 100, "y": 200}', SIZE) == (100, 200)


def test_parse_json_wrapped_in_prose_and_fences():
    raw = 'Sure! Here it is:\n```json\n{"found": true, "x": 5, "y": 7}\n```\nDone.'
    assert _parse_point(raw, SIZE) == (5, 7)


def test_parse_not_found_and_garbage():
    assert _parse_point('{"found": false}', SIZE) is None
    assert _parse_point("no json here at all", SIZE) is None
    assert _parse_point('{"x": 5, "y": 7}', SIZE) is None, "found:true is required"


def test_parse_rejects_off_image_hallucination():
    # a hallucinated point must NOT be clamped into a click
    assert _parse_point('{"found": true, "x": 5000, "y": 10}', SIZE) is None
    assert _parse_point('{"found": true, "x": -3, "y": 10}', SIZE) is None


def test_locator_payload_shape(tmp_path, monkeypatch):
    shot = tmp_path / "s.png"
    shot.write_bytes(_png_bytes(800, 600))
    seen = {}

    def fake_post(self, body):
        seen.update(body)
        return '{"found": true, "x": 10, "y": 20}'
    monkeypatch.setattr(VisionLocator, "_post", fake_post)
    loc = VisionLocator("http://model:8000/v1", model="qwen-vl")
    assert loc.locate(shot, "the Send button") == (10, 20)
    assert seen["model"] == "qwen-vl" and seen["temperature"] == 0
    content = seen["messages"][0]["content"]
    assert "800x600" in content[0]["text"] and "the Send button" in content[0]["text"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_locator_transport_fault_returns_none_never_raises(tmp_path, monkeypatch):
    shot = tmp_path / "s.png"
    shot.write_bytes(_png_bytes(100, 100))

    def boom(self, body):
        raise OSError("connection refused")
    monkeypatch.setattr(VisionLocator, "_post", boom)
    assert VisionLocator("http://dead:1/v1").locate(shot, "x") is None


def test_locator_unreadable_image_returns_none(tmp_path):
    assert VisionLocator("http://m/v1").locate(tmp_path / "missing.png", "x") is None


def test_from_env_and_configured(monkeypatch):
    monkeypatch.delenv("WINDYTALK_VISION_URL", raising=False)
    assert VisionLocator.from_env() is None and not VisionLocator.configured()
    monkeypatch.setenv("WINDYTALK_VISION_URL", "http://veron:8000/v1/")
    loc = VisionLocator.from_env()
    assert loc is not None and loc.base_url == "http://veron:8000/v1"
    assert VisionLocator.configured()


# ============ the shared vision rung (_click_visual) ==============================

class _SpineBackend(win.WindowsBackend):
    """Any backend works — _click_visual is base-class shared. Fakes capture +
    click and records the flow."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.clicks = []

    def screenshot(self, path=None):
        dest = self.tmp / (path or "shot.png")
        dest.write_bytes(_png_bytes(1000, 500))
        self._last_capture = CaptureGeometry(1000, 500, 500, 250)  # 2x capture
        return f"Saved screenshot to {dest}"

    def mouse_click(self, x, y, button="left"):
        self.clicks.append(self._map_capture_point(x, y))
        return f"clicked ({x}, {y})"


def test_click_visual_full_spine_with_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDYTALK_VISION_URL", "http://model/v1")
    monkeypatch.setattr(VisionLocator, "_post",
                        lambda self, body: '{"found": true, "x": 600, "y": 300}')
    b = _SpineBackend(tmp_path)
    out = b._click_visual("Subscribe button")
    assert out and "located visually" in out
    assert b.clicks == [(300, 150)], \
        "vision returns CAPTURE px; the click maps them to logical via the fresh geometry"


def test_click_visual_none_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("WINDYTALK_VISION_URL", raising=False)
    b = _SpineBackend(tmp_path)
    assert b._click_visual("x") is None and b.clicks == []


def test_click_visual_none_when_model_cant_find(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDYTALK_VISION_URL", "http://model/v1")
    monkeypatch.setattr(VisionLocator, "_post", lambda self, body: '{"found": false}')
    b = _SpineBackend(tmp_path)
    assert b._click_visual("ghost") is None
    assert b.clicks == [], "no located point, no click — never guess"


# ============ AT-SPI fast lane: action-name variance + extents guard =============

class _Action:
    def __init__(self, names):
        self.names = names
        self.done = []

    def get_n_actions(self):
        return len(self.names)

    def get_action_name(self, i):
        return self.names[i]

    def do_action(self, i):
        self.done.append(self.names[i])


class _Node:
    def __init__(self, names=None):
        self.action = _Action(names) if names is not None else None

    def get_action_iface(self):
        return self.action

    def get_name(self):
        return "el"


def test_preferred_action_picks_click_like_name_over_index_zero():
    b = lx.LinuxBackend()
    n = _Node(["expand or contract", "jump"])  # a web link: 'jump' is the click
    assert b._do_preferred_action(n) is True
    assert n.action.done == ["jump"], "must pick the recognized name, not action 0"


def test_preferred_action_falls_back_to_action_zero():
    b = lx.LinuxBackend()
    n = _Node(["frobnicate"])
    assert b._do_preferred_action(n) is True
    assert n.action.done == ["frobnicate"]


def test_preferred_action_false_without_actions():
    b = lx.LinuxBackend()
    assert b._do_preferred_action(_Node(None)) is False
    assert b._do_preferred_action(_Node([])) is False


def test_click_element_no_atspi_goes_straight_to_vision(monkeypatch):
    monkeypatch.setattr(lx, "_atspi", lambda: (_ for _ in ()).throw(ImportError("no gi")))
    hits = []
    monkeypatch.setattr(lx.LinuxBackend, "_click_visual",
                        lambda self, label: hits.append(label) or f"Clicked {label!r} (located visually)")
    out = lx.LinuxBackend().click_element("Send")
    assert "located visually" in out and hits == ["Send"]


def test_click_element_not_found_falls_to_vision_then_honest(monkeypatch):
    class _App:
        def get_child_count(self):
            return 0

        def get_name(self):
            return "chrome-ish"
    monkeypatch.setattr(lx, "_atspi", lambda: object())
    monkeypatch.setattr(lx.LinuxBackend, "_active_app", lambda self, A: _App())
    monkeypatch.setattr(lx.LinuxBackend, "_click_visual", lambda self, label: None)
    out = lx.LinuxBackend().click_element("Send")
    assert out == "Couldn't find a clickable element named 'Send'."


# ============ capabilities: the vision lane counts, honestly =====================

def test_click_element_capability_via_vision_without_atspi(monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(lx, "_which", lambda t: t if t == "xdotool" else None)
    monkeypatch.setattr(lx, "_atspi_probe", lambda: False)
    monkeypatch.setattr(lx, "_screenshot_probe", lambda: True)
    monkeypatch.setattr(lx, "_vision_configured", lambda: False)
    assert lx.LinuxBackend().capabilities()["click_element"] is False
    monkeypatch.setattr(lx, "_vision_configured", lambda: True)
    caps = lx.LinuxBackend().capabilities()
    assert caps["click_element"] is True, "vision + capture + pointer = a working click_element"


# ============ macOS / Windows: notfound falls through to the vision rung =========

def test_macos_click_element_vision_fallback(monkeypatch):
    monkeypatch.setattr(mac, "_osa", lambda s, **k: "notfound")
    monkeypatch.setattr(mac.MacOSBackend, "_click_visual",
                        lambda self, label: f"Clicked {label!r} (located visually)")
    assert "located visually" in mac.MacOSBackend().click_element("Send")


def test_windows_click_element_vision_fallback(monkeypatch):
    monkeypatch.setattr(win, "_ps", lambda s, timeout=15: "notfound")
    monkeypatch.setattr(win.WindowsBackend, "_click_visual",
                        lambda self, label: f"Clicked {label!r} (located visually)")
    assert "located visually" in win.WindowsBackend().click_element("Send")


def test_windows_click_element_honest_without_vision(monkeypatch):
    monkeypatch.delenv("WINDYTALK_VISION_URL", raising=False)
    monkeypatch.setattr(win, "_ps", lambda s, timeout=15: "notfound")
    out = win.WindowsBackend().click_element("Send")
    assert out == "Couldn't find a clickable element named 'Send'."
