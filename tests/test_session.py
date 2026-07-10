"""Task 1.5a tests for engine/session.py — the turn-loop state machine, driven by
fake providers and synthetic mic frames (no audio hardware, no models)."""
import asyncio

import pytest

from brains.base import BrainEvent, ToolCall
from engine.session import VoiceSession
from engine.vad import FRAME_BYTES


def _voiced(): return b"\x10\x10" * (FRAME_BYTES // 2)
def _silent(): return b"\x00\x00" * (FRAME_BYTES // 2)


class FakeSTT:
    def __init__(self, text="open the calculator"):
        self.text = text

    def is_speech(self, frame, sr):
        return frame[:2] != b"\x00\x00"

    def transcribe(self, pcm16, sample_rate=16000):
        from engine.providers.stt.base import Transcript
        return Transcript(text=self.text)


class FakeTTS:
    output_rate = 24000

    def synthesize(self, text):
        return b"\x01\x02" * (len(text) * 8)  # deterministic non-empty pcm


class FakeBrain:
    """Yields scripted rounds. Each round is a list of BrainEvents (minus done)."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    def stream(self, messages, tools=None, model=None):
        evs = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        yield from evs
        yield BrainEvent(kind="done", finish_reason="stop")


def make_session(brain, stt=None, **kw):
    events = []

    async def emit(e):
        events.append(e)
    s = VoiceSession(stt or FakeSTT(), FakeTTS(), brain, emit,
                     session_id="t", pace=False, **kw)
    s._events = events
    return s


async def _drive_utterance(s):
    """Feed enough voiced then silent frames to trigger one EOS, then await the turn."""
    await s.on_mic(True)
    for _ in range(10):
        await s.on_mic_frame(_voiced())
    for _ in range(36):
        await s.on_mic_frame(_silent())
    if s._turn_task:
        await s._turn_task


def types(events):
    return [e["type"] for e in events]


@pytest.mark.asyncio
async def test_full_turn_sequence():
    brain = FakeBrain([[BrainEvent(kind="text", text="Opening the calculator now.")]])
    s = make_session(brain)
    await s.start()
    await _drive_utterance(s)
    t = types(s._events)
    assert t[0] == "state" and s._events[0]["value"] == "idle"
    assert "heard" in t and "say_start" in t and "audio" in t and "say_end" in t
    # state progression includes listening → thinking → speaking → listening
    states = [e["value"] for e in s._events if e["type"] == "state"]
    assert states == ["idle", "listening", "thinking", "speaking", "listening"]
    heard = next(e for e in s._events if e["type"] == "heard")
    assert heard["text"] == "open the calculator" and heard["final"] is True
    say = next(e for e in s._events if e["type"] == "say_start")
    assert say["text"] == "Opening the calculator now." and say["say_id"] == 1


@pytest.mark.asyncio
async def test_multi_sentence_makes_multiple_say_ids():
    brain = FakeBrain([[BrainEvent(kind="text",
                                   text="Opening the calculator now. It is ready to use.")]])
    s = make_session(brain)
    await s.start()
    await _drive_utterance(s)
    starts = [e for e in s._events if e["type"] == "say_start"]
    assert [e["say_id"] for e in starts] == [1, 2]
    assert starts[0]["text"] == "Opening the calculator now."
    assert starts[1]["text"] == "It is ready to use."


@pytest.mark.asyncio
async def test_mic_off_pauses():
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="hi there friend")]]))
    await s.start()
    await s.on_mic(True)
    assert s.state == "listening"
    await s.on_mic(False)
    assert s.state == "paused"


@pytest.mark.asyncio
async def test_brain_error_speaks_fallback():
    brain = FakeBrain([[BrainEvent(kind="error", message="Mind unreachable")]])
    s = make_session(brain)
    await s.start()
    await _drive_utterance(s)
    say = next(e for e in s._events if e["type"] == "say_start")
    assert "trouble" in say["text"].lower()
    assert s.state == "listening"


@pytest.mark.asyncio
async def test_client_barge_in_with_voiced_confirms():
    # §7.3: client barge_in starts a verdict window; ≥60ms voiced → say_cancel.
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="a long reply here")]]))
    await s.start()
    s.mic_on = True
    s.state = "speaking"
    s._active_say_id = 3
    s._turn_task = asyncio.ensure_future(asyncio.sleep(5))
    await s.on_barge_in(say_id=3)
    for _ in range(3):                      # 60ms voiced → confirm
        await s.on_mic_frame(_voiced())
    assert s.state == "listening"
    cancel = next(e for e in s._events if e["type"] == "say_cancel")
    assert cancel["say_id"] == 3 and cancel["reason"] == "barge_in"


@pytest.mark.asyncio
async def test_client_barge_in_false_positive_resumes():
    # §7.3: barge_in with no voiced evidence → say_resume at the deadline (no cut).
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="reply")]]))
    await s.start()
    s.mic_on = True
    s.state = "speaking"
    s._active_say_id = 4
    s._turn_task = asyncio.ensure_future(asyncio.sleep(5))
    await s.on_barge_in(say_id=4)
    # only silence during the window
    for _ in range(3):
        await s.on_mic_frame(_silent())
    await asyncio.sleep(0.30)               # past the 250ms verdict deadline
    assert s.state == "speaking"            # not cut
    assert any(e["type"] == "say_resume" for e in s._events)
    assert not any(e["type"] == "say_cancel" for e in s._events)


@pytest.mark.asyncio
async def test_engine_detected_barge_after_60ms_voiced():
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="reply")]]))
    await s.start()
    s.mic_on = True
    s.state = "speaking"
    s._active_say_id = 2
    s._turn_task = asyncio.ensure_future(asyncio.sleep(5))
    # 3 voiced 20 ms frames = 60 ms → confirm
    for _ in range(3):
        await s.on_mic_frame(_voiced())
    assert s.state == "listening"
    assert any(e["type"] == "say_cancel" for e in s._events)


@pytest.mark.asyncio
async def test_tool_round_emits_tool_call_and_continues():
    brain = FakeBrain([
        [BrainEvent(kind="tool_calls",
                    tool_calls=[ToolCall(id="c1", name="open_app", arguments={"name": "calc"})])],
        [BrainEvent(kind="text", text="The calculator is open now.")],
    ])
    s = make_session(brain)
    await s.start()
    await s.on_mic(True)
    # inject via text path to keep it deterministic
    turn = asyncio.ensure_future(s.on_text("open the calculator"))
    # wait for the tool_call to be emitted, then answer it
    for _ in range(100):
        await asyncio.sleep(0.005)
        tc = next((e for e in s._events if e["type"] == "tool_call"), None)
        if tc:
            break
    assert tc is not None and tc["tool"] == "open_app"
    await s.on_tool_result("c1", ok=True, result="Opening calc")
    await turn
    if s._turn_task:
        await s._turn_task
    say = [e for e in s._events if e["type"] == "say_start"]
    assert any("calculator is open" in e["text"].lower() for e in say)


@pytest.mark.asyncio
async def test_conversation_history_records_assistant_replies():
    # multi-turn: the brain must see its own prior reply (was total amnesia before).
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="I am Windy.")]]))
    await s.start()
    await s.on_text("who are you")
    if s._turn_task:
        await s._turn_task
    roles = [m["role"] for m in s._history]
    assert "user" in roles and "assistant" in roles
    assert any(m["role"] == "assistant" and "Windy" in m["content"] for m in s._history)


@pytest.mark.asyncio
async def test_markdown_and_emoji_are_not_spoken():
    # §10: engine sanitizes before TTS — no asterisks/bullets/emoji reach say_start.
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="**Sure!** Here it is. 🎉")]]))
    await s.start()
    await s.on_text("do it")
    if s._turn_task:
        await s._turn_task
    for e in s._events:
        if e["type"] == "say_start":
            assert "*" not in e["text"] and "🎉" not in e["text"]


@pytest.mark.asyncio
async def test_text_mid_turn_does_not_overlap():
    # §11.4: a second turn cancels the first, never runs two concurrently.
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="First reply here now.")]]))
    await s.start()
    await s.on_text("one")
    t1 = s._turn_task
    await s.on_text("two")           # supersedes turn 1
    assert t1 is not s._turn_task     # a new task
    assert t1 is None or t1.cancelled() or t1.done()
    if s._turn_task:
        await s._turn_task


@pytest.mark.asyncio
async def test_level_events_emitted_while_speaking():
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="Hello there friend.")]]))
    await s.start()
    await s.on_text("hi")
    if s._turn_task:
        await s._turn_task
    assert any(e["type"] == "level" for e in s._events)  # §5 lip-sync path is live
