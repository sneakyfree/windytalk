// Wake-gate tests — the "Hey Windy" hands-free state machine (genome 1.6).
// Pure logic, driven by an injected fake detector + controllable clock.
import assert from "node:assert/strict";
import { test } from "node:test";

import { framePcm16, WakeGate, type WakeDetector } from "../src/wake.js";

const FRAME = 320; // samples per 20 ms mic frame

// A fake detector whose score we control; records reset() calls.
class FakeDetector implements WakeDetector {
  score = 0;
  resets = 0;
  predict(_chunk: Int16Array): number {
    return this.score;
  }
  reset(): void {
    this.resets++;
  }
}

function frame(fill = 0): Int16Array {
  return new Int16Array(FRAME).fill(fill);
}

test("asleep: nothing is forwarded and a quiet stream never wakes", () => {
  const det = new FakeDetector(); // score stays 0
  const gate = new WakeGate(det, { now: () => 0 });
  for (let i = 0; i < 20; i++) {
    const r = gate.feed(frame(), false);
    assert.equal(r.forward, false);
    assert.equal(r.transition, null);
  }
  assert.equal(gate.isAwake, false);
});

test("wake fires when the detector clears threshold on a full 80 ms window", () => {
  const det = new FakeDetector();
  const gate = new WakeGate(det, { threshold: 0.5, now: () => 1000 });
  det.score = 0.9;
  // 4 × 320-sample frames = one 1280-sample detector window.
  let transitions: (string | null)[] = [];
  for (let i = 0; i < 4; i++) transitions.push(gate.feed(frame(), false).transition);
  assert.ok(transitions.includes("wake"));
  assert.equal(gate.isAwake, true);
  assert.equal(det.resets, 1); // reset after a confirmed wake
});

test("awake: real frames forward, then sleep after the grace window of quiet", () => {
  const det = new FakeDetector();
  let t = 0;
  const gate = new WakeGate(det, { threshold: 0.5, graceMs: 8000, now: () => t });
  det.score = 0.9;
  for (let i = 0; i < 4; i++) gate.feed(frame(), false); // wake at t=0
  det.score = 0.0; // stop matching; detector irrelevant while awake
  t = 100;
  assert.equal(gate.feed(frame(), false).forward, true); // still inside grace
  t = 9000; // past 8 s of quiet
  const r = gate.feed(frame(), false);
  assert.equal(r.transition, "sleep");
  assert.equal(r.forward, false);
  assert.equal(gate.isAwake, false);
});

test("awake stays open while the agent is speaking (grace keeps resetting)", () => {
  const det = new FakeDetector();
  let t = 0;
  const gate = new WakeGate(det, { graceMs: 1000, now: () => t });
  det.score = 0.9;
  for (let i = 0; i < 4; i++) gate.feed(frame(), false);
  det.score = 0;
  for (t = 500; t < 20000; t += 500) {
    // speaking=true past many grace windows → never sleeps
    assert.equal(gate.feed(frame(), true).forward, true);
  }
  assert.equal(gate.isAwake, true);
});

test("loud speech keeps the window open even without the speaking flag", () => {
  const det = new FakeDetector();
  let t = 0;
  const gate = new WakeGate(det, { graceMs: 1000, now: () => t });
  det.score = 0.9;
  for (let i = 0; i < 4; i++) gate.feed(frame(), false);
  det.score = 0;
  t = 900;
  gate.feed(frame(12000), false); // loud (RMS > 0.02) → resets grace
  t = 1500; // would be past the original grace, but the loud frame extended it
  assert.equal(gate.feed(frame(12000), false).forward, true);
});

test("framePcm16 views bytes as little-endian int16 without copying data", () => {
  const bytes = new Uint8Array([0x00, 0x01, 0x00, 0xff]); // 256, -256
  const s = framePcm16(bytes);
  assert.equal(s.length, 2);
  assert.equal(s[0], 256);
  assert.equal(s[1], -256);
});
