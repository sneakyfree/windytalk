"""AWS Transcribe Streaming STT — the house-standard provider (ADR-009/ADR-044).

Forced-honest stub: the ABC exists and is selectable, but calling it raises so a
half-wired vendor can never masquerade as working. Implemented in a later phase
(the local whisper path carries Phase 0/1).
"""
from __future__ import annotations

from .base import MIC_RATE, STTProvider, Transcript


class TranscribeSTT(STTProvider):
    name = "transcribe"

    def transcribe(self, pcm16: bytes, sample_rate: int = MIC_RATE) -> Transcript:
        raise NotImplementedError(
            "AWS Transcribe Streaming STT is not implemented yet "
            "(ADR-044 forced-honest stub). Use provider 'whisper' for now."
        )
