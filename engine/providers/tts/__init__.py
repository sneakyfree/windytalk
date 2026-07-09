"""TTS provider registry (ADR-044). Select a provider by name."""
from __future__ import annotations

from .base import TTS_RATE, TTSProvider
from .cloud import CloudTTS
from .kokoro import KokoroTTS

_PROVIDERS = {
    "kokoro": KokoroTTS,
    "cloud": CloudTTS,
}


def get_tts(name: str = "kokoro", **kwargs) -> TTSProvider:
    try:
        cls = _PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown TTS provider {name!r}; have {sorted(_PROVIDERS)}"
        ) from None
    return cls(**kwargs)


__all__ = ["get_tts", "TTSProvider", "TTS_RATE", "KokoroTTS", "CloudTTS"]
