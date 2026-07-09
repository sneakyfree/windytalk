// AudioWorklet capture processor (voice-session.v1 §4).
//
// Runs in the audio render thread. Resamples the device rate → 16 kHz, emits
// exactly-20 ms (320-sample) PCM16 mono frames, and runs a cheap energy
// speech-onset detector so the main thread can fire a local barge-in (§7.1)
// while the agent is speaking. MediaRecorder is forbidden (§4.2) — this is the
// AudioWorklet path. Echo cancellation is requested on the getUserMedia track in
// the renderer (§4.1); this processor sees the AEC-cleaned signal.

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.frameSamples = 320; // 20 ms @ 16 kHz
    this.ratio = sampleRate / this.targetRate; // device rate ÷ 16k
    this.resamplePos = 0;
    this.acc = new Int16Array(this.frameSamples);
    this.accLen = 0;
    // rolling short-term energy for onset detection
    this.energy = 0;
    this.speaking = false; // set by the main thread when the agent is speaking
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === "speaking") this.speaking = !!e.data.on;
    };
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch = input[0]; // mono (first channel)
    if (!ch) return true;

    // linear-resample device rate → 16 kHz
    for (let i = 0; i < ch.length; i++) {
      this.resamplePos -= 1;
      if (this.resamplePos <= 0) {
        const s = Math.max(-1, Math.min(1, ch[i]));
        this.acc[this.accLen++] = (s * 32767) | 0;
        // energy accumulation for onset detection
        this.energy = this.energy * 0.9 + s * s * 0.1;
        this.resamplePos += this.ratio;
        if (this.accLen === this.frameSamples) {
          // ship a 640-byte PCM16 frame
          this.port.postMessage(
            { type: "frame", pcm: this.acc.slice().buffer },
            [this.acc.slice().buffer],
          );
          this.accLen = 0;
          // local barge-in: while the agent is speaking, a burst of energy on
          // the AEC-cleaned mic means the user is talking over it.
          if (this.speaking && this.energy > 0.0025) {
            this.port.postMessage({ type: "barge" });
          }
        }
      }
    }
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
