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


def make_session(brain, stt=None, session_id="t", **kw):
    events = []

    async def emit(e):
        events.append(e)
    s = VoiceSession(stt or FakeSTT(), FakeTTS(), brain, emit,
                     session_id=session_id, pace=False, **kw)
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
    s._streaming_audio = True      # audio IS playing — there is something to interrupt
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


@pytest.mark.asyncio
async def test_every_segment_resets_barge_grace_and_echo_tally():
    """Multi-sentence replies: segments 2+ must get the same start-of-speech grace
    and a clean echo tally that segment 1 gets.

    Live-session evidence (07-22 hand test, transcript.jsonl): 11 of 20 spoken
    segments were cancelled, 8 of them with reason=barge_in, and they were almost
    always the LAST sentence of a multi-sentence reply — the user heard the reply
    start and stop. Cause: the grace/tally reset sat under `if self.state ==
    "thinking"`, which is only true for the FIRST segment of a turn. Every later
    sentence therefore ran with an expired grace window AND the voiced-echo tally
    accumulated while segment 1 was playing, so it self-barged almost immediately.

    Note the pre-existing grace test sets `_speaking_since` by hand, so it passes
    either way — this one drives the real code path.
    """
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="x")]]))
    await s.start()

    s.state = "thinking"
    await s._speak_segment("First sentence.")
    assert s.state == "speaking"
    first_since = s._speaking_since
    assert first_since > 0

    # Echo from segment 1 bleeds into the mic while it plays.
    s._barge_voiced_ms = 999
    s._barge_unvoiced_run = 7
    await asyncio.sleep(0.01)

    await s._speak_segment("Second sentence.")
    assert s._speaking_since > first_since, \
        "segment 2 did not restart the grace window — it is barge-unprotected"
    assert s._barge_voiced_ms == 0, \
        "segment 2 inherited segment 1's echo tally — it can self-barge instantly"
    assert s._barge_unvoiced_run == 0


@pytest.mark.asyncio
async def test_tool_round_exhaustion_never_ends_in_silence():
    """A turn must never end without saying SOMETHING.

    Live evidence (07-22 hand test, turn 101): the user asked "Can you find some
    music for me? I haven't heard you respond" — the engine emitted exactly SIX
    tool_calls over 18s and then went back to listening without a single say_start.
    `_stream_and_speak` bounds tool rounds at `range(6)`; on exhaustion it falls out
    of the loop and returns "" (nothing was ever spoken), and `_run_turn` does
    `if reply:` with no else — so the turn dies silently. From the user's seat the
    assistant simply went mute, and because the NEXT turn then answers, the whole
    conversation slips one question out of phase.
    """
    brain = FakeBrain([[BrainEvent(
        kind="tool_calls",
        tool_calls=[ToolCall(id="c1", name="open_app", arguments={"name": "x"})])]])
    s = make_session(brain)
    await s.start()
    await s.on_mic(True)
    await s.on_text("play track six")
    task = s._turn_task
    assert task is not None

    async def answer_tools():
        # Resolve whatever future is actually pending. on_text returns as soon as
        # the turn task is spawned, so the loop must watch the TASK, not on_text.
        while not task.done():
            await asyncio.sleep(0.002)
            for cid in list(s._tool_futures):
                await s.on_tool_result(cid, ok=True, result="done")

    helper = asyncio.ensure_future(answer_tools())
    await task
    helper.cancel()

    assert brain.calls >= 6, "expected the tool-round limit to be reached"
    assert any(e["type"] == "say_start" for e in s._events), \
        "six tool rounds and not one word spoken — the user is left in silence"


