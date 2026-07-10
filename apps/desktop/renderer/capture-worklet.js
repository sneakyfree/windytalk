// AudioWorklet capture processor (voice-session.v1 §4).
//
// Runs in the audio render thread. Resamples the device rate → 16 kHz with a
// box-average low-pass (not naive sample-drop — that aliased HF into the speech
// band), emits exactly-20 ms (320-sample) PCM16 mono frames, and runs a cheap
// energy speech-onset detector so the main thread can fire a local barge-in (§7.1)
// while the agent is speaking. MediaRecorder is forbidden (§4.2). Echo cancellation
// is requested on the getUserMedia track in the renderer (§4.1).

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.frameSamples = 320; // 20 ms @ 16 kHz
    this.ratio = sampleRate / this.targetRate; // device rate ÷ 16k (samples per output)
    this.acc = new Int16Array(this.frameSamples);
    this.accLen = 0;
    // box-average decimation state
    this.bucket = 0;
    this.bucketSum = 0;
    this.bucketCount = 0;
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

    for (let i = 0; i < ch.length; i++) {
      // accumulate device samples into the current output-sample bucket, then
      // average when the bucket fills (crude but real anti-alias vs sample-drop)
      this.bucketSum += ch[i];
      this.bucketCount++;
      this.bucket += 1;
      if (this.bucket >= this.ratio) {
        this.bucket -= this.ratio;
        const avg = this.bucketCount > 0 ? this.bucketSum / this.bucketCount : 0;
        this.bucketSum = 0;
        this.bucketCount = 0;
        const s = Math.max(-1, Math.min(1, avg));
        this.acc[this.accLen++] = (s * 32767) | 0;
        this.energy = this.energy * 0.9 + s * s * 0.1;
        if (this.accLen === this.frameSamples) {
          const buf = this.acc.slice().buffer; // one buffer: transferred AND carried
          this.port.postMessage({ type: "frame", pcm: buf }, [buf]);
          this.accLen = 0;
          // local barge-in: energy on the AEC-cleaned mic while the agent speaks
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
