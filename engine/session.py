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
  {"type": "level", "value"}                                 → lip-sync (§5)
  {"type": "tool_call", "call_id", "turn_id", "tool", "args"}→ client runs hands
  {"type": "error", "code", "message", "fatal"}

Audio is paced ~real-time so unsent audio stays cancellable — that is what makes
barge-in able to cut speech that hasn't been played yet.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import struct
import threading
from collections.abc import Awaitable, Callable

from engine.segment import cut_point
from engine.vad import DEFAULT_MIN_SPEECH_MS, DEFAULT_SILENCE_MS, FRAME_MS, Segmenter

Emit = Callable[[dict], Awaitable[None]]

TTS_RATE = 24000
_AUDIO_FRAME_MS = 20
_AUDIO_FRAME_BYTES = TTS_RATE * _AUDIO_FRAME_MS // 1000 * 2  # 960
_BARGE_CONFIRM_MS = 60           # §7.3 voiced to confirm a barge (within the window)
_BARGE_VERDICT_MS = 250          # §7.3 engine must reply within 250 ms
_BARGE_DECAY_MS = 100            # a silence gap this long resets the voiced tally
_MIC_FRAME_BYTES = 16000 * FRAME_MS // 1000 * 2  # 640
_LEVEL_EVERY = 2                 # emit `level` every N audio chunks (~25 Hz)
_FALLBACK_LINE = "Sorry, I'm having trouble reaching my brain right now."


