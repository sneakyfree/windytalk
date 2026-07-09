"""TTS provider abstraction (ADR-044).

Text-to-speech is a swappable provider behind this ABC. Concretes:
  - kokoro — local kokoro-onnx (the Phase 0/1 path)
  - cloud  — a cloud TTS vendor (forced-honest stub)

All providers emit the voice-session.v1 §3 output format: PCM signed 16-bit LE,
mono, 24 kHz. A provider that natively produces another rate MUST resample to
24 kHz before returning, so the wire format is invariant.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

TTS_RATE = 24000  # voice-session.v1 §3 — the only rate the engine emits


class TTSProvider(ABC):
    """A text-to-speech engine. May hold a warm model."""

    name: str = "base"
    output_rate: int = TTS_RATE

    @abstractmethod
    def synthesize(self, text: str) -> bytes:
        """Synthesize one text segment → PCM16 mono @ output_rate (24 kHz)."""
        raise NotImplementedError

    def warmup(self) -> None:
        """Preload models so the first real turn isn't cold. Optional, idempotent."""
