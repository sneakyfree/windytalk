"""Felt-latency measurement against the frozen §0.1 budget.

The genome makes §0.1 a *release gate, measured not vibed*. Every stage the
engine runs is timed here and checked against its budget; the composed
EOS→first-audio number is the headline. Figures leave the engine as out-of-band
telemetry (telemetry.v1 `latency_ms`), never on the voice websocket.

This module has no third-party deps so it imports on any machine (CI included).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field

# voice-session.v1 §0.1 — the frozen budget (p90 over ≥20 real turns), in ms.
BUDGET_MS = {
    "eos_to_first_audio": 1200,
    "barge_to_silent": 150,
    "wake_to_listening": 300,
    "transport": 60,
}


@dataclass
class LatencyLog:
    """Accumulates per-stage timings for one turn and composes §0.1 metrics."""

    stages: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def time(self, stage: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages[stage] = (time.perf_counter() - t0) * 1000.0

    def record(self, stage: str, ms: float) -> None:
        self.stages[stage] = ms

    def eos_to_first_audio_ms(self) -> float:
        """EOS→first-audio = STT + brain + first-segment TTS (the stages that run
        between end-of-speech and the first spoken chunk)."""
        return sum(self.stages.get(s, 0.0)
                   for s in ("stt", "brain", "tts_first_segment"))

    def check(self, metric: str, value_ms: float) -> tuple[bool, str]:
        budget = BUDGET_MS.get(metric)
        if budget is None:
            return True, f"{metric}={value_ms:.0f}ms (no budget)"
        ok = value_ms <= budget
        mark = "PASS" if ok else "OVER"
        return ok, f"{metric}={value_ms:.0f}ms / {budget}ms [{mark}]"

    def report(self) -> dict:
        eos = self.eos_to_first_audio_ms()
        ok, line = self.check("eos_to_first_audio", eos)
        return {
            "stages_ms": {k: round(v, 1) for k, v in self.stages.items()},
            "eos_to_first_audio_ms": round(eos, 1),
            "eos_budget_ok": ok,
            "summary": line,
        }

    def telemetry_latency(self) -> dict:
        """Shape for telemetry.v1 `latency_ms` (numbers only, content-free)."""
        return {"eos_to_first_audio_p90": round(self.eos_to_first_audio_ms(), 1)}
