"""Task 0.4 verify: emit() never raises, never sends content, no-ops unconfigured."""
import json
from pathlib import Path

import pytest
from jsonschema import validate

from telemetry import emit as emit_mod

CONTRACT = json.loads(
    (Path(__file__).resolve().parent.parent / "contracts" / "telemetry.v1.json").read_text()
)


@pytest.fixture
def captured(monkeypatch):
    """Configure telemetry and capture outbound bodies instead of hitting the network."""
    sent = []
    monkeypatch.setenv("WINDYTALK_TELEMETRY_TOKEN", "wat_test")

    def fake_send(url, token, body):
        sent.append((url, token, json.loads(body)))
        return 202

    monkeypatch.setattr(emit_mod, "_send", fake_send)
    return sent


def test_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("WINDYTALK_TELEMETRY_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(emit_mod, "_send", lambda *a: calls.append(a))
    emit_mod.emit("session.start", session_id="s1")
    emit_mod.flush()
    assert calls == []


def test_happy_path_batch_matches_frozen_contract(captured):
    emit_mod.emit("session.end", actor_type="human", actor_id="s1", session_id="s1",
                  dur_ms=1000, turns=3, model="llama-3.3-70b-versatile",
                  latency_ms={"transport_p90": 21.0},
                  metadata={"install_id": "inst-abc"})
    emit_mod.flush()
    assert len(captured) == 1
    url, token, body = captured[0]
    assert url == emit_mod.DEFAULT_URL and token == "wat_test"
    validate(body, CONTRACT)  # what leaves the emitter IS the frozen schema


def test_content_is_structurally_stripped(captured):
    emit_mod.emit("turn.complete", session_id="s1",
                  transcript="user said something private",
                  message="hi", text="hello", args={"a": 1}, prompt="p")
    emit_mod.flush()
    (_, _, body), = captured
    event = body["events"][0]
    forbidden = {"transcript", "message", "text", "args", "prompt"}
    assert not (forbidden & set(event)), event
    validate(body, CONTRACT)


def test_metadata_subkeys_are_whitelisted(captured):
    emit_mod.emit("session.start", actor_type="human", actor_id="s1", session_id="s1",
                  metadata={"app_version": "0.1.0", "os": "linux", "install_id": "i",
                            "transcript": "secret", "note": "leak"})
    emit_mod.flush()
    (_, _, body), = captured
    md = body["events"][0]["metadata"]
    assert set(md) == {"app_version", "os", "install_id"}  # non-whitelisted dropped
    validate(body, CONTRACT)


def test_never_raises_on_garbage(monkeypatch):
    monkeypatch.setenv("WINDYTALK_TELEMETRY_TOKEN", "wat_test")
    monkeypatch.setenv("WINDYTALK_TELEMETRY_URL", "http://127.0.0.1:1")  # refused
    emit_mod.emit("engine.error", session_id="s1", error_code=object())  # non-JSON value
    emit_mod.emit(None)          # nonsense event_type
    emit_mod.emit("x", latency_ms=float("nan"))
    emit_mod.flush()             # includes real (failing) network sends — must stay silent


def test_send_timeout_is_within_budget():
    assert emit_mod.TIMEOUT_S <= 0.2
