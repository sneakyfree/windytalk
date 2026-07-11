// Watcher tier-matrix tests (contract resurrection.heartbeat.staleness_tiers +
// service_backoff) — the slice-0 acceptance criteria as passing measurements.
// Pure logic: injected clock, fs, proc identity, kill, relaunch.
import assert from "node:assert/strict";
import { test } from "node:test";

import type { IdentityRecord, LiveIdentity } from "../electron/control/identity.js";
import {
  checkOnce,
  type BackoffState,
  type WatcherDeps,
} from "../electron/resurrection/watcher.js";

const EXE = "/opt/windytalk/windytalk";
const REC: IdentityRecord = { pid: 4242, started_at: 1_000_000, exe: EXE };

interface HarnessOpts {
  /** null = heartbeat file absent; otherwise heartbeat age in seconds. */
  ageS: number | null;
  record?: IdentityRecord | null;
  live?: LiveIdentity; // what procIdentity(record.pid) reports
  writable?: boolean;
  state?: BackoffState;
  saveOk?: boolean;
  hasSpec?: boolean;
}

function harness(opts: HarnessOpts) {
  const nowMs = 10_000_000_000;
  const killed: number[] = [];
  const relaunches: string[] = [];
  const notices: string[] = [];
  let state: BackoffState = opts.state ?? { relaunches: [], slow: false, freshSince: null };
  const deps: WatcherDeps = {
    heartbeatPath: "/x/heartbeat",
    now: () => nowMs,
    readHb: () =>
      opts.ageS === null
        ? null
        : { record: opts.record === undefined ? REC : opts.record, mtimeMs: nowMs - opts.ageS * 1000 },
    getIdentity: async () =>
      opts.live ?? { alive: true, exe: EXE, started_at: REC.started_at },
    kill: (pid) => killed.push(pid),
    relaunch: () => {
      if (opts.hasSpec === false) return false;
      relaunches.push("relaunch");
      return true;
    },
    probeWritable: () => opts.writable ?? true,
    notify: (title) => notices.push(title),
    loadState: () => state,
    saveState: (s) => {
      if (opts.saveOk === false) return false;
      state = s;
      return true;
    },
  };
  return { deps, killed, relaunches, notices, nowS: nowMs / 1000, state: () => state };
}

// identityMatches uses realpath on the record exe; use a live that mismatches
// by pid-dead instead of path when we mean "gone".
const DEAD: LiveIdentity = { alive: false, exe: null, started_at: null };
const RECYCLED: LiveIdentity = { alive: true, exe: "/usr/bin/firefox", started_at: 2_000_000 };

test("fresh heartbeat: healthy, nothing killed, nothing relaunched", async () => {
  const h = harness({ ageS: 10 });
  assert.equal(await checkOnce(h.deps), "healthy");
  assert.deepEqual(h.killed, []);
  assert.deepEqual(h.relaunches, []);
});

test("tier1: heartbeat file ABSENT -> relaunch immediately (restart_app fast path)", async () => {
  const h = harness({ ageS: null });
  assert.equal(await checkOnce(h.deps), "relaunch");
  assert.deepEqual(h.relaunches, ["relaunch"]);
  assert.deepEqual(h.killed, []);
});

test("tier1: stale >30s + pid dead -> relaunch, nobody killed", async () => {
  const h = harness({ ageS: 31, live: DEAD });
  assert.equal(await checkOnce(h.deps), "relaunch");
  assert.deepEqual(h.killed, []);
});

test("SAFETY-INVERSE: recycled pid (exe/start mismatch) is ABSENT -> relaunch at the 45s budget, innocent NEVER killed", async () => {
  // Even deep past the tier-2 threshold, a mismatched identity must never be
  // SIGKILLed (round-5 pin) — it relaunches via tier1 instead.
  for (const ageS of [31, 44, 120, 500]) {
    const h = harness({ ageS, live: RECYCLED });
    assert.equal(await checkOnce(h.deps), "relaunch", `age ${ageS}`);
    assert.deepEqual(h.killed, [], `age ${ageS}: innocent process was killed`);
  }
});

test("tier1: unparseable heartbeat content = identity unknown -> treated absent, never killed", async () => {
  const h = harness({ ageS: 40, record: null });
  assert.equal(await checkOnce(h.deps), "relaunch");
  assert.deepEqual(h.killed, []);
});

test("grace zone: stale 30-90s with live-by-identity pid -> wait", async () => {
  const h = harness({ ageS: 60 });
  assert.equal(await checkOnce(h.deps), "wait");
  assert.deepEqual(h.killed, []);
  assert.deepEqual(h.relaunches, []);
});

test("SAFETY-INVERSE: healthy holder (fresh heartbeat) is never signaled regardless of pid state", async () => {
  const h = harness({ ageS: 4, live: RECYCLED });
  assert.equal(await checkOnce(h.deps), "healthy");
  assert.deepEqual(h.killed, []);
});

