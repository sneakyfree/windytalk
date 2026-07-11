// Single-instance lock + takeover (contract resurrection.single_instance),
// exercised END-TO-END: real lock sockets, real child processes as holders,
// real SIGKILLs on the takeover path, and the pinned safety-inverse assertions
// (a HEALTHY holder is never killed; a foreign/mismatched holder is never killed).
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";

import { acquireInstanceLock, type InstanceDeps } from "../electron/control/instance.js";
import { procIdentity, selfIdentity, type IdentityRecord } from "../electron/control/identity.js";
import { ControlServer } from "../electron/control/server.js";

const TOKEN = "instance-test-token";
const children: ChildProcess[] = [];
const cleanups: (() => void)[] = [];

after(() => {
  children.forEach((c) => {
    try {
      c.kill("SIGKILL");
    } catch {
      /* gone */
    }
  });
  cleanups.forEach((f) => f());
});

function tempPaths() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-inst-"));
  cleanups.push(() => fs.rmSync(dir, { recursive: true, force: true }));
  return {
    dir,
    socketPath: path.join(dir, "instance.sock"),
    lockFilePath: path.join(dir, "instance.lock"),
    portFilePath: path.join(dir, "control-port"),
  };
}

function deps(p: ReturnType<typeof tempPaths>, extra: Partial<InstanceDeps> = {}): InstanceDeps {
  return {
    socketPath: p.socketPath,
    lockFilePath: p.lockFilePath,
    portFilePath: p.portFilePath,
    readToken: () => TOKEN,
    ackTimeoutMs: 400,
    ackRetries: 1,
    ...extra,
  };
}

/** A real child that binds the lock socket and answers {pid}, nothing else. */
function spawnSocketHolder(socketPath: string): Promise<ChildProcess> {
  const script = `
    const net = require("net");
    const srv = net.createServer((c) => c.end(JSON.stringify({ pid: process.pid })));
    srv.listen(process.argv[1], () => console.log("ready"));
    setInterval(() => {}, 1000);
  `;
  const child = spawn(process.execPath, ["-e", script, socketPath]);
  children.push(child);
  return new Promise((resolve, reject) => {
    child.stdout?.on("data", (d) => String(d).includes("ready") && resolve(child));
    child.on("error", reject);
    child.on("exit", () => reject(new Error("holder died before ready")));
  });
}

async function writeLockRecordFor(pid: number, lockFilePath: string): Promise<IdentityRecord> {
  const live = await procIdentity(pid);
  assert.ok(live.alive && live.exe && live.started_at != null);
  const record: IdentityRecord = { pid, started_at: live.started_at!, exe: live.exe! };
  fs.writeFileSync(lockFilePath, JSON.stringify(record));
  return record;
}

test("fresh claim: no holder -> role holder, lock content {pid, started_at, exe} written", async () => {
  const p = tempPaths();
  const r = await acquireInstanceLock(deps(p));
  assert.equal(r.role, "holder");
  const content = JSON.parse(fs.readFileSync(p.lockFilePath, "utf8"));
  assert.equal(content.pid, process.pid);
  assert.equal(content.exe, process.execPath);
  assert.equal(typeof content.started_at, "number");
  if (r.role === "holder") r.release();
});

test("stale socket file (holder SIGKILLed) is cleared and claimed", async () => {
  const p = tempPaths();
  const holder = await spawnSocketHolder(p.socketPath);
  holder.kill("SIGKILL");
  await new Promise((r) => holder.on("exit", r));
  // The dead holder's socket FILE remains; the kernel released the listener.
  assert.ok(fs.existsSync(p.socketPath), "precondition: stale socket file present");
  const r = await acquireInstanceLock(deps(p));
  assert.equal(r.role, "holder");
  if (r.role === "holder") r.release();
});

