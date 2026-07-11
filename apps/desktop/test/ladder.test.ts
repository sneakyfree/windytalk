// Slice-3 tests: the recovery ladder + reset_to_defaults + the tier gate
// (tier_resolution steps on the ladder tools) + the LKG store (contract
// last_known_good) + response_ordering's act-after-reply discipline.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { ConfigStore, FACTORY_CONFIG } from "../electron/control/config.js";
import { RecoveryCoordinator } from "../electron/control/coordinator.js";
import { EngineAllowList } from "../electron/control/engine-allow.js";
import { CrashLoopDetector } from "../electron/control/layer1.js";
import { LkgStore } from "../electron/control/lkg.js";
import { LogRing } from "../electron/control/logring.js";
import { resolveTier } from "../electron/control/tier.js";
import { ControlTools, OFFLINE_STATUS, type Confirmer, type RendererStatus } from "../electron/control/tools.js";

function clock(startMs = 11_000_000_000) {
  let t = startMs;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

interface Harness {
  tools: ControlTools;
  config: ConfigStore;
  lkg: LkgStore;
  detector: CrashLoopDetector;
  status: RendererStatus;
  confirmOutcome: { value: Awaited<ReturnType<Confirmer>> };
  confirmCalls: { tool: string; allowSessionGrant: boolean }[];
  restarts: number[];
  repairs: number;
  cachesCleared: number;
  deepReconnects: number;
  applied: number;
  counterResets: string[];
  dir: string;
  c: ReturnType<typeof clock>;
}

function harness(opts: { repairArmed?: boolean } = {}): Harness {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-ladder-"));
  const c = clock();
  const lkg = new LkgStore(dir);
  const config = new ConfigStore(dir, { fallback: () => lkg.loadBest() });
  const detector = new CrashLoopDetector({ now: c.now, tripSafeMode: () => {} });
  const h: Harness = {
    tools: null as unknown as ControlTools,
    config,
    lkg,
    detector,
    status: { ...OFFLINE_STATUS, connection: "online", state: "idle" },
    confirmOutcome: { value: "allow" },
    confirmCalls: [],
    restarts: [],
    repairs: 0,
    cachesCleared: 0,
    deepReconnects: 0,
    applied: 0,
    counterResets: [],
    dir,
    c,
  };
  h.tools = new ControlTools({
    coordinator: new RecoveryCoordinator({ now: c.now }),
    config,
    allowList: new EngineAllowList(dir),
    detector,
    rendererStatus: () => h.status,
    reconnectEngine: async () => true,
    applyActiveConfig: () => h.applied++,
    resurrectionArmed: () => true,
    version: "0.1.0-test",
    startedAtMs: c.now(),
    emit: () => {},
    logs: new LogRing({ now: c.now }),
    probe: async () => null,
    confirm: async (req) => {
      h.confirmCalls.push({ tool: req.tool, allowSessionGrant: req.allowSessionGrant });
      return h.confirmOutcome.value;
    },
    lkg,
    deepReconnectEngine: async () => {
      h.deepReconnects++;
      return true;
    },
    clearCaches: async () => {
      h.cachesCleared++;
    },
    repairResurrection: async () =>
      opts.repairArmed === false
        ? { armed: false, detail: "privilege blocked: run `loginctl enable-linger grant` manually" }
        : (h.repairs++, { armed: true, detail: "re-armed" }),
    restartApp: () => h.restarts.push(c.now()),
    resetCrashCounter: (why) => h.counterResets.push(why),
    entitledBrains: () => ["opus"],
    now: c.now,
    reconnectTimeoutMs: 200,
  });
  return h;
}

function cleanup(h: Harness) {
  fs.rmSync(h.dir, { recursive: true, force: true });
}

// -- tier gate on the ladder -----------------------------------------------------

test("ask_first ladder tools prompt once; deny returns the bare 'denied' code", async () => {
  const h = harness();
  h.confirmOutcome.value = "deny";
  const res = await h.tools.dispatch("restart_engine");
  assert.deepEqual(res, { ok: false, error: "denied" });
  assert.equal(h.deepReconnects, 0, "a denied call must not act");
  assert.deepEqual(h.confirmCalls, [{ tool: "restart_engine", allowSessionGrant: true }]);
  cleanup(h);
});

test("a DENIED call does not charge the rate counters (retry allowed immediately)", async () => {
  const h = harness();
  h.confirmOutcome.value = "deny";
  await h.tools.dispatch("restart_engine");
  h.confirmOutcome.value = "allow";
  const res = await h.tools.dispatch("restart_engine");
  assert.equal(res.ok, true, "the denied attempt must not debounce the allowed one");
  assert.equal(h.deepReconnects, 1);
  cleanup(h);
});

test("exit_safe_mode is on the always_confirm_floor: prompts EVERY time, no session grant offered, autonomy 10 does not dissolve it", async () => {
  const h = harness();
  h.config.setSaved({ autonomy: 10 });
  h.config.setSafeMode(true);
  h.confirmOutcome.value = "allow";
  await h.tools.dispatch("exit_safe_mode");
  assert.equal(h.confirmCalls.length, 1, "autonomy 10 must NOT auto-allow a floor tool");
  assert.equal(h.confirmCalls[0].allowSessionGrant, false, "floor: no session grant ever offered");
  h.c.advance(6_000);
  h.config.setSafeMode(true);
  await h.tools.dispatch("exit_safe_mode");
  assert.equal(h.confirmCalls.length, 2, "floor tools confirm every invocation");
  cleanup(h);
});

test("ask_first at autonomy >= 7 is a standing grant (restart_engine runs without a prompt)", async () => {
  const h = harness();
  h.config.setSaved({ autonomy: 7 });
  const res = await h.tools.dispatch("restart_engine");
  assert.equal(res.ok, true);
  assert.equal(h.confirmCalls.length, 0, "autonomy 7-10: ask_first behaves as a standing grant");
  cleanup(h);
});

test("session grant: 'allow_session' upgrades an ask_first tool for the session (user-granted, never agent)", async () => {
  const h = harness();
  h.confirmOutcome.value = "allow_session";
  await h.tools.dispatch("clear_cache");
  assert.equal(h.confirmCalls.length, 1);
  h.c.advance(6_000);
  h.confirmOutcome.value = "deny"; // even if a prompt WOULD deny — there is no prompt
  const res = await h.tools.dispatch("clear_cache");
  assert.equal(res.ok, true);
  assert.equal(h.confirmCalls.length, 1, "the session grant stands; no second prompt");
  cleanup(h);
});

test("confirmer unavailable (headless, native dialog cannot render) fails CLOSED", async () => {
  const h = harness();
  h.confirmOutcome.value = "unavailable";
  const res = await h.tools.dispatch("restart_app");
  assert.deepEqual(res, { ok: false, error: "denied" });
  assert.equal(h.restarts.length, 0);
  cleanup(h);
});

// -- the ladder tools --------------------------------------------------------------

test("exit_safe_mode: drops the overlay, saves made DURING safe mode become active, counter resets", async () => {
  const h = harness();
  h.config.setSafeMode(true);
  h.config.setSaved({ volume: 42 }); // a set_* save made during safe mode
  const res = await h.tools.dispatch("exit_safe_mode");
  assert.equal(res.ok, true);
  assert.equal(h.config.inSafeMode, false);
  assert.equal(h.config.getActive().volume, 42, "safe-mode saves apply on exit — never an entry snapshot");
  assert.equal(h.applied, 1, "the active config is pushed to the renderer");
  assert.deepEqual(h.counterResets, ["exit_safe_mode"]);
  cleanup(h);
});

test("repair_resurrection: armed -> ok; privilege-blocked -> unsupported with the manual step in result", async () => {
  const h = harness();
  const ok = await h.tools.dispatch("repair_resurrection");
  assert.equal(ok.ok, true);
  assert.equal(h.repairs, 1);

  const blocked = harness({ repairArmed: false });
  const res = await blocked.tools.dispatch("repair_resurrection");
  assert.equal(res.ok, false);
  assert.equal(res.error, "unsupported");
  assert.match(String(res.result), /enable-linger/, "the manual step rides in result");
  cleanup(h);
  cleanup(blocked);
});

test("restart_engine: remote engine returns the pinned string; deep reconnect distinct from reconnect", async () => {
  const h = harness(); // harness leaves engineIsLocal unset -> treated as remote
  const res = await h.tools.dispatch("restart_engine");
  assert.deepEqual(res, { ok: true, result: "engine is remote — performed deep reconnect" });
  assert.equal(h.deepReconnects, 1);
  cleanup(h);
});

test("restart_engine: on the LOOPBACK engine it does NOT falsely claim 'remote'", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-local-eng-"));
  const c = clock();
  const config = new ConfigStore(dir);
  const tools = new ControlTools({
    coordinator: new RecoveryCoordinator({ now: c.now }),
    config,
    allowList: new EngineAllowList(dir),
    detector: new CrashLoopDetector({ now: c.now, tripSafeMode: () => {} }),
    rendererStatus: () => ({ ...OFFLINE_STATUS, connection: "online" }),
    reconnectEngine: async () => true,
    applyActiveConfig: () => {},
    resurrectionArmed: () => true,
    version: "t",
    startedAtMs: c.now(),
    emit: () => {},
    logs: new LogRing({ now: c.now }),
    probe: async () => null,
    confirm: async () => "allow",
    lkg: new LkgStore(dir),
    deepReconnectEngine: async () => true,
    clearCaches: async () => {},
    repairResurrection: async () => ({ armed: true, detail: "" }),
    restartApp: () => {},
    resetCrashCounter: () => {},
    entitledBrains: () => [],
    engineIsLocal: () => true, // ws://127.0.0.1 — a local engine
    now: c.now,
  });
  config.setSaved({ autonomy: 8 }); // dodge the ask_first prompt
  const res = await tools.dispatch("restart_engine");
  assert.equal(res.ok, true);
  assert.ok(!String(res.result).includes("remote"), "must not claim remote about a loopback engine");
  assert.match(String(res.result), /deep reconnect/);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("clear_cache: clears and reconnects; settings untouched", async () => {
  const h = harness();
  h.config.setSaved({ volume: 33 });
  const res = await h.tools.dispatch("clear_cache");
  assert.equal(res.ok, true);
  assert.equal(h.cachesCleared, 1);
  assert.equal(h.config.getSaved().volume, 33, "settings kept");
  cleanup(h);
});

test("restart_app (response_ordering): replies {ok:'restarting'} FIRST, acts >=250 ms later", async () => {
  const h = harness();
  const res = await h.tools.dispatch("restart_app");
  assert.deepEqual(res, { ok: true, result: "restarting" });
  assert.equal(h.restarts.length, 0, "the exit must NOT happen in-handler");
  await new Promise((r) => setTimeout(r, 450));
  assert.equal(h.restarts.length, 1, "the exit fires after the response had time to flush");
  cleanup(h);
});

test("reset_to_defaults: factory config, safe flag cleared, LKG invalidated, counter reset, then restart", async () => {
  const h = harness();
  h.config.setSaved({ volume: 15, brain: "opus", autonomy: 9, hands_free: true });
  h.lkg.write(h.config.getSaved());
  h.config.setSafeMode(true);
  h.confirmOutcome.value = "allow"; // always_confirm floor
  const res = await h.tools.dispatch("reset_to_defaults");
  assert.deepEqual(res, { ok: true, result: "restarting" });
  assert.equal(h.confirmCalls.length, 1);
  assert.equal(h.confirmCalls[0].allowSessionGrant, false, "floor: never upgradeable");
  assert.deepEqual(h.config.getSaved(), FACTORY_CONFIG, "factory, incl. autonomy back to cap 3");
  assert.equal(h.config.inSafeMode, false, "reset clears the safe-mode flag -> lands in normal");
  assert.equal(h.lkg.loadBest(), null, "SAFETY-INVERSE: LKG invalidated — recovery can never restore the discarded customization");
  assert.deepEqual(h.counterResets, ["reset_to_defaults"]);
  await new Promise((r) => setTimeout(r, 450));
  assert.equal(h.restarts.length, 1);
  cleanup(h);
});

test("the physical Reset button path: preconfirmed dispatch skips the confirmer (its dialog WAS the confirm)", async () => {
  const h = harness();
  const res = await h.tools.dispatch("reset_to_defaults", {}, { preconfirmed: true });
  assert.equal(res.ok, true);
  assert.equal(h.confirmCalls.length, 0);
  cleanup(h);
});

// -- LKG store (contract last_known_good) --------------------------------------------

test("LKG: atomic + checksummed generations; corrupt gen-1 falls back to gen-2; all-corrupt -> null (factory constant)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-lkg-"));
  const lkg = new LkgStore(dir);
  lkg.write({ ...FACTORY_CONFIG, volume: 10 });
  lkg.write({ ...FACTORY_CONFIG, volume: 20 });
  lkg.write({ ...FACTORY_CONFIG, volume: 30 });
  assert.equal(lkg.loadBest()!.volume, 30, "newest generation wins");
  fs.writeFileSync(path.join(dir, "lkg-1.json"), "rotted{{{");
  assert.equal(lkg.loadBest()!.volume, 20, "corrupt gen skipped, next verifies");
  // Checksum mismatch (silent bit-rot) is treated as corrupt too.
  const g2 = JSON.parse(fs.readFileSync(path.join(dir, "lkg-2.json"), "utf8"));
  g2.config.volume = 99; // tampered content, stale checksum
  fs.writeFileSync(path.join(dir, "lkg-2.json"), JSON.stringify(g2));
  assert.equal(lkg.loadBest()!.volume, 10, "checksum mismatch skipped");
  fs.writeFileSync(path.join(dir, "lkg-3.json"), "");
  assert.equal(lkg.loadBest(), null, "every generation corrupt -> null -> factory constant");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("LKG: dedupes identical writes (no generation churn on a stable config)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-lkg2-"));
  const lkg = new LkgStore(dir);
  lkg.write({ ...FACTORY_CONFIG, volume: 50 });
  lkg.write({ ...FACTORY_CONFIG, volume: 50 });
  lkg.write({ ...FACTORY_CONFIG, volume: 50 });
  assert.equal(fs.existsSync(path.join(dir, "lkg-2.json")), false, "no rotation for identical content");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("corrupt config.json + valid LKG: ConfigStore recovers the customization (Layer-1 auto-recovery)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-lkg3-"));
  const lkg = new LkgStore(dir);
  lkg.write({ ...FACTORY_CONFIG, volume: 64, brain: "opus" });
  fs.writeFileSync(path.join(dir, "config.json"), "corrupt{{{");
  const store = new ConfigStore(dir, { fallback: () => lkg.loadBest() });
  assert.equal(store.loadedFrom, "fallback");
  assert.equal(store.getSaved().volume, 64, "her working setup came back");
  // And after reset invalidates LKG, the same corruption lands on FACTORY.
  lkg.invalidateAll();
  fs.writeFileSync(path.join(dir, "config.json"), "corrupt{{{");
  const store2 = new ConfigStore(dir, { fallback: () => lkg.loadBest() });
  assert.equal(store2.loadedFrom, "factory");
  assert.deepEqual(store2.getSaved(), FACTORY_CONFIG);
  fs.rmSync(dir, { recursive: true, force: true });
});

// -- tier_resolution unit checks on the ladder set (full set_* matrix lands in slice 4)

test("tier_resolution: ladder tools' effective tiers at autonomy 2/5/8", () => {
  const grants = new Set<string>();
  for (const [autonomy, tool, expect] of [
    [5, "reconnect", "allow"],
    [5, "enter_safe_mode", "allow"],
    [5, "restart_app", "confirm"],
    [8, "restart_app", "allow"], // ask_first dissolves at >=7
    [8, "exit_safe_mode", "confirm"], // floor never dissolves
    [8, "reset_to_defaults", "confirm"],
    [2, "reconnect", "allow"], // allowed, but notify-after
  ] as const) {
    const d = resolveTier(tool, {}, { currentAutonomy: autonomy, sessionGrants: grants });
    assert.equal(d.action, expect, `${tool}@${autonomy}`);
  }
  // autonomy 0-2 notify-after flag:
  const low = resolveTier("reconnect", {}, { currentAutonomy: 1, sessionGrants: grants });
  assert.ok(low.action === "allow" && low.notify_after === true);
  const mid = resolveTier("reconnect", {}, { currentAutonomy: 5, sessionGrants: grants });
  assert.ok(mid.action === "allow" && mid.notify_after === false);
});
