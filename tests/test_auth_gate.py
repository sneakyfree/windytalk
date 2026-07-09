"""Task 1.7 tests — the entitlement gate + brokered-token seam (auth/eternitas.py)
and the server's not_entitled path. The dev-key scrub + live 'entitled connects'
are Grant-gated (see auth/eternitas.py); these verify the refuse path, the dev
allow-path, and the no-long-lived-token property."""
import json

import pytest
import websockets

from auth.eternitas import (
    DevAuthorizer,
    Entitlement,
    EternitasAuthorizer,
    broker_token,
    get_authorizer,
)
from brains.base import BrainEvent
from engine.server import VoiceServer, build_frame  # noqa: F401


def test_dev_authorizer_allows_all():
    ent = DevAuthorizer().authorize({"token": "anything"})
    assert ent.entitled and ent.tier == "dev"


def test_get_authorizer_defaults_to_dev(monkeypatch):
    monkeypatch.delenv("WINDYTALK_STRICT_AUTH", raising=False)
    assert isinstance(get_authorizer(), DevAuthorizer)
    monkeypatch.setenv("WINDYTALK_STRICT_AUTH", "1")
    assert isinstance(get_authorizer(), EternitasAuthorizer)


def test_strict_denies_without_passport():
    ent = EternitasAuthorizer().authorize(None)
    assert ent.entitled is False and "no passport" in ent.reason.lower()


def test_strict_denies_until_sku_defined():
    # forced-honest: even a well-formed passport is denied until the windy-talk
    # entitlement SKU exists (never fabricates entitlement)
    ent = EternitasAuthorizer().authorize({"token": "ET26-ABCD-1234"})
    assert ent.entitled is False
    assert "windy-talk" in ent.reason


def test_broker_token_is_forced_honest():
    with pytest.raises(NotImplementedError):
        broker_token("ET26-ABCD-1234", "voice")


def test_entitlement_dataclass():
    e = Entitlement(True, "u1", tier="paid")
    assert e.entitled and e.user_id == "u1" and e.tier == "paid"


# -- server gate: a denying authorizer refuses the connection ----------------

class _Deny:
    def authorize(self, auth):
        return Entitlement(False, "anon", reason="not_entitled test")


class _AllowAs:
    def __init__(self, uid):
        self.uid = uid

    def authorize(self, auth):
        return Entitlement(True, self.uid, tier="paid")


def _providers():
    class STT:
        def is_speech(self, f, sr):
            return f[:2] != b"\x00\x00"

        def transcribe(self, p, sample_rate=16000):
            from engine.providers.stt.base import Transcript
            return Transcript(text="hi")

    class TTS:
        output_rate = 24000

        def synthesize(self, t):
            return b"\x01\x02" * 8

    class Brain:
        model = "m"

        def stream(self, m, tools=None, model=None):
            yield BrainEvent(kind="text", text="Hello.")
            yield BrainEvent(kind="done", finish_reason="stop")

    return STT(), TTS(), Brain()


@pytest.fixture
async def endpoint_with(request):
    srv = VoiceServer(_providers, pace=False, authorizer=request.param)
    server = await srv.serve("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}"
    server.close()
    await server.wait_closed()


@pytest.mark.parametrize("endpoint_with", [_Deny()], indirect=True)
async def test_server_refuses_unentitled(endpoint_with):
    async with websockets.connect(endpoint_with) as ws:
        await ws.send(json.dumps({"type": "hello", "protocol": "voice-session.v1",
                                  "client": {"app": "t", "version": "1", "platform": "x"}}))
        err = json.loads(await ws.recv())
        assert err["type"] == "error" and err["code"] == "not_entitled" and err["fatal"]


@pytest.mark.parametrize("endpoint_with", [_AllowAs("user-42")], indirect=True)
async def test_server_admits_entitled(endpoint_with):
    async with websockets.connect(endpoint_with) as ws:
        await ws.send(json.dumps({"type": "hello", "protocol": "voice-session.v1",
                                  "client": {"app": "t", "version": "1", "platform": "x"}}))
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready"


def test_no_long_lived_token_written_to_disk(tmp_path, monkeypatch):
    # auth/eternitas.py must never persist a passport/token. Point HOME at an empty
    # dir, run the authorizers, and assert nothing was written under it.
    monkeypatch.setenv("HOME", str(tmp_path))
    DevAuthorizer().authorize({"token": "ET26-SECRET-TOKEN"})
    EternitasAuthorizer().authorize({"token": "ET26-SECRET-TOKEN"})
    assert list(tmp_path.rglob("*")) == []  # nothing persisted
