"""Sentence segmentation for streaming TTS (voice-session.v1 §10).

The engine cuts brain text into TTS segments: at the first sentence boundary
(. ! ? : newline followed by whitespace/EOS) *at which the pending segment has
≥ 3 words*, or at 24 words (run-on guard), or at stream end. The first segment
is the latency-critical one — it should be synthesized the instant it is cut.

`first_segment()` is what the Task 0.5 harness uses to measure EOS→first-audio;
`segment_stream()` is the general incremental cutter the Phase 1 session loop
will drive from brain tokens.
"""
from __future__ import annotations

import re
from typing import Iterable, Iterator

_BOUNDARY = re.compile(r"[.!?:\n]")
_WORD = re.compile(r"\S+")
MIN_WORDS = 3        # §10
RUNON_WORDS = 24     # §10


def _word_count(s: str) -> int:
    return len(s.split())


def _runon_pos(buf: str) -> int | None:
    """Char index just past the 24th word (whitespace-safe), or None if < 24 words."""
    ends = [m.end() for m in _WORD.finditer(buf)]
    if len(ends) >= RUNON_WORDS:
        return ends[RUNON_WORDS - 1]
    return None


def _cut_point(buf: str) -> int | None:
    """Return the index just past a qualifying cut, or None if none yet.

    §10: cut at the first sentence boundary at which the pending segment has ≥3
    words — but only if that boundary falls within the first 24 words; otherwise
    the run-on guard cuts at 24 words (a boundary way out past the guard doesn't
    get to suppress it)."""
    boundary = None
    for m in _BOUNDARY.finditer(buf):
        end = m.end()
        after = buf[end:end + 1]
        if (after == "" or after.isspace()) and _word_count(buf[:end]) >= MIN_WORDS:
            boundary = end
            break
    runon = _runon_pos(buf)
    if boundary is None:
        return runon
    if runon is None or boundary <= runon:
        return boundary
    return runon


def segment_stream(tokens: Iterable[str]) -> Iterator[str]:
    """Consume a token stream, yield TTS segments per §10 (empty segments skipped)."""
    buf = ""
    for tok in tokens:
        buf += tok
        while True:
            cut = _cut_point(buf)
            if cut is None:
                break
            seg = buf[:cut].strip()
            buf = buf[cut:]
            if seg:
                yield seg
    tail = buf.strip()
    if tail:
        yield tail


def first_segment(text: str) -> str:
    """The first TTS segment of a complete text (the latency-critical one)."""
    for seg in segment_stream([text]):
        return seg
    return ""
