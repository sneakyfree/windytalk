// AudioWorklet capture processor (voice-session.v1 §4).
//
// Runs in the audio render thread. Resamples the device rate → 16 kHz with a
// box-average low-pass (not naive sample-drop — that aliased HF into the speech
// band), emits exactly-20 ms (320-sample) PCM16 mono frames, and runs a cheap
// energy speech-onset detector so the main thread can fire a local barge-in (§7.1)
// while the agent is speaking. MediaRecorder is forbidden (§4.2). Echo cancellation
// is requested on the getUserMedia track in the renderer (§4.1).

const BARGE_ENERGY = 0.0025;   // per-frame energy bar
const BARGE_HOT_FRAMES = 4;    // consecutive 20 ms frames = 80 ms sustained (§7 rule 1)

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
    // Consecutive 20 ms frames over the threshold. Speech SUSTAINS; a glass set on
    // the desk, a chair creak or a cough is a transient that clears the bar for one
    // frame and is gone. Firing on a single frame is what cut Windy off 7 times in
    // the 2026-07-29 session at 0.2-0.7s into her sentence, after which Grant did
    // not speak for another 14-155 seconds — he had put a glass down.
    // BARGE_HOT_FRAMES * 20 ms = 80 ms, which is exactly the detection budget §7
    // rule 1 already allows ("SHOULD trigger within 80 ms of speech onset"), so this
    // costs nothing against the contract.
    this.hot = 0;
    this.speaking = false; // set by the main thread when the agent is speaking
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === "speaking") {
        this.speaking = !!e.data.on;
        this.hot = 0; // a new speaking phase starts with a clean tally
      }
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
          // local barge-in: SUSTAINED energy on the AEC-cleaned mic while the agent
          // speaks. One loud frame is furniture; 80 ms of it is a person.
          if (this.speaking) {
            this.hot = this.energy > BARGE_ENERGY ? this.hot + 1 : 0;
            if (this.hot === BARGE_HOT_FRAMES) {
              this.port.postMessage({ type: "barge" });
            }
          } else {
            this.hot = 0;
          }
        }
      }
    }
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