class VoiceSession:
    def __init__(self, stt, tts, brain, emit: Emit, *, session_id: str,
                 system_prompt: str | None = None, tools: list[dict] | None = None,
                 min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
                 silence_ms: int = DEFAULT_SILENCE_MS,
                 pace: bool = True, loop=None, level_events: bool = True) -> None:
        self.stt, self.tts, self.brain, self.emit = stt, tts, brain, emit
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.tools = tools
        self.pace = pace
        self.loop = loop
        self.level_events = level_events
        self.min_speech_ms = min_speech_ms
        self.silence_ms = silence_ms
        self.state = "idle"
        self.turn_id = 0
        self.say_id = 0
        self.mic_on = False
        self._seg = Segmenter(min_speech_ms, silence_ms,
                              is_speech=getattr(stt, "is_speech", None) or _vad_or_none())
        self._turn_task: asyncio.Task | None = None
        self._active_say_id = 0
        self._barge_voiced_ms = 0
        self._barge_unvoiced_run = 0            # consecutive unvoiced frames (decay window)
        self._barge_frames: list[bytes] = []   # frames captured during a barge (carried to new turn)
        self._barge_verdict: asyncio.Task | None = None
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
                self._seg = self._fresh_seg()
                await self._set_state("listening")
        else:
            # §6: mic-off SHOULD let the in-flight turn finish its speech; the
            # turn's `finally` transitions to `paused` (not `listening`) because
            # mic is now off. A pending barge verdict is voided (send neither).
            self._cancel_barge_verdict()
            if self.state in ("thinking", "speaking"):
                pass  # let the turn complete; it will land in `paused`
            else:
                await self._set_state("paused")

    async def on_text(self, message: str) -> None:
        """Dev path: inject a completed user utterance as text (no audio)."""
        message = (message or "").strip()
        if message:
            await self._start_turn(user_text=message)

    async def on_tool_result(self, call_id: str, ok: bool,
                             result: str = "", error: str = "") -> None:
        if not isinstance(call_id, str):
            return
        fut = self._tool_futures.get(call_id)
        if fut and not fut.done():
            fut.set_result({"ok": ok, "result": result, "error": error})

    async def on_barge_in(self, say_id: int | None = None) -> None:
        # §7.3: client-signaled barge → start a ≤250 ms verdict window. Confirm if
        # ≥60 ms voiced arrives; otherwise reply say_resume at the deadline. (The
        # engine's own detector in on_mic_frame can confirm sooner.)
        # §6: a barge adjacent to mic-off gets NO verdict — if the mic is off there
        # is no voiced audio to confirm it, so don't open a window that can only
        # resolve as a false-positive say_resume.
        if self.state != "speaking" or self._barge_verdict is not None or not self.mic_on:
            return
        loop = self.loop or asyncio.get_running_loop()
        self._barge_verdict = loop.create_task(self._barge_verdict_window())

    async def _barge_verdict_window(self) -> None:
        try:
            await asyncio.sleep(_BARGE_VERDICT_MS / 1000.0)
            # deadline reached without the confirm threshold → false positive
            if self.state == "speaking" and self._barge_voiced_ms < _BARGE_CONFIRM_MS:
                await self.emit({"type": "say_resume", "say_id": self._active_say_id})
        except asyncio.CancelledError:
            pass
        finally:
            self._barge_verdict = None

    def _cancel_barge_verdict(self) -> None:
        if self._barge_verdict is not None:
            self._barge_verdict.cancel()
            self._barge_verdict = None

    # -- inbound: audio --------------------------------------------------------

    async def on_mic_frame(self, pcm: bytes) -> None:
        if not self.mic_on:
            return
        if self.state == "listening":
            for utter in self._seg.push(pcm):
                # EOS: leave `listening` synchronously so no second utterance can
                # race in, then run STT+brain in a task (keeps the recv loop free).
                await self._start_turn(utter_pcm=utter)
                return
        elif self.state == "speaking":
            # engine-detected barge-in (§7.5): ≥60 ms cumulative voiced cuts speech.
            fb = _MIC_FRAME_BYTES
            voiced = len(pcm) >= fb and self._seg._is_speech(pcm[:fb], 16000)
            self._barge_frames.append(pcm)          # keep for carrying into the new turn
            if len(self._barge_frames) > 40:
                self._barge_frames.pop(0)
            if voiced:
                self._barge_voiced_ms += FRAME_MS
                self._barge_unvoiced_run = 0
                if self._barge_voiced_ms >= _BARGE_CONFIRM_MS:
                    await self._confirm_barge()
            else:
                # Decay: a silence gap resets the tally so sparse false-voiced frames
                # (imperfect AEC, background noise) spread across a long reply can't
                # accumulate to a spurious barge. §7.3 wants ≥60 ms voiced *within*
                # the decision window, not cumulative over the whole speaking phase.
                self._barge_unvoiced_run += 1
                if self._barge_unvoiced_run * FRAME_MS >= _BARGE_DECAY_MS:
                    self._barge_voiced_ms = 0
        # thinking/idle/paused: frames feed VAD only (dropped, §6/§11.4)

    # -- turn orchestration ----------------------------------------------------

    async def _start_turn(self, *, user_text: str | None = None,
                          utter_pcm: bytes | None = None) -> None:
        """Begin a turn from text or a completed utterance. Cancels any in-flight
        turn first (§11.4: no overlapping turns)."""
        await self._cancel_turn(reason="superseded")
        self.turn_id += 1
        self._active_say_id = 0
        self._barge_voiced_ms = 0
        self._barge_unvoiced_run = 0
        self._barge_frames = []
        # Enter "thinking" internally (so no second utterance races in) but DON'T
        # emit it yet — §6 requires heard{final} to precede state{thinking} on the
        # wire, and on the mic path the transcript isn't known until STT runs inside
        # _run_turn. _run_turn emits `heard` then announces `thinking`.
        self.state = "thinking"
        loop = self.loop or asyncio.get_running_loop()
        self._turn_task = loop.create_task(self._run_turn(user_text, utter_pcm))

    async def _run_turn(self, user_text: str | None, utter_pcm: bytes | None) -> None:
        try:
            if user_text is None and utter_pcm is not None:
                loop = self.loop or asyncio.get_running_loop()
                user_text = await loop.run_in_executor(
                    None, lambda: self.stt.transcribe(utter_pcm).text)
                user_text = (user_text or "").strip()
                if len(user_text) < 2:
                    return  # nothing intelligible; `finally` returns to listening
            await self.emit({"type": "heard", "text": user_text, "final": True,
                             "turn_id": self.turn_id})
            await self._set_state("thinking")  # §6: announced AFTER heard{final}
            self._history.append({"role": "user", "content": user_text})
            reply = await self._stream_and_speak()
            if reply:
                self._history.append({"role": "assistant", "content": reply})
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._speak_fallback()
        finally:
            if self.state in ("thinking", "speaking"):
                await self._set_state("listening" if self.mic_on else "paused")

    async def _stream_and_speak(self) -> str:
        """Drive the brain (with tool rounds), sentence-chunk, speak. Returns the
        full spoken reply text (for history), or '' if nothing was spoken."""
        messages = self._build_messages()
        reply_parts: list[str] = []
        for _round in range(6):  # bounded tool rounds
            buf = ""
            tool_calls = []
            errored = False
            async for ev in self._brain_events(messages):
                if ev.kind == "text":
                    buf += ev.text
                    while (cut := cut_point(buf)) is not None:
                        seg, buf = buf[:cut].strip(), buf[cut:]
                        spoken = await self._speak_segment(seg)
                        if spoken:
                            reply_parts.append(spoken)
                elif ev.kind == "tool_calls":
                    tool_calls = ev.tool_calls
                elif ev.kind == "error":
                    errored = True
            tail = buf.strip()
            if tail:
                spoken = await self._speak_segment(tail)
                if spoken:
                    reply_parts.append(spoken)
            if errored and not reply_parts:
                await self._speak_fallback()
                return _FALLBACK_LINE
            if not tool_calls:
                return " ".join(reply_parts)
            results = await self._run_tools(tool_calls)
            # OpenAI wire shape, not ToolCall.__dict__ — the brain 4xxes on the
            # follow-up call otherwise (arguments must be a JSON string).
            messages = messages + [{"role": "assistant", "content": None,
                                    "tool_calls": [
                                        {"id": tc.id, "type": "function",
                                         "function": {"name": tc.name,
                                                      "arguments": json.dumps(tc.arguments)}}
                                        for tc in tool_calls]}] + results
        return " ".join(reply_parts)

    async def _run_tools(self, tool_calls) -> list[dict]:
        results = []
        for tc in tool_calls:
            fut = (self.loop or asyncio.get_running_loop()).create_future()
            self._tool_futures[tc.id] = fut
            try:
                await self.emit({"type": "tool_call", "call_id": tc.id,
                                 "turn_id": self.turn_id, "tool": tc.name,
                                 "args": tc.arguments})
                try:
                    res = await asyncio.wait_for(fut, timeout=45)
                except TimeoutError:
                    res = {"ok": False, "error": "timeout"}
            finally:
                self._tool_futures.pop(tc.id, None)
            # carry ok so the brain can tell success from failure
            content = res.get("result") if res.get("ok") else f"error: {res.get('error', '')}"
            results.append({"role": "tool", "tool_call_id": tc.id,
                            "content": content or ("ok" if res.get("ok") else "error")})
        return results

    async def _brain_events(self, messages):
        """Async-iterate the (sync, blocking) brain generator via a thread + queue.
        A stop flag lets a cancelled turn tell the pump to stop draining the LLM."""
        loop = self.loop or asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        sentinel = object()
        stop = threading.Event()

        def pump():
            try:
                for ev in self.brain.stream(messages, tools=self.tools):
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception:
                from brains.base import BrainEvent
                loop.call_soon_threadsafe(q.put_nowait, BrainEvent(kind="error",
                                                                   message="brain stream failed"))
            finally:
                loop.call_soon_threadsafe(q.put_nowait, sentinel)

        threading.Thread(target=pump, daemon=True).start()
        try:
            while True:
                ev = await q.get()
                if ev is sentinel:
                    return
                yield ev
        finally:
            stop.set()  # cancellation / early return → stop draining the LLM (no leak)

    async def _speak_segment(self, text: str) -> str:
        """Synthesize + stream one sentence. Returns the sanitized text spoken, or
        '' if the segment was empty after sanitation (§10)."""
        text = _sanitize(text)
        if not text:
            return ""
        loop = self.loop or asyncio.get_running_loop()
        self.say_id += 1
        self._active_say_id = self.say_id
        await self.emit({"type": "say_start", "say_id": self.say_id,
                         "turn_id": self.turn_id, "text": text})
        pcm = await loop.run_in_executor(None, self.tts.synthesize, text)
        if self.state == "thinking":
            await self._set_state("speaking")
            self._barge_voiced_ms = 0
            self._barge_unvoiced_run = 0
        n = 0
        for i in range(0, len(pcm), _AUDIO_FRAME_BYTES):
            chunk = pcm[i:i + _AUDIO_FRAME_BYTES]
            final = i + _AUDIO_FRAME_BYTES >= len(pcm)
            await self.emit({"type": "audio", "say_id": self.say_id,
                             "pcm": chunk, "final": final})
            if self.level_events and n % _LEVEL_EVERY == 0:
                await self.emit({"type": "level", "value": _rms(chunk)})
            n += 1
            if self.pace:
                await asyncio.sleep(_AUDIO_FRAME_MS / 1000.0)  # keep unsent audio cancellable
        await self.emit({"type": "say_end", "say_id": self.say_id})
        return text

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
        self._cancel_barge_verdict()
        carried = list(self._barge_frames)      # §7.3: barging speech starts the new turn
        await self._cancel_turn(reason="barge_in")
        await self._set_state("listening")
        self._seg = self._fresh_seg()
        for f in carried:                        # replay the barge audio into the new turn
            for utter in self._seg.push(f):
                await self._start_turn(utter_pcm=utter)
                return

    async def _cancel_turn(self, reason: str | None) -> None:
        self._cancel_barge_verdict()
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
        self._active_say_id = 0
        self._barge_voiced_ms = 0
        self._barge_unvoiced_run = 0
        self._barge_frames = []

    # -- helpers ---------------------------------------------------------------

    def _fresh_seg(self) -> Segmenter:
        return Segmenter(self.min_speech_ms, self.silence_ms, is_speech=self._seg._is_speech)

    def _build_messages(self) -> list[dict]:
        msgs = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        msgs.extend(self._history[-12:])
        if msgs:
            msgs[0] = {**msgs[0], "session_id": self.session_id}
        return msgs


