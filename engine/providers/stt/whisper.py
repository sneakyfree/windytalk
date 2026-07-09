"""Local faster-whisper STT concrete (ported from reference/server/veron_server.py).

Runs on the 5090 via CUDA. `faster_whisper` is imported lazily so this module
imports cleanly on machines without CUDA/the package (unit tests, CI); the model
loads on first `warmup()`/`transcribe()`.

Runtime gotcha (carried from the prototype): CTranslate2 aborts unless
LD_LIBRARY_PATH includes the venv's nvidia/*/lib (cuDNN9). run_server.sh / the
engine launcher set this before the process starts.
"""
from __future__ import annotations

import os

import numpy as np

from .base import MIC_RATE, STTProvider, Transcript


class WhisperSTT(STTProvider):
    name = "whisper"

    def __init__(self, size: str | None = None, device: str = "cuda",
                 compute_type: str = "float16") -> None:
        self.size = size or os.environ.get("WINDYTALK_WHISPER", "base")
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def warmup(self) -> None:
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy: needs CUDA + package
            self._model = WhisperModel(self.size, device=self.device,
                                       compute_type=self.compute_type)

    def transcribe(self, pcm16: bytes, sample_rate: int = MIC_RATE) -> Transcript:
        if sample_rate != MIC_RATE:
            raise ValueError(f"whisper expects {MIC_RATE} Hz, got {sample_rate}")
        self.warmup()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = self._model.transcribe(
            audio, language="en", beam_size=1, vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text for s in segments).strip()
        conf = getattr(info, "language_probability", None)
        return Transcript(text=text, is_final=True, confidence=conf)
