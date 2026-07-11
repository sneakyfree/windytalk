// Slice-4 tests: the seven set_* config dials + THE FULL TIER MATRIX (build
// notes §6: the set_autonomy / set_volume(0) interaction is the recurring trap
// — three of five freeze rounds hit it; tier_resolution is the single source
// of truth and this file exercises the whole matrix: raise/lower/equal,
// mute/unmute, at autonomy 2/5/8, plus floor-vs-session-grant interactions).
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { ConfigStore } from "../electron/control/config.js";
import { RecoveryCoordinator } from "../electron/control/coordinator.js";
import { EngineAllowList } from "../electron/control/engine-allow.js";
import { CrashLoopDetector } from "../electron/control/layer1.js";
import { LkgStore } from "../electron/control/lkg.js";
import { LogRing } from "../electron/control/logring.js";
import { resolveTier } from "../electron/control/tier.js";
import { ControlTools, OFFLINE_STATUS, type Confirmer } from "../electron/control/tools.js";

function clock(startMs = 13_000_000_000) {
  let t = startMs;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

function harness() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-dials-"));
  const c = clock();
  const config = new ConfigStore(dir);
  const allowList = new EngineAllowList(dir);
  const confirmCalls: { tool: string; allowSessionGrant: boolean }[] = [];
  const confirmOutcome = { value: "allow" as Awaited<ReturnType<Confirmer>> };
  const applied: unknown[] = [];
  const reconnects = { count: 0 };
  const tools = new ControlTools({
    coordinator: new RecoveryCoordinator({ now: c.now }),
    config,
    allowList,
    detector: new CrashLoopDetector({ now: c.now, tripSafeMode: () => {} }),
    rendererStatus: () => ({ ...OFFLINE_STATUS, connection: "online" }),
    reconnectEngine: async () => (reconnects.count++, true),
    applyActiveConfig: () => applied.push(config.getActive()),
    resurrectionArmed: () => true,
    version: "t",
    startedAtMs: c.now(),
    emit: () => {},
    logs: new LogRing({ now: c.now }),
    probe: async () => null,
    confirm: async (req) => {
      confirmCalls.push({ tool: req.tool, allowSessionGrant: req.allowSessionGrant });
      return confirmOutcome.value;
    },
    lkg: new LkgStore(dir),
    deepReconnectEngine: async () => true,
    clearCaches: async () => {},
    repairResurrection: async () => ({ armed: true, detail: "armed" }),
    restartApp: () => {},
    resetCrashCounter: () => {},
    entitledBrains: () => ["opus", "haiku"],
    now: c.now,
    reconnectTimeoutMs: 100,
  });
  return { tools, config, allowList, confirmCalls, confirmOutcome, applied, reconnects, c, dir };
}

// -- THE FULL TIER MATRIX (pure, via resolveTier) -------------------------------------

test("MATRIX set_autonomy: raise/lower/equal at autonomy 2/5/8 (tier_override fully replaces ask_first)", () => {
  const g = new Set<string>();
  const cases: [number, number, "allow" | "confirm"][] = [
    // [current, requested, expected]
    [2, 1, "allow"], [2, 2, "allow"], [2, 3, "confirm"],
    [5, 0, "allow"], [5, 4, "allow"], [5, 5, "allow"], [5, 6, "confirm"], [5, 10, "confirm"],
    [8, 3, "allow"], [8, 8, "allow"], [8, 9, "confirm"], [8, 10, "confirm"],
  ];
  for (const [current, level, expect] of cases) {
    const d = resolveTier("set_autonomy", { level }, { currentAutonomy: current, sessionGrants: g });
    assert.equal(d.action, expect, `set_autonomy(${level}) @ autonomy ${current}`);
    if (expect === "confirm") {
      assert.equal(
        (d as { session_grant_allowed: boolean }).session_grant_allowed,
        false,
        `raising @ ${current}->${level} is on the FLOOR: no session grant, no autonomy dissolve`,
      );
    }
  }
});

test("MATRIX set_autonomy: lowering is auto_allow even at autonomy 8 (the floor must NOT dead-code the safe direction)", () => {
  // The round-3 defect: putting set_autonomy unconditionally on the floor
  // wrongly force-confirmed LOWERING. The conditional floor ('when raising')
  // keeps lowering free at every autonomy band.
  for (const current of [2, 5, 8, 10]) {
    for (const level of [0, Math.max(0, current - 1), current]) {
      const d = resolveTier("set_autonomy", { level }, { currentAutonomy: current, sessionGrants: new Set() });
      assert.equal(d.action, "allow", `lowering/equal ${current}->${level} must be auto_allow`);
    }
  }
});

