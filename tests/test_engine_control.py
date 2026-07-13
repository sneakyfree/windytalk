"""Engine-box control surface (engine.mcp.v1 — ADR-060 §7) + supervisor.

The surface is tested against a FAKE EngineController (no GPU/models); the
supervisor's restart/liveness logic is tested with a dummy sleep subprocess.
Covers the doctrine invariants: contract parity, tri-state capabilities,
surface-side tier enforcement, honest 'unsupported', the security wall, and
doctor-not-in-patient restart.
"""
from __future__ import annotations

import json
import sys
import urllib.request

import pytest

from engine.control import EngineController, EngineControlSurface


class FakeController(EngineController):
    def __init__(self):
        self.calls = []

    def health(self):
        return {"healthy": True, "worker_alive": True, "providers_warm": True,
                "gpu": True, "serving": True, "active_sessions": 2, "mode": "normal"}

    def status(self):
        return {"state": "serving", "active_sessions": 2, "uptime_s": 42.0}

    def config(self):
        return {"stt": "whisper", "tts": "kokoro", "brain": "mind",
                "device": "cuda", "port": 8788}

    def logs(self, lines):
        self.calls.append(("logs", lines))
        return "line1\nline2"

    def selftest(self):
        return {"ok": True, "stages": [{"stage": "roundtrip", "pass": True,
                                        "detail": "audio", "ms": 900.0}]}

    def reconnect(self):
        self.calls.append(("reconnect",))
        return "re-warmed"

    def restart_engine(self):
        self.calls.append(("restart_engine",))
        return "restarting the engine worker"

    def reset_to_defaults(self):
        self.calls.append(("reset_to_defaults",))
        return "reset; restarting"


@pytest.fixture
def surface():
    return EngineControlSurface(FakeController(), token="tok")


# ---- contract parity + tri-state capabilities --------------------------------

def test_tool_list_serves_every_contract_tool(surface):
    names = {t["name"] for t in surface.tool_list()}
    # the 14 baseline knobs must all be present (never absent — ADR-060)
    for t in ("get_health", "restart_engine", "reset_to_defaults", "apply_update",
              "enter_safe_mode", "set_engine_config", "check_for_update"):
        assert t in names
    assert len(names) == 14


def test_capabilities_are_honest_tri_state(surface):
    caps = surface.invoke("get_capabilities")["result"]["tools"]
    assert caps["get_health"] is True
    assert caps["restart_engine"] is True
    assert caps["reset_to_defaults"] is True
    # declared-but-unwired knobs report 'unsupported', never absent, never a lie
    for t in ("enter_safe_mode", "exit_safe_mode", "set_engine_config",
              "check_for_update", "apply_update"):
        assert caps[t] == "unsupported", t


# ---- surface-side tier enforcement (headless server) -------------------------

def test_auto_allow_runs_without_confirm(surface):
    assert surface.invoke("get_health")["ok"] is True
    assert surface.invoke("reconnect")["ok"] is True  # auto_allow


def test_ask_first_requires_confirm(surface):
    r = surface.invoke("restart_engine")  # ask_first, no confirm
    assert r["ok"] is False and r["error"] == "confirm_required"
    ok = surface.invoke("restart_engine", {"confirm": True})
    assert ok["ok"] is True and ok["result"] == "restarting the engine worker"
    assert surface.controller.calls[-1] == ("restart_engine",)


def test_always_confirm_requires_confirm(surface):
    assert surface.invoke("reset_to_defaults")["error"] == "confirm_required"
    assert surface.invoke("reset_to_defaults", {"confirm": True})["ok"] is True


# ---- honest unsupported + unknown --------------------------------------------

def test_unwired_tools_are_unsupported_not_fake_success(surface):
    r = surface.invoke("apply_update", {"confirm": True})
    assert r["ok"] is False and r["error"] == "unsupported"
    r2 = surface.invoke("set_engine_config", {"stt": "tiny", "confirm": True})
    assert r2["error"] == "unsupported"


def test_unknown_tool(surface):
    assert surface.invoke("frobnicate")["error"] == "unknown tool: frobnicate"


def test_bad_args(surface):
    assert surface.invoke("get_health", "not a dict")["error"].startswith("bad args")


# ---- diagnostics pass through ------------------------------------------------