test("SAFETY-INVERSE: healthy holder acks -> second instance focuses + exits; holder UNINTERRUPTED", async () => {
  const p = tempPaths();
  // The "holder" is this process: lock socket + a real serving :8782-style wall.
  const first = await acquireInstanceLock(deps(p));
  assert.equal(first.role, "holder");
  let focused = 0;
  const server = new ControlServer({ token: TOKEN, port: 0, onFocusRequest: () => focused++ });
  const bind = await server.bind();
  assert.ok(bind.ok);
  fs.writeFileSync(p.portFilePath, String((bind as { port: number }).port));

  let killed = 0;
  const second = await acquireInstanceLock(
    deps(p, { kill: () => killed++, identity: await selfIdentity() }),
  );
  assert.equal(second.role, "second-focused");
  assert.equal(focused, 1, "the ack must carry the focus request");
  assert.equal(killed, 0, "a healthy holder must NEVER be killed");
  server.close();
  if (first.role === "holder") first.release();
});

test("wedged holder (no ack, identity MATCH) -> SIGKILL + takeover; second instance becomes the holder", async () => {
  const p = tempPaths();
  const holder = await spawnSocketHolder(p.socketPath); // binds the lock, serves NO :8782
  await writeLockRecordFor(holder.pid!, p.lockFilePath);

  const exited = new Promise<void>((r) => holder.on("exit", () => r()));
  const r = await acquireInstanceLock(deps(p));
  assert.equal(r.role, "holder", "the dock-click on a wedged holder must relaunch via takeover");
  await exited;
  assert.equal(holder.exitCode, null, "killed by signal, not clean exit");
  assert.equal(holder.signalCode, "SIGKILL");
  // And the takeover rewrote the lock content to OUR identity.
  const content = JSON.parse(fs.readFileSync(p.lockFilePath, "utf8"));
  assert.equal(content.pid, process.pid);
  if (r.role === "holder") r.release();
});

test("SAFETY-INVERSE: identity MISMATCH (recorded holder gone) -> squatter surfaced, live process NEVER killed", async () => {
  const p = tempPaths();
  const squatter = await spawnSocketHolder(p.socketPath);
  // Recorded holder: same pid but a different identity (a recycled-pid story).
  const live = await procIdentity(squatter.pid!);
  fs.writeFileSync(
    p.lockFilePath,
    JSON.stringify({ pid: squatter.pid, started_at: live.started_at! - 5000, exe: live.exe }),
  );
  const r = await acquireInstanceLock(deps(p));
  assert.equal(r.role, "squatter");
  assert.equal(squatter.exitCode, null, "squatter must still be alive");
  assert.equal(squatter.killed, false, "we must never signal a mismatched holder");
  squatter.kill("SIGKILL");
});

test("unreadable lock content with a live socket holder -> squatter (never a blind kill)", async () => {
  const p = tempPaths();
  const holderChild = await spawnSocketHolder(p.socketPath);
  fs.writeFileSync(p.lockFilePath, "not json {{{");
  const r = await acquireInstanceLock(deps(p));
  assert.equal(r.role, "squatter");
  assert.equal(holderChild.exitCode, null);
  holderChild.kill("SIGKILL");
});

test("claim race: two concurrent acquires -> exactly one holder", async () => {
  const p = tempPaths();
  // Both racers share this process's identity; a served ack lets the loser
  // resolve to second-focused. Serve the wall up-front.
  let focused = 0;
  const server = new ControlServer({ token: TOKEN, port: 0, onFocusRequest: () => focused++ });
  const bind = await server.bind();
  assert.ok(bind.ok);
  fs.writeFileSync(p.portFilePath, String((bind as { port: number }).port));

  const [a, b] = await Promise.all([acquireInstanceLock(deps(p)), acquireInstanceLock(deps(p))]);
  const roles = [a.role, b.role].sort();
  assert.deepEqual(roles, ["holder", "second-focused"], `got ${roles.join(",")}`);
  for (const r of [a, b]) if (r.role === "holder") r.release();
  server.close();
});
