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
        self.seen_messages = []

    def stream(self, messages, tools=None, model=None):
        self.seen_messages.append(messages)
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
async def test_engine_detected_barge_after_sustained_voiced():
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="reply")]]))
    await s.start()
    s.mic_on = True
    s.state = "speaking"
    s._speaking_since = 0.0        # far in the past → past the grace window
    s._active_say_id = 2
    s._turn_task = asyncio.ensure_future(asyncio.sleep(5))
    # sustained voiced past the (raised) confirm threshold → barge confirms
    for _ in range(s._barge_confirm_ms // 20 + 1):
        await s.on_mic_frame(_voiced())
    assert s.state == "listening"
    assert any(e["type"] == "say_cancel" for e in s._events)


@pytest.mark.asyncio
async def test_barge_grace_protects_start_of_speech():
    # The #1 first-voice-session bug: speaker echo / trailing user speech must not
    # cancel a reply the instant it starts. During the grace window, even sustained
    # voiced frames do not barge.
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="reply")]]))
    await s.start()
    s.mic_on = True
    s.state = "speaking"
    loop = asyncio.get_running_loop()
    s._speaking_since = loop.time()   # speaking JUST started
    s._active_say_id = 2
    s._turn_task = asyncio.ensure_future(asyncio.sleep(5))
    for _ in range(s._barge_confirm_ms // 20 + 5):   # more than enough voiced
        await s.on_mic_frame(_voiced())
    assert s.state == "speaking"      # grace held — no self-cancel
    assert not any(e["type"] == "say_cancel" for e in s._events)
    s._turn_task.cancel()


@pytest.mark.asyncio
async def test_stuck_turn_is_superseded():
    # A genuinely HUNG turn (no output, thinking a long time) is replaced by a new
    # utterance — the round-2 win, now scoped to stuck turns only.
    class SlowBrain:
        def stream(self, messages, tools=None, model=None):
            yield BrainEvent(kind="text", text="old answer")
            yield BrainEvent(kind="done", finish_reason="stop")

    s = make_session(SlowBrain(), stt=FakeSTT(text="the new question"))
    await s.start()
    await s.on_mic(True)
    s.state = "thinking"
    s.turn_id = 1
    s._turn_produced = False
    s._turn_started_at = asyncio.get_running_loop().time() - 999  # long-stuck
    prior = asyncio.ensure_future(asyncio.sleep(5))
    s._turn_task = prior
    for _ in range(10):
        await s.on_mic_frame(_voiced())
    for _ in range(36):
        await s.on_mic_frame(_silent())
    assert prior.cancelled() or prior.done()   # stuck turn was superseded
    if s._turn_task:
        await s._turn_task
    heard = [e for e in s._events if e["type"] == "heard"]
    assert any(e["text"] == "the new question" for e in heard)


@pytest.mark.asyncio
async def test_think_supersede_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WINDYTALK_NO_THINK_SUPERSEDE", "1")
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="x")]]))
    await s.start()
    await s.on_mic(True)
    s.state = "thinking"
    prior = asyncio.ensure_future(asyncio.sleep(5))
    s._turn_task = prior
    for _ in range(10):
        await s.on_mic_frame(_voiced())
    for _ in range(36):
        await s.on_mic_frame(_silent())
    assert not prior.done()          # nothing superseded; old turn untouched
    prior.cancel()


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
    # the follow-up brain call must carry OpenAI wire shape, not ToolCall.__dict__
    # (Mind 422s otherwise — found live on the Mac mini, first real tool round)
    followup = brain.seen_messages[1]
    tc_msg = next(m for m in followup if m["role"] == "assistant" and m.get("tool_calls"))
    call = tc_msg["tool_calls"][0]
    assert call["type"] == "function" and call["id"] == "c1"
    assert call["function"]["name"] == "open_app"
    assert call["function"]["arguments"] == '{"name": "calc"}'
    tool_msg = next(m for m in followup if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1" and tool_msg["content"] == "Opening calc"


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


@pytest.mark.asyncio
async def test_heard_precedes_thinking_on_wire():
    # §6: heard{final} MUST be emitted before state{thinking} (fresh-audit H2).
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="Opening it now for you.")]]))
    await s.start()
    await _drive_utterance(s)
    seq = [(e["type"], e.get("value")) for e in s._events
           if e["type"] == "heard" or (e["type"] == "state" and e.get("value") == "thinking")]
    assert ("heard", None) in seq and ("state", "thinking") in seq
    assert seq.index(("heard", None)) < seq.index(("state", "thinking"))


