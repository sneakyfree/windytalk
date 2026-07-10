"""Voice-activity endpointing (ported from reference/server/veron_server.py's
Segmenter), aligned to voice-session.v1.

Consumes PCM16 mono 16 kHz in 20 ms frames — the exact mic frame the contract
sends (§3: 320 samples = 640 bytes). Endpointing follows voice-session.v1 §6:
an utterance opens after `min_speech_ms` of cumulative voiced audio and closes
after `silence_ms` of contiguous silence. Defaults are the contract's (150/700),
not the prototype's (150/600) — build to the contract.

webrtcvad is imported lazily and can be injected (a callable
`is_speech(frame_bytes, sample_rate) -> bool`) so endpointing logic is unit-
testable without the C extension.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator

MIC_RATE = 16000               # voice-session.v1 §3
FRAME_MS = 20                  # voice-session.v1 §3 (20 ms mic frames)
FRAME_BYTES = MIC_RATE * FRAME_MS // 1000 * 2   # 640

DEFAULT_MIN_SPEECH_MS = 150    # voice-session.v1 §6
DEFAULT_SILENCE_MS = 700       # voice-session.v1 §6

IsSpeech = Callable[[bytes, int], bool]


def _default_is_speech(aggressiveness: int = 2) -> IsSpeech:
    import webrtcvad  # lazy: C extension
    vad = webrtcvad.Vad(aggressiveness)
    return vad.is_speech


class Segmenter:
    """Feed 16 kHz PCM16; yields complete utterances. Tracks `.in_speech` so the
    turn loop can arm barge-in only while the agent is speaking."""

    def __init__(self, min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
                 silence_ms: int = DEFAULT_SILENCE_MS,
                 is_speech: IsSpeech | None = None) -> None:
        self.min_speech_ms = min_speech_ms
        self.silence_ms = silence_ms
        self._is_speech = is_speech or _default_is_speech()
        self.buf = bytearray()
        self.utter = bytearray()
        self.in_speech = False
        self.silence = 0
        self.speech = 0
        # Pre-roll: the last few frames before the utterance opens, so the leading
        # phonemes aren't chopped (§6). ~200 ms is enough to catch a word onset.
        self._preroll_frames = max(1, 200 // FRAME_MS)
        self._preroll: list[bytes] = []

    def push(self, pcm: bytes) -> list[bytes]:
        """Append audio; return a list of completed utterances (PCM16 bytes).

        §6: the utterance opens on CUMULATIVE voiced time (voiced frames need not
        be contiguous — VAD flicker on short words must still open a turn), and a
        pre-roll of recent frames is prepended so the first syllable survives."""
        self.buf.extend(pcm)
        out: list[bytes] = []
        while len(self.buf) >= FRAME_BYTES:
            frame = bytes(self.buf[:FRAME_BYTES])
            del self.buf[:FRAME_BYTES]
            voiced = self._is_speech(frame, MIC_RATE)
            if not self.in_speech:
                # accumulate voiced time cumulatively; keep a rolling pre-roll
                self._preroll.append(frame)
                if len(self._preroll) > self._preroll_frames:
                    self._preroll.pop(0)
                if voiced:
                    self.speech += FRAME_MS
                if self.speech >= self.min_speech_ms:
                    self.in_speech = True
                    self.silence = 0
                    for f in self._preroll:  # prepend pre-roll so onset isn't clipped
                        self.utter.extend(f)
                    self._preroll = []
            else:
                self.utter.extend(frame)
                if voiced:
                    self.silence = 0
                else:
                    self.silence += FRAME_MS
                    if self.silence >= self.silence_ms:
                        out.append(bytes(self.utter))
                        self.utter = bytearray()
                        self.in_speech = False
                        self.silence = 0
                        self.speech = 0
        return out

    def onset(self, pcm: bytes) -> bool:
        """Cheap check: does this chunk contain speech onset (≥ min_speech_ms of
        voiced audio)? Used for barge-in detection while the agent speaks."""
        voiced_ms = 0
        for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
            if self._is_speech(pcm[i:i + FRAME_BYTES], MIC_RATE):
                voiced_ms += FRAME_MS
        return voiced_ms >= self.min_speech_ms

    def frames(self, pcm: bytes) -> Iterator[bytes]:
        """Yield 20 ms frames from a byte buffer (whole 20 ms frames only)."""
        for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
            yield pcm[i:i + FRAME_BYTES]