def _pcm(level: int):
    """A frame at a chosen amplitude (FakeSTT calls any non-zero frame voiced, so
    this isolates LEVEL from voicedness)."""
    return bytes([level, level]) * (FRAME_BYTES // 2)


@pytest.mark.asyncio
async def test_echo_floor_suppresses_self_barge_but_not_real_speech():
    """AEC-lite: our own speaker bleed must not count as the user interrupting,
    but a genuinely louder voice still must.

    Measured live against the running engine: with only 5% speaker->mic bleed,
    webrtcvad called every echo frame voiced and EVERY reply was cut at ~840ms
    (grace 600 + confirm 240) — the user heard 0.82s of a 5.87s answer. Voicedness
    alone cannot mean "the user is talking" on a machine with open speakers.
    So the grace window doubles as calibration: whatever the mic hears while only
    WE are speaking is the echo floor, and barge evidence must exceed it.
    """
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="reply")]]))
    await s.start()
    s.mic_on = True
    s.state = "speaking"
    s._streaming_audio = True                              # audio IS playing
    s._speaking_since = asyncio.get_running_loop().time()   # grace open
    s._active_say_id = 2
    s._turn_task = asyncio.ensure_future(asyncio.sleep(10))

    # Levels chosen to exercise the RATIO, not the clamp: the guard compares against
    # floor * margin, so both sides must sit in the unsaturated range.
    echo = _pcm(0x06)
    for _ in range(int(s._barge_grace_ms / 20)):    # calibrate on our own bleed
        await s.on_mic_frame(echo)
    assert s._echo_floor > 0, "grace window did not calibrate an echo floor"

    s._speaking_since = 0.0                          # grace now expired
    for _ in range(s._barge_confirm_ms // 20 + 10):  # sustained echo, same level
        await s.on_mic_frame(echo)
    assert not any(e["type"] == "say_cancel" for e in s._events), \
        "sustained echo at the calibrated floor self-barged — the truncation bug"

    for _ in range(s._barge_confirm_ms // 20 + 1):   # a real, louder interruption
        await s.on_mic_frame(_pcm(0x40))
    assert any(e["type"] == "say_cancel" and e.get("reason") == "barge_in"
               for e in s._events), "a genuine interrupt was swallowed by the echo gate"
    if s._turn_task:            # the confirmed barge already cancelled it
        s._turn_task.cancel()


@pytest.mark.asyncio
async def test_noise_after_a_reply_does_not_emit_a_phantom_supersede():
    """Speaker echo / a cough must not start a turn at all.

    2026-07-26 hand test: 53 turns started, 29 (55%) never produced a transcript,
    and 17 completed replies were cancelled `superseded` by those phantoms. Cause:
    _start_turn cancelled FIRST and only afterwards did _run_turn transcribe and
    bail on an unintelligible result — so every stray noise burned a turn_id and
    fired a say_cancel at the client, while the user got nothing back.
    """
    stt = FakeSTT("open the calculator")
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="Opening it now.")]]), stt=stt)
    await s.start()
    await _drive_utterance(s)                      # one complete, successful turn
    assert any(e["type"] == "say_end" for e in s._events)
    turn_after_real = s.turn_id
    before = len(s._events)

    stt.text = ""                                  # now only noise reaches the mic
    for _ in range(10):
        await s.on_mic_frame(_voiced())
    for _ in range(40):
        await s.on_mic_frame(_silent())            # trip EOS on the noise
    if s._turn_task:
        await s._turn_task

    new = s._events[before:]
    assert not any(e["type"] == "say_cancel" for e in new), \
        "noise fired a phantom say_cancel at the client"
    assert not any(e["type"] == "heard" for e in new)
    assert s.turn_id == turn_after_real, \
        f"noise burned a turn_id ({turn_after_real} -> {s.turn_id})"


@pytest.mark.asyncio
async def test_undecipherable_speech_tells_the_user_instead_of_vanishing():
    """Captured-but-unintelligible speech must produce a signal, not silence.

    Dropping it silently (a bare `return`) is why a question chopped in half by
    endpointing is indistinguishable from "still thinking" and from "the app is
    broken" — the 2026-07-26 hand test shows clusters of 3-5 such drops seconds
    apart, one sentence fragmented, with zero feedback to the user each time.
    Non-fatal, and shown rather than spoken (speaking it would feed the echo loop
    that causes some of these).
    """
    stt = FakeSTT("")
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="unused")]]), stt=stt)
    await s.start()
    await s.on_mic(True)
    for _ in range(10):
        await s.on_mic_frame(_voiced())
    for _ in range(40):
        await s.on_mic_frame(_silent())
    if s._turn_task:
        await s._turn_task

    errs = [e for e in s._events if e["type"] == "error"]
    assert errs, "unintelligible speech vanished with no signal at all"
    assert errs[0]["code"] == "not_understood" and errs[0]["fatal"] is False
    assert not any(e["type"] == "say_start" for e in s._events), "it must not SPEAK this"
    assert not any(e["type"] == "heard" for e in s._events)


