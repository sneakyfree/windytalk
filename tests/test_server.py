"""Task 1.5b tests for engine/server.py — the voice-session.v1 wire protocol,
exercised by a real websocket client against the server with fake providers."""
import asyncio
import json

import pytest
import websockets

from brains.base import BrainEvent
from engine.server import FLAG_FINAL, MIC_TYPE, TTS_TYPE, VoiceServer, build_frame, parse_frame
from engine.vad import FRAME_BYTES


class FakeSTT:
    def is_speech(self, frame, sr): return frame[:2] != b"\x00\x00"

    def transcribe(self, pcm16, sample_rate=16000):
        from engine.providers.stt.base import Transcript
        return Transcript(text="open the calculator")


class FakeTTS:
    output_rate = 24000

    def synthesize(self, text): return b"\x01\x02" * (len(text) * 8)


class FakeBrain:
    def stream(self, messages, tools=None, model=None):
        yield BrainEvent(kind="text", text="Opening the calculator now. It is ready to use.")
        yield BrainEvent(kind="done", finish_reason="stop")


def providers(): return FakeSTT(), FakeTTS(), FakeBrain()


@pytest.fixture
async def endpoint():
    srv = VoiceServer(providers, pace=False)
    ws_server = await srv.serve("127.0.0.1", 0)
    port = ws_server.sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}"
    ws_server.close()
    await ws_server.wait_closed()


def _voiced(): return b"\x10\x10" * (FRAME_BYTES // 2)
def _silent(): return b"\x00\x00" * (FRAME_BYTES // 2)


async def _collect(ws, stop, timeout=5.0):
    """Read until stop(events) is true; return (json_events, audio_frames)."""
    events, audio = [], []
    async def run():
        while True:
            msg = await ws.recv()
            if isinstance(msg, (bytes, bytearray)):
                ftype, flags, s, ts, sid, payload = parse_frame(bytes(msg))
                audio.append((ftype, sid, bool(flags & FLAG_FINAL), len(payload)))
            else:
                e = json.loads(msg)
                events.append(e)
                if e.get("type") == "time_ping":
                    await ws.send(json.dumps({"type": "pong", "t0": e["t0"], "t_client": 0}))
                if stop(events):
                    return
    await asyncio.wait_for(run(), timeout)
    return events, audio


def _turn_ended(events):
    """True once state has gone speaking → listening (the turn finished speaking)."""
    seen_speaking = False
    for e in events:
        if e.get("type") == "state" and e.get("value") == "speaking":
            seen_speaking = True
        elif seen_speaking and e.get("type") == "state" and e.get("value") == "listening":
            return True
    return False


async def test_full_turn_over_websocket(endpoint):
    async with websockets.connect(endpoint) as ws:
        await ws.send(json.dumps({"type": "hello", "protocol": "voice-session.v1",
                                  "client": {"app": "test", "version": "1", "platform": "linux"}}))
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready"
        assert ready["audio_out"]["rate"] == 24000
        assert ready["limits"]["vad"]["silence_ms"] == 700

        await ws.send(json.dumps({"type": "mic", "on": True, "ts": 0}))
        seq = 0
        for pcm in [_voiced()] * 10 + [_silent()] * 36:
            await ws.send(build_frame(MIC_TYPE, 0, seq, 0, 0, pcm))
            seq = (seq + 1) & 0xFFFF

        events, audio = await _collect(ws, stop=_turn_ended)
        etypes = [e["type"] for e in events]
        assert "heard" in etypes
        states = [e["value"] for e in events if e["type"] == "state"]
        assert "thinking" in states and "speaking" in states
        # two sentences → two say_starts
        starts = [e for e in events if e["type"] == "say_start"]
        assert [e["say_id"] for e in starts] == [1, 2]
        # real audio came back as binary TTS frames, last one final
        assert audio and all(a[0] == TTS_TYPE for a in audio)
        assert any(a[2] for a in audio)  # a final-flagged frame exists


async def test_version_mismatch_is_fatal(endpoint):
    async with websockets.connect(endpoint) as ws:
        await ws.send(json.dumps({"type": "hello", "protocol": "voice-session.v9",
                                  "client": {"app": "t", "version": "1", "platform": "x"}}))
        err = json.loads(await ws.recv())
        assert err["type"] == "error" and err["code"] == "version_mismatch" and err["fatal"]


async def test_first_message_must_be_hello(endpoint):
    async with websockets.connect(endpoint) as ws:
        await ws.send(json.dumps({"type": "mic", "on": True}))
        err = json.loads(await ws.recv())
        assert err["type"] == "error" and err["fatal"]


def test_frame_roundtrip():
    f = build_frame(TTS_TYPE, FLAG_FINAL, 7, 123456789, 42, b"\xaa\xbb")
    ftype, flags, seq, ts, sid, payload = parse_frame(f)
    assert (ftype, flags & FLAG_FINAL, seq, ts, sid, payload) == \
        (TTS_TYPE, FLAG_FINAL, 7, 123456789, 42, b"\xaa\xbb")
