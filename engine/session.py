"""The voice turn loop (voice-session.v1, engine side).

One VoiceSession owns one conversation's state machine (§6) and drives the
pipeline: mic frames → VAD endpointing → STT → brain (streaming) → §10
sentence-chunked TTS → speech, with barge-in (§7). It is transport-agnostic —
engine/server.py feeds it decoded frames/messages and serializes the logical
events it emits onto the wire. Blocking provider calls run in a thread executor
so the event loop never stalls.

Emitted logical events (server maps to the wire):
  {"type": "state", "value", "turn_id"}                      → §6
  {"type": "heard", "text", "final", "turn_id"}              → STT result
  {"type": "say_start"/"say_end"/"say_cancel"/"say_resume"}  → §10/§7
  {"type": "audio", "say_id", "pcm": bytes, "final"}         → server frames as 0x02
  {"type": "level", "value"}                                 → lip-sync
  {"type": "tool_call", "call_id", "turn_id", "tool", "args"}→ client runs hands
  {"type": "error", "code", "message", "fatal"}

Audio is paced ~real-time so unsent audio stays cancellable — that is what makes
barge-in able to cut speech that hasn't been played yet.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable

from engine.segment import cut_point
from engine.vad import DEFAULT_MIN_SPEECH_MS, DEFAULT_SILENCE_MS, FRAME_MS, Segmenter

Emit = Callable[[dict], Awaitable[None]]

TTS_RATE = 24000
_AUDIO_FRAME_MS = 20
_AUDIO_FRAME_BYTES = TTS_RATE * _AUDIO_FRAME_MS // 1000 * 2  # 960
_BARGE_CONFIRM_MS = 60          # §7.3 cumulative voiced to confirm a barge
_FALLBACK_LINE = "Sorry, I'm having trouble reaching my brain right now."


class VoiceSession:
    def __init__(self, stt, tts, brain, emit: Emit, *, session_id: str,
                 system_prompt: str | None = None, tools: list[dict] | None = None,
                 min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
                 silence_ms: int = DEFAULT_SILENCE_MS,
                 pace: bool = True, loop=None) -> None:
        self.stt, self.tts, self.brain, self.emit = stt, tts, brain, emit
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.tools = tools
        self.pace = pace
        self.loop = loop
        self.state = "idle"
        self.turn_id = 0
        self.say_id = 0
        self.mic_on = False
        self._seg = Segmenter(min_speech_ms, silence_ms,
                              is_speech=getattr(stt, "is_speech", None) or _vad_or_none())
        self._turn_task: asyncio.Task | None = None
        self._active_say_id = 0
        self._barge_voiced_ms = 0
        self._history: list[dict] = []
        self._tool_futures: dict[str, asyncio.Future] = {}

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        await self._set_state("idle")

    async def _set_state(self, value: str) -> None:
        self.state = value
        await self.emit({"type": "state", "value": value, "turn_id": self.turn_id})

    # -- inbound: control ------------------------------------------------------

    async def on_mic(self, on: bool) -> None:
        if on == self.mic_on:
            return
        self.mic_on = on
        if on:
            if self.state in ("idle", "paused"):
                self._seg = _fresh_like(self._seg)
                await self._set_state("listening")
        else:
            await self._cancel_turn(reason=None)  # mic-off voids a pending turn quietly
            await self._set_state("paused")

    async def on_text(self, message: str) -> None:
        """Dev path: inject a completed user utterance as text (no audio)."""
        await self._begin_turn(message)

    async def on_tool_result(self, call_id: str, ok: bool,
                             result: str = "", error: str = "") -> None:
        fut = self._tool_futures.get(call_id)
        if fut and not fut.done():
            fut.set_result({"ok": ok, "result": result, "error": error})

    async def on_barge_in(self, say_id: int | None = None) -> None:
        # Client-signaled barge while speaking → confirm immediately (the engine's
        # own detector, below, is the authority; a client signal just accelerates).
        if self.state == "speaking":
            await self._confirm_barge()

    # -- inbound: audio --------------------------------------------------------

    async def on_mic_frame(self, pcm: bytes) -> None:
        if not self.mic_on:
            return
        if self.state == "listening":
            for utter in self._seg.push(pcm):
                await self._begin_turn_from_audio(utter)
                return
        elif self.state == "speaking":
            # engine-detected barge-in (§7.5): ≥60 ms cumulative voiced cuts speech
            fb = _frame_bytes()
            voiced = len(pcm) >= fb and self._seg._is_speech(pcm[:fb], 16000)
            if voiced:
                self._barge_voiced_ms += FRAME_MS
                if self._barge_voiced_ms >= _BARGE_CONFIRM_MS:
                    await self._confirm_barge()
            else:
                self._barge_voiced_ms = 0
        # thinking/idle/paused: frames feed VAD only (dropped, §6/§11.4)

    # -- turn orchestration ----------------------------------------------------

    async def _begin_turn_from_audio(self, utter_pcm: bytes) -> None:
        loop = self.loop or asyncio.get_running_loop()
        text = await loop.run_in_executor(None, lambda: self.stt.transcribe(utter_pcm).text)
        text = (text or "").strip()
        if len(text) < 2:
            return
        await self._begin_turn(text)

    async def _begin_turn(self, user_text: str) -> None:
        self.turn_id += 1
        await self.emit({"type": "heard", "text": user_text, "final": True,
                         "turn_id": self.turn_id})
        await self._set_state("thinking")
        self._active_say_id = 0
        self._barge_voiced_ms = 0
        self._turn_task = asyncio.ensure_future(self._run_turn(user_text))

    async def _run_turn(self, user_text: str) -> None:
        try:
            self._history.append({"role": "user", "content": user_text})
            spoke = await self._stream_and_speak()
            if not spoke and self.state == "thinking":
                # brain produced nothing sayable — return to listening cleanly
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._speak_fallback()
        finally:
            if self.state in ("thinking", "speaking"):
                await self._set_state("listening")

    async def _stream_and_speak(self) -> bool:
        """Drive the brain (with tool rounds), sentence-chunk, speak. Returns True
        if anything was spoken."""
        messages = self._build_messages()
        spoke = False
        for _round in range(6):  # bounded tool rounds
            buf = ""
            tool_calls = []
            errored = False
            async for ev in self._brain_events(messages):
                if ev.kind == "text":
                    buf += ev.text
                    while (cut := cut_point(buf)) is not None:
                        seg, buf = buf[:cut].strip(), buf[cut:]
                        if seg:
                            await self._speak_segment(seg)
                            spoke = True
                elif ev.kind == "tool_calls":
                    tool_calls = ev.tool_calls
                elif ev.kind == "error":
                    errored = True
            tail = buf.strip()
            if tail:
                await self._speak_segment(tail)
                spoke = True
            if errored and not spoke:
                await self._speak_fallback()
                return True
            if not tool_calls:
                return spoke
            # tool round: emit tool_call events, await results from the client
            results = await self._run_tools(tool_calls)
            messages = messages + [{"role": "assistant", "tool_calls":
                                    [tc.__dict__ for tc in tool_calls]}] + results
        return spoke

    async def _run_tools(self, tool_calls) -> list[dict]:
        results = []
        for tc in tool_calls:
            fut = (self.loop or asyncio.get_running_loop()).create_future()
            self._tool_futures[tc.id] = fut
            await self.emit({"type": "tool_call", "call_id": tc.id,
                             "turn_id": self.turn_id, "tool": tc.name,
                             "args": tc.arguments})
            try:
                res = await asyncio.wait_for(fut, timeout=45)
            except TimeoutError:
                res = {"ok": False, "error": "timeout"}
            finally:
                self._tool_futures.pop(tc.id, None)
            results.append({"role": "tool", "tool_call_id": tc.id,
                            "content": res.get("result") or res.get("error", "")})
        return results

    async def _brain_events(self, messages):
        """Async-iterate the (sync, blocking) brain generator via a thread + queue."""
        loop = self.loop or asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        sentinel = object()

        def pump():
            try:
                for ev in self.brain.stream(messages, tools=self.tools):
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, sentinel)

        threading.Thread(target=pump, daemon=True).start()
        while True:
            ev = await q.get()
            if ev is sentinel:
                return
            yield ev

    async def _speak_segment(self, text: str) -> None:
        loop = self.loop or asyncio.get_running_loop()
        self.say_id += 1
        self._active_say_id = self.say_id
        await self.emit({"type": "say_start", "say_id": self.say_id,
                         "turn_id": self.turn_id, "text": text})
        pcm = await loop.run_in_executor(None, self.tts.synthesize, text)
        if self.state == "thinking":
            await self._set_state("speaking")
            self._barge_voiced_ms = 0
        for i in range(0, len(pcm), _AUDIO_FRAME_BYTES):
            chunk = pcm[i:i + _AUDIO_FRAME_BYTES]
            final = i + _AUDIO_FRAME_BYTES >= len(pcm)
            await self.emit({"type": "audio", "say_id": self.say_id,
                             "pcm": chunk, "final": final})
            if self.pace:
                await asyncio.sleep(_AUDIO_FRAME_MS / 1000.0)  # keep unsent audio cancellable
        await self.emit({"type": "say_end", "say_id": self.say_id})

    async def _speak_fallback(self) -> None:
        loop = self.loop or asyncio.get_running_loop()
        try:
            self.say_id += 1
            self._active_say_id = self.say_id
            await self.emit({"type": "say_start", "say_id": self.say_id,
                             "turn_id": self.turn_id, "text": _FALLBACK_LINE})
            if self.state == "thinking":
                await self._set_state("speaking")
            pcm = await loop.run_in_executor(None, self.tts.synthesize, _FALLBACK_LINE)
            await self.emit({"type": "audio", "say_id": self.say_id, "pcm": pcm,
                             "final": True})
            await self.emit({"type": "say_end", "say_id": self.say_id})
        except Exception:
            pass  # even the fallback failed — stay silent, never crash the loop

    # -- barge-in --------------------------------------------------------------

    async def _confirm_barge(self) -> None:
        await self._cancel_turn(reason="barge_in")
        await self._set_state("listening")
        self._seg = _fresh_like(self._seg)

    async def _cancel_turn(self, reason: str | None) -> None:
        task = self._turn_task
        self._turn_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if reason and self._active_say_id:
            await self.emit({"type": "say_cancel", "say_id": self._active_say_id,
                             "reason": reason})
        self._barge_voiced_ms = 0

    # -- helpers ---------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        msgs = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        msgs.extend(self._history[-12:])
        if msgs:
            msgs[0] = {**msgs[0], "session_id": self.session_id}
        return msgs


def _frame_bytes() -> int:
    return 16000 * FRAME_MS // 1000 * 2  # 640, mic frame


def _vad_or_none():
    try:
        from engine.vad import _default_is_speech
        return _default_is_speech()
    except Exception:
        return lambda frame, sr: False


def _fresh_like(seg: Segmenter) -> Segmenter:
    return Segmenter(seg.min_speech_ms, seg.silence_ms, is_speech=seg._is_speech)
