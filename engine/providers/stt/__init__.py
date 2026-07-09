"""STT provider registry (ADR-044). Select a provider by name."""
from __future__ import annotations

from .base import MIC_RATE, STTProvider, Transcript
from .transcribe import TranscribeSTT
from .whisper import WhisperSTT

_PROVIDERS = {
    "whisper": WhisperSTT,
    "transcribe": TranscribeSTT,
}


def get_stt(name: str = "whisper", **kwargs) -> STTProvider:
    try:
        cls = _PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown STT provider {name!r}; have {sorted(_PROVIDERS)}"
        ) from None
    return cls(**kwargs)


__all__ = ["get_stt", "STTProvider", "Transcript", "MIC_RATE",
           "WhisperSTT", "TranscribeSTT"]
