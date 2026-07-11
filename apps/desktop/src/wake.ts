// "Hey Windy" wake gate (client side, local — voice-session.v1 §0 / genome 1.6).
//
// Hands-free mode: when armed, the client keeps the mic open locally but sends
// NOTHING to the engine until it hears the wake word on-device. On a wake it
// forwards real mic frames so you can give a command, and drifts back to sleep
// after a few seconds of quiet. Wake detection is local, so no audio leaves the
// machine until you've said "Hey Windy" — the privacy property from reference/wake.py.
//
// The state machine here is PURE and unit-tested with an injected detector +
// clock (the same pattern the engine VAD uses for is_speech). The real detector
// is an onnxruntime-web model (openWakeWord) loaded via loadWakeDetector(); it is
// a drop-in for the WakeDetector interface. Until the "hey_windy" model is trained
// (Grant-gated) and bundled, loadWakeDetector() returns null and the app stays in
// push-to-talk — honest, not a fake pass.

// openWakeWord consumes 80 ms windows @ 16 kHz = 1280 samples.
const WAKE_CHUNK_SAMPLES = 1280;
const RMS_KEEPALIVE = 0.02; // loudness that keeps the awake window open

// Accept both owned buffers and zero-copy views over mic-frame bytes.
export type Samples = Int16Array<ArrayBufferLike>;

export interface WakeDetector {
  // Score 0..1 that the wake word is present in this 1280-sample (80 ms) chunk.
  predict(chunk: Samples): number;
  // Reset internal state after a confirmed wake (avoid immediate re-trigger).
  reset(): void;
}

export interface WakeOpts {
  threshold?: number; // wake when predict() exceeds this (default 0.5)
  graceMs?: number; // stay awake this long after the last speech (default 8000)
  now?: () => number; // injectable clock (ms) for tests
}

export type WakeTransition = "wake" | "sleep" | null;

export interface WakeResult {
  forward: boolean; // should this frame be sent to the engine?
  transition: WakeTransition; // set on the frame that flips asleep⇄awake
}

// Convert a raw mic frame (bytes) to Int16 samples without copying the buffer.
export function framePcm16(frame: Uint8Array): Samples {
  // A frame is 640 bytes = 320 samples; view it as Int16 (respect byteOffset).
  return new Int16Array(frame.buffer, frame.byteOffset, frame.byteLength >> 1);
}

function rms(samples: Samples): number {
  if (samples.length === 0) return 0;
  let ss = 0;
  for (let i = 0; i < samples.length; i++) {
    const v = samples[i] / 32768;
    ss += v * v;
  }
  return Math.sqrt(ss / samples.length);
}

export class WakeGate {
  private detector: WakeDetector;
  private threshold: number;
  private graceMs: number;
  private now: () => number;
  private awake = false;
  private awakeUntil = 0;
  // pending samples awaiting a full 1280-sample detector window
  private buf: Samples = new Int16Array(0);

  constructor(detector: WakeDetector, opts: WakeOpts = {}) {
    this.detector = detector;
    this.threshold = opts.threshold ?? 0.5;
    this.graceMs = opts.graceMs ?? 8000;
    this.now = opts.now ?? (() => Date.now());
  }

  get isAwake(): boolean {
    return this.awake;
  }

  // Feed one mic frame; decide whether it should reach the engine.
  // `speaking` = the agent is currently talking (keeps the window open so a
  // reply doesn't get cut by the grace timer).
  feed(samples: Samples, speaking: boolean): WakeResult {
    const now = this.now();
    if (!this.awake) {
      // Accumulate into 1280-sample windows and run the detector on each.
      this.buf = concat(this.buf, samples);
      while (this.buf.length >= WAKE_CHUNK_SAMPLES) {
        const chunk = this.buf.subarray(0, WAKE_CHUNK_SAMPLES);
        this.buf = new Int16Array(this.buf.subarray(WAKE_CHUNK_SAMPLES));
        if (this.detector.predict(chunk) > this.threshold) {
          this.wakeNow(now);
          return { forward: true, transition: "wake" };
        }
      }
      return { forward: false, transition: null }; // asleep: send nothing
    }
    // Awake: keep the window open while the user (or Windy) is talking.
    if (speaking || rms(samples) > RMS_KEEPALIVE) {
      this.awakeUntil = now + this.graceMs;
    }
    if (now > this.awakeUntil) {
      this.awake = false;
      return { forward: false, transition: "sleep" };
    }
    return { forward: true, transition: null };
  }

  private wakeNow(now: number): void {
    this.awake = true;
    this.awakeUntil = now + this.graceMs;
    this.buf = new Int16Array(0);
    this.detector.reset();
  }

  // Force back to sleep (e.g. the user turned hands-free off).
  sleep(): void {
    this.awake = false;
    this.awakeUntil = 0;
    this.buf = new Int16Array(0);
  }
}

function concat(a: Samples, b: Samples): Samples {
  if (a.length === 0) return b.slice();
  const out = new Int16Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

// Load the local wake detector, or null if no model is bundled yet.
//
// The real implementation loads the openWakeWord melspectrogram + embedding +
// "hey_windy" classifier ONNX models through onnxruntime-web. That model is
// trained on the 5090 (wakeword/ recipe) and is Grant-gated; until it ships,
// this returns null so the app runs in push-to-talk mode rather than pretending
// hands-free works. Wiring the onnx models here is the only remaining step once
// hey_windy.onnx exists — the WakeGate above needs no change.
export async function loadWakeDetector(): Promise<WakeDetector | null> {
  return null;
}
