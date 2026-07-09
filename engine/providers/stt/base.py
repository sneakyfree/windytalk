"""STT provider abstraction (ADR-044).

Speech-to-text is a swappable provider behind this ABC. Concretes:
  - whisper    — local faster-whisper on the 5090 (the Phase 0/1 path)
  - transcribe — AWS Transcribe Streaming, the house standard (forced-honest stub)

All providers consume the voice-session.v1 §3 mic format: PCM signed 16-bit LE,
mono, 16 kHz. The ABC method transcribes one *complete* utterance (the VAD in
engine/vad.py decides where utterances end); streaming-partial support is a
provider-optional extension, not required by v1.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

MIC_RATE = 16000  # voice-session.v1 §3 — the only rate the engine ingests


@dataclass
class Transcript:
    text: str
    is_final: bool = True
    confidence: float | None = None


class STTProvider(ABC):
    """A speech-to-text engine. Stateless per call; may hold a warm model."""

    name: str = "base"

    @abstractmethod
    def transcribe(self, pcm16: bytes, sample_rate: int = MIC_RATE) -> Transcript:
        """Transcribe a complete utterance of PCM16 mono audio → final text."""
        raise NotImplementedError

    def warmup(self) -> None:
        """Preload models so the first real turn isn't cold. Optional, idempotent."""
