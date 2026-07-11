// Layer-1 crash-loop detector tests (contract crash_loop) + config/safe-mode
// overlay (contract safe_mode) + the engine allow-list host-pin discipline
// (contract security.engine_allow_list).
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { CrashLoopDetector, TRIP_WINDOW_MS, RESET_HEALTHY_MS } from "../electron/control/layer1.js";
import { ConfigStore, FACTORY_CONFIG } from "../electron/control/config.js";
import { EngineAllowList } from "../electron/control/engine-allow.js";

function clock(startMs = 5_000_000) {
  let t = startMs;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

// -- crash_loop ------------------------------------------------------------------

test("ACCEPTANCE: 3 restarts within 120 s -> safe-mode trip fires exactly once (no zombie loop)", () => {
  const c = clock();
  const trips: string[] = [];
  const d = new CrashLoopDetector({ now: c.now, tripSafeMode: (r) => trips.push(r) });
  d.recordRestart("engine");
  c.advance(30_000);
  d.recordRestart("engine");
  assert.equal(trips.length, 0, "two restarts must not trip");
  c.advance(30_000);
  d.recordRestart("engine");
  assert.equal(trips.length, 1, "the third restart in the window trips");
  assert.equal(d.crashLoop, true);
  // Further thrash must NOT re-trip (already in safe mode; stop, don't churn).
  c.advance(5_000);
  d.recordRestart("engine");
  assert.equal(trips.length, 1);
  assert.equal(d.restarts, 4, "get_health.restarts counts all restarts since supervisor start");
});

test("restarts spread wider than 120 s never trip", () => {
  const c = clock();
  const trips: string[] = [];
  const d = new CrashLoopDetector({ now: c.now, tripSafeMode: (r) => trips.push(r) });
  for (let i = 0; i < 6; i++) {
    d.recordRestart("engine");
    c.advance(TRIP_WINDOW_MS + 1_000);
  }
  assert.equal(trips.length, 0);
});

test("counter resets after 300 s of continuous healthy uptime; crash_loop clears", () => {
  const c = clock();
  const d = new CrashLoopDetector({ now: c.now, tripSafeMode: () => {} });
  d.recordRestart("a");
  d.recordRestart("b");
  d.recordRestart("c"); // tripped
  assert.equal(d.crashLoop, true);
  d.observeHealthy(true);
  c.advance(RESET_HEALTHY_MS - 1);
  d.observeHealthy(true);
  assert.equal(d.crashLoop, true, "not yet — the streak is 1 ms short");
  c.advance(2);
  d.observeHealthy(true);
  assert.equal(d.crashLoop, false, "300 s continuous healthy resets the counter");
});

test("an unhealthy blip restarts the healthy streak", () => {
  const c = clock();
  const d = new CrashLoopDetector({ now: c.now, tripSafeMode: () => {} });
  d.recordRestart("a");
  d.recordRestart("b");
  d.recordRestart("c");
  d.observeHealthy(true);
  c.advance(200_000);
  d.observeHealthy(false); // blip
  c.advance(200_000);
  d.observeHealthy(true);
  c.advance(200_000);
  d.observeHealthy(true);
  assert.equal(d.crashLoop, true, "no 300 s CONTINUOUS streak yet");
});

// -- safe_mode overlay -------------------------------------------------------------

function tempStore() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-cfg-"));
  return { dir, store: new ConfigStore(dir) };
}