@pytest.mark.asyncio
async def test_no_autonomous_barge_while_she_is_silent():
    """The autonomous detector must only run while audio is actually playing.

    The `speaking` STATE spans a whole turn, including the long silent stretches
    while tools run. The detector stayed armed through all of it, judging against an
    echo floor calibrated for a segment that had already ended. Live 2026-07-28:
    11 of 18 barge cuts fired while she was completely silent, up to 14.9s after
    say_end — there was nothing to interrupt.

    A client-signalled barge is still honoured here: the user's own AEC-backed
    detector heard real speech, and redirecting mid-tool-round is legitimate.
    """
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="reply")]]))
    await s.start()
    s.mic_on = True
    s.state = "speaking"           # still 'speaking', but between segments
    s._streaming_audio = False     # ...and no audio is going out
    s._speaking_since = 0.0        # grace long expired
    s._echo_floor = 0.0            # stale floor from a finished segment
    s._active_say_id = 9
    s._turn_task = asyncio.ensure_future(asyncio.sleep(10))

    for _ in range(s._barge_confirm_ms // 20 + 20):   # plenty of loud voiced audio
        await s.on_mic_frame(_pcm(0x40))
    assert not any(e["type"] == "say_cancel" for e in s._events), \
        "barged while silent — there was nothing playing to interrupt"

    # but the client's own detector must still be able to redirect mid-tool-round
    await s.on_barge_in(say_id=9)
    for _ in range(4):
        await s.on_mic_frame(_pcm(0x40))
    assert any(e["type"] == "say_cancel" and e.get("reason") == "barge_in"
               for e in s._events), "client-signalled barge was swallowed too"
    if s._turn_task:
        s._turn_task.cancel()


@pytest.mark.asyncio
async def test_short_noise_does_not_announce_didnt_catch_that():
    """A cough or a chair squeak must not make the mic feel hair-trigger.

    An utterance opens on 150 ms of voiced audio, which furniture clears easily.
    Announcing "didn't catch that" at every one of those fired 61 notices in a
    28-minute session — Grant's report: "if I breathed a little heavy or coughed,
    it would say I didn't catch that". Below the threshold it is noise and is
    dropped in silence, as the engine always did; above it, somebody said something
    real and losing it wordlessly is the bug the notice exists to fix.
    """
    stt = FakeSTT("")                      # nothing transcribable either way
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="unused")]]), stt=stt)
    await s.start()
    await s.on_mic(True)

    short = b"\x10\x10" * (FRAME_BYTES // 2) * 10        # ~0.2s — a cough
    await s._start_turn(utter_pcm=short)
    assert not [e for e in s._events if e["type"] == "error"], \
        "a cough announced 'didn't catch that'"

    long = b"\x10\x10" * (FRAME_BYTES // 2) * 60         # ~1.2s — a real utterance
    await s._start_turn(utter_pcm=long)
    errs = [e for e in s._events if e["type"] == "error"]
    assert errs and errs[0]["code"] == "not_understood", \
        "a real lost utterance vanished with no signal"


@pytest.mark.asyncio
async def test_a_normal_length_turn_is_not_superseded_by_a_follow_up():
    """Slowness must not become total silence.

    2026-07-29 hand test: seven questions over four and a half minutes, not one
    answered. The engine WAS working — degraded to ~13s per turn against 4.5s on a
    fresh process — but the supersede threshold was 8s, SHORTER than a normal turn.
    So every reply became supersede-able before finishing, Grant reasonably re-asked
    at ~20s intervals, and each new question killed the answer in flight. Seven turns
    went `-> thinking` and produced nothing.

    The threshold has to sit above a realistic turn, not below it.
    """
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="the answer")]]))
    await s.start()
    await s.on_mic(True)
    await s.on_text("first question")
    first = s._turn_task
    assert first is not None

    # a turn that started 10s ago and hasn't spoken yet is THINKING, not stuck
    s._turn_started_at = (s.loop or asyncio.get_running_loop()).time() - 10.0
    s._turn_produced = False
    s.state = "thinking"
    for _ in range(10):
        await s.on_mic_frame(_voiced())
    for _ in range(40):
        await s.on_mic_frame(_silent())
    assert s._turn_task is first, "a 10s turn was superseded — slowness becomes silence"

    # but one silent for 30s really is stuck and may be replaced
    s._turn_started_at = (s.loop or asyncio.get_running_loop()).time() - 30.0
    s.state = "thinking"
    for _ in range(10):
        await s.on_mic_frame(_voiced())
    for _ in range(40):
        await s.on_mic_frame(_silent())
    assert s._turn_task is not first, "a genuinely stuck turn was never replaced"
    if s._turn_task:
        await s._turn_task