test("tier2: stale >90s + live pid + writable disk = genuine wedge -> SIGKILL then relaunch", async () => {
  const h = harness({ ageS: 91 });
  assert.equal(await checkOnce(h.deps), "kill-relaunch");
  assert.deepEqual(h.killed, [4242]);
  assert.deepEqual(h.relaunches, ["relaunch"]);
});

test("tier2: a bare :8782 accept cannot veto the kill — the decision never consults any port signal", async () => {
  // The deps surface has no port-probe hook AT ALL (by construction, per the
  // round-4 pin): writable + stale-past-90s + live pid kills, full stop.
  const h = harness({ ageS: 120, writable: true });
  assert.equal(await checkOnce(h.deps), "kill-relaunch");
  assert.deepEqual(h.killed, [4242]);
});

test("tier2 disk-full: stale >90s + live pid + UNWRITABLE dir -> OS notification, NO kill, NO relaunch", async () => {
  const h = harness({ ageS: 120, writable: false });
  assert.equal(await checkOnce(h.deps), "disk-full");
  assert.deepEqual(h.killed, []);
  assert.deepEqual(h.relaunches, []);
  assert.equal(h.notices.length, 1);
});

test("service_backoff: 3 relaunches in 300s, then the 4th defers to the 5-min cadence", async () => {
  const h = harness({ ageS: null });
  const t = h.nowS;
  h.deps.loadState = () => ({
    relaunches: [t - 40, t - 25, t - 10],
    slow: false,
    freshSince: null,
  });
  assert.equal(await checkOnce(h.deps), "backoff");
  assert.deepEqual(h.relaunches, []);
  assert.equal(h.state().slow, true, "ceiling trip must be sticky");
});

test("service_backoff: slow mode allows 1 attempt once 300s have passed since the last", async () => {
  const h = harness({ ageS: null });
  const t = h.nowS;
  h.deps.loadState = () => ({
    relaunches: [t - 700, t - 650, t - 600, t - 301],
    slow: true,
    freshSince: null,
  });
  assert.equal(await checkOnce(h.deps), "relaunch");
  assert.deepEqual(h.relaunches, ["relaunch"]);
});

test("service_backoff: slow mode blocks attempts inside the 5-min gap (crash-at-boot cannot thrash every 15s)", async () => {
  const h = harness({ ageS: null });
  const t = h.nowS;
  h.deps.loadState = () => ({
    relaunches: [t - 700, t - 650, t - 600, t - 100],
    slow: true,
    freshSince: null,
  });
  assert.equal(await checkOnce(h.deps), "backoff");
  assert.deepEqual(h.relaunches, []);
});

test("service_backoff: counter resets after 300s of continuous fresh heartbeat", async () => {
  const h = harness({ ageS: 5 });
  const t = h.nowS;
  h.deps.loadState = () => ({
    relaunches: [t - 900, t - 880, t - 860],
    slow: true,
    freshSince: t - 301,
  });
  assert.equal(await checkOnce(h.deps), "healthy");
  assert.deepEqual(h.state().relaunches, []);
  assert.equal(h.state().slow, false);
});

test("service_backoff: a fresh streak shorter than 300s does NOT reset the counter", async () => {
  const h = harness({ ageS: 5 });
  const t = h.nowS;
  h.deps.loadState = () => ({
    relaunches: [t - 900],
    slow: true,
    freshSince: t - 100,
  });
  assert.equal(await checkOnce(h.deps), "healthy");
  assert.equal(h.state().slow, true);
  assert.equal(h.state().relaunches.length, 1);
});

test("fail-safe: unwritable backoff state -> notify + stand down (no unbounded thrash past the ceiling)", async () => {
  const h = harness({ ageS: null, saveOk: false });
  assert.equal(await checkOnce(h.deps), "state-unwritable");
  assert.deepEqual(h.relaunches, []);
  assert.equal(h.notices.length, 1);
});

test("no relaunch spec -> honest 'no-spec', never a guessed process-name launch", async () => {
  const h = harness({ ageS: null, hasSpec: false });
  assert.equal(await checkOnce(h.deps), "no-spec");
});

test("45s budget: worst case (heartbeat bumped at T0, SIGKILL at T0, ticks every 15s) relaunches by T0+45", async () => {
  // Staleness crosses 30s at T0+30; the next 15s tick is at T0+45 at the latest.
  // Verify the decision is already RELAUNCH for every age in (30, 45].
  for (const ageS of [30.5, 37, 45]) {
    const h = harness({ ageS, live: DEAD });
    assert.equal(await checkOnce(h.deps), "relaunch", `age ${ageS}s must relaunch`);
  }
  // And at exactly 30s it must still wait (mtime > 30s is strict).
  const h = harness({ ageS: 30, live: DEAD });
  assert.equal(await checkOnce(h.deps), "healthy");
});
