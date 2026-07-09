"""Task 0.3 verify: the frozen contract schemas validate, and telemetry.v1
rejects every content-ish payload (the local mirror of the ingest's 422)."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError, validate

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def load(name):
    return json.loads((CONTRACTS / name).read_text())


# ---------- hands.mcp.v1 ----------

def test_hands_contract_parses_and_tools_have_valid_schemas():
    doc = load("hands.mcp.v1.json")
    assert doc["contract"] == "hands.mcp.v1"
    tiers = set(doc["tiers"]["values"])
    assert tiers == {"auto_allow", "ask_first", "always_confirm"}
    names = [t["name"] for t in doc["tools"]]
    assert len(names) == len(set(names)) == 12  # the proven prototype surface
    for tool in doc["tools"]:
        assert tool["tier"] in tiers, tool["name"]
        Draft202012Validator.check_schema(tool["inputSchema"])  # raises if invalid
        assert tool["inputSchema"].get("additionalProperties") is False


def test_hands_dangerous_tools_are_gated():
    doc = load("hands.mcp.v1.json")
    tier = {t["name"]: t["tier"] for t in doc["tools"]}
    assert tier["run_shell"] == "always_confirm"
    assert tier["mouse_click"] == "ask_first"


# ---------- telemetry.v1 ----------

TELEMETRY = None


def telemetry_schema():
    global TELEMETRY
    if TELEMETRY is None:
        TELEMETRY = load("telemetry.v1.json")
        Draft202012Validator.check_schema(TELEMETRY)
    return TELEMETRY


def good_event(**overrides):
    ev = {
        "service": "windytalk",
        "platform": "windy-talk",
        "event_type": "session.end",
        "actor_type": "user",
        "session_id": "s-123",
        "dur_ms": 61000,
        "turns": 14,
        "model": "llama-3.3-70b-versatile",
        "cost_microcents": 240,
        "latency_ms": {"eos_to_first_audio_p90": 940.5, "transport_p90": 22.1},
    }
    ev.update(overrides)
    return ev


def test_good_batch_validates():
    validate({"events": [good_event()]}, telemetry_schema())


@pytest.mark.parametrize(
    "content_key",
    ["transcript", "message", "text", "content", "prompt", "response", "args", "query"],
)
def test_content_ish_key_rejected_at_event_level(content_key):
    with pytest.raises(ValidationError):
        validate({"events": [good_event(**{content_key: "the user said something"})]},
                 telemetry_schema())


def test_content_ish_key_rejected_at_root_level():
    with pytest.raises(ValidationError):
        validate({"events": [good_event()], "transcript": "smuggled"}, telemetry_schema())


def test_free_text_cannot_ride_in_enum_or_id_fields():
    with pytest.raises(ValidationError):  # event_type is a closed vocabulary
        validate({"events": [good_event(event_type="the user asked about their bank")]},
                 telemetry_schema())
    with pytest.raises(ValidationError):  # ids are capped at 64 chars
        validate({"events": [good_event(session_id="x" * 65)]}, telemetry_schema())
    with pytest.raises(ValidationError):  # latency values are numbers, not strings
        validate({"events": [good_event(latency_ms={"transport_p90": "fast"})]},
                 telemetry_schema())


def test_required_ingest_trio_enforced():
    for missing in ("service", "event_type", "actor_type", "session_id", "platform"):
        ev = good_event()
        del ev[missing]
        with pytest.raises(ValidationError):
            validate({"events": [ev]}, telemetry_schema())


def test_empty_batch_rejected():
    with pytest.raises(ValidationError):
        validate({"events": []}, telemetry_schema())
