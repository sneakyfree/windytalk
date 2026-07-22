"""voice-session.v1 websocket server (engine side).

Wraps a VoiceSession per connection with the wire protocol: the 16-byte binary
frame header (§2), hello/ready, JSON control/event messages (§5), and clock-sync
time_ping (§8). Providers (STT/TTS/brain) are injected via a factory so tests
drive fakes and the 5090 runs the real stack.

§9 session resume is NOT implemented yet: every `hello` builds a fresh session
and `ready.resumed` is always false. `session_ttl_s` is advertised but no session
store retains state across a reconnect, and the engine does not send `bye` on
shutdown/supersede. A reconnect after a network blip therefore loses conversation
context (the client survives — §9 permits resumed:false — but the context is gone).
Full resume + supersession is a tracked gap, not a claim this file makes.

Run live:  python -m engine.server --host 0.0.0.0 --port 8788
"""
from __future__ import annotations

import argparse
import asyncio
import json
import struct
import time

import websockets

from auth.eternitas import Authorizer, get_authorizer
from engine.session import VoiceSession

try:
    from telemetry.emit import emit as emit_telemetry
    from telemetry.emit import flush as flush_telemetry
except Exception:  # telemetry pkg absent → no-op (matches emit's inert-unless-configured)
    def emit_telemetry(event_type: str, **fields):  # type: ignore
        pass

    def flush_telemetry(timeout_s: float = 1.0):  # type: ignore
        pass

PROTOCOL = "voice-session.v1"
APP_VERSION = "0.1.0"
_HEADER = struct.Struct("<BBHQI")  # type u8, flags u8, seq u16, ts_ms u64, stream_id u32 = 16
MIC_TYPE = 0x01
TTS_TYPE = 0x02
FLAG_FINAL = 0x01
SESSION_TTL_S = 60
_MIC_FRAME_BYTES = 640  # §3: 20 ms @ 16 kHz PCM16 mono


def _clamp_int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _p90(samples: list[float]) -> float:
    """90th-percentile (nearest-rank) of the collected per-turn latencies."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, round(0.9 * len(ordered) + 0.5) - 1))
    return round(ordered[idx], 1)


def _install_id() -> str:
    """A stable per-install id for telemetry metadata (INTEL-CONTRACT-V2). Persisted
    under ~/.windytalk/ so it survives restarts; content-free (random, no PII)."""
    import os
    from pathlib import Path
    p = Path.home() / ".windytalk" / "install-id"
    try:
        if p.exists():
            return p.read_text().strip()
        p.parent.mkdir(parents=True, exist_ok=True)
        val = "inst-" + os.urandom(8).hex()
        p.write_text(val)
        return val
    except Exception:
        return "inst-unknown"


def _session_metadata(hello: dict) -> dict:
    """Non-content descriptors the ingest requires on session events. Prefer the
    client's hello values; fall back to engine-side info."""
    import sys
    client = hello.get("client") or {}
    return {
        "app_version": str(client.get("version") or APP_VERSION),
        "os": str(client.get("platform") or sys.platform),
        "install_id": str(client.get("install_id") or _install_id()),
    }


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

    def __init__(self, ws, session: VoiceSession, session_id: str = "",
                 model: str = "", actor_id: str = ""):
        self.ws = ws
        self.session = session
        self.session_id = session_id
        self.actor_id = actor_id or session_id
        self.model = model
        self.seq_out = 0
        self.min_rtt = float("inf")
        self.offset = 0.0
        self._t_eos: float | None = None
        self._first_audio_seen = False
        self.eos_to_first_audio_ms: float | None = None
        self._latency_samples: list[float] = []  # per-turn EOS→first-audio, for a real p90
        self.turns = 0
        self._prev_state: str | None = None

    async def emit(self, e: dict) -> None:
        etype = e["type"]
        if etype == "audio":
            if not self._first_audio_seen and self._t_eos is not None:
                self.eos_to_first_audio_ms = (time.perf_counter() - self._t_eos) * 1000
                self._latency_samples.append(self.eos_to_first_audio_ms)
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
        self._telemetry(e)
        e.setdefault("ts", now_ms())  # §5: JSON events carry a session-clock ts
        await self.ws.send(json.dumps(e))

    def _telemetry(self, e: dict) -> None:
        # Content-free (telemetry.v1): ids/counts/latencies only — never text.
        etype = e["type"]
        if etype == "state":
            value = e.get("value")
            if value == "listening" and self._prev_state == "speaking":
                self.turns += 1
                # A genuine running p90 over the session's samples — not the last
                # turn's value mislabeled as p90 (the master plan's "measured, not
                # vibed" release gate depends on this being real).
                lat = ({"eos_to_first_audio_p90": _p90(self._latency_samples)}
                       if self._latency_samples else None)
                emit_telemetry("turn.complete", actor_type="human",
                               actor_id=self.actor_id, session_id=self.session_id, model=self.model or None,
                               latency_ms=lat)
            self._prev_state = value
        elif etype == "tool_call":
            emit_telemetry("tool.invoked", actor_type="agent",
                           actor_id=self.actor_id, session_id=self.session_id, tool=e.get("tool"))
        elif etype == "say_cancel" and e.get("reason") == "barge_in":
            emit_telemetry("say.barge_in", actor_type="human", actor_id=self.actor_id, session_id=self.session_id)


