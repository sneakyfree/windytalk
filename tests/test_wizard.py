"""Phase 4 of docs/GAP_CLOSING_PLAN.md — the first-run wizard engine.

The engine's promises under test:

  1. `list` never pops UI: every state comes from probe() reads, and a broken
     probe reports "unknown" instead of raising.
  2. A step only reports done when its live re-verify passes — run() results
     are (ok, detail) with ok derived from probing, never assumed.
  3. Idempotency: re-running a satisfied grant step is a no-op (no second
     dialog); "not-needed" steps (X11 portal grants, everything on Windows)
     never execute anything.
  4. The interactive Screenshot grant() asks with interactive=True and a
     human-scale timeout, and discards the grant capture's file.

No test touches a live desktop, bus, or sudo: probes and runners are faked at
the same seams the engine calls through.
"""
from __future__ import annotations

import json

import pytest

from hands import wizard as wz
from hands.backends import portal as pt

# ---- step tables per platform -------------------------------------------------


def test_linux_wayland_step_table(monkeypatch):
    monkeypatch.setattr(wz.sys, "platform", "linux")
    monkeypatch.setattr(wz, "_linux_session", lambda: "wayland")
    ids = [s.id for s in wz.steps()]
    assert ids == ["accessibility-bus", "input-uinput", "pointer-grant",
                   "screenshot-grant", "selftest"]
    assert [s.needs_sudo for s in wz.steps()] == [False, True, False, False, False]


def test_linux_x11_portal_steps_not_needed(monkeypatch):
    monkeypatch.setattr(wz.sys, "platform", "linux")
    monkeypatch.setattr(wz, "_linux_session", lambda: "x11")
    states = {s.id: s.probe() for s in wz.steps()
              if s.id in ("pointer-grant", "screenshot-grant")}
    assert states == {"pointer-grant": wz.NOT_NEEDED,
                      "screenshot-grant": wz.NOT_NEEDED}


def test_mac_and_windows_step_tables(monkeypatch):
    monkeypatch.setattr(wz.sys, "platform", "darwin")
    assert [s.id for s in wz.steps()] == ["accessibility", "screen-recording",
                                          "selftest"]
    monkeypatch.setattr(wz.sys, "platform", "win32")
    assert [s.id for s in wz.steps()] == ["selftest"]


# ---- list: probe-only, crash-safe ----------------------------------------------


def test_list_reports_unknown_for_broken_probe(monkeypatch):
    def boom():
        raise RuntimeError("bus exploded")
    monkeypatch.setattr(wz, "steps", lambda: [
        wz.Step("x", "t", "w", False, boom, None)])
    assert wz.list_steps() == [{"id": "x", "title": "t", "why": "w",
                                "needs_sudo": False, "state": wz.UNKNOWN}]


def test_list_never_calls_run(monkeypatch):
    ran = []
    monkeypatch.setattr(wz, "steps", lambda: [
        wz.Step("x", "t", "w", False, lambda: wz.NEEDED,
                lambda: (ran.append(1) or (True, "")))])
    wz.list_steps()
    assert ran == []


# ---- run: honesty + idempotency -------------------------------------------------


def test_run_not_needed_step_executes_nothing(monkeypatch):
    ran = []
    monkeypatch.setattr(wz, "steps", lambda: [
        wz.Step("p", "t", "w", False, lambda: wz.NOT_NEEDED,
                lambda: (ran.append(1) or (True, "")))])
    res = wz.run_step("p")
    assert res["ok"] is True and res["state"] == wz.NOT_NEEDED and ran == []


def test_run_satisfied_grant_step_is_idempotent_noop(monkeypatch):
    ran = []
    monkeypatch.setattr(wz, "steps", lambda: [
        wz.Step("g", "t", "w", False, lambda: wz.SATISFIED,
                lambda: (ran.append(1) or (True, "")))])
    res = wz.run_step("g")
    assert res["ok"] is True and ran == []  # no second grant dialog, ever


def test_run_selftest_always_runs_even_when_probe_needed(monkeypatch):
    ran = []
    monkeypatch.setattr(wz, "steps", lambda: [
        wz.Step("selftest", "t", "w", False, lambda: wz.SATISFIED,
                lambda: (ran.append(1) or (True, "proof")))])
    res = wz.run_step("selftest")
    assert ran == [1] and res["ok"] is True


def test_run_crashing_step_reports_honest_failure(monkeypatch):
    def crash():
        raise OSError("pkexec vanished")
    monkeypatch.setattr(wz, "steps", lambda: [
        wz.Step("u", "t", "w", True, lambda: wz.NEEDED, crash)])
    res = wz.run_step("u")
    assert res["ok"] is False and "pkexec vanished" in res["detail"]


def test_run_unknown_step_id():
    res = wz.run_step("nope")
    assert res["ok"] is False and "unknown step" in res["detail"]


def test_run_result_state_is_reprobed_after_run(monkeypatch):
    """ok comes from the runner, state from a LIVE re-probe — a runner that
    claims success while the probe still says needed is visible to the client."""
    monkeypatch.setattr(wz, "steps", lambda: [
        wz.Step("g", "t", "w", False, lambda: wz.NEEDED, lambda: (True, "sure"))])
    res = wz.run_step("g")
    assert res["ok"] is True and res["state"] == wz.NEEDED  # the lie is visible


# ---- the interactive Screenshot grant -------------------------------------------


class _FakeGrantShot(pt.PortalScreenshot):
    def __init__(self, response):
        self._response = response
        self.requests = []

    def _request(self, options, timeout):
        self.requests.append({"options": options, "timeout": timeout})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_grant_is_interactive_with_human_timeout_and_discards_file(tmp_path):
    f = tmp_path / "Screenshot-9.png"
    f.write_bytes(b"grant-capture")
    shot = _FakeGrantShot({"uri": f"file://{f}"})
    assert shot.grant() is True
    assert shot.requests[0]["options"] == {"interactive": ("b", True)}
    assert shot.requests[0]["timeout"] == pytest.approx(120.0)
    assert not f.exists()  # the grant's capture is discarded, not delivered


def test_grant_false_on_denial_or_timeout():
    shot = _FakeGrantShot(pt.PortalError("Screenshot.Screenshot response code 1"))
    assert shot.grant() is False


def test_grant_true_even_without_uri():
    # the grant is about the permission, not the pixels — a compositor that
    # returns no uri still granted (code 0)
    assert _FakeGrantShot({}).grant() is True


# ---- CLI shape -------------------------------------------------------------------


def test_cli_list_and_run_shapes(monkeypatch, capsys):
    monkeypatch.setattr(wz, "steps", lambda: [
        wz.Step("a", "T", "W", False, lambda: wz.NEEDED, lambda: (True, "d"))])
    assert wz.main(["list"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["steps"][0]["id"] == "a"
    assert wz.main(["run", "a"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"id": "a", "ok": True, "state": wz.NEEDED, "detail": "d"}
    assert wz.main(["bogus"]) == 2
