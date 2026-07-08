"""
Wake-word gate ("Hey Jarvis") — optional hands-free mode.

When armed, the client stays asleep and streams silence to the brain (nothing
leaves the machine) until it hears the wake word locally via openWakeWord. It then
wakes, passes real microphone audio through so you can give a command, and drifts
back to sleep after a few seconds of quiet. Runs on CPU; the model is tiny.
"""
import os
import time

import numpy as np

_WW = "hey_jarvis_v0.1"
_FRAME = 1280   # openWakeWord wants 80 ms @ 16 kHz


def _model_path():
    import openwakeword
    return os.path.join(os.path.dirname(openwakeword.__file__),
                        "resources", "models", _WW + ".onnx")


class WakeGate:
    def __init__(self, speaker=None, on_state=None, grace=8.0, threshold=0.5):
        from openwakeword.model import Model
        self.model = Model(wakeword_model_paths=[_model_path()])
        self.speaker = speaker
        self.on_state = on_state          # callback(awake: bool)
        self.grace = grace
        self.threshold = threshold
        self.buf = np.zeros(0, dtype=np.int16)
        self.awake = False
        self.awake_until = 0.0

    def process(self, frame: bytes) -> bytes:
        """Return the audio to actually send to the brain (real frame or silence)."""
        samples = np.frombuffer(frame, dtype=np.int16)
        now = time.time()
        if not self.awake:
            self.buf = np.concatenate([self.buf, samples])
            while len(self.buf) >= _FRAME:
                chunk = self.buf[:_FRAME]
                self.buf = self.buf[_FRAME:]
                if self.model.predict(chunk).get(_WW, 0.0) > self.threshold:
                    self._set(True, now)
                    self.buf = np.zeros(0, dtype=np.int16)
                    self.model.reset()
                    break
            return frame if self.awake else b"\x00" * len(frame)
        # awake: keep the window open while the user (or Windy) is talking
        rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768.0) ** 2))) if samples.size else 0.0
        if rms > 0.02 or (self.speaker and self.speaker.speaking):
            self.awake_until = now + self.grace
        if now > self.awake_until:
            self._set(False, now)
            return b"\x00" * len(frame)
        return frame

    def _set(self, awake: bool, now: float):
        self.awake = awake
        self.awake_until = now + self.grace if awake else 0.0
        if self.on_state:
            self.on_state(awake)
