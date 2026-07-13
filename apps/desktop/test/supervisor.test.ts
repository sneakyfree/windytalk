// Supervisor bus tests + the :8782 tool routes end-to-end through the wall.
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";

import { TOKEN_HEADER } from "../electron/control/constants.js";
import { ConfigStore } from "../electron/control/config.js";
import { RecoveryCoordinator } from "../electron/control/coordinator.js";
import { EngineAllowList } from "../electron/control/engine-allow.js";
import { CrashLoopDetector } from "../electron/control/layer1.js";
import { LogRing } from "../electron/control/logring.js";
import { ControlMcp } from "../electron/control/mcp.js";
import { ControlServer } from "../electron/control/server.js";
import { Supervisor, type RendererCommand } from "../electron/control/supervisor.js";
import { ControlTools, OFFLINE_STATUS } from "../electron/control/tools.js";

const TOKEN = "sup-test-token";
const cleanups: (() => void)[] = [];
after(() => cleanups.forEach((f) => f()));

test("supervisor: online->offline is a RESTART; offline->offline retries are an outage, not restarts", () => {
  const restarts: string[] = [];
  const detector = new CrashLoopDetector({ tripSafeMode: () => {} });
  const origRecord = detector.recordRestart.bind(detector);
  detector.recordRestart = (w: string) => {
    restarts.push(w);
    origRecord(w);
  };
  const sup = new Supervisor({ detector, sendCommand: () => {} });
  sup.onRendererStatus({ ...OFFLINE_STATUS, connection: "online" });
  sup.onRendererStatus({ ...OFFLINE_STATUS, connection: "offline" });
  assert.equal(restarts.length, 1, "came up then died = restart");
  sup.onRendererStatus({ ...OFFLINE_STATUS, connection: "connecting" });
  sup.onRendererStatus({ ...OFFLINE_STATUS, connection: "offline" });
  sup.onRendererStatus({ ...OFFLINE_STATUS, connection: "connecting" });
  sup.onRendererStatus({ ...OFFLINE_STATUS, connection: "offline" });
  assert.equal(restarts.length, 1, "failed retries during an outage never count");
});

test("supervisor: reconnectEngine sends the command and resolves once status turns online", async () => {
  const commands: RendererCommand[] = [];
  const detector = new CrashLoopDetector({ tripSafeMode: () => {} });
  const sup = new Supervisor({ detector, sendCommand: (c) => commands.push(c) });
  sup.onRendererStatus({ ...OFFLINE_STATUS, connection: "offline" });
  const pending = sup.reconnectEngine(3_000);
  assert.deepEqual(commands, [{ type: "reconnect" }]);
  setTimeout(() => sup.onRendererStatus({ ...OFFLINE_STATUS, connection: "online" }), 100);
  assert.equal(await pending, true);
});

test("supervisor: reconnectEngine times out honestly when the engine never comes back", async () => {
  const detector = new CrashLoopDetector({ tripSafeMode: () => {} });
  const sup = new Supervisor({ detector, sendCommand: () => {} });
  sup.onRendererStatus({ ...OFFLINE_STATUS, connection: "offline" });
  assert.equal(await sup.reconnectEngine(300), false);
});

test("supervisor: a gone/hung renderer is reloaded and counted as a restart", () => {
  const detector = new CrashLoopDetector({ tripSafeMode: () => {} });
  const sup = new Supervisor({ detector, sendCommand: () => {} });
  let reloaded = 0;
  sup.onRendererGone("crashed", () => reloaded++);
  assert.equal(reloaded, 1);
  assert.equal(detector.restarts, 1);
  assert.equal(sup.rendererStatus().connection, "offline");
});

test("HTTP routes: /tools, /invoke, /mcp all behind the wall; MCP notification -> 204", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-routes-"));
  cleanups.push(() => fs.rmSync(dir, { recursive: true, force: true }));
  const config = new ConfigStore(dir);
  const detector = new CrashLoopDetector({ tripSafeMode: () => {} });
  const tools = new ControlTools({
    coordinator: new RecoveryCoordinator(),
    config,
    allowList: new EngineAllowList(dir),
    detector,
    rendererStatus: () => ({ ...OFFLINE_STATUS, connection: "online" }),
    reconnectEngine: async () => true,
    applyActiveConfig: () => {},
    resurrectionArmed: () => false,
    version: "t",
    startedAtMs: Date.now(),
    emit: () => {},
    logs: new LogRing(),
    confirm: async () => "allow",
    lkg: { invalidateAll() {}, write() {}, loadBest: () => null } as any,
    deepReconnectEngine: async () => true,
    clearCaches: async () => {},
    repairResurrection: async () => ({ armed: true, detail: "armed" }),
    restartApp: () => {},
    resetCrashCounter: () => {},
    entitledBrains: () => [],
    probe: async () => null,
  });
  const mcp = new ControlMcp({ tools, version: "t" });
  const server = new ControlServer({
    token: TOKEN,
    port: 0,
    dispatch: (t, a) => tools.dispatch(t, a),
    toolList: () => mcp.toolList(),
    mcp: (r) => mcp.handle(r),
  });
  const bind = await server.bind();
  assert.ok(bind.ok);
  const port = (bind as { port: number }).port;
  cleanups.push(() => server.close());

  const call = (opts: http.RequestOptions, body?: string) =>
    new Promise<{ status: number; body: string }>((resolve, reject) => {
      const req = http.request({ host: "127.0.0.1", port, ...opts }, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode ?? 0, body: data }));
      });
      req.on("error", reject);
      if (body) req.write(body);
      req.end();
    });
  const auth = { [TOKEN_HEADER]: TOKEN, "Content-Type": "application/json" };

  // The wall gates the new routes exactly like /ping.
  const anon = await call({ path: "/tools", method: "GET" });
  assert.equal(anon.status, 401);

  const list = await call({ path: "/tools", method: "GET", headers: auth });
  assert.equal(list.status, 200);
  const names = (JSON.parse(list.body).tools as { name: string }[]).map((t) => t.name);
  assert.ok(names.includes("get_health") && names.includes("reconnect") && names.includes("get_logs"));

  // ADR-060 §3.2 canonical native shape: {name, arguments} -> {ok, result}.
  const invoke = await call(
    { path: "/invoke", method: "POST", headers: auth },
    JSON.stringify({ name: "get_health", arguments: {} }),
  );
  assert.equal(invoke.status, 200);
  const health = JSON.parse(invoke.body);
  assert.equal(health.ok, true);
  assert.equal(typeof health.result.healthy, "boolean");

  // Legacy {tool, args} shape still accepted (back-compat, non-breaking).
  const legacy = await call(
    { path: "/invoke", method: "POST", headers: auth },
    JSON.stringify({ tool: "get_health", args: {} }),
  );
  assert.equal(JSON.parse(legacy.body).ok, true);

  const mcpInit = await call(
    { path: "/mcp", method: "POST", headers: auth },
    JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }),
  );
  assert.equal(mcpInit.status, 200);
  assert.equal(JSON.parse(mcpInit.body).result.protocolVersion, "2025-06-18");

  const notif = await call(
    { path: "/mcp", method: "POST", headers: auth },
    JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
  );
  assert.equal(notif.status, 204, "a JSON-RPC notification gets no body");

  const badJson = await call({ path: "/invoke", method: "POST", headers: auth }, "not json");
  assert.equal(badJson.status, 400);
});