test("config: factory defaults on first run; fresh-install autonomy cap is 3", () => {
  const { dir, store } = tempStore();
  assert.deepEqual(store.getSaved(), FACTORY_CONFIG);
  assert.equal(store.getSaved().autonomy, 3);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("config: persists across instances; corrupt file lands on factory (never strands)", () => {
  const { dir, store } = tempStore();
  store.setSaved({ volume: 55, brain: "opus" });
  const reloaded = new ConfigStore(dir);
  assert.equal(reloaded.getSaved().volume, 55);
  assert.equal(reloaded.getSaved().brain, "opus");
  fs.writeFileSync(path.join(dir, "config.json"), "corrupt{{{");
  const corrupt = new ConfigStore(dir);
  assert.deepEqual(corrupt.getSaved(), FACTORY_CONFIG);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("safe mode: overlay = factory behavioral values but the SAVED engine_url (the pinned exception)", () => {
  const { dir, store } = tempStore();
  store.setSaved({
    engine_url: "wss://engine.thewindstorm.uk:8788",
    hands_free: true,
    volume: 20,
    autonomy: 8,
    brain: "opus",
  });
  store.setSafeMode(true);
  const active = store.getActive();
  assert.equal(active.engine_url, "wss://engine.thewindstorm.uk:8788", "safe mode must NOT be voiceless on a LAN/cloud engine");
  assert.equal(active.hands_free, FACTORY_CONFIG.hands_free, "hands off");
  assert.equal(active.volume, FACTORY_CONFIG.volume);
  assert.equal(active.brain, FACTORY_CONFIG.brain, "factory brain");
  assert.equal(active.autonomy, FACTORY_CONFIG.autonomy);
  // The UNDERLYING config is untouched.
  assert.equal(store.getSaved().hands_free, true);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("safe mode: config writes in safe mode hit the UNDERLYING config; overlay stays factory", () => {
  const { dir, store } = tempStore();
  store.setSafeMode(true);
  store.setSaved({ volume: 33 });
  assert.equal(store.getSaved().volume, 33, "saved — will apply when you leave safe mode");
  assert.equal(store.getActive().volume, FACTORY_CONFIG.volume, "the overlay is unaffected");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("safe mode: the flag is PERSISTED — a crash-looping machine relaunches INTO safe mode", () => {
  const { dir, store } = tempStore();
  store.setSafeMode(true);
  const relaunched = new ConfigStore(dir); // a new process reading the same dir
  assert.equal(relaunched.inSafeMode, true);
  relaunched.setSafeMode(false);
  const again = new ConfigStore(dir);
  assert.equal(again.inSafeMode, false);
  fs.rmSync(dir, { recursive: true, force: true });
});

// -- engine allow-list ---------------------------------------------------------------

function allowList() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-allow-"));
  return { dir, allow: new EngineAllowList(dir) };
}

test("allow-list: loopback permits ws://; cloud suffix requires wss://", () => {
  const { dir, allow } = allowList();
  assert.equal(allow.check("ws://127.0.0.1:8788").allowed, true);
  assert.equal(allow.check("ws://localhost:8788").allowed, true);
  assert.equal(allow.check("ws://[::1]:8788").allowed, true);
  assert.equal(allow.check("wss://engine.thewindstorm.uk").allowed, true);
  assert.equal(allow.check("ws://engine.thewindstorm.uk").allowed, false, "wss REQUIRED off-box");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("allow-list: the host-pin discipline defeats substring/userinfo attacks (never reintroduce naive matching)", () => {
  const { dir, allow } = allowList();
  // 'evil.com/windymind.ai'-class: allow-listed string in the PATH, not the host.
  assert.equal(allow.check("wss://evil.com/engine.thewindstorm.uk").allowed, false);
  // 'user@evil'-class: allow-listed string in the USERINFO.
  assert.equal(allow.check("wss://engine.thewindstorm.uk@evil.com/").allowed, false);
  // Leading-dot suffix: a domain that merely ENDS with the string must fail.
  assert.equal(allow.check("wss://evil-thewindstorm.uk").allowed, false);
  assert.equal(allow.check("wss://sub.engine.thewindstorm.uk").allowed, true, "true subdomains pass");
  // Loopback-in-query trick.
  assert.equal(allow.check("wss://evil.com/?u=127.0.0.1").allowed, false);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("allow-list: paired hosts (UI flow only) pass with wss; reject_error semantics preserved by scrub", () => {
  const { dir, allow } = allowList();
  assert.equal(allow.check("wss://10.10.0.6:8788").allowed, false, "unpaired LAN host");
  allow.recordPairedHost("10.10.0.6");
  assert.equal(allow.check("wss://10.10.0.6:8788").allowed, true, "paired via the UI flow");
  assert.equal(allow.check("ws://10.10.0.6:8788").allowed, false, "paired still requires wss");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("diagnostics scrub: scheme+host+port verbatim when allowed; '<untrusted-host>' otherwise; query stripped", () => {
  const { dir, allow } = allowList();
  assert.equal(allow.scrubForDiagnostics("ws://127.0.0.1:8788"), "ws://127.0.0.1:8788");
  assert.equal(
    allow.scrubForDiagnostics("wss://engine.thewindstorm.uk:9000/path?token=secret123"),
    "wss://engine.thewindstorm.uk:9000",
    "path + query must never leave the machine",
  );
  assert.equal(allow.scrubForDiagnostics("wss://evil.com/x?y=1"), "<untrusted-host>");
  fs.rmSync(dir, { recursive: true, force: true });
});
