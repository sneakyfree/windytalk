"""Shared, provider-agnostic audio I/O: microphone capture + interruptible playback."""
import asyncio

import pyaudio

_PA = None


def _pa() -> pyaudio.PyAudio:
    global _PA
    if _PA is None:
        _PA = pyaudio.PyAudio()
    return _PA


class Mic:
    """Async microphone: read() returns one pcm16 chunk at the given rate."""

    def __init__(self, rate: int, chunk_ms: int = 40):
        self.rate = rate
        self.frames = rate * chunk_ms // 1000
        self.stream = _pa().open(format=pyaudio.paInt16, channels=1, rate=rate,
                                 input=True, frames_per_buffer=self.frames)

    async def read(self) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.stream.read(self.frames, exception_on_overflow=False))

    def close(self):
        try:
            self.stream.stop_stream(); self.stream.close()
        except Exception:
            pass


class Speaker:
    """Non-blocking pcm16 playback with instant flush for barge-in."""

    def __init__(self, rate: int):
        import threading
        self.rate = rate
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.stream = _pa().open(format=pyaudio.paInt16, channels=1, rate=rate,
                                 output=True, frames_per_buffer=1024,
                                 stream_callback=self._cb)

    def _cb(self, in_data, frame_count, time_info, status):
        need = frame_count * 2
        with self.lock:
            out = bytes(self.buf[:need]); del self.buf[:need]
        if len(out) < need:
            out += b"\x00" * (need - len(out))
        return (out, pyaudio.paContinue)

    def play(self, pcm: bytes):
        with self.lock:
            self.buf.extend(pcm)

    def clear(self):
        """Barge-in: drop everything queued so Windy stops mid-sentence."""
        with self.lock:
            self.buf.clear()

    def close(self):
        try:
            self.stream.stop_stream(); self.stream.close()
        except Exception:
            pass


def shutdown():
    global _PA
    if _PA is not None:
        _PA.terminate(); _PA = None