class VoiceServer:
    def __init__(self, make_providers, *, pace: bool = True,
                 system_prompt: str | None = None, tools: list[dict] | None = None,
                 authorizer: Authorizer | None = None):
        self.make_providers = make_providers
        self.pace = pace
        self.system_prompt = system_prompt
        self.tools = tools
        self.authorizer = authorizer or get_authorizer()

    async def handle(self, ws) -> None:
        # 1) hello → ready. §1: anything before a valid hello is silently ignored
        # (binary frames, non-hello JSON, unparseable) — keep waiting, don't kill.
        hello = None
        deadline = 15.0
        while hello is None:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline)
            except TimeoutError:
                return
            except websockets.ConnectionClosed:
                return
            if isinstance(raw, (bytes, bytearray)):
                continue  # binary before hello → ignore (§1)
            try:
                m = json.loads(raw)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                continue
            if isinstance(m, dict) and m.get("type") == "hello":
                hello = m
        proto = hello.get("protocol", "")
        if not _major_ok(proto):
            await ws.send(json.dumps({"type": "error", "code": "version_mismatch",
                                      "message": f"need {PROTOCOL}", "fatal": True}))
            return

        # §10 entitlement gate — deny is a fatal not_entitled (§9). Default
        # DevAuthorizer allows all; WINDYTALK_STRICT_AUTH=1 flips to Eternitas.
        ent = self.authorizer.authorize(hello.get("auth"))
        if not ent.entitled:
            await ws.send(json.dumps({"type": "error", "code": "not_entitled",
                                      "message": ent.reason, "fatal": True}))
            return

        session_id = hello.get("session_id") or f"s-{now_ms()}"
        loop = asyncio.get_running_loop()
        stt, tts, brain = self.make_providers()
        model = getattr(brain, "model", "") or ""
        conn = _Conn(ws, None, session_id=session_id, model=model, actor_id=ent.user_id)
        # §6: honor + clamp the client's requested endpointing (or defaults)
        opts = hello.get("options") if isinstance(hello.get("options"), dict) else {}
        vad = opts.get("vad") if isinstance(opts.get("vad"), dict) else {}
        min_speech = _clamp_int(vad.get("min_speech_ms"), 150, 50, 1000)
        silence = _clamp_int(vad.get("silence_ms"), 700, 200, 2000)
        level_events = opts.get("level_events", True) is not False
        # The brain must know the CLIENT's OS (this engine may be remote): a mac
        # user asked for a new tab gets cmd+t, not ctrl+t.
        client_platform = str((hello.get("client") or {}).get("platform") or "")
        prompt = self.system_prompt
        if prompt and self.tools and client_platform:
            os_name = {"darwin": "macOS", "win32": "Windows", "linux": "Linux"}.get(
                client_platform, client_platform)
            prompt += (f" The user's computer runs {os_name}."
                       + (" Use cmd (not ctrl) keyboard shortcuts."
                          if os_name == "macOS" else ""))
        session = VoiceSession(stt, tts, brain, conn.emit, session_id=session_id,
                               system_prompt=prompt, tools=self.tools,
                               min_speech_ms=min_speech, silence_ms=silence,
                               level_events=level_events, pace=self.pace, loop=loop)
        conn.session = session
        t_session_start = time.perf_counter()
        meta = _session_metadata(hello)
        emit_telemetry("session.start", actor_type="human", actor_id=ent.user_id,
                       session_id=session_id, model=model or None, metadata=meta)

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
                try:
                    await self._route(conn, msg)
                except websockets.ConnectionClosed:
                    raise
                except Exception:
                    # a malformed message must never kill the session (§5)
                    try:
                        await ws.send(json.dumps({"type": "error", "code": "internal",
                                                  "message": "handler error", "fatal": False}))
                    except Exception:
                        pass
        except websockets.ConnectionClosed:
            pass
        finally:
            ping_task.cancel()
            await session._cancel_turn(reason=None)
            emit_telemetry("session.end", actor_type="human", actor_id=ent.user_id,
                           session_id=session_id,
                           dur_ms=int((time.perf_counter() - t_session_start) * 1000),
                           turns=conn.turns, model=model or None,
                           metadata={"install_id": meta["install_id"]})
            flush_telemetry(timeout_s=0.5)

    async def _route(self, conn: _Conn, msg) -> None:
        session = conn.session
        if isinstance(msg, (bytes, bytearray)):
            parsed = parse_frame(bytes(msg))
            if parsed is None:
                return
            ftype, _flags, _seq, ts_ms, sid, payload = parsed
            # §2: mic frames MUST be 640 bytes with stream_id 0; drop malformed
            # (an odd-length payload would permanently misalign the VAD buffer).
            if ftype == MIC_TYPE and len(payload) == _MIC_FRAME_BYTES and sid == 0:
                self._measure_transport(conn, ts_ms)
                await session.on_mic_frame(payload)
            return
        try:
            m = json.loads(msg)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(m, dict):
            return  # §5: non-object JSON is ignored, not fatal
        t = m.get("type")
        if t == "mic":
            await session.on_mic(bool(m.get("on")))
        elif t == "barge_in":
            await session.on_barge_in(m.get("say_id"))
        elif t == "tool_result":
            await session.on_tool_result(m.get("call_id"), bool(m.get("ok")),
                                         m.get("result", ""), m.get("error", ""))
        elif t == "text":
            msgtext = m.get("message", "")
            if isinstance(msgtext, str):
                await session.on_text(msgtext)
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
        t0 = m.get("t0")
        t_client = m.get("t_client")
        if not isinstance(t0, (int, float)) or not isinstance(t_client, (int, float)):
            return  # §5: malformed pong ignored, never crashes
        t_recv = now_ms()
        rtt = t_recv - t0
        if 0 <= rtt < conn.min_rtt:  # ignore negative RTT (clock step) — §8 min-filter
            conn.min_rtt = rtt
            conn.offset = t_client - (t0 + rtt / 2)

    async def serve(self, host: str = "0.0.0.0", port: int = 8788):
        # A non-loopback bind + the permissive DevAuthorizer = anyone who reaches
        # this port gets a full brain session billed to the dev Mind key. Warn
        # loudly; the fix is WINDYTALK_STRICT_AUTH=1 or an access-gated tunnel.
        from auth.eternitas import DevAuthorizer
        if host not in ("127.0.0.1", "localhost", "::1") and isinstance(self.authorizer, DevAuthorizer):
            print(f"[engine] WARNING: bound to {host} with the fail-OPEN dev gate — "
                  "anyone reaching this port gets a brain session on the dev key. "
                  "Set WINDYTALK_STRICT_AUTH=1 or keep the tunnel access-gated.",
                  flush=True)
        # §1: deflate off for binary audio (pure latency/CPU tax); cap frame size
        # so a pre-hello client can't buffer a giant frame (memory DoS).
        return await websockets.serve(self.handle, host, port,
                                      compression=None, max_size=4 * 1024 * 1024)


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
    from engine.tools import hands_tools_enabled, load_hands_tools
    tools = load_hands_tools() if hands_tools_enabled() else None
    prompt = ("You are Windy, a concise, friendly voice assistant. Keep replies "
              "short and natural for speech.")
    if tools:
        prompt += (" You can operate this computer with your tools: open apps and "
                   "URLs, press keys, type, click, scroll, read the screen, and take "
                   "screenshots. When the user asks you to do something on the "
                   "computer, do it with tools instead of saying you can't. Prefer "
                   "click_element and press_keys (keyboard shortcuts) over raw "
                   "mouse_click. Say what you did in one short sentence.")
    server = VoiceServer(real_providers, pace=True, system_prompt=prompt, tools=tools)
    await server.serve(host, port)
    print(f"[engine] voice-session.v1 server on ws://{host}:{port}", flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8788)
    args = ap.parse_args()
    asyncio.run(_amain(args.host, args.port))
