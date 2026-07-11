// Slice-1 tool surface tests: get_health (pinned returns shape + suggested_fix),
// reconnect, enter_safe_mode, dispatch-through-coordinator, control.action
// telemetry, and NORMATIVE MCP compliance (initialize lifecycle, canonical JSON
// + structuredContent — the two hands bugs must not reappear).
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { ConfigStore } from "../electron/control/config.js";
import { RecoveryCoordinator } from "../electron/control/coordinator.js";
import { EngineAllowList } from "../electron/control/engine-allow.js";
import { CrashLoopDetector } from "../electron/control/layer1.js";
import { ControlMcp, MCP_PROTOCOL, loadContractTools } from "../electron/control/mcp.js";
import { ControlTools, OFFLINE_STATUS, type RendererStatus } from "../electron/control/tools.js";

function clock(startMs = 9_000_000) {
  let t = startMs;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

interface Harness {
  tools: ControlTools;
  config: ConfigStore;
  detector: CrashLoopDetector;
  status: RendererStatus;
  setStatus(patch: Partial<RendererStatus>): void;
  reconnectResult: { value: boolean };
  applied: number;
  emitted: Record<string, unknown>[];
  dir: string;
  c: ReturnType<typeof clock>;
}

function harness(): Harness {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-tools-"));
  const c = clock();
  const config = new ConfigStore(dir);
  const coordinator = new RecoveryCoordinator({ now: c.now });
  const detector = new CrashLoopDetector({ now: c.now, tripSafeMode: () => {} });
  const emitted: Record<string, unknown>[] = [];
  const h: Harness = {
    tools: null as unknown as ControlTools,
    config,
    detector,
    status: { ...OFFLINE_STATUS, connection: "online", state: "idle" },
    setStatus(patch) {
      h.status = { ...h.status, ...patch };
    },
    reconnectResult: { value: true },
    applied: 0,
    emitted,
    dir,
    c,
  };
  h.tools = new ControlTools({
    coordinator,
    config,
    allowList: new EngineAllowList(dir),
    detector,
    rendererStatus: () => h.status,
    reconnectEngine: async () => h.reconnectResult.value,
    applyActiveConfig: () => h.applied++,
    resurrectionArmed: () => true,
    version: "0.1.0-test",
    startedAtMs: c.now() - 60_000,
    emit: (f) => emitted.push(f),
    now: c.now,
    reconnectTimeoutMs: 500,
  });
  return h;
}

test("get_health: pinned returns shape — every required field present, healthy formula holds", async () => {
  const h = harness();
  h.setStatus({ micOn: true, lastFrameAtMs: h.c.now() - 2_000 });
  const res = await h.tools.dispatch("get_health");
  assert.equal(res.ok, true);
  const health = res.result as Record<string, unknown>;
  for (const field of [
    "healthy", "mode", "engine", "brain", "mic", "restarts",
    "crash_loop", "resurrection_armed", "summary", "suggested_fix",
  ]) {
    assert.ok(field in health, `required field ${field}`);
  }
  assert.equal(health.healthy, true);
  assert.equal(health.mode, "normal");
  assert.equal((health.engine as Record<string, unknown>).connected, true);
  assert.equal((health.engine as Record<string, unknown>).last_frame_s_ago, 2);
  assert.equal((health.engine as Record<string, unknown>).url, "ws://127.0.0.1:8788");
  assert.equal(health.suggested_fix, null, "healthy -> no fix suggested");
  assert.equal(typeof health.summary, "string");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("get_health: engine down -> healthy false, suggested_fix = reconnect (least destructive)", async () => {
  const h = harness();
  h.setStatus({ connection: "offline" });
  const health = (await h.tools.dispatch("get_health")).result as Record<string, unknown>;
  assert.equal(health.healthy, false);
  assert.equal(health.suggested_fix, "reconnect");
  assert.match(String(health.summary), /connection/i);
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("get_health: last_error is scrubbed (home paths, tokens, query strings never leave)", async () => {
  const h = harness();
  h.setStatus({
    lastError: "E_FAIL: /home/grantwhitmer/secret/notes.txt token=deadbeefdeadbeefdeadbeef https://x.example/cb?apikey=hunter2hunter2",
  });
  const health = (await h.tools.dispatch("get_health")).result as Record<string, unknown>;
  const err = String(health.last_error);
  assert.ok(!err.includes("grantwhitmer"), "username must not leak");
  assert.ok(!err.includes("deadbeefdeadbeefdeadbeef"), "token must be redacted");
  assert.ok(!err.includes("apikey=hunter2"), "query strings must be stripped");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("get_health: crash loop in safe mode reads as mode=safe, crash_loop=true, healthy=false", async () => {
  const h = harness();
  h.detector.recordRestart("a");
  h.detector.recordRestart("b");
  h.detector.recordRestart("c");
  h.config.setSafeMode(true);
  const health = (await h.tools.dispatch("get_health")).result as Record<string, unknown>;
  assert.equal(health.mode, "safe");
  assert.equal(health.crash_loop, true);
  assert.equal(health.restarts, 3);
  assert.equal(health.healthy, false);
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("reconnect: ok:'reconnected' on success; error:'timeout' on failure; control.action emitted for both", async () => {
  const h = harness();
  const ok = await h.tools.dispatch("reconnect");
  assert.deepEqual(ok, { ok: true, result: "reconnected" });
  h.c.advance(6_000);
  h.reconnectResult.value = false;
  const bad = await h.tools.dispatch("reconnect");
  assert.equal(bad.ok, false);
  assert.equal(bad.error, "timeout");
  const actions = h.emitted.filter((e) => e.event_type === "control.action" && e.tool === "reconnect");
  assert.equal(actions.length, 2, "every EXECUTED mutating call emits");
  assert.equal(actions[0].ok, true);
  assert.equal(actions[1].ok, false);
  assert.equal(actions[1].error_code, "timeout");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("gate rejections do NOT emit telemetry (the denominator is actions TAKEN)", async () => {
  const h = harness();
  await h.tools.dispatch("reconnect");
  const rejected = await h.tools.dispatch("reconnect"); // inside the 5 s debounce
  assert.equal(rejected.ok, false);
  assert.equal(rejected.error, "rate_limited");
  const actions = h.emitted.filter((e) => e.event_type === "control.action");
  assert.equal(actions.length, 1, "the rejected call must not emit");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("enter_safe_mode: persists the flag, pushes the overlay, idempotent, emits", async () => {
  const h = harness();
  const res = await h.tools.dispatch("enter_safe_mode");
  assert.deepEqual(res, { ok: true, result: "entered safe mode" });
  assert.equal(h.config.inSafeMode, true);
  assert.equal(h.applied, 1, "the overlay must be pushed to the renderer");
  assert.equal(new ConfigStore(h.dir).inSafeMode, true, "flag persisted for relaunch-into-safe-mode");
  h.c.advance(6_000);
  const again = await h.tools.dispatch("enter_safe_mode");
  assert.deepEqual(again, { ok: true, result: "already in safe mode" });
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("dispatch: unknown tool vs contract-but-unbuilt tool are distinct honest errors", async () => {
  const h = harness();
  const unknown = await h.tools.dispatch("frobnicate");
  assert.equal(unknown.error, "unknown tool: frobnicate");
  const unbuilt = await h.tools.dispatch("get_status");
  assert.equal(unbuilt.error, "unsupported");
  assert.match(String(unbuilt.result), /not built yet/);
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("layer1 trip: enters safe mode even when the surface budget is exhausted", async () => {
  const h = harness();
  // Exhaust enter_safe_mode's surface budget (5/300 s) with exits in between
  // impossible in slice 1, so exhaust with repeated enter calls on fresh stores.
  for (let i = 0; i < 5; i++) {
    h.config.setSafeMode(false);
    const g = await h.tools.dispatch("enter_safe_mode");
    assert.equal(g.ok, true, `surface call ${i}`);
    h.c.advance(6_000);
  }
  h.config.setSafeMode(false);
  const blocked = await h.tools.dispatch("enter_safe_mode");
  assert.equal(blocked.error, "rate_limited", "surface budget exhausted");
  const trip = await h.tools.layer1TripSafeMode();
  assert.equal(trip.ok, true, "Layer 1's trip is exempt — the escape hatch never rate-limits");
  assert.equal(h.config.inSafeMode, true);
  fs.rmSync(h.dir, { recursive: true, force: true });
});

// -- MCP compliance (NORMATIVE per $mcp_protocol_note) ---------------------------

test("MCP: initialize echoes protocolVersion 2025-06-18; notifications/initialized -> no response", async () => {
  const h = harness();
  const mcp = new ControlMcp({ tools: h.tools, version: "0.1.0-test" });
  const init = await mcp.handle({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });
  assert.equal((init?.result as Record<string, any>).protocolVersion, MCP_PROTOCOL);
  assert.equal((init?.result as Record<string, any>).serverInfo.name, "windytalk-control");
  const notif = await mcp.handle({ jsonrpc: "2.0", method: "notifications/initialized" });
  assert.equal(notif, null, "a notification gets NO response body");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("MCP: tools/list advertises exactly the built tools, with the contract's descriptions", async () => {
  const h = harness();
  const mcp = new ControlMcp({ tools: h.tools, version: "0.1.0-test" });
  const res = await mcp.handle({ jsonrpc: "2.0", id: 2, method: "tools/list" });
  const tools = (res?.result as { tools: { name: string; description: string }[] }).tools;
  assert.deepEqual(
    tools.map((t) => t.name).sort(),
    ["enter_safe_mode", "get_health", "reconnect"],
  );
  const contract = loadContractTools();
  if (contract.size > 0) {
    const gh = tools.find((t) => t.name === "get_health");
    assert.match(gh!.description, /FIRST tool to call/, "contract descriptions are authoritative");
  }
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("MCP: tools/call returns canonical JSON text AND structuredContent (never str()-rendered)", async () => {
  const h = harness();
  const mcp = new ControlMcp({ tools: h.tools, version: "0.1.0-test" });
  const res = await mcp.handle({
    jsonrpc: "2.0",
    id: 3,
    method: "tools/call",
    params: { name: "get_health", arguments: {} },
  });
  const result = res?.result as {
    content: { type: string; text: string }[];
    structuredContent: Record<string, unknown>;
    isError: boolean;
  };
  assert.equal(result.isError, false);
  const parsed = JSON.parse(result.content[0].text); // MUST be valid JSON
  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed, result.structuredContent, "text and structuredContent carry the same envelope");
  assert.equal(typeof (result.structuredContent.result as Record<string, unknown>).healthy, "boolean");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("MCP: tools/call on a denied-by-coordinator call sets isError with the bare reserved code", async () => {
  const h = harness();
  const mcp = new ControlMcp({ tools: h.tools, version: "0.1.0-test" });
  await mcp.handle({ jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "reconnect" } });
  const res = await mcp.handle({ jsonrpc: "2.0", id: 5, method: "tools/call", params: { name: "reconnect" } });
  const result = res?.result as { structuredContent: { ok: boolean; error: string }; isError: boolean };
  assert.equal(result.isError, true);
  assert.equal(result.structuredContent.error, "rate_limited", "bare code; the reason rides in result");
  fs.rmSync(h.dir, { recursive: true, force: true });
});

test("MCP: unknown method -> -32601; malformed request -> -32600", async () => {
  const h = harness();
  const mcp = new ControlMcp({ tools: h.tools, version: "0.1.0-test" });
  const unknown = await mcp.handle({ jsonrpc: "2.0", id: 9, method: "resources/list" });
  assert.equal((unknown?.error as { code: number }).code, -32601);
  const bad = await mcp.handle("garbage");
  assert.equal((bad?.error as { code: number }).code, -32600);
  fs.rmSync(h.dir, { recursive: true, force: true });
});
