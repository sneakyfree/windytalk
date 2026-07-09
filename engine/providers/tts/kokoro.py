"""Local kokoro-onnx TTS concrete (ported from reference/server/veron_server.py).

`kokoro_onnx` is imported lazily so this module imports cleanly without the
package/models. Model + voices files come from env or the prototype's proven
locations. kokoro-v1.0 emits 24 kHz natively (matches the contract); if a build
ever returns another rate we resample to 24 kHz so the wire format is invariant.
"""
from __future__ import annotations

import os

import numpy as np

from .base import TTS_RATE, TTSProvider

_DEFAULT_DIR = os.path.expanduser("~/windy-jarvis-server")


class KokoroTTS(TTSProvider):
    name = "kokoro"
    output_rate = TTS_RATE

    def __init__(self, model_path: str | None = None,
                 voices_path: str | None = None, voice: str | None = None) -> None:
        self.model_path = model_path or os.environ.get(
            "WINDYTALK_KOKORO_MODEL", os.path.join(_DEFAULT_DIR, "kokoro-v1.0.onnx"))
        self.voices_path = voices_path or os.environ.get(
            "WINDYTALK_KOKORO_VOICES", os.path.join(_DEFAULT_DIR, "voices-v1.0.bin"))
        self.voice = voice or os.environ.get("WINDYTALK_VOICE", "af_heart")
        self._kokoro = None

    def warmup(self) -> None:
        if self._kokoro is None:
            from kokoro_onnx import Kokoro  # lazy: needs package + model files
            self._kokoro = Kokoro(self.model_path, self.voices_path)

    def synthesize(self, text: str) -> bytes:
        if not text or not text.strip():
            return b""
        self.warmup()
        samples, sr = self._kokoro.create(text, voice=self.voice, speed=1.0,
                                          lang="en-us")
        samples = np.asarray(samples, dtype=np.float32)
        if sr != self.output_rate:
            samples = _resample_linear(samples, sr, self.output_rate)
        return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _resample_linear(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Dependency-free linear resample (rare fallback; kokoro-v1.0 is already 24 kHz)."""
    if src_sr == dst_sr or samples.size == 0:
        return samples
    n_dst = int(round(samples.size * dst_sr / src_sr))
    x_src = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    x_dst = np.linspace(0.0, 1.0, n_dst, endpoint=False)
    return np.interp(x_dst, x_src, samples).astype(np.float32)
