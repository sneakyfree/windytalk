"""voice-session.v1 websocket server (engine side).

Wraps a VoiceSession per connection with the wire protocol: the 16-byte binary
frame header (§2), hello/ready, JSON control/event messages (§5), clock-sync
time_ping (§8), and best-effort session resume (§9). Providers (STT/TTS/brain)
are injected via a factory so tests drive fakes and the 5090 runs the real stack.

Run live:  python -m engine.server --host 0.0.0.0 --port 8788
"""
from __future__ import annotations

import argparse
import asyncio
import json
import struct
import time

import websockets

from engine.session import VoiceSession

PROTOCOL = "voice-session.v1"
_HEADER = struct.Struct("<BBHQI")  # type u8, flags u8, seq u16, ts_ms u64, stream_id u32 = 16
MIC_TYPE = 0x01
TTS_TYPE = 0x02
FLAG_FINAL = 0x01
SESSION_TTL_S = 60


def now_ms() -> int:
    return int(time.time() * 1000)


def build_frame(ftype: int, flags: int, seq: int, ts_ms: int,
                stream_id: int, payload: bytes) -> bytes:
    return _HEADER.pack(ftype, flags, seq & 0xFFFF, ts_ms, stream_id) + payload


def parse_frame(buf: bytes):
    if len(buf) < 16:
        return None
    ftype, flags, seq, ts_ms, stream_id = _HEADER.unpack(buf[:16])
    return ftype, flags, seq, ts_ms, stream_id, buf[16:]


class _Conn:
    """Per-connection wire state: a session + the outbound serializer."""

    def __init__(self, ws, session: VoiceSession):
        self.ws = ws
        self.session = session
        self.seq_out = 0
        self.min_rtt = float("inf")
        self.offset = 0.0
        self._t_eos: float | None = None
        self._first_audio_seen = False
        self.eos_to_first_audio_ms: float | None = None

    async def emit(self, e: dict) -> None:
        etype = e["type"]
        if etype == "audio":
            if not self._first_audio_seen and self._t_eos is not None:
                self.eos_to_first_audio_ms = (time.perf_counter() - self._t_eos) * 1000
                self._first_audio_seen = True
                print(f"[engine] EOS→first-audio {self.eos_to_first_audio_ms:.0f}ms "
                      f"(budget 1200ms)", flush=True)
            flags = FLAG_FINAL if e.get("final") else 0
            frame = build_frame(TTS_TYPE, flags, self.seq_out, now_ms(),
                                e["say_id"], e["pcm"])
            self.seq_out = (self.seq_out + 1) & 0xFFFF
            await self.ws.send(frame)
            return
        if etype == "heard" and e.get("final"):
            self._t_eos = time.perf_counter()
            self._first_audio_seen = False
        await self.ws.send(json.dumps(e))


