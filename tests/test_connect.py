"""Task 1.3 tests for agents/connect.py — bundle reading + the Mind brain handle.
Pure-logic tests use a fixture bundle; a live mock-pairing test (skipped if the
`windy` CLI isn't installed) proves the full pair→bundle→handle flow."""
import importlib.util
import json
from datetime import UTC, datetime, timedelta

import pytest

from agents import ConnectError, WindyConnect
from brains.mind import MindBrain

_HAS_WINDY_CONNECT = importlib.util.find_spec("windy_connect") is not None


def write_state(home, bundle):
    d = home / ".windy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"state_version": "1", "bundle": bundle}))


def fixture_bundle(expires_days=30):
    exp = (datetime.now(UTC) + timedelta(days=expires_days)).isoformat()
    return {
        "bundle_version": "1.0",
        "expires_at": exp,
        "tier": "credentialed",
        "windy_mind": {
            "kind": "openai-compatible",
            "base_url": "https://api.windymind.ai/v1",
            "api_key": "wm_user_paired_key",
            "default_model": "windy-mind-auto",
        },
    }


def test_not_connected_when_no_state(tmp_path):
    c = WindyConnect(home=str(tmp_path))
    assert c.is_connected() is False
    with pytest.raises(ConnectError):
        c.bundle()


def test_connected_reads_bundle_and_builds_mind_brain(tmp_path):
    write_state(tmp_path, fixture_bundle())
    c = WindyConnect(home=str(tmp_path))
    assert c.is_connected() is True
    cfg = c.mind_config()
    assert cfg["base_url"].endswith("/v1")
    assert cfg["api_key"] == "wm_user_paired_key"
    brain = c.mind_brain()
    assert isinstance(brain, MindBrain)
    assert brain.api_key == "wm_user_paired_key"
    # "windy-mind-auto" collapses to the brain's own fast-TTFT default, not a literal model
    assert brain.model and brain.model != "windy-mind-auto"


def test_expired_bundle_is_not_connected(tmp_path):
    write_state(tmp_path, fixture_bundle(expires_days=-1))
    assert WindyConnect(home=str(tmp_path)).is_connected() is False


def test_bundle_without_mind_section_raises(tmp_path):
    b = fixture_bundle()
    del b["windy_mind"]
    write_state(tmp_path, b)
    with pytest.raises(ConnectError, match="windy_mind"):
        WindyConnect(home=str(tmp_path)).mind_config()


@pytest.mark.skipif(not _HAS_WINDY_CONNECT,
                    reason="windy-connect not importable (pip install windy-connect)")
def test_live_mock_pairing_yields_addressable_handle(tmp_path):
    # The full pair→bundle→handle flow, no browser (CLI --mock bundle).
    c = WindyConnect(home=str(tmp_path))
    bundle = c.pair(agents="generic", mock=True)
    assert "windy_mind" in bundle
    assert c.is_connected()
    brain = c.mind_brain()
    assert brain.base_url.startswith("https://api.windymind.ai")
    assert brain.api_key  # a key was provisioned into the handle


def test_untrusted_base_url_refused(tmp_path):
    from agents.connect import ConnectError
    b = fixture_bundle()
    b["windy_mind"]["base_url"] = "http://evil.example/v1"  # non-https, non-windymind
    write_state(tmp_path, b)
    import pytest as _pytest
    with _pytest.raises(ConnectError):
        WindyConnect(home=str(tmp_path)).mind_config()
