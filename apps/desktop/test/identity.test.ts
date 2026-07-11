// Identity-reader tests: the {pid, started_at, exe} discipline both killers
// verify through (staleness_tiers.identity_aware). Parsers on fixtures for all
// three OSes + a live self-check on the host OS.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  identityMatches,
  linuxStartTimeEpoch,
  parseDarwinPs,
  parseWindowsCim,
  pidAlive,
  procIdentity,
  selfIdentity,
  type IdentityRecord,
} from "../electron/control/identity.js";

test("linux /proc stat parse: hostile comm names with ') (' cannot shift the fields", () => {
  // comm is "evil) R 1 (x" — everything before the LAST ')' is comm.
  const fields = Array.from({ length: 50 }, (_, i) => String(i + 3));
  fields[19] = "500000"; // starttime ticks (field 22 = index 19 after comm)
  const line = `1234 (evil) R 1 (x) ${fields.join(" ")}`;
  const started = linuxStartTimeEpoch(line, 1_700_000_000, 100);
  assert.equal(started, 1_700_000_000 + 5000);
});

test("linux /proc stat parse: ordinary comm", () => {
  // Tokens after the ')' begin at field 3 (state), so starttime is index 19.
  const fields = Array.from({ length: 50 }, (_, i) => String(i));
  fields[0] = "S"; // state
  fields[19] = "12345"; // starttime ticks
  const line = `77 (windytalk) ${fields.join(" ")}`;
  assert.equal(linuxStartTimeEpoch(line, 1_000, 100), 1_000 + 123.45);
});

test("darwin ps lstart parse: fixed English date + exe path with spaces", () => {
  const out = "Sat Jul 11 10:20:30 2026 /Applications/Windy Talk.app/Contents/MacOS/Windy Talk\n";
  const live = parseDarwinPs(out);
  assert.equal(live.alive, true);
  assert.equal(live.exe, "/Applications/Windy Talk.app/Contents/MacOS/Windy Talk");
  assert.equal(live.started_at, Date.parse("Sat Jul 11 10:20:30 2026") / 1000);
});

test("darwin ps parse: empty output = process gone", () => {
  assert.equal(parseDarwinPs("").alive, false);
});

test("windows CIM parse", () => {
  const live = parseWindowsCim('{"exe":"C:\\\\Program Files\\\\WindyTalk\\\\WindyTalk.exe","start":1780000000}');
  assert.equal(live.alive, true);
  assert.equal(live.exe, "C:\\Program Files\\WindyTalk\\WindyTalk.exe");
  assert.equal(live.started_at, 1_780_000_000);
});

test("identityMatches: exact exe + start within 2s tolerance", () => {
  const rec: IdentityRecord = { pid: 1, started_at: 1000, exe: "/nonexistent/wt-bin" };
  assert.equal(identityMatches(rec, { alive: true, exe: "/nonexistent/wt-bin", started_at: 1001.5 }), true);
  assert.equal(identityMatches(rec, { alive: true, exe: "/nonexistent/wt-bin", started_at: 1003 }), false);
  assert.equal(identityMatches(rec, { alive: true, exe: "/other/bin", started_at: 1000 }), false);
  assert.equal(identityMatches(rec, { alive: false, exe: null, started_at: null }), false);
  // Unreadable identity on a live pid = NOT a match (never kill on unknown).
  assert.equal(identityMatches(rec, { alive: true, exe: null, started_at: 1000 }), false);
});

test("identityMatches: linux '(deleted)' suffix after an on-disk binary swap still matches", () => {
  const rec: IdentityRecord = { pid: 1, started_at: 1000, exe: "/nonexistent/wt-bin" };
  assert.equal(
    identityMatches(rec, { alive: true, exe: "/nonexistent/wt-bin (deleted)", started_at: 1000 }),
    true,
  );
});

test("live: selfIdentity matches procIdentity(process.pid) on this box", async () => {
  const self = await selfIdentity();
  assert.equal(self.pid, process.pid);
  assert.equal(self.exe, process.execPath);
  const live = await procIdentity(process.pid);
  assert.equal(live.alive, true);
  assert.equal(identityMatches(self, live), true, "our own identity must match itself");
});

test("live: a freshly-dead child is absent-by-identity", async () => {
  const { spawn } = await import("node:child_process");
  const child = spawn(process.execPath, ["-e", "setTimeout(()=>{}, 60000)"]);
  await new Promise((r) => setTimeout(r, 200));
  const alivePid = child.pid as number;
  assert.equal(pidAlive(alivePid), true);
  child.kill("SIGKILL");
  await new Promise((r) => child.on("exit", r));
  await new Promise((r) => setTimeout(r, 100));
  const live = await procIdentity(alivePid);
  assert.equal(live.alive, false);
});