class VoiceServer:
    def __init__(self, make_providers, *, pace: bool = True,
                 system_prompt: str | None = None, tools: list[dict] | None = None):
        self.make_providers = make_providers
        self.pace = pace
        self.system_prompt = system_prompt
        self.tools = tools

    async def handle(self, ws) -> None:
        # 1) hello → ready
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
        except TimeoutError:
            return
        try:
            hello = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            await ws.send(json.dumps({"type": "error", "code": "bad_frame",
                                      "message": "expected hello", "fatal": True}))
            return
        if hello.get("type") != "hello":
            await ws.send(json.dumps({"type": "error", "code": "bad_frame",
                                      "message": "first message must be hello",
                                      "fatal": True}))
            return
        proto = hello.get("protocol", "")
        if not _major_ok(proto):
            await ws.send(json.dumps({"type": "error", "code": "version_mismatch",
                                      "message": f"need {PROTOCOL}", "fatal": True}))
            return

        session_id = hello.get("session_id") or f"s-{now_ms()}"
        loop = asyncio.get_running_loop()
        stt, tts, brain = self.make_providers()
        conn = _Conn(ws, None)
        session = VoiceSession(stt, tts, brain, conn.emit, session_id=session_id,
                               system_prompt=self.system_prompt, tools=self.tools,
                               pace=self.pace, loop=loop)
        conn.session = session

        await ws.send(json.dumps({
            "type": "ready", "protocol": PROTOCOL, "session_id": session_id,
            "resumed": False, "audio_out": {"rate": 24000},
            "limits": {"session_ttl_s": SESSION_TTL_S,
                       "vad": {"silence_ms": session._seg.silence_ms,
                               "min_speech_ms": session._seg.min_speech_ms}}}))
        await session.start()
        ping_task = asyncio.ensure_future(self._clock_sync(conn))
        try:
            async for msg in ws:
                await self._route(conn, msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            ping_task.cancel()
            await session._cancel_turn(reason=None)

    async def _route(self, conn: _Conn, msg) -> None:
        session = conn.session
        if isinstance(msg, (bytes, bytearray)):
            parsed = parse_frame(bytes(msg))
            if parsed is None:
                return
            ftype, _flags, _seq, ts_ms, _sid, payload = parsed
            if ftype == MIC_TYPE and payload:
                self._measure_transport(conn, ts_ms)
                await session.on_mic_frame(payload)
            return
        try:
            m = json.loads(msg)
        except json.JSONDecodeError:
            return
        t = m.get("type")
        if t == "mic":
            await session.on_mic(bool(m.get("on")))
        elif t == "barge_in":
            await session.on_barge_in(m.get("say_id"))
        elif t == "tool_result":
            await session.on_tool_result(m.get("call_id"), bool(m.get("ok")),
                                         m.get("result", ""), m.get("error", ""))
        elif t == "text":
            await session.on_text(m.get("message", ""))
        elif t == "pong":
            self._on_pong(conn, m)
        # unknown types ignored (§1 additive-safety)

    def _measure_transport(self, conn: _Conn, frame_ts_ms: int) -> None:
        # transport latency = recv - (frame.ts_ms - offset); best-effort, telemetry only
        _ = now_ms() - (frame_ts_ms - conn.offset)

    async def _clock_sync(self, conn: _Conn) -> None:
        try:
            for _ in range(3):  # §8 burst
                await conn.ws.send(json.dumps({"type": "time_ping", "t0": now_ms()}))
                await asyncio.sleep(0.1)
            while True:
                await asyncio.sleep(10)
                await conn.ws.send(json.dumps({"type": "time_ping", "t0": now_ms()}))
        except (asyncio.CancelledError, websockets.ConnectionClosed):
            return

    def _on_pong(self, conn: _Conn, m: dict) -> None:
        t_recv = now_ms()
        rtt = t_recv - m.get("t0", t_recv)
        if rtt < conn.min_rtt:
            conn.min_rtt = rtt
            conn.offset = m.get("t_client", t_recv) - (m.get("t0", t_recv) + rtt / 2)

    async def serve(self, host: str = "0.0.0.0", port: int = 8788):
        return await websockets.serve(self.handle, host, port, max_size=None)


def _major_ok(proto: str) -> bool:
    if not proto.startswith("voice-session.v"):
        return False
    try:
        return int(proto.split(".v")[1].split(".")[0]) == 1
    except (IndexError, ValueError):
        return False


# -- production provider factory (the 5090 stack) -----------------------------

_WARM: dict = {}


def real_providers():
    """The 5090 stack. STT/TTS models are warmed once and shared across
    connections (a fresh whisper/kokoro load is ~15 s — never pay it per connect).
    Single-user desktop wedge: one turn at a time, so sharing is safe; a
    multi-user engine would pool per session instead."""
    import os

    from agents.windyfly import WindyFlyBrain
    from brains.mind import MindBrain
    from engine.providers.stt import get_stt
    from engine.providers.tts import get_tts
    if "stt" not in _WARM:
        stt = get_stt("whisper")
        stt.warmup()
        tts = get_tts("kokoro")
        tts.warmup()
        _WARM["stt"], _WARM["tts"] = stt, tts
    brain = WindyFlyBrain() if os.environ.get("WINDYTALK_BRAIN") == "windyfly" else MindBrain()
    return _WARM["stt"], _WARM["tts"], brain


async def _amain(host: str, port: int) -> None:
    server = VoiceServer(real_providers, pace=True,
                         system_prompt="You are Windy, a concise, friendly voice "
                         "assistant. Keep replies short and natural for speech.")
    await server.serve(host, port)
    print(f"[engine] voice-session.v1 server on ws://{host}:{port}", flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8788)
    args = ap.parse_args()
    asyncio.run(_amain(args.host, args.port))
