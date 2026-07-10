"""Task 0.5 unit tests — run anywhere (no CUDA / no models needed).

The real wav→speak latency run happens on the 5090 (see docs/PROBE_RESULTS.md /
the Task 0.5 PR); these lock the ABC contracts, the forced-honest stubs, and the
pure logic (VAD endpointing, §10 segmentation, §0.1 budget)."""
import pytest

from engine.latency import BUDGET_MS, LatencyLog
from engine.providers.stt import STTProvider, get_stt
from engine.providers.tts import TTSProvider, get_tts
from engine.segment import first_segment, segment_stream
from engine.vad import FRAME_BYTES, Segmenter

# ---------- provider ABCs + registries ----------

def test_stt_registry_and_abc():
    w = get_stt("whisper")
    assert isinstance(w, STTProvider) and w.name == "whisper"
    with pytest.raises(ValueError):
        get_stt("nope")


def test_tts_registry_and_abc():
    k = get_tts("kokoro")
    assert isinstance(k, TTSProvider) and k.name == "kokoro"
    assert k.output_rate == 24000          # voice-session.v1 §3
    with pytest.raises(ValueError):
        get_tts("nope")


def test_forced_honest_stubs_raise():
    with pytest.raises(NotImplementedError):
        get_stt("transcribe").transcribe(b"\x00\x00")
    with pytest.raises(NotImplementedError):
        get_tts("cloud").synthesize("hello")


def test_cannot_instantiate_bare_abcs():
    with pytest.raises(TypeError):
        STTProvider()
    with pytest.raises(TypeError):
        TTSProvider()


# ---------- VAD endpointing (injected detector, no webrtcvad needed) ----------

def _marked_speech(loud_ranges):
    """is_speech that returns True for frames whose first sample != 0."""
    def is_speech(frame: bytes, sr: int) -> bool:
        return frame[0] != 0 or frame[1] != 0
    return is_speech


def _frame(voiced: bool) -> bytes:
    return (b"\x10\x10" if voiced else b"\x00\x00") * (FRAME_BYTES // 2)


def test_vad_emits_one_utterance_after_trailing_silence():
    seg = Segmenter(min_speech_ms=150, silence_ms=700, is_speech=_marked_speech(None))
    out = []
    # 10 voiced frames (200ms > 150ms opens), then 36 silent (720ms > 700ms closes)
    for _ in range(10):
        out += seg.push(_frame(True))
    assert out == []                       # not closed yet
    for _ in range(36):
        out += seg.push(_frame(False))
    assert len(out) == 1                    # exactly one utterance
    assert not seg.in_speech


def test_vad_ignores_subthreshold_blip():
    seg = Segmenter(min_speech_ms=150, silence_ms=700, is_speech=_marked_speech(None))
    out = []
    for _ in range(3):                     # 60ms < 150ms → never opens
        out += seg.push(_frame(True))
    for _ in range(40):
        out += seg.push(_frame(False))
    assert out == []


def test_vad_opens_on_cumulative_not_contiguous_voiced():
    # §6: voiced frames need not be contiguous. Alternate voiced/unvoiced so no
    # 8-frame contiguous run ever occurs, but cumulative voiced crosses 150ms.
    seg = Segmenter(min_speech_ms=150, silence_ms=700, is_speech=_marked_speech(None))
    for _ in range(16):                    # 8 voiced (160ms cumulative) interleaved
        seg.push(_frame(True))
        seg.push(_frame(False))
    assert seg.in_speech                    # opened despite no contiguous run


def test_vad_preroll_keeps_leading_frames():
    # The utterance should include pre-roll frames captured before it opened, so
    # the first syllable isn't chopped. Open with 8 voiced frames, then close.
    seg = Segmenter(min_speech_ms=150, silence_ms=700, is_speech=_marked_speech(None))
    out = []
    for _ in range(8):                     # 160ms voiced → opens
        out += seg.push(_frame(True))
    for _ in range(36):                    # trailing silence closes
        out += seg.push(_frame(False))
    assert len(out) == 1
    # utterance carries >= the 8 opening frames (pre-roll included, not fewer)
    assert len(out[0]) >= 8 * FRAME_BYTES


# ---------- §10 sentence segmentation ----------

def test_first_segment_is_first_sentence_with_3plus_words():
    assert first_segment("Opening the calculator. Anything else?") == "Opening the calculator."


def test_segmentation_skips_early_boundary_under_3_words():
    # "OK." is < 3 words → carried forward to the next qualifying boundary
    segs = list(segment_stream(["OK. Now opening the files app."]))
    assert segs[0] == "OK. Now opening the files app."


def test_segmentation_runon_guard_at_24_words():
    text = " ".join(f"w{i}" for i in range(30)) + "."
    segs = list(segment_stream([text]))
    assert len(segs[0].split()) == 24      # cut at the run-on guard


def test_segmentation_merges_sub_3_word_sentence_forward():
    # "Hello there." is only 2 words → cannot be cut alone (§10); it merges forward.
    segs = list(segment_stream(["Hello there. How are you today?"]))
    assert segs == ["Hello there. How are you today?"]


def test_segmentation_streams_incrementally():
    segs = list(segment_stream(["Opening the calculator now. ", "It is ", "ready to use."]))
    assert segs == ["Opening the calculator now.", "It is ready to use."]


# ---------- §0.1 latency budget ----------

def test_latency_composes_eos_to_first_audio():
    lat = LatencyLog()
    lat.record("stt", 300)
    lat.record("brain", 400)
    lat.record("tts_first_segment", 250)
    lat.record("tts_rest", 900)            # NOT on the critical path
    assert lat.eos_to_first_audio_ms() == 950
    rep = lat.report()
    assert rep["eos_budget_ok"] is True    # 950 <= 1200


def test_latency_flags_over_budget():
    lat = LatencyLog()
    lat.record("stt", 700)
    lat.record("brain", 600)
    lat.record("tts_first_segment", 300)   # 1600 > 1200
    ok, line = lat.check("eos_to_first_audio", lat.eos_to_first_audio_ms())
    assert ok is False and "OVER" in line


def test_budget_matches_frozen_contract():
    assert BUDGET_MS == {
        "eos_to_first_audio": 1200,
        "barge_to_silent": 150,
        "wake_to_listening": 300,
        "transport": 60,
    }