# ── module helpers ──────────────────────────────────────────────────────────

_MD = re.compile(r"[*_`#>~|]+")
_BANNER = re.compile(r"^\s*\[[^\]]*\]\s*")
_BULLET = re.compile(r"^\s*[-•]\s+", re.MULTILINE)
# emoji + variation selectors (U+FE0F) + ZWJ (U+200D) + skin-tone modifiers, so a
# multi-codepoint emoji leaves no combining-char crumbs behind.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F\U0000200D\U0001F3FB-\U0001F3FF\U00002190-\U000021FF]")


def _sanitize(text: str) -> str:
    """Strip markdown, list bullets, status banners, and emoji before TTS (§10).
    Returns '' if nothing speakable remains (the segment is then skipped)."""
    if not text:
        return ""
    text = _BANNER.sub("", text)
    text = _BULLET.sub("", text)
    text = _MD.sub("", text)
    text = _EMOJI.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    # nothing but punctuation/symbols left → not worth a say_id
    return text if any(c.isalnum() for c in text) else ""


def _rms(pcm16: bytes) -> float:
    """0..1 loudness of a PCM16 chunk, for lip-sync level events."""
    n = len(pcm16) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm16[:n * 2])
    ss = sum(s * s for s in samples) / n
    return min(1.0, math.sqrt(ss) / 32768.0 * 3.0)


def _vad_or_none():
    try:
        from engine.vad import _default_is_speech
        return _default_is_speech()
    except Exception:
        return lambda frame, sr: False
