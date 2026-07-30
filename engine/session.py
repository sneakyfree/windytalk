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
import os
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
# Why the contract's 60 ms confirm appeared to fail, and why it now works again.
#
# §4.1 requires the CLIENT to send AEC-cleaned mic audio, and §7 rule 3 says confirm
# on ≥60 ms of voiced. But AEC attenuates echo — it does not remove it, and the
# residual is still SPEECH-SHAPED. webrtcvad is a speech/non-speech classifier with
# no loudness opinion, so it calls that residual "voiced" for as long as Windy talks.
# 60 ms of "voiced" therefore arrives instantly and every reply self-cancelled
# (49/58 in the first live session; 8 of 11 cancels in the 07-22 hand test).
#
# The 2026-07-22 response was to wait longer and hope: a 600 ms un-interruptible
# grace plus a 240 ms confirm (4x the contract). That traded one bug for two — it
# put the engine out of spec, and it still only bought 840 ms, less than any real
# reply, so replies kept getting cut at 0.82 s.
#
# The actual missing piece was a LOUDNESS opinion. Voicedness cannot separate "AEC
# residual" from "the user is talking" — level can. So the engine now calibrates the
# echo floor from its own bleed-through (see _echo_floor) and requires barge evidence
# to exceed it. With that discriminator in place the two timing patches are no longer
# load-bearing and are removed: confirm returns to the contract's 60 ms, and the grace
# window shrinks to the span actually needed to measure the floor. Verified against a
# live speaker->mic loopback at 0/5/20/35/60% bleed: no self-barge at any level, and a
# real interruption still cuts her off at every level.
#
# CORRECTION, 2026-07-27, from a real room instead of a synthetic loopback.
# The paragraph above was right about the mechanism and wrong to conclude the timing
# requirement could go entirely. Measured over a live stress session on the Mac mini
# with real speakers (music playing for part of it):
#
#     replies cut by say_cancel(barge_in) .................. 33
#     cuts the CLIENT's detector also signalled ............  9
#     cuts the engine invented with no client signal ....... 24  (73%)
#
# The client's detector runs the same AEC-cleaned audio through a plain energy
# threshold and stayed quiet; the engine, on the same audio, fired anyway. So the
# echo floor alone is not a sufficient discriminator in a real room, where the echo
# level moves (volume changes, music, the user shifting position) while the floor is
# only sampled during the grace window.
#
# The asymmetry decides the tuning: a FALSE barge destroys the conversation (she is
# cut off mid-sentence, repeatedly), a MISSED barge costs one repeat. So the
# autonomous path — a guess about raw audio — now needs more evidence than a
# client-signalled barge, which is the user's own AEC-backed detector declaring
# intent. §7 rule 3's 60 ms is kept for the client path exactly as specified; the
# deviation is confined to rule 5's autonomous path and is recorded here.
_BARGE_GRACE_MS = 300            # start-of-segment window: measures the echo floor,
                                 # and is un-interruptible while it does so
_BARGE_AUTONOMOUS_CONFIRM_MS = 200   # rule 5 only; rule 3 (client-signalled) stays 60


def _env_ms(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ[name]))
    except (KeyError, ValueError):
        return default
_MIC_FRAME_BYTES = 16000 * FRAME_MS // 1000 * 2  # 640
_LEVEL_EVERY = 2                 # emit `level` every N audio chunks (~25 Hz)
_FALLBACK_LINE = "Sorry, I'm having trouble reaching my brain right now."
# Distinct from the line above ON PURPOSE: the brain was reachable and answered, we
# just never produced anything sayable (tool-round limit hit, or an empty reply).
# Saying "trouble reaching my brain" there would be a lie, and silence is worse —
# the user cannot tell a mute turn from a crashed app.
_NO_REPLY_LINE = "Sorry, I didn't get that one finished. Could you say it again?"
# Shortest unintelligible utterance still worth telling the user about. Under this
# it is a cough, a breath, or a chair moving — furniture, not a lost question.
_NOTICE_MIN_UTTERANCE_S = 0.7
# How long a turn may sit silent before she volunteers that she is still on it.
# 18s: comfortably longer than a normal turn (~4-6s here, occasionally 15s with
# tool rounds) so a healthy reply never triggers it, and far short of the 90s brain
# timeout that left Grant staring at a yellow dot with no information.
_WORKING_AFTER_S = 18.0
_STILL_WORKING_LINE = "Still working on that one — hang with me."
# Turns kept on disk per session. Far more than the 12 the brain is given, so a
# resumed conversation still has depth behind the window.
HISTORY_KEEP = 60


