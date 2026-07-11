// Live slice-0 integration on the host OS: a REAL child supervisor writing a
// real heartbeat, really SIGKILLed / SIGSTOPped, judged by the watcher with the
// real fs + real /proc identity. Staleness ages are simulated with utimes (the
// tiers read mtime), so the suite runs in seconds, not minutes.
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, test } from "node:test";

import { procIdentity, pidAlive } from "../electron/control/identity.js";
import { checkOnce, type WatcherDeps } from "../electron/resurrection/watcher.js";

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

function tempDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-live-"));
  cleanups.push(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

/**
 * A real "supervisor": bumps a heartbeat every 150 ms. A FRESH heartbeat never
 * has its identity read (the tiers only verify identity once stale), so the
 * child records itself without needing an out-of-band identity handshake.
 */
async function spawnSupervisor(hbPath: string): Promise<ChildProcess> {
  const script = `
    const fs = require("fs");
    const hb = process.argv[1];
    const record = JSON.stringify({ pid: process.pid, started_at: 0, exe: process.execPath });
    const bump = () => { try { fs.writeFileSync(hb, record); } catch {} };
    bump();
    setInterval(bump, 150);
    console.log("up");
  `;
  const child = spawn(process.execPath, ["-e", script, hbPath]);
  children.push(child);
  await new Promise((r) => child.stdout?.on("data", r));
  return child;
}

function liveDeps(hbPath: string, hooks: { relaunches: string[] }): WatcherDeps {
  return {
    heartbeatPath: hbPath,
    relaunch: () => {
      hooks.relaunches.push("x");
      return true;
    },
    loadState: () => ({ relaunches: [], slow: false, freshSince: null }),
    saveState: () => true,
  };
}

function ageHeartbeat(hbPath: string, ageS: number): void {
  const past = new Date(Date.now() - ageS * 1000);
  fs.utimesSync(hbPath, past, past);
}

async function writeIdentityHeartbeat(hbPath: string, pid: number, skewStart = 0): Promise<void> {
  const live = await procIdentity(pid);
  assert.ok(live.alive && live.exe && live.started_at != null);
  fs.writeFileSync(
    hbPath,
    JSON.stringify({ pid, started_at: live.started_at! + skewStart, exe: live.exe }),
  );
}

test("live SIGKILL: dead supervisor with a stale heartbeat is relaunched (tier1)", async () => {
  const dir = tempDir();
  const hb = path.join(dir, "heartbeat");
  const child = spawn(process.execPath, ["-e", "setInterval(()=>{},1000)"]);
  children.push(child);
  await new Promise((r) => setTimeout(r, 100));
  await writeIdentityHeartbeat(hb, child.pid!);

  child.kill("SIGKILL");
  await new Promise((r) => child.on("exit", r));
  ageHeartbeat(hb, 40); // past the 30 s tier-1 line

  const relaunches: string[] = [];
  const action = await checkOnce(liveDeps(hb, { relaunches }));
  assert.equal(action, "relaunch");
  assert.equal(relaunches.length, 1);
});

test("live SIGSTOP wedge: pid alive-by-identity, heartbeat stale >90s -> SIGKILLed + relaunched (tier2)", async () => {
  const dir = tempDir();
  const hb = path.join(dir, "heartbeat");
  const child = spawn(process.execPath, ["-e", "setInterval(()=>{},1000)"]);
  children.push(child);
  await new Promise((r) => setTimeout(r, 100));
  await writeIdentityHeartbeat(hb, child.pid!);

  child.kill("SIGSTOP"); // wedged: alive, no longer serving
  ageHeartbeat(hb, 120);

  const relaunches: string[] = [];
  const exited = new Promise<void>((r) => child.on("exit", () => r()));
  const action = await checkOnce(liveDeps(hb, { relaunches }));
  assert.equal(action, "kill-relaunch");
  await exited;
  assert.equal(child.signalCode, "SIGKILL", "the wedge must be killed, then relaunched");
  assert.equal(relaunches.length, 1);
});

test("live SAFETY-INVERSE: recycled-pid victim (live pid, mismatched identity) survives; app relaunched via tier1", async () => {
  const dir = tempDir();
  const hb = path.join(dir, "heartbeat");
  const innocent = spawn(process.execPath, ["-e", "setInterval(()=>{},1000)"]);
  children.push(innocent);
  await new Promise((r) => setTimeout(r, 100));
  // The heartbeat records the innocent's pid but the CRASHED APP's identity
  // (start time 1 hour earlier) — the OS recycled the pid.
  await writeIdentityHeartbeat(hb, innocent.pid!, -3600);
  ageHeartbeat(hb, 120); // deep past both tiers

  const relaunches: string[] = [];
  const action = await checkOnce(liveDeps(hb, { relaunches }));
  assert.equal(action, "relaunch", "mismatched pid is ABSENT -> tier1, not tier2");
  assert.equal(relaunches.length, 1);
  assert.equal(pidAlive(innocent.pid!), true, "the innocent process must survive");
  innocent.kill("SIGKILL");
});

test("live continuous serving: a bumping supervisor is left alone across many ticks", async () => {
  const dir = tempDir();
  const hb = path.join(dir, "heartbeat");
  const sup = await spawnSupervisor(hb);
  const relaunches: string[] = [];
  for (let i = 0; i < 3; i++) {
    const action = await checkOnce(liveDeps(hb, { relaunches }));
    assert.equal(action, "healthy");
    await new Promise((r) => setTimeout(r, 200));
  }
  assert.equal(relaunches.length, 0);
  assert.equal(pidAlive(sup.pid!), true);
  sup.kill("SIGKILL");
});