@pytest.mark.asyncio
async def test_a_long_silent_turn_volunteers_that_it_is_still_working(monkeypatch):
    """A yellow dot cannot say whether she is 5 seconds out or dead.

    2026-07-29: a turn sat silent for 90s (the brain timeout) and Grant asked out
    loud, "you've been yellow for quite a while, are you still working on it?" The
    face was telling the truth; it just could not tell him ENOUGH. The doctrine
    names this proactive honesty and wants it (P7).
    """
    import engine.session as sess
    monkeypatch.setattr(sess, "_WORKING_AFTER_S", 0.05)   # keep the test fast

    class SlowBrain:
        def stream(self, messages, tools=None, model=None):
            import time
            time.sleep(0.5)                                # longer than the threshold
            yield BrainEvent(kind="text", text="Here is the answer.")
            yield BrainEvent(kind="done", finish_reason="stop")

    s = make_session(SlowBrain())
    await s.start()
    await s.on_mic(True)
    await s.on_text("something that takes a while")
    if s._turn_task:
        await s._turn_task

    spoken = [e.get("text", "") for e in s._events if e["type"] == "say_start"]
    assert any("still working" in t.lower() for t in spoken), \
        "sat silent through a long turn and told the user nothing"
    assert any("here is the answer" in t.lower() for t in spoken), \
        "the courtesy line replaced the real answer instead of preceding it"


@pytest.mark.asyncio
async def test_a_normal_speed_turn_never_mentions_still_working(monkeypatch):
    # The courtesy line must be invisible at healthy speed, or it becomes noise.
    import engine.session as sess
    monkeypatch.setattr(sess, "_WORKING_AFTER_S", 5.0)
    s = make_session(FakeBrain([[BrainEvent(kind="text", text="Quick answer.")]]))
    await s.start()
    await s.on_mic(True)
    await s.on_text("something quick")
    if s._turn_task:
        await s._turn_task
    spoken = [e.get("text", "") for e in s._events if e["type"] == "say_start"]
    assert not any("still working" in t.lower() for t in spoken)
    assert any("quick answer" in t.lower() for t in spoken)


@pytest.mark.asyncio
async def test_conversation_survives_an_engine_restart(tmp_path, monkeypatch):
    """§9 / P7: the conversation must outlive the process.

    Windy Talk's own server docstring admitted this hole — "every hello builds a
    fresh session ... the context is gone". P7 promises grandma "can never tell when
    it refreshed context", and it is the precondition for ever offering her a restart
    button: P8 ranks stability above capability, but a restart that forgets the
    weekend trades one instability for a worse one.
    """
    monkeypatch.setenv("WINDYTALK_SESSION_DIR", str(tmp_path))
    sid = "s-restart-me"

    first = make_session(FakeBrain([[BrainEvent(kind="text", text="Blue is the sky.")]]),
                         session_id=sid)
    await first.start()
    await first.on_mic(True)
    await first.on_text("what colour is the sky")
    if first._turn_task:
        await first._turn_task
    assert first.resumed is False, "a brand-new session must not claim it resumed"

    del first                                   # the process dies here

    second = make_session(FakeBrain([[BrainEvent(kind="text", text="ok")]]),
                          session_id=sid)
    assert second.resumed is True, "reconnect did not report a resume"
    joined = " ".join(m.get("content", "") for m in second._history)
    assert "what colour is the sky" in joined, "the user's question was forgotten"
    assert "blue is the sky" in joined.lower(), "her own answer was forgotten"

    fresh = make_session(FakeBrain([[BrainEvent(kind="text", text="ok")]]),
                         session_id="s-somebody-else")
    assert fresh._history == [] and fresh.resumed is False, "sessions leaked into each other"
