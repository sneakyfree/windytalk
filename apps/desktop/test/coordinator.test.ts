// Recovery-coordinator tests (contract recovery_coordinator) — every number is
// pinned there; these are the acceptance criteria as measurements.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  RecoveryCoordinator,
  DEBOUNCE_MS,
  LOCK_CEILING_MS,
} from "../electron/control/coordinator.js";

function clock(startMs = 1_000_000) {
  let t = startMs;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

test("ACCEPTANCE: reconnect ×50 in a tight loop -> ≤5 execute, every other call rate_limited OR already_recovering", async () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  let executed = 0;
  const outcomes: string[] = [];
  // 50 calls over ~25 s (the "tight loop"); each executing call holds the lock
  // for 1 s (a fast reconnect), so some calls land inside the lock window.
  for (let i = 0; i < 50; i++) {
    const gate = coord.gate("reconnect");
    if (gate.proceed) {
      gate.ticket.commit();
      executed++;
      outcomes.push("executed");
      c.advance(1_000); // handler runs 1 s while holding the lock
      gate.ticket.release();
    } else {
      outcomes.push(gate.error);
      c.advance(500);
    }
  }
  assert.ok(executed <= 5, `executed ${executed} > 5`);
  assert.ok(executed >= 1);
  for (const o of outcomes) {
    assert.ok(["executed", "rate_limited", "already_recovering"].includes(o), o);
  }
});

test("debounce: same tool+args within 5 s -> rate_limited; a REJECTED call does not start a new window", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  const g1 = coord.gate("run_selftest");
  assert.ok(g1.proceed);
  g1.ticket.commit();
  g1.ticket.release();
  c.advance(3_000);
  const g2 = coord.gate("run_selftest");
  assert.ok(!g2.proceed && g2.error === "rate_limited");
  c.advance(DEBOUNCE_MS - 3_000 + 1); // 5 s past the EXECUTED call (not the rejected one)
  const g3 = coord.gate("run_selftest");
  assert.ok(g3.proceed, "the rejected call must not have restarted the debounce window");
});

test("debounce key includes args: set_audio_input(A) then (B) is NOT debounced (the try-the-other-mic flow)", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  const gA = coord.gate("set_audio_input", { device_id: "A" });
  assert.ok(gA.proceed);
  gA.ticket.commit();
  c.advance(100);
  const b = coord.gate("set_audio_input", { device_id: "B" });
  assert.ok(b.proceed, "different args = different debounce key");
  c.advance(100);
  const a2 = coord.gate("set_audio_input", { device_id: "A" });
  assert.ok(!a2.proceed && a2.error === "rate_limited");
});

test("ceiling: 5 executed per tool per rolling 300 s; window slides", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  for (let i = 0; i < 5; i++) {
    const g = coord.gate("run_selftest");
    assert.ok(g.proceed, `call ${i} should execute`);
    g.ticket.commit();
    g.ticket.release();
    c.advance(10_000);
  }
  const over = coord.gate("run_selftest");
  assert.ok(!over.proceed && over.error === "rate_limited");
  c.advance(260_001); // first executed call slides out of the 300 s window
  const again = coord.gate("run_selftest");
  assert.ok(again.proceed, "the rolling window must free budget as calls age out");
});

test("lock: a holder invoked while held fails FAST with already_recovering (never queues)", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  const g1 = coord.gate("reconnect");
  assert.ok(g1.proceed);
  const g2 = coord.gate("restart_engine");
  assert.ok(!g2.proceed && g2.error === "already_recovering");
  g1.ticket.release();
});

test("lock: config set_* tools don't take the lock but are BLOCKED while held", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  const held = coord.gate("reconnect");
  assert.ok(held.proceed);
  const cfg = coord.gate("set_volume", { level: 50 });
  assert.ok(!cfg.proceed && cfg.error === "already_recovering");
  held.ticket.release();
  const cfg2 = coord.gate("set_volume", { level: 50 });
  assert.ok(cfg2.proceed, "set_* takes no lock, so it proceeds once the lock is free");
  const parallelCfg = coord.gate("set_wake_mode", { hands_free: false });
  assert.ok(parallelCfg.proceed, "config tools take no lock (two dials can't deadlock)");
});

test("ACCEPTANCE: enter_safe_mode PREEMPTS a held lock — the escape hatch is never blocked", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  const stuck = coord.gate("reconnect"); // never releases (a hung handler)
  assert.ok(stuck.proceed);
  const esc = coord.gate("enter_safe_mode");
  assert.ok(esc.proceed, "enter_safe_mode must reclaim the lock");
  assert.ok(stuck.proceed && stuck.ticket.abandoned, "the preempted handler is abandoned");
  esc.ticket.release();
  // The abandoned handler's late release must not free the NEW holder's lock.
  const post = coord.gate("restart_engine");
  assert.ok(post.proceed, "lock must be free after the preemptor released");
  stuck.ticket.release(); // late no-op
  const post2 = coord.gate("clear_cache");
  assert.ok(!post2.proceed && post2.error === "already_recovering", "restart_engine still holds");
  post.ticket.release();
});