# ── conversation persistence (§9 resume) ────────────────────────────────────
# Windy Talk's own docstring called this out: "§9 session resume is NOT implemented
# yet ... the context is gone." That is a P7 hole with teeth — P7 promises grandma
# "can never tell when it refreshed context", and every engine restart wiped the
# conversation. It is also the PREREQUISITE for giving her a restart button at all:
# P8 ranks stability above capability, but a restart that forgets the weekend trades
# one instability for a worse one.
#
# Deliberately dumb (P5): a plain JSON list of {role, content} per session, written
# after each turn, atomically. No schema, no index, no db. The substrate is dumb;
# the model supplies the skill of using it.

def _session_dir():
    from pathlib import Path
    d = Path(os.environ.get("WINDYTALK_SESSION_DIR",
                            Path.home() / ".windytalk" / "sessions"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_file(session_id: str):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "unknown")[:120]
    return _session_dir() / f"{safe}.json"


def load_history(session_id: str, limit: int = HISTORY_KEEP) -> list[dict]:
    """Prior turns for this session, or [] — never raises. A missing/corrupt file
    means a fresh conversation, which is exactly the old behaviour."""
    try:
        raw = json.loads(_session_file(session_id).read_text("utf-8"))
        msgs = raw.get("messages") if isinstance(raw, dict) else raw
        return [m for m in msgs if isinstance(m, dict) and m.get("role")][-limit:]
    except Exception:
        return []


def save_history(session_id: str, history: list[dict], limit: int = HISTORY_KEEP) -> None:
    """Atomic write of the tail. Never raises: losing the transcript must not be
    able to break the turn that produced it."""
    try:
        f = _session_file(session_id)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"session_id": session_id,
                                   "messages": history[-limit:]}), "utf-8")
        os.replace(tmp, f)
    except Exception:
        pass


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
        self._barge_grace_ms = _env_ms("WINDYTALK_BARGE_GRACE_MS", _BARGE_GRACE_MS)
        # AEC-lite: mean mic level measured during the grace window of the CURRENT
        # segment. While we speak and the user does not, whatever the mic hears is
        # our own echo — so this self-calibrates to any speaker/mic/volume setup
        # without the client needing real AEC. Reset per segment.
        self._echo_floor = 0.0
        self._echo_n = 0
        # 2.0 chosen by sweep, not taste: at 2.5 a genuine interruption was swallowed
        # once speaker bleed hit 60% (the floor rose above the user's own voice); at
        # 1.7 the headroom against self-barge gets thin. 2.0 passed the whole matrix —
        # no self-barge at 5/20/35/60% bleed, real interrupt still cuts through at
        # 35% and 60%.
        # 3.5, raised from 2.0 after the 2026-07-27 room test: at 2.0 the engine
        # invented 24 of 33 cuts that the client's AEC-backed detector never agreed
        # with. Echo in a real room moves; the floor is only sampled at segment start.
        self._echo_margin = float(os.environ.get("WINDYTALK_ECHO_MARGIN", "3.5") or 3.5)
        # Back to the contract's §7 rule 3 value. The 240 ms patch it replaces was
        # compensating for the missing loudness discriminator, not for real acoustics.
        self._barge_confirm_ms = _env_ms("WINDYTALK_BARGE_CONFIRM_MS",
                                         _BARGE_AUTONOMOUS_CONFIRM_MS)
        self._speaking_since: float = 0.0       # monotonic start of the current speaking phase
        self._auditioning = False               # transcribing a candidate utterance
        self._fallback_announced = False        # brain-substitution notice, once per session
        # True only while audio frames are actually leaving for this segment. The
        # `speaking` STATE persists across a whole turn — including the long silent
        # stretches while tools run — but there is nothing to barge into when no
        # audio is playing, and the echo floor from a segment that ended 15s ago is
        # meaningless. Measured 2026-07-28: 11 of 18 barge cuts fired while she was
        # completely silent, up to 14.9s after say_end, during tool rounds.
        self._streaming_audio = False
        # A second segmenter that runs while THINKING so a new utterance spoken
        # mid-processing (before Windy speaks) supersedes the in-flight turn
        # instead of vanishing — the "you're two questions behind" fix. Disabled
        # with WINDYTALK_NO_THINK_SUPERSEDE=1.
        self._think_seg = self._fresh_seg()
        self._think_supersede = os.environ.get("WINDYTALK_NO_THINK_SUPERSEDE") != "1"
        # 25s, raised from 8s after the 2026-07-29 hand test, where Grant asked seven
        # questions over four and a half minutes and heard NOTHING back. The engine
        # was answering — it had degraded to ~13s per turn (4.5s on a fresh process)
        # — but 8s is SHORTER than a normal turn, so every reply became supersede-able
        # before it could finish. He reasonably re-asked at ~20s intervals, each new
        # question killed the in-flight answer, and the loop never converged: seven
        # turns went `-> thinking` and not one produced a word.
        #
        # This threshold must sit ABOVE a realistic turn, not below it, or slowness
        # silently becomes total silence. A turn that has emitted nothing for 25s is
        # genuinely stuck; one at 10s is just thinking.
        self._supersede_stuck_ms = _env_ms("WINDYTALK_SUPERSEDE_STUCK_MS", 25000)
        self._turn_started_at: float = 0.0
        self._turn_produced = False             # has this turn emitted a tool_call or speech?
        # Resume the conversation if this session_id has one on disk (§9). P7:
        # she picks up mid-thought instead of greeting a stranger she spoke to
        # ten minutes ago.
        self._history: list[dict] = load_history(session_id)
        self.resumed = bool(self._history)
        self._partial_reply: list[str] = []
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
            if self._auditioning:
                return      # already transcribing a candidate; don't stack a second
            for utter in self._seg.push(pcm):
                # EOS: audition the utterance (STT) before it is allowed to
                # supersede anything, then run the brain in a task.
                await self._start_turn(utter_pcm=utter)
                return
        elif self.state == "speaking":
            # engine-detected barge-in (§7.5): sustained voiced cuts speech.
            fb = _MIC_FRAME_BYTES
            voiced = len(pcm) >= fb and self._seg._is_speech(pcm[:fb], 16000)
            level = _rms_raw(pcm[:fb]) if len(pcm) >= fb else 0.0
            self._barge_frames.append(pcm)          # keep for carrying into the new turn
            if len(self._barge_frames) > 40:
                self._barge_frames.pop(0)
            # A client-SIGNALED barge (verdict window open) = the user's own onset
            # detector fired = explicit intent → honor it at the contract's 60 ms
            # and skip the grace. The AUTONOMOUS path (no client signal) is the one
            # that echo/tail-speech trips, so it gets the start-of-speech grace and
            # the higher sustained threshold — the fix for "only the first word came
            # out" without weakening a real interrupt.
            client_signaled = self._barge_verdict is not None
            # Nothing is playing → nothing to barge into. The `speaking` STATE spans
            # the whole turn, including the long silent stretches while tools run, and
            # the autonomous detector stayed armed through all of it against an echo
            # floor measured for a segment that had already ended. Live 2026-07-28:
            # 11 of 18 barge cuts fired while she was SILENT, up to 14.9s after
            # say_end. A client-signalled barge still counts here — the user's own
            # detector heard real speech, and redirecting mid-tool-round is legitimate.
            if not self._streaming_audio and not client_signaled:
                return
            loop = self.loop or asyncio.get_running_loop()
            in_grace = (loop.time() - self._speaking_since) * 1000 < self._barge_grace_ms
            if in_grace and not client_signaled:
                # Calibrate the echo floor: during the grace window WE are the only
                # sound source, so the mic level here IS our own bleed-through.
                self._echo_n += 1
                self._echo_floor += (level - self._echo_floor) / self._echo_n
                return
            # webrtcvad calls our OWN speech "voiced" — measured live, even 5%
            # speaker bleed produced a 100% false-barge rate and cut every reply at
            # ~840 ms (grace + confirm). Voicedness alone therefore cannot mean
            # "the user is talking"; it must also be meaningfully LOUDER than the
            # echo we just calibrated. A client-signaled barge still bypasses this,
            # because that is the user's own onset detector declaring intent.
            above_echo = level > self._echo_floor * self._echo_margin
            if voiced and (client_signaled or above_echo):
                self._barge_voiced_ms += FRAME_MS
                self._barge_unvoiced_run = 0
                # §7 rule 3's 60 ms applies to the CLIENT-signalled path exactly as
                # written — that is the user's own AEC-backed detector declaring
                # intent, and it earned its speed. The autonomous path (rule 5) is a
                # guess about raw audio and needs more, because in a real room it was
                # wrong 73% of the time at the contract's number.
                threshold = _BARGE_CONFIRM_MS if client_signaled else self._barge_confirm_ms
                if self._barge_voiced_ms >= threshold:
                    await self._confirm_barge()
            else:
                # Decay: a silence gap resets the tally so sparse false-voiced frames
                # (imperfect AEC, background noise) spread across a long reply can't
                # accumulate to a spurious barge — the confirm needs SUSTAINED voiced.
                self._barge_unvoiced_run += 1
                if self._barge_unvoiced_run * FRAME_MS >= _BARGE_DECAY_MS:
                    self._barge_voiced_ms = 0
        elif self.state == "thinking" and self._think_supersede:
            # A new utterance during thinking supersedes the in-flight turn — BUT
            # only a turn that is genuinely STUCK (no tool_call or speech emitted
            # yet, and thinking for >_supersede_stuck_ms). On slow CPU a reply can
            # take several seconds; killing every such turn the moment the user
            # says "you there?" produced a silence spiral where 15/23 utterances
            # got no answer at all (2026-07-22 round 3). A turn that is actively
            # producing output is protected — the nudge is dropped and the turn
            # completes, so the user actually hears the answer. Only a truly hung
            # turn gets replaced. WINDYTALK_SUPERSEDE_STUCK_MS tunes the floor.
            loop = self.loop or asyncio.get_running_loop()
            stuck = (not self._turn_produced
                     and (loop.time() - self._turn_started_at) * 1000 >= self._supersede_stuck_ms)
            if not stuck:
                return  # protect the working turn; drop the interjecting frames
            for utter in self._think_seg.push(pcm):
                self._think_seg = self._fresh_seg()
                await self._start_turn(utter_pcm=utter)
                return
        # idle/paused: frames feed VAD only (dropped, §6/§11.4)

    # -- turn orchestration ----------------------------------------------------

    async def _start_turn(self, *, user_text: str | None = None,
                          utter_pcm: bytes | None = None) -> None:
        """Begin a turn from text or a completed utterance. Cancels any in-flight
        turn first (§11.4: no overlapping turns) — but only once we know the new
        utterance is REAL.

        Audition before supersede. Previously this cancelled the in-flight reply
        immediately, and only afterwards did _run_turn transcribe and bail with a
        bare `return` on an unintelligible result. So any speaker echo, cough, or
        room noise that tripped end-of-speech killed the answer in progress and
        then evaporated silently — the user got nothing, asked again, got nothing,
        and eventually a later turn answered, which reads exactly as "it's three
        questions behind". Measured in the 2026-07-26 hand test: 53 turns started,
        29 of them (55%) never produced a transcript, and 17 completed replies were
        cancelled `superseded` by those phantoms.
        """
        if utter_pcm is not None and user_text is None:
            loop = self.loop or asyncio.get_running_loop()
            self._auditioning = True          # don't let a second utterance race in
            try:
                heard = await loop.run_in_executor(
                    None, lambda: self.stt.transcribe(utter_pcm).text)
            finally:
                self._auditioning = False
            heard = (heard or "").strip()
            if len(heard) < 2:
                # Tell the user only if that could plausibly have been a QUESTION.
                # An utterance opens on 150 ms of voiced audio, which a cough, a
                # breath or a chair squeak clears easily — and announcing "didn't
                # catch that" at every one of those makes the mic feel hair-trigger
                # (61 notices in a 28-minute session, most of them furniture). Below
                # this, it was noise, not a lost question: drop it in silence exactly
                # as the engine always did. Above it, somebody said something real
                # and losing it wordlessly is the bug this notice exists to fix.
                secs = len(utter_pcm) / 2 / 16000
                if secs >= _NOTICE_MIN_UTTERANCE_S:
                    await self.emit({"type": "error", "code": "not_understood",
                                     "message": "didn't catch that",
                                     "fatal": False})
                return      # phantom: leave the in-flight turn completely untouched
            user_text, utter_pcm = heard, None
        await self._cancel_turn(reason="superseded")
        self.turn_id += 1
        self._active_say_id = 0
        self._barge_voiced_ms = 0
        self._barge_unvoiced_run = 0
        self._barge_frames = []
        self._think_seg = self._fresh_seg()
        self._turn_started_at = (self.loop or asyncio.get_running_loop()).time()
        self._turn_produced = False
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
            self._partial_reply = []
            # Say something if the thinking runs long. 2026-07-29: a turn sat silent
            # for 90s (the brain timeout) and Grant asked out loud "you've been
            # yellow for quite a while, are you still working on it?" — the face
            # showed thinking, correctly, but a yellow dot cannot say whether it is
            # 5 seconds from an answer or dead. The doctrine calls this out as
            # proactive honesty and explicitly wants it (P7).
            waiter = (self.loop or asyncio.get_running_loop()).create_task(
                self._say_still_working())
            try:
                reply = await self._stream_and_speak()
            finally:
                waiter.cancel()
            if reply:
                self._history.append({"role": "assistant", "content": reply})
                save_history(self.session_id, self._history)
            else:
                # A turn that produces nothing sayable must still SAY so. Otherwise
                # the engine slides back to listening in silence and the user cannot
                # distinguish "thinking", "gave up", and "crashed" — and since the
                # next turn does answer, the conversation slips a question out of
                # phase. Goes into history too, so the brain knows it went quiet.
                await self._speak_fallback(_NO_REPLY_LINE)
                self._history.append({"role": "assistant", "content": _NO_REPLY_LINE})
                save_history(self.session_id, self._history)
            # Whatever the turn produced, if a DIFFERENT model served it, say so.
            await self._announce_brain_substitution()
        except asyncio.CancelledError:
            # A cancelled (barged/superseded) reply must still enter history —
            # otherwise the brain has no memory it ever answered and confabulates
            # ("I'm a text-only assistant") when the user asks why it went quiet.
            partial = " ".join(self._partial_reply).strip()
            if partial:
                self._history.append({"role": "assistant", "content":
                                      partial + " [interrupted by the user before finishing]"})
                save_history(self.session_id, self._history)
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
        reply_parts = self._partial_reply  # shared: a cancelled turn still records what it spoke
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
                # Tell the client, not just the user's ears. Brain failure was
                # previously swallowed entirely into the spoken fallback line, so the
                # client had NO brain-health signal at all — which is why the Brain
                # lamp had to be faked off the engine's own websocket state (#75).
                # Non-fatal: the session is fine, this one turn's brain was not.
                await self.emit({"type": "error", "code": "brain_unreachable",
                                 "message": "the brain could not be reached for this turn",
                                 "fatal": False})
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
            self._turn_produced = True   # doing real work → protect from supersede
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
                try:
                    loop.call_soon_threadsafe(q.put_nowait, sentinel)
                except RuntimeError:
                    pass  # loop already closed (turn cancelled + session gone)

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
        self._turn_produced = True   # speaking = producing output → protect from supersede
        await self.emit({"type": "say_start", "say_id": self.say_id,
                         "turn_id": self.turn_id, "text": text})
        pcm = await loop.run_in_executor(None, self.tts.synthesize, text)
        if self.state == "thinking":
            await self._set_state("speaking")
        # EVERY segment is its own speaking phase (§7.3), not just the first. This
        # reset used to live inside the `thinking` branch above, which is true only
        # for sentence 1 of a reply — so sentences 2+ ran with a long-expired grace
        # window AND the voiced tally that echo accumulated while sentence 1 played,
        # and self-barged almost immediately. Live evidence (07-22): 8 of 11 cancelled
        # segments were reason=barge_in, nearly all the last sentence of a reply.
        self._barge_voiced_ms = 0
        self._barge_unvoiced_run = 0
        self._speaking_since = loop.time()
        self._echo_floor = 0.0
        self._echo_n = 0
        self._streaming_audio = True
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
        self._streaming_audio = False
        await self.emit({"type": "say_end", "say_id": self.say_id})
        return text

    async def _announce_brain_substitution(self) -> None:
        """Say it out loud when Mind served a DIFFERENT model than we asked for.

        Mind is a broker; when a provider is down it falls back silently and still
        reports the requested model at the top level of the response. On 2026-07-30
        every external provider was erroring and it served qwen2.5:7b-instruct for a
        whole hand-test session while Grant believed he was testing Opus. He only
        caught it because the model volunteered that its training cutoff was early
        2025. Nothing in the voice path said a word.

        Once per session, not per turn — this is news the first time and nagging
        after that. Never raises: a courtesy notice must not break the turn.
        """
        try:
            if self._fallback_announced:
                return
            got = (getattr(self.brain, "last_model", "") or "").strip()
            want = (getattr(self.brain, "model", "") or "").strip()
            if not got or not want or got == want:
                return
            self._fallback_announced = True
            await self.emit({"type": "error", "code": "brain_fallback",
                             "message": f"running on {got}, not {want}",
                             "fatal": False})
            await self._speak_fallback(
                f"Heads up — I'm running on a backup brain right now, {got}, "
                f"not {want}. I'll be less sharp than usual.")
        except Exception:
            pass

    async def _say_still_working(self) -> None:
        """After _WORKING_AFTER_S of a silent turn, say so once. Cancelled the moment
        real output appears, so a normal-speed reply never hears from this at all."""
        try:
            await asyncio.sleep(_WORKING_AFTER_S)
            if self._turn_produced or self.state != "thinking":
                return                      # already talking or already tool-calling
            await self._speak_fallback(_STILL_WORKING_LINE)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass    # a courtesy line must never be able to break the turn

    async def _speak_fallback(self, line: str = _FALLBACK_LINE) -> None:
        loop = self.loop or asyncio.get_running_loop()
        try:
            self.say_id += 1
            self._active_say_id = self.say_id
            await self.emit({"type": "say_start", "say_id": self.say_id,
                             "turn_id": self.turn_id, "text": line})
            if self.state == "thinking":
                await self._set_state("speaking")
            pcm = await loop.run_in_executor(None, self.tts.synthesize, line)
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


def _rms_raw(pcm16: bytes) -> float:
    """Unclamped RMS, 0..1 of full scale. The barge gate compares level against a
    MULTIPLE of the echo floor, and a ratio test cannot be built on a saturating
    value: _rms() below clamps at 1.0, so once the floor exceeded 1/margin no
    signal could ever clear the bar and the autonomous path silently disabled
    itself. Lip-sync still wants the clamped, boosted version; the guard wants
    this one."""
    n = len(pcm16) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm16[:n * 2])
    return math.sqrt(sum(s * s for s in samples) / n) / 32768.0


def _rms(pcm16: bytes) -> float:
    """0..1 loudness of a PCM16 chunk, for lip-sync level events."""
    return min(1.0, _rms_raw(pcm16) * 3.0)


def _vad_or_none():
    try:
        from engine.vad import _default_is_speech
        return _default_is_speech()
    except Exception:
        return lambda frame, sr: False