test("MATRIX set_volume: mute vs unmute at autonomy 2/5/8 (value-conditional + floor)", () => {
  const g = new Set<string>();
  for (const autonomy of [2, 5, 8]) {
    // level > 0 -> auto_allow (the value-conditional resolver REPLACES ask_first)
    for (const level of [1, 50, 100]) {
      const d = resolveTier("set_volume", { level }, { currentAutonomy: autonomy, sessionGrants: g });
      assert.equal(d.action, "allow", `set_volume(${level}) @ ${autonomy}`);
    }
    // level == 0 -> always_confirm via the FLOOR: even autonomy 8 confirms.
    const mute = resolveTier("set_volume", { level: 0 }, { currentAutonomy: autonomy, sessionGrants: g });
    assert.equal(mute.action, "confirm", `mute @ ${autonomy} must confirm`);
    assert.equal(
      (mute as { session_grant_allowed: boolean }).session_grant_allowed,
      false,
      "mute is a stranding vector: never session-upgradeable",
    );
  }
});

test("MATRIX set_volume: a session grant on set_volume can NEVER bleed into mute (floor overrides grants)", () => {
  const grants = new Set(["set_volume"]); // hypothetical standing grant
  const unmuted = resolveTier("set_volume", { level: 30 }, { currentAutonomy: 5, sessionGrants: grants });
  assert.equal(unmuted.action, "allow");
  const mute = resolveTier("set_volume", { level: 0 }, { currentAutonomy: 5, sessionGrants: grants });
  assert.equal(mute.action, "confirm", "the floor ignores session grants entirely");
});

test("MATRIX set_wake_mode: enabling always-listen is floor; disabling follows plain ask_first", () => {
  const g = new Set<string>();
  for (const autonomy of [2, 5]) {
    const on = resolveTier("set_wake_mode", { hands_free: true }, { currentAutonomy: autonomy, sessionGrants: g });
    assert.equal(on.action, "confirm");
    assert.equal((on as { session_grant_allowed: boolean }).session_grant_allowed, false, "privacy escalation: floor");
    const off = resolveTier("set_wake_mode", { hands_free: false }, { currentAutonomy: autonomy, sessionGrants: g });
    assert.equal(off.action, "confirm", "ask_first at low autonomy");
    assert.equal((off as { session_grant_allowed: boolean }).session_grant_allowed, true, "safe direction: normal path");
  }
  // At autonomy 8: disabling dissolves (standing grant); enabling still confirms.
  const off8 = resolveTier("set_wake_mode", { hands_free: false }, { currentAutonomy: 8, sessionGrants: g });
  assert.equal(off8.action, "allow");
  const on8 = resolveTier("set_wake_mode", { hands_free: true }, { currentAutonomy: 8, sessionGrants: g });
  assert.equal(on8.action, "confirm", "always-listen confirms even at autonomy 8");
});

test("MATRIX set_engine_url / set_brain: unconditional floor at every autonomy", () => {
  for (const autonomy of [2, 5, 8, 10]) {
    for (const tool of ["set_engine_url", "set_brain"]) {
      const d = resolveTier(tool, { url: "ws://127.0.0.1:8788", brain: "default" }, { currentAutonomy: autonomy, sessionGrants: new Set([tool]) });
      assert.equal(d.action, "confirm", `${tool} @ ${autonomy}`);
      assert.equal((d as { session_grant_allowed: boolean }).session_grant_allowed, false);
    }
  }
});

test("MATRIX audio dials: plain ask_first — session grant works, autonomy 7 dissolves", () => {
  const d = resolveTier("set_audio_input", { device_id: "x" }, { currentAutonomy: 5, sessionGrants: new Set() });
  assert.equal(d.action, "confirm");
  assert.equal((d as { session_grant_allowed: boolean }).session_grant_allowed, true);
  const granted = resolveTier("set_audio_input", { device_id: "x" }, { currentAutonomy: 5, sessionGrants: new Set(["set_audio_input"]) });
  assert.equal(granted.action, "allow");
  const high = resolveTier("set_audio_output", { device_id: "x" }, { currentAutonomy: 7, sessionGrants: new Set() });
  assert.equal(high.action, "allow");
});

// -- dial behavior through dispatch ----------------------------------------------------

