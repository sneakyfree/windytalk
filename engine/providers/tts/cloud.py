"""Cloud TTS — a hosted vendor behind the same ABC (ADR-044).

Forced-honest stub: selectable but raises until implemented, so a half-wired
vendor can never masquerade as working. The local kokoro path carries Phase 0/1.
"""
from __future__ import annotations

from .base import TTSProvider


class CloudTTS(TTSProvider):
    name = "cloud"

    def synthesize(self, text: str) -> bytes:
        raise NotImplementedError(
            "Cloud TTS is not implemented yet (ADR-044 forced-honest stub). "
            "Use provider 'kokoro' for now."
        )