def test_diagnostics(surface):
    assert surface.invoke("get_health")["result"]["mode"] == "normal"
    assert surface.invoke("get_status")["result"]["state"] == "serving"
    assert surface.invoke("get_config")["result"]["stt"] == "whisper"
    assert surface.invoke("get_logs", {"lines": 50})["result"] == "line1\nline2"
    assert surface.controller.calls[-1] == ("logs", 50)
    assert surface.invoke("run_selftest")["result"]["ok"] is True


# ---- MCP JSON-RPC ------------------------------------------------------------

def test_mcp_lifecycle(surface):
    init = surface.handle_mcp({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert surface.handle_mcp({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    lst = surface.handle_mcp({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(lst["result"]["tools"]) == 14
    call = surface.handle_mcp({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "get_health", "arguments": {}}})
    assert call["result"]["isError"] is False
    assert json.loads(call["result"]["content"][0]["text"])["ok"] is True


# ---- the security wall (over a real loopback socket) -------------------------

def _post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_security_wall_and_native_shapes():
    surface = EngineControlSurface(FakeController(), token="secret")
    host, port = surface.serve("127.0.0.1", 0)  # ephemeral port
    base = f"http://127.0.0.1:{port}"
    try:
        auth = {"X-Windytalk-Engine-Token": "secret"}
        # no token -> 401
        code, _ = _post(f"{base}/invoke", {"name": "get_health"}, {})
        assert code == 401
        # Origin header -> 403 (a browser can never drive us)
        code, _ = _post(f"{base}/invoke", {"name": "get_health"},
                        {**auth, "Origin": "http://evil.example"})
        assert code == 403
        # canonical {name, arguments}
        code, body = _post(f"{base}/invoke", {"name": "get_health", "arguments": {}}, auth)
        assert code == 200 and body["ok"] is True
        # legacy {tool, args} still accepted
        code, body = _post(f"{base}/invoke", {"tool": "get_status", "args": {}}, auth)
        assert code == 200 and body["result"]["state"] == "serving"
        # tier still enforced over HTTP
        code, body = _post(f"{base}/invoke", {"name": "restart_engine"}, auth)
        assert body["error"] == "confirm_required"
    finally:
        surface.stop()


# ---- the supervisor: doctor-not-in-patient restart ---------------------------

def _sleeper():
    # a dummy "worker" that just sleeps — stands in for engine/server.py
    return [sys.executable, "-c", "import time,sys; sys.stderr.write('worker up\\n'); "
            "sys.stderr.flush(); time.sleep(300)"]


def test_supervisor_restart_respawns_a_hung_worker():
    from server.supervise import EngineSupervisor
    sup = EngineSupervisor(spawn_cmd=_sleeper(), engine_port=59999)
    try:
        sup.start()
        assert sup.health()["worker_alive"] is True
        pid1 = sup._proc.pid
        msg = sup.restart_engine()
        assert "restarting" in msg
        pid2 = sup._proc.pid
        assert pid2 != pid1, "restart spawned a NEW worker process"
        assert sup.health()["worker_alive"] is True
    finally:
        sup.stop()
    assert sup.health()["worker_alive"] is False


def test_supervisor_reconnect_respawns_when_dead():
    from server.supervise import EngineSupervisor
    sup = EngineSupervisor(spawn_cmd=_sleeper(), engine_port=59998)
    try:
        sup.start()
        sup.stop()  # simulate a dead worker
        assert sup.health()["worker_alive"] is False
        msg = sup.reconnect()
        assert "respawned" in msg
        assert sup.health()["worker_alive"] is True
    finally:
        sup.stop()


def test_supervisor_registers_and_unregisters_surface(tmp_path):
    from server.supervise import register_surface, unregister_surface
    entry = {"product": "windytalk-engine", "version": "0.1.0",
             "contract": "engine.mcp.v1", "http": "http://127.0.0.1:8783"}
    register_surface(entry, home=str(tmp_path))
    reg = json.loads((tmp_path / ".windy" / "surfaces.json").read_text())
    assert reg["surfaces"][0]["product"] == "windytalk-engine"
    unregister_surface("windytalk-engine", home=str(tmp_path))
    reg = json.loads((tmp_path / ".windy" / "surfaces.json").read_text())
    assert reg["surfaces"] == []