test("set_volume(0) through dispatch: confirms (floor), then mutes; set_volume(50) sails through at autonomy 8", async () => {
  const h = harness();
  h.config.setSaved({ autonomy: 8 });
  const up = await h.tools.dispatch("set_volume", { level: 50 });
  assert.deepEqual(up, { ok: true, result: "saved" });
  assert.equal(h.confirmCalls.length, 0, "level>0 is auto_allow — no prompt even charged");
  assert.equal(h.config.getSaved().volume, 50);
  h.c.advance(6_000);
  const mute = await h.tools.dispatch("set_volume", { level: 0 });
  assert.equal(mute.ok, true);
  assert.equal(h.confirmCalls.length, 1, "mute confirmed even at autonomy 8");
  assert.equal(h.confirmCalls[0].allowSessionGrant, false);
  assert.equal(h.config.getSaved().volume, 0);
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("set_autonomy raise persists ONLY after confirm; deny leaves autonomy untouched and uncharged", async () => {
  const h = harness();
  assert.equal(h.config.getSaved().autonomy, 3, "fresh-install cap");
  h.confirmOutcome.value = "deny";
  const denied = await h.tools.dispatch("set_autonomy", { level: 9 });
  assert.deepEqual(denied, { ok: false, error: "denied" });
  assert.equal(h.config.getSaved().autonomy, 3);
  h.confirmOutcome.value = "allow";
  const raised = await h.tools.dispatch("set_autonomy", { level: 9 });
  assert.equal(raised.ok, true, "denied attempt must not have debounced this");
  assert.equal(h.config.getSaved().autonomy, 9);
  h.c.advance(6_000);
  const lowered = await h.tools.dispatch("set_autonomy", { level: 2 });
  assert.equal(lowered.ok, true);
  assert.equal(h.confirmCalls.length, 2, "both raise attempts prompted; lowering never does");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("set_engine_url: allow-listed host saved + reconnect fires; untrusted host -> the pinned template, never 'denied'", async () => {
  const h = harness();
  h.config.setSaved({ autonomy: 8 }); // dodge the ask_first prompt path for the audio dial below
  const bad = await h.tools.dispatch("set_engine_url", { url: "wss://evil.example/x" });
  assert.equal(bad.ok, false);
  assert.equal(bad.error, "untrusted host: evil.example", "exact template: prefix + host");
  assert.equal(h.confirmCalls.length, 1, "the floor confirm ran; the allow-list rejected AFTER");
  h.c.advance(6_000);
  const good = await h.tools.dispatch("set_engine_url", { url: "wss://engine.thewindstorm.uk:9000" });
  assert.deepEqual(good, { ok: true, result: "saved" });
  assert.equal(h.config.getSaved().engine_url, "wss://engine.thewindstorm.uk:9000");
  assert.equal(h.reconnects.count, 1, "a new engine URL redials");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("set_brain: entitlement cache validates offline; 'default' always accepted; unentitled -> pinned template", async () => {
  const h = harness();
  const no = await h.tools.dispatch("set_brain", { brain: "gpt-9" });
  assert.equal(no.ok, false);
  assert.equal(no.error, "not entitled to brain: gpt-9");
  h.c.advance(6_000);
  const yes = await h.tools.dispatch("set_brain", { brain: "opus" });
  assert.equal(yes.ok, true);
  assert.equal(h.config.getSaved().brain, "opus");
  h.c.advance(6_000);
  const dflt = await h.tools.dispatch("set_brain", { brain: "default" });
  assert.equal(dflt.ok, true, "'default' always accepted");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("safe mode: set_* writes the underlying config with the pinned 'saved — will apply' string; overlay untouched", async () => {
  const h = harness();
  h.config.setSaved({ autonomy: 8 });
  h.config.setSafeMode(true);
  // NOTE: in safe mode the ACTIVE autonomy is factory 3, so ask_first prompts.
  h.confirmOutcome.value = "allow";
  const res = await h.tools.dispatch("set_audio_input", { device_id: "mic-b" });
  assert.deepEqual(res, { ok: true, result: "saved — will apply when you leave safe mode" });
  assert.equal(h.config.getSaved().audio_input_id, "mic-b");
  assert.equal(h.config.getActive().audio_input_id, null, "overlay stays factory");
  assert.equal(h.applied.length, 0, "nothing pushed to the renderer while the overlay holds");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("set_* during a held recovery lock -> already_recovering (config tools never queue)", async () => {
  const h = harness();
  h.config.setSaved({ autonomy: 8 });
  // Hold the lock with a slow reconnect (never resolves within the test).
  const coordinator = (h.tools as unknown as { deps: { coordinator: RecoveryCoordinator } }).deps.coordinator;
  const held = coordinator.gate("restart_engine");
  assert.ok(held.proceed);
  const res = await h.tools.dispatch("set_volume", { level: 40 });
  assert.equal(res.ok, false);
  assert.equal(res.error, "already_recovering");
  if (held.proceed) held.ticket.release();
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("invalid dial values are rejected honestly (never clamped silently)", async () => {
  const h = harness();
  h.config.setSaved({ autonomy: 8 });
  const vol = await h.tools.dispatch("set_volume", { level: 101 });
  assert.equal(vol.ok, false);
  h.c.advance(6_000);
  const auto = await h.tools.dispatch("set_autonomy", { level: -1 });
  assert.equal(auto.ok, false);
  fs.rmSync(h.dir, { recursive: true, force: true });
});
