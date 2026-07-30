// Layer 1's degradation sense. Every failure that actually reached Grant in the
// 2026-07 hand tests was a slowdown, not a crash — and the crash-loop detector is
// structurally blind to all of them.
import assert from "node:assert/strict";
import test from "node:test";

import {
  BASELINE_MIN_SAMPLES,
  SlownessDetector,
} from "../electron/control/slowness.js";

function make(nowRef: { t: number }) {
  const asked: string[] = [];
  const d = new SlownessDetector({
    now: () => nowRef.t,
    requestRecovery: (r) => asked.push(r),
  });
  return { d, asked };
}

/** Feed n healthy turns to establish the baseline. */
function warmUp(d: SlownessDetector, ms = 4500, n = BASELINE_MIN_SAMPLES) {
  for (let i = 0; i < n; i++) d.noteTurn(ms);
}

test("says nothing until it has earned a baseline", () => {
  const nowRef = { t: 0 };
  const { d, asked } = make(nowRef);
  for (let i = 0; i < BASELINE_MIN_SAMPLES - 1; i++) d.noteTurn(90_000);
  assert.equal(asked.length, 0, "asked for recovery before knowing what normal is");
  assert.equal(d.baselineMs(), 0);
});

test("the baseline is measured, not hardcoded", () => {
  // A 5090 and a laptop must each measure themselves; nobody's number is universal.
  const nowRef = { t: 0 };
  const { d } = make(nowRef);
  warmUp(d, 4500);
  assert.equal(d.baselineMs(), 4500);

  const slow = make({ t: 0 });
  warmUp(slow.d, 9000);
  assert.equal(slow.d.baselineMs(), 9000);
});

test("the real failure: healthy for hours, then 2.9x slower — asks for recovery", () => {
  // The measured 2026-07-29 shape EXACTLY: 4.5s fresh, 13.2s after 13 hours of
  // uptime. That is 2.93x — a 3x threshold would have missed it entirely.
  const nowRef = { t: 0 };
  const { d, asked } = make(nowRef);
  warmUp(d, 4500);
  assert.equal(asked.length, 0);

  for (let i = 0; i < 3; i++) d.noteTurn(13_200);
  assert.equal(asked.length, 1, "sat through a 3x slowdown without asking for help");
  assert.match(asked[0], /slower than normal/);
  assert.equal(d.isDegraded(), true);
});

test("a fast baseline cannot trip on ordinary variance", () => {
  // 3x of a 1.5s baseline is 4.5s — a perfectly normal turn. The absolute floor
  // stops a quick machine from restarting itself over nothing.
  const nowRef = { t: 0 };
  const { d, asked } = make(nowRef);
  warmUp(d, 1500);
  for (let i = 0; i < 3; i++) d.noteTurn(5_000);
  assert.equal(asked.length, 0, "tripped on a turn that was not actually slow");
});

test("one slow turn is not a trend", () => {
  const nowRef = { t: 0 };
  const { d, asked } = make(nowRef);
  warmUp(d, 4500);
  d.noteTurn(60_000);          // a single stall — a big shell command, a cold model
  d.noteTurn(4_600);
  d.noteTurn(4_400);
  assert.equal(asked.length, 0, "restarted over one slow turn");
});

test("does not nag: one recovery request per cooldown", () => {
  const nowRef = { t: 0 };
  const { d, asked } = make(nowRef);
  warmUp(d, 4500);
  for (let i = 0; i < 12; i++) d.noteTurn(20_000);
  assert.equal(asked.length, 1, "asked repeatedly inside the cooldown");

  nowRef.t += 300_001;                       // past the cooldown, still slow
  for (let i = 0; i < 3; i++) d.noteTurn(20_000);
  assert.equal(asked.length, 2, "never asked again after the cooldown expired");
});

test("recovering clears the flag but keeps the earned baseline", () => {
  const nowRef = { t: 0 };
  const { d } = make(nowRef);
  warmUp(d, 4500);
  for (let i = 0; i < 3; i++) d.noteTurn(20_000);
  assert.equal(d.isDegraded(), true);

  d.resetAfterRecovery();
  assert.equal(d.isDegraded(), false);
  assert.equal(d.baselineMs(), 4500, "threw away what it had learned about this machine");
});

test("garbage samples are ignored", () => {
  const nowRef = { t: 0 };
  const { d, asked } = make(nowRef);
  for (const bad of [0, -1, NaN, Infinity]) d.noteTurn(bad as number);
  assert.equal(d.baselineMs(), 0);
  assert.equal(asked.length, 0);
});
