// QA-hardening regression tests — each pins a defect a review pass found in the
// merged slices, so it can never silently return. Grouped by the reviewed area.
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
import { ControlMcp } from "../electron/control/mcp.js";
import { scrubShortError } from "../electron/control/scrub.js";
import { ControlTools, OFFLINE_STATUS, type Confirmer, type ToolDeps } from "../electron/control/tools.js";

function clock(startMs = 30_000_000_000) {
  let t = startMs;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

function toolsHarness(over: Partial<ToolDeps> = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-qa-"));
  const c = clock();
  const config = new ConfigStore(dir);
  const confirmOutcome = { value: "allow" as Awaited<ReturnType<Confirmer>> };
  const confirmCalls: string[] = [];
  const emitted: Record<string, unknown>[] = [];
  const status = { ...OFFLINE_STATUS, connection: "online" as const };
  const deps: ToolDeps = {
    coordinator: new RecoveryCoordinator({ now: c.now }),
    config,
    allowList: new EngineAllowList(dir),
    detector: new CrashLoopDetector({ now: c.now, tripSafeMode: () => {} }),
    rendererStatus: () => status,
    reconnectEngine: async () => true,
    applyActiveConfig: () => {},
    resurrectionArmed: () => true,
    version: "qa",
    startedAtMs: c.now(),
    emit: (f) => emitted.push(f),
    logs: new LogRing({ now: c.now }),
    probe: async () => null,
    confirm: async (r) => {
      confirmCalls.push(r.tool);
      return confirmOutcome.value;
    },
    lkg: new LkgStore(dir),
    deepReconnectEngine: async () => true,
    clearCaches: async () => {},
    repairResurrection: async () => ({ armed: true, detail: "armed" }),
    restartApp: () => {},
    resetCrashCounter: () => {},
    entitledBrains: () => [],
    now: c.now,
    reconnectTimeoutMs: 100,
    ...over,
  };
  return { tools: new ControlTools(deps), config, confirmOutcome, confirmCalls, emitted, c, dir };
}

// -- scrub: network identifiers (diagnostics_privacy.never lists IP/MAC/SSID) -------

test("scrub: IP / IPv6 / MAC / email / SSID are redacted; version strings are NOT mangled", () => {
  assert.match(scrubShortError("ECONNREFUSED 192.168.1.50:8788")!, /<ip>:8788/);
  assert.ok(!scrubShortError("route via 10.10.0.6")!.includes("10.10.0.6"));
  assert.ok(!scrubShortError("peer fe80::1ff:fe23:4567:890a")!.includes("fe80"));
  assert.ok(!scrubShortError("MAC a4:83:e7:2b:1c:9f")!.includes("a4:83"));
  assert.ok(!scrubShortError("user grant@windstorm.uk")!.includes("@windstorm"));
  assert.ok(!scrubShortError("SSID WhitmerHouse-5G")!.includes("WhitmerHouse"));
  // Versions have only 3 dot-groups, not 4 — must survive.
  assert.equal(scrubShortError("version 1.2.3 ready"), "version 1.2.3 ready");
  assert.equal(scrubShortError("codec cs8409 at 3.4.0"), "codec cs8409 at 3.4.0");
});

test("scrub GOLDEN gap: a bare IP-bearing error with NO other cut marker still gets redacted", () => {
  // The original golden test only caught IP/SSID incidentally (downstream of a
  // path/transcript cut). This asserts the network scrub fires on its own.
  const out = scrubShortError("connect ETIMEDOUT 192.168.1.77:8788 while dialing")!;
  assert.ok(!out.includes("192.168.1.77"), out);
});

// -- coordinator/dispatch: preempt discard is reachable again (was dead code) --------

test("preempt: a preempted handler's result is DISCARDED as already_recovering (not returned as success)", async () => {
  // reconnect is in flight (never resolves during this test); enter_safe_mode
  // preempts it. When reconnect finally returns, dispatch must yield
  // already_recovering, not the stale 'reconnected'.
  let releaseReconnect: (v: boolean) => void = () => {};
  const h = toolsHarness({
    reconnectEngine: () => new Promise<boolean>((r) => (releaseReconnect = r)),
  });
  const reconnectPromise = h.tools.dispatch("reconnect");
  await new Promise((r) => setTimeout(r, 10)); // let reconnect acquire the lock + start
  const safe = await h.tools.dispatch("enter_safe_mode"); // preempts
  assert.equal(safe.ok, true);
  releaseReconnect(true); // the abandoned reconnect now completes
  const reconnectResult = await reconnectPromise;
  assert.equal(reconnectResult.ok, false, "the preempted result must be discarded");
  assert.equal(reconnectResult.error, "already_recovering");
  // And a 'preempted' telemetry event fired for reconnect.
  const preempted = h.emitted.find((e) => e.tool === "reconnect" && e.error_code === "preempted");
  assert.ok(preempted, "the preempted handler must emit a preempted control.action");
  void h;
});

test("preempt: a RATE-LIMITED enter_safe_mode does NOT clear the lock / abandon the holder", () => {
  const c = clock();
  const coord = new RecoveryCoordinator({ now: c.now });
  // Exhaust enter_safe_mode's own budget so the next one is rate-limited.
  for (let i = 0; i < 5; i++) {
    const g = coord.gate("enter_safe_mode");
    assert.ok(g.proceed);
    if (g.proceed) {
      g.ticket.commit();
      g.ticket.release();
    }
    c.advance(6_000);
  }
  // A holder takes the lock.
  const held = coord.gate("reconnect");
  assert.ok(held.proceed, "reconnect should acquire the lock");
  if (!held.proceed) return;
  assert.equal(coord.lockHolderTool, "reconnect");
  // enter_safe_mode is now rate-limited AND the lock is held. It must be
  // rejected WITHOUT preempting (the old bug cleared the lock then returned
  // rate_limited, freeing it for a concurrent recovery).
  const blocked = coord.gate("enter_safe_mode");
  assert.equal(blocked.proceed, false);
  assert.equal((blocked as { error: string }).error, "rate_limited");
  assert.equal(coord.lockHolderTool, "reconnect", "the lock must STILL be held by reconnect");
  assert.equal(held.ticket.abandoned, false, "the holder must NOT have been abandoned");
  held.ticket.release();
});

test("confirmer rejection releases the lock (does not leak it to the 30 s ceiling)", async () => {
  const h = toolsHarness({
    confirm: async () => {
      throw new Error("dialog module blew up");
    },
  });
  // restart_engine is ask_first + a lock holder; the confirmer throws.
  const res = await h.tools.dispatch("restart_engine");
  assert.equal(res.ok, false, "a thrown confirmer must not succeed");
  // The lock must be free immediately — a following holder proceeds.
  const next = h.tools.dispatch("reconnect");
  assert.equal((await next).ok !== undefined, true);
  // Prove the lock was released: enter_safe_mode (a fresh holder) can acquire.
  const coord = (h.tools as unknown as { deps: { coordinator: RecoveryCoordinator } }).deps.coordinator;
  assert.equal(coord.recovering, false, "no lock should be held after a rejected confirmer");
});

test("denied call still releases the lock and charges nothing", async () => {
  const h = toolsHarness();
  h.confirmOutcome.value = "deny";
  const denied = await h.tools.dispatch("restart_engine");
  assert.deepEqual(denied, { ok: false, error: "denied" });
  const coord = (h.tools as unknown as { deps: { coordinator: RecoveryCoordinator } }).deps.coordinator;
  assert.equal(coord.recovering, false, "denied must release the lock");
});

// -- restart_app stranding guard (platform_note) ------------------------------------

test("restart_app: refuses with 'unsupported' when resurrection is unarmed (never strands the user)", async () => {
  let exited = 0;
  const h = toolsHarness({ resurrectionArmed: () => false, restartApp: () => exited++ });
  h.config.setSaved({ autonomy: 8 }); // dodge the ask_first prompt
  const res = await h.tools.dispatch("restart_app");
  assert.equal(res.ok, false);
  assert.equal(res.error, "unsupported");
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(exited, 0, "the app must NOT exit when nothing would relaunch it");
});

// -- set_audio_* device_id validation ------------------------------------------------

test("set_audio_input/output: a missing device_id is rejected, never persisted as 'undefined'", async () => {
  const h = toolsHarness();
  h.config.setSaved({ autonomy: 8 });
  const badIn = await h.tools.dispatch("set_audio_input", {});
  assert.equal(badIn.ok, false);
  assert.match(String(badIn.error), /invalid device_id/);
  assert.equal(h.config.getSaved().audio_input_id, null, "nothing was written");
  const badOut = await h.tools.dispatch("set_audio_output", { device_id: "" });
  assert.equal(badOut.ok, false);
});

// -- MCP JSON-RPC hygiene ------------------------------------------------------------

test("MCP: a batch (array) body -> -32600 Invalid Request, not a silent notification", async () => {
  const h = toolsHarness();
  const mcp = new ControlMcp({ tools: h.tools, version: "qa" });
  const res = await mcp.handle([{ jsonrpc: "2.0", id: 1, method: "ping" }]);
  assert.equal((res?.error as { code: number }).code, -32600);
});

test("MCP: a REQUEST method sent WITHOUT an id does not execute and returns no response", async () => {
  let exited = 0;
  const h = toolsHarness({ resurrectionArmed: () => true, restartApp: () => exited++ });
  const mcp = new ControlMcp({ tools: h.tools, version: "qa" });
  // tools/call with no id — a JSON-RPC notification. Must NOT run restart_app.
  const res = await mcp.handle({ jsonrpc: "2.0", method: "tools/call", params: { name: "restart_app" } });
  assert.equal(res, null, "a notification gets no response");
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(exited, 0, "a request method as a notification must not execute");
});

test("MCP: notifications/initialized still returns null; a real request still answers", async () => {
  const h = toolsHarness();
  const mcp = new ControlMcp({ tools: h.tools, version: "qa" });
  assert.equal(await mcp.handle({ jsonrpc: "2.0", method: "notifications/initialized" }), null);
  const init = await mcp.handle({ jsonrpc: "2.0", id: 5, method: "initialize", params: {} });
  assert.equal((init?.result as Record<string, unknown>).protocolVersion, "2025-06-18");
  assert.equal(init?.id, 5);
});

test("repair_resurrection capability: FALSE where re-arming is not feasible (privilege-blocked), TRUE otherwise", async () => {
  const blocked = toolsHarness({ resurrectionRepairable: () => false });
  const capsBlocked = (await blocked.tools.dispatch("get_capabilities")).result as {
    tools: Record<string, boolean | string>;
  };
  assert.equal(capsBlocked.tools.repair_resurrection, false, "must be false where re-arming can't work here");
  const ok = toolsHarness({ resurrectionRepairable: () => true });
  const capsOk = (await ok.tools.dispatch("get_capabilities")).result as {
    tools: Record<string, boolean | string>;
  };
  assert.equal(capsOk.tools.repair_resurrection, true);
});
