// The :8782 wall (contract `security`) — the same proven gate as hands/surface.py:
// loopback bind, reject any Origin, constant-time per-install token. Real HTTP
// against an ephemeral port (the port FILE, not the number, is the discovery
// mechanism, so tests never fight over 8782).
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";

import { TOKEN_HEADER } from "../electron/control/constants.js";
import { HeartbeatWriter, readHeartbeat } from "../electron/control/heartbeat.js";
import { makeAttestor } from "../electron/control/attest.js";
import { selfIdentity } from "../electron/control/identity.js";
import { ControlServer, helloControlPort, pingControlPort } from "../electron/control/server.js";
import { loadOrCreateToken, tokenEquals } from "../electron/control/token.js";

const TOKEN = "test-token-abc";
const servers: ControlServer[] = [];

async function boundServer(opts: Partial<ConstructorParameters<typeof ControlServer>[0]> = {}) {
  const server = new ControlServer({ token: TOKEN, port: 0, ...opts });
  const bind = await server.bind();
  assert.ok(bind.ok, "bind must succeed on an ephemeral port");
  servers.push(server);
  return { server, port: (bind as { ok: true; port: number }).port };
}

after(() => servers.forEach((s) => s.close()));

function raw(
  port: number,
  reqOpts: http.RequestOptions,
  body?: string,
): Promise<{ status: number; json: any }> {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, ...reqOpts }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode ?? 0, json: JSON.parse(data) });
        } catch {
          resolve({ status: res.statusCode ?? 0, json: null });
        }
      });
    });
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

test("any Origin header is rejected 403 — even with a valid token", async () => {
  const { port } = await boundServer();
  const res = await raw(port, {
    path: "/ping",
    method: "GET",
    headers: { Origin: "https://evil.example", [TOKEN_HEADER]: TOKEN },
  });
  assert.equal(res.status, 403);
  assert.deepEqual(res.json, { ok: false, error: "forbidden" });
});

test("missing/wrong token is 401 unauthorized", async () => {
  const { port } = await boundServer();
  for (const headers of [{}, { [TOKEN_HEADER]: "nope" }, { [TOKEN_HEADER]: TOKEN + "x" }]) {
    const res = await raw(port, { path: "/ping", method: "GET", headers });
    assert.equal(res.status, 401);
    assert.equal(res.json.error, "unauthorized");
  }
});

test("non-loopback Host header is rejected (DNS-rebinding guard)", async () => {
  const { port } = await boundServer();
  const res = await raw(port, {
    path: "/ping",
    method: "GET",
    headers: { Host: "evil.example:8782", [TOKEN_HEADER]: TOKEN },
  });
  assert.equal(res.status, 403);
  assert.equal(res.json.error, "forbidden host");
});

test("OPTIONS preflight is never honored (no CORS)", async () => {
  const { port } = await boundServer();
  const res = await raw(port, { path: "/ping", method: "OPTIONS" });
  assert.equal(res.status, 405);
});

test("valid /ping serves {ok, result:{pong, pid}} and records a served round-trip", async () => {
  const { server, port } = await boundServer();
  assert.equal(server.lastServedAt, 0);
  const res = await pingControlPort(port, TOKEN, 1000);
  assert.equal(res.ok, true);
  assert.equal(res.pid, process.pid);
  assert.ok(server.lastServedAt > 0, "a served round-trip must be recorded for attestation");
});

test("/instance/hello acks with the holder pid and fires the focus hook", async () => {
  let focused = 0;
  const { port } = await boundServer({ onFocusRequest: () => focused++ });
  const res = await helloControlPort(port, TOKEN, 1000);
  assert.equal(res.ok, true);
  assert.equal(res.pid, process.pid);
  assert.equal(focused, 1);
});

test("unknown path is 404; guard runs first (a 404 probe without a token is 401)", async () => {
  const { port } = await boundServer();
  const ok = await raw(port, { path: "/nope", method: "GET", headers: { [TOKEN_HEADER]: TOKEN } });
  assert.equal(ok.status, 404);
  const anon = await raw(port, { path: "/nope", method: "GET" });
  assert.equal(anon.status, 401);
});

test("token file: minted once 0600, persists across loads, env override wins", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-token-"));
  const p = path.join(dir, "control-token");
  const t1 = loadOrCreateToken(p, {});
  const t2 = loadOrCreateToken(p, {});
  assert.equal(t1, t2, "per-install token must persist (never per-launch)");
  assert.match(t1, /^[0-9a-f]{48}$/);
  if (process.platform !== "win32") {
    assert.equal(fs.statSync(p).mode & 0o777, 0o600);
  }
  assert.equal(loadOrCreateToken(p, { WINDYTALK_CONTROL_TOKEN: "override" }), "override");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("tokenEquals: constant-time compare handles unequal lengths without throwing", () => {
  assert.equal(tokenEquals("a", "abc"), false);
  assert.equal(tokenEquals("abc", "abc"), true);
  assert.equal(tokenEquals("", "abc"), false);
});

test("heartbeat is fate-coupled to serving: attest false -> no bump; attest true -> bump", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-hb-"));
  const hbPath = path.join(dir, "heartbeat");
  const identity = await selfIdentity();
  let serving = true;
  const writer = new HeartbeatWriter({
    heartbeatPath: hbPath,
    identity,
    attest: async () => serving,
    intervalMs: 3600_000, // ticks driven manually
  });
  assert.equal(await writer.tick(), true);
  const first = readHeartbeat(hbPath);
  assert.ok(first);
  assert.deepEqual(first.record, identity, "content is exactly {pid, started_at, exe}");

  serving = false; // the serving loop "stopped" — the writer must go quiet
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(await writer.tick(), false);
  const second = readHeartbeat(hbPath);
  assert.equal(second?.mtimeMs, first.mtimeMs, "no attestation -> no bump -> file goes stale");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("attestor: recent served :8782 round-trip attests without a self-ping; otherwise a real self round-trip is required", async () => {
  const { server, port } = await boundServer();
  const attest = makeAttestor(server, port, TOKEN);
  // No traffic yet -> falls through to the self-ping, which succeeds (round-trip
  // through our own live event loop) and verifies OUR pid answered.
  assert.equal(await attest(), true);
  // Now lastServedAt is fresh (the self-ping was served) -> fast path.
  assert.ok(server.lastServedAt > 0);
  assert.equal(await attest(), true);
});

test("attestor: a dead server cannot attest (no bare-accept liveness)", async () => {
  const { server, port } = await boundServer();
  server.close();
  await new Promise((r) => setTimeout(r, 50));
  const attest = makeAttestor(server, port, TOKEN);
  assert.equal(await attest(), false);
});