@pytest.mark.asyncio
async def test_unintelligible_utterance_emits_no_thinking():
    # <2 chars STT → no heard, and no orphan thinking blip (H2 wart).
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="x")]]), stt=FakeSTT(text="a"))
    await s.start()
    await _drive_utterance(s)
    assert not any(e["type"] == "heard" for e in s._events)
    assert not any(e["type"] == "state" and e.get("value") == "thinking" for e in s._events)


@pytest.mark.asyncio
async def test_sparse_false_voiced_frames_do_not_barge():
    # H3: isolated voiced frames spread across a long reply must NOT accumulate to
    # a spurious barge — the decay window resets the tally after a silence gap.
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="This is a fairly long spoken reply.")]]))
    await s.start()
    await s.on_mic(True)
    s.state = "speaking"          # simulate mid-speech
    s._active_say_id = 1
    for _ in range(30):          # 1 voiced frame every ~120 ms (well past the 100 ms decay)
        await s.on_mic_frame(_voiced())
        for _ in range(6):
            await s.on_mic_frame(_silent())
    assert not any(e["type"] == "say_cancel" and e.get("reason") == "barge_in"
                   for e in s._events)


@pytest.mark.asyncio
async def test_cancelled_reply_still_enters_history():
    # Round-2 finding: a barged/superseded reply vanished from history, so the
    # brain confabulated ("I'm a text-only assistant"). The spoken part must be
    # recorded, marked interrupted.
    import time as _time

    class SlowSecondBrain:
        def stream(self, messages, tools=None, model=None):
            yield BrainEvent(kind="text", text="First part said. ")
            _time.sleep(0.8)
            yield BrainEvent(kind="text", text="Never reached aloud.")
            yield BrainEvent(kind="done", finish_reason="stop")

    s = make_session(SlowSecondBrain())
    await s.start()
    await s.on_mic(True)
    await s.on_text("question one")
    for _ in range(200):                       # wait until the first segment spoke
        await asyncio.sleep(0.005)
        if any(e["type"] == "say_end" for e in s._events):
            break
    await s._cancel_turn(reason="superseded")
    entries = [m for m in s._history if m["role"] == "assistant"]
    assert entries, "cancelled reply must still be recorded"
    assert "First part said." in entries[-1]["content"]
    assert "[interrupted by the user before finishing]" in entries[-1]["content"]


@pytest.mark.asyncio
async def test_working_turn_is_protected_from_supersede():
    # Round-3 spiral fix: a turn that has PRODUCED output (a tool_call or speech)
    # must NOT be killed by an anxious "you there?" nudge — the interjecting
    # frames are dropped and the turn completes so the user hears the answer.
    class WorkingBrain:
        def stream(self, messages, tools=None, model=None):
            yield BrainEvent(kind="text", text="the answer you were waiting for")
            yield BrainEvent(kind="done", finish_reason="stop")

    s = make_session(WorkingBrain(), stt=FakeSTT(text="are you there"))
    await s.start()
    await s.on_mic(True)
    s.state = "thinking"
    s.turn_id = 1
    s._turn_produced = True                       # already producing output
    s._turn_started_at = asyncio.get_running_loop().time()
    prior = asyncio.ensure_future(asyncio.sleep(5))
    s._turn_task = prior
    for _ in range(10):
        await s.on_mic_frame(_voiced())
    for _ in range(36):
        await s.on_mic_frame(_silent())
    assert not prior.done()                       # protected — nudge dropped
    prior.cancel()


@pytest.mark.asyncio
async def test_fast_producing_turn_survives_immediate_nudge():
    # Even a NOT-yet-stuck turn (young, no output) is protected — supersede only
    # fires past the stuck floor, so a quick nudge right after speaking can't
    # kill a turn that's about to answer.
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="x")]]),
                     stt=FakeSTT(text="hello"))
    await s.start()
    await s.on_mic(True)
    s.state = "thinking"
    s._turn_produced = False
    s._turn_started_at = asyncio.get_running_loop().time()   # just started
    prior = asyncio.ensure_future(asyncio.sleep(5))
    s._turn_task = prior
    for _ in range(10):
        await s.on_mic_frame(_voiced())
    for _ in range(36):
        await s.on_mic_frame(_silent())
    assert not prior.done()                       # young turn protected
    prior.cancel()
