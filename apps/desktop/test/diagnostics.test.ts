// Slice-2 tests: the seven remaining diagnostics on their pinned returns
// schemas, the device-name scrub matrix, and THE GOLDEN PRIVACY TEST (contract
// diagnostics_privacy + design Gap 4): crafted PII/paths/tokens/transcripts fed
// through EVERY get_* tool, asserting none of it appears anywhere in output —
// the same negative-test shape as tests/test_contracts.py's content-free scrub.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { ConfigStore } from "../electron/control/config.js";
import { RecoveryCoordinator } from "../electron/control/coordinator.js";
import { EngineAllowList } from "../electron/control/engine-allow.js";
import { CrashLoopDetector } from "../electron/control/layer1.js";
import { LogRing } from "../electron/control/logring.js";
import { scrubDeviceName } from "../electron/control/scrub.js";
import { ControlTools, OFFLINE_STATUS, type RendererStatus } from "../electron/control/tools.js";

function clock(startMs = 7_000_000_000) {
  let t = startMs;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

interface Harness {
  tools: ControlTools;
  config: ConfigStore;
  logs: LogRing;
  status: RendererStatus;
  setStatus(patch: Partial<RendererStatus>): void;
  probeResult: { value: unknown };
  dir: string;
  c: ReturnType<typeof clock>;
}

function harness(): Harness {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-diag-"));
  const c = clock();
  const config = new ConfigStore(dir);
  const logs = new LogRing({ now: c.now });
  const h: Harness = {
    tools: null as unknown as ControlTools,
    config,
    logs,
    status: { ...OFFLINE_STATUS, connection: "online", state: "idle", sessionId: "sess-1" },
    setStatus(patch) {
      h.status = { ...h.status, ...patch };
    },
    probeResult: { value: null },
    dir,
    c,
  };
  h.tools = new ControlTools({
    coordinator: new RecoveryCoordinator({ now: c.now }),
    config,
    allowList: new EngineAllowList(dir),
    detector: new CrashLoopDetector({ now: c.now, tripSafeMode: () => {} }),
    rendererStatus: () => h.status,
    reconnectEngine: async () => true,
    applyActiveConfig: () => {},
    resurrectionArmed: () => true,
    version: "0.1.0-test",
    startedAtMs: c.now() - 1_000,
    emit: () => {},
    logs,
    probe: async () => h.probeResult.value,
    confirm: async () => "allow",
    lkg: { invalidateAll() {}, write() {}, loadBest: () => null } as any,
    deepReconnectEngine: async () => true,
    clearCaches: async () => {},
    repairResurrection: async () => ({ armed: true, detail: "armed" }),
    restartApp: () => {},
    resetCrashCounter: () => {},
    entitledBrains: () => [],
    engineIsLocal: () => true,
    now: c.now,
    selftestStageTimeoutMs: 50,
  });
  return h;
}

function cleanup(h: Harness) {
  fs.rmSync(h.dir, { recursive: true, force: true });
}

// -- pinned returns shapes ---------------------------------------------------------

test("get_status: {state, mic_on, session_id}; voice-state and health-mode are independent axes", async () => {
  const h = harness();
  h.setStatus({ state: "listening", micOn: true });
  h.config.setSafeMode(true); // health mode is safe; voice state still listening
  const res = await h.tools.dispatch("get_status");
  assert.deepEqual(res, { ok: true, result: { state: "listening", mic_on: true, session_id: "sess-1" } });
  h.setStatus({ connection: "offline" });
  const off = await h.tools.dispatch("get_status");
  assert.equal((off.result as { state: string }).state, "offline");
  cleanup(h);
});

test("get_config: active vs saved differ in safe mode; engine_url scrubbed in both", async () => {
  const h = harness();
  h.config.setSaved({ volume: 25, hands_free: true, engine_url: "wss://evil.example/x?token=abc" });
  h.config.setSafeMode(true);
  const res = (await h.tools.dispatch("get_config")).result as {
    active: Record<string, unknown>;
    saved: Record<string, unknown>;
  };
  assert.equal(res.saved.volume, 25);
  assert.equal(res.active.volume, 80, "overlay = factory");
  assert.equal(res.saved.hands_free, true);
  assert.equal(res.active.hands_free, false);
  assert.equal(res.active.engine_url, "<untrusted-host>");
  assert.equal(res.saved.engine_url, "<untrusted-host>");
  cleanup(h);
});

test("get_logs: newest LAST, lines clamped, truncated flag honest", async () => {
  const h = harness();
  for (let i = 0; i < 150; i++) h.logs.append("info", `event ${i}`);
  const res = (await h.tools.dispatch("get_logs", { lines: 100 })).result as {
    lines: { msg: string; ts: string; level: string }[];
    truncated: boolean;
  };
  assert.equal(res.lines.length, 100);
  assert.equal(res.truncated, true);
  assert.equal(res.lines.at(-1)!.msg, "event 149", "newest LAST");
  assert.match(res.lines[0].ts, /^\d{4}-\d{2}-\d{2}T/);
  cleanup(h);
});

test("list_audio_devices: ids pass through, names scrubbed, selection marked; renderer down = honest timeout", async () => {
  const h = harness();
  h.probeResult.value = {
    inputs: [
      { id: "mic-a", name: "Grant's AirPods Pro", selected: true },
      { id: "mic-b", name: "USB Microphone", selected: false },
    ],
    outputs: [{ id: "spk-1", name: "iPhone de Marie", selected: true }],
  };
  const res = (await h.tools.dispatch("list_audio_devices")).result as {
    inputs: { id: string; name: string; selected: boolean }[];
    outputs: { id: string; name: string; selected: boolean }[];
  };
  assert.equal(res.inputs[0].id, "mic-a", "the ID (the API surface) is untouched");
  assert.equal(res.inputs[0].name, "AirPods Pro", "leading genitive stripped");
  assert.equal(res.inputs[0].selected, true);
  assert.equal(res.inputs[1].name, "USB Microphone");
  assert.equal(res.outputs[0].name, "iPhone", "trailing localized possessive stripped");

  h.probeResult.value = null; // renderer down
  h.c.advance(6_000);
  const down = await h.tools.dispatch("list_audio_devices");
  assert.equal(down.ok, false);
  assert.equal(down.error, "timeout");
  cleanup(h);
});

test("run_selftest: 4 stages with pass/detail; renderer-down mic/speaker stages report timeout, never fake a pass", async () => {
  const h = harness();
  h.probeResult.value = {
    mic: { pass: true, detail: "capturing (device present)" },
    speaker: { pass: false, detail: "audio context state: suspended" },
  };
  const res = (await h.tools.dispatch("run_selftest")).result as {
    stages: Record<string, { pass: boolean; detail: string }>;
  };
  assert.equal(res.stages.engine.pass, true);
  assert.equal(res.stages.brain.pass, true);
  assert.equal(res.stages.mic.pass, true);
  assert.equal(res.stages.speaker.pass, false);

  h.probeResult.value = null;
  h.c.advance(6_000);
  const down = (await h.tools.dispatch("run_selftest")).result as {
    stages: Record<string, { pass: boolean; detail: string }>;
  };
  assert.deepEqual(down.stages.mic, { pass: false, detail: "timeout" });
  assert.deepEqual(down.stages.speaker, { pass: false, detail: "timeout" });
  cleanup(h);
});

test("get_capabilities: tri-state; built=true, unbuilt=false, restart_engine degraded on a remote engine", async () => {
  const h = harness();
  const caps = (await h.tools.dispatch("get_capabilities")).result as {
    os: string;
    tools: Record<string, boolean | string>;
  };
  assert.equal(caps.tools.get_health, true);
  assert.equal(caps.tools.reconnect, true);
  assert.equal(caps.tools.exit_safe_mode, true);
  assert.equal(caps.tools.apply_update, false, "unbuilt slice reads false (forced-honest)");
  assert.equal(caps.tools.set_volume, true);
  assert.equal(caps.tools.restart_engine, "degraded", "no child engine in the desktop client — deep reconnect");
  assert.equal(caps.tools.restart_app, true, "resurrection armed in this harness");
  assert.equal(Object.keys(caps.tools).length, 24, "all 24 contract tools reported");
  cleanup(h);
});

test("check_for_update: INERT — ok:true, update_available:false, reason:'no update source configured'", async () => {
  const h = harness();
  const res = await h.tools.dispatch("check_for_update");
  assert.deepEqual(res, {
    ok: true,
    result: {
      update_available: false,
      current: "0.1.0-test",
      latest: null,
      reason: "no update source configured",
    },
  });
  cleanup(h);
});

// -- device-name scrub matrix (the pinned locales + default-safe rule) ---------------

test("device scrub matrix: pinned examples + default-to-type for marker-less names", () => {
  // Pinned examples from the contract:
  assert.equal(scrubDeviceName("Grant's AirPods", "id1", "input"), "AirPods");
  assert.equal(scrubDeviceName("iPhone de Marie", "id2", "input"), "iPhone");
  assert.equal(scrubDeviceName("AirPods von Grant", "id3", "output"), "AirPods");
  assert.equal(scrubDeviceName("iPhone di Maria", "id4", "input"), "iPhone");
  assert.equal(scrubDeviceName("GRANT-PC Bluetooth", "id5", "input"), "Bluetooth");
  // Marker-less personal name -> device TYPE + id (default-safe):
  assert.equal(scrubDeviceName("Grant Speaker", "spk9", "output"), "Speaker (spk9)");
  // Known device vocabulary passes through:
  assert.equal(scrubDeviceName("USB Microphone", "u1", "input"), "USB Microphone");
  assert.equal(scrubDeviceName("Sony WH-1000XM5", "s1", "output"), "Sony WH-1000XM5");
  assert.equal(scrubDeviceName("MacBook Pro Microphone", "m1", "input"), "MacBook Pro Microphone");
  // Unknown token that could be a name -> over-scrub on purpose:
  assert.equal(scrubDeviceName("Willemina Headset", "w1", "input"), "Microphone (w1)");
  // Empty label -> type + id.
  assert.equal(scrubDeviceName("", "x1", "output"), "Speaker (x1)");
});

// -- THE GOLDEN TEST (diagnostics_privacy) -------------------------------------------

test("GOLDEN: crafted PII/tokens/paths/transcripts through EVERY get_* tool — nothing leaks", async () => {
  const h = harness();
  const POISON = {
    username: "grantwhitmer",
    homePath: "/home/grantwhitmer/Desktop/secret plans.txt",
    token: "sk-ant-api03-abcdef0123456789abcdef0123456789",
    hexSecret: "deadbeefcafe0123456789abcdef00ff",
    transcript: "user said: my bank pin is 4422 please remember it",
    ssid: "WhitmerHouse-5G",
    query: "?apikey=supersecretvalue123&user=grant",
  };
  // Poison every input surface the diagnostics read from:
  h.setStatus({
    lastError: `E: ${POISON.homePath} ${POISON.token} wss://x.example/cb${POISON.query} — ${POISON.transcript}`,
    sessionId: "sess-2",
  });
  h.logs.append("error", `crash at ${POISON.homePath} token=${POISON.hexSecret}`);
  h.logs.append("info", `${POISON.transcript} on ${POISON.ssid}`);
  h.config.setSaved({
    engine_url: `wss://evil.example/steal${POISON.query}`,
    brain: "opus", // model id: the deliberate verbatim exception
  });
  h.probeResult.value = {
    inputs: [{ id: "mic-1", name: `${POISON.username}'s AirPods`, selected: true }],
    outputs: [{ id: "spk-1", name: `Speaker de ${POISON.username}`, selected: true }],
  };

  const reads: [string, Record<string, unknown>][] = [
    ["get_health", {}],
    ["get_status", {}],
    ["get_config", {}],
    ["get_logs", { lines: 500 }],
    ["list_audio_devices", {}],
    ["run_selftest", {}],
    ["get_capabilities", {}],
    ["check_for_update", {}],
  ];
  for (const [tool, args] of reads) {
    const res = await h.tools.dispatch(tool, args);
    assert.equal(res.ok, true, `${tool} must succeed`);
    const out = JSON.stringify(res);
    assert.ok(!out.includes(POISON.username), `${tool} leaked the username: ${out.slice(0, 400)}`);
    assert.ok(!out.includes("secret plans"), `${tool} leaked a home path`);
    assert.ok(!out.includes(POISON.token), `${tool} leaked an API token`);
    assert.ok(!out.includes(POISON.hexSecret), `${tool} leaked a hex secret`);
    assert.ok(!out.includes("bank pin"), `${tool} leaked transcript-looking text`);
    assert.ok(!out.includes(POISON.ssid), `${tool} leaked an SSID`);
    assert.ok(!out.includes("apikey=supersecret"), `${tool} leaked a URL query string`);
    assert.ok(!out.includes("evil.example"), `${tool} leaked an untrusted host`);
  }
  // And the deliberate exceptions DO ride: brain model id verbatim.
  const cfg = (await h.tools.dispatch("get_config")).result as { saved: { brain: string } };
  assert.equal(cfg.saved.brain, "opus");
  cleanup(h);
});