test("lock ceiling: a stuck holder auto-releases at 30 s — no permanent deadlock", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  const stuck = coord.gate("restart_engine");
  assert.ok(stuck.proceed);
  c.advance(LOCK_CEILING_MS - 1);
  assert.equal(coord.gate("reconnect").proceed, false);
  c.advance(2);
  const freed = coord.gate("reconnect");
  assert.ok(freed.proceed, "the 30 s ceiling must have released the stuck lock");
  freed.ticket.release();
});

test("exempt reads: never locked, never rate-limited — 100× get_health during a held lock all proceed", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  const held = coord.gate("reconnect");
  assert.ok(held.proceed);
  for (let i = 0; i < 100; i++) {
    assert.ok(coord.gate("get_health").proceed, `get_health call ${i} must be exempt`);
  }
  for (const t of ["get_status", "get_config", "get_logs", "list_audio_devices", "get_capabilities", "check_for_update"]) {
    assert.ok(coord.gate(t).proceed, `${t} is exempt BY NAME`);
  }
  held.ticket.release();
});

test("run_selftest + repair_resurrection: NOT blocked by a held lock, but rate-limited", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  const held = coord.gate("reconnect");
  assert.ok(held.proceed);
  const st = coord.gate("run_selftest");
  assert.ok(st.proceed, "run_selftest is lock-exempt");
  st.ticket.commit();
  const rr = coord.gate("repair_resurrection");
  assert.ok(rr.proceed, "repair_resurrection is not lock-blocked");
  rr.ticket.commit();
  c.advance(1_000);
  const st2 = coord.gate("run_selftest");
  assert.ok(!st2.proceed && st2.error === "rate_limited", "but it IS debounced");
  held.ticket.release();
});

test("layer1 exemption: Layer 1's calls go through the lock but are never charged debounce/ceiling", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  // Exhaust the surface budget for enter_safe_mode.
  for (let i = 0; i < 5; i++) {
    const g = coord.gate("enter_safe_mode");
    assert.ok(g.proceed);
    g.ticket.commit();
    g.ticket.release();
    c.advance(6_000);
  }
  assert.equal(coord.gate("enter_safe_mode").proceed, false, "surface budget exhausted");
  // Layer 1's crash-loop trip MUST still fire (the exact failure the exemption stops).
  const trip = coord.gate("enter_safe_mode", {}, { layer1: true });
  assert.ok(trip.proceed, "Layer 1's trip must never be rate_limited mid-thrash");
  trip.ticket.release();
  // And Layer 1's reconnect is unbounded: 20 back-to-back attempts all pass.
  for (let i = 0; i < 20; i++) {
    const g = coord.gate("reconnect", {}, { layer1: true });
    assert.ok(g.proceed, `layer1 reconnect ${i}`);
    g.ticket.release();
  }
});

test("layer1 executed calls do not charge the SURFACE budget", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  for (let i = 0; i < 10; i++) {
    const g = coord.gate("reconnect", {}, { layer1: true });
    assert.ok(g.proceed);
    g.ticket.release();
  }
  const surface = coord.gate("reconnect");
  assert.ok(surface.proceed, "the surface's own 5/300s budget must be untouched by Layer 1");
  surface.ticket.release();
});

test("recovering flag: true while a lock is held (drives get_health.mode)", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  assert.equal(coord.recovering, false);
  const g = coord.gate("reconnect");
  assert.ok(g.proceed);
  assert.equal(coord.recovering, true);
  assert.equal(coord.lockHolderTool, "reconnect");
  g.ticket.release();
  assert.equal(coord.recovering, false);
});

test("a DENIED call never charges the executed counters (commit is post-confirmer)", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  // Gate passes but the user denies -> no commit -> no debounce window starts.
  const g1 = coord.gate("restart_engine");
  assert.ok(g1.proceed);
  g1.ticket.release(); // denied: released without commit
  const g2 = coord.gate("restart_engine");
  assert.ok(g2.proceed, "an uncommitted (denied) call must not debounce the next one");
  g2.ticket.commit();
  g2.ticket.release();
  const g3 = coord.gate("restart_engine");
  assert.ok(!g3.proceed && g3.error === "rate_limited", "the EXECUTED call does debounce");
});
