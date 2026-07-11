// Resurrection installer tests (contract resurrection.self_check): the single
// serialized idempotent repair routine, the three service definitions, the
// linger gotcha, and privilege-blocked honesty. OS commands are injected.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { controlPaths } from "../electron/control/paths.js";
import {
  checkArmed,
  ensureResurrection,
  launchdPlist,
  writeRelaunchSpec,
  type InstallerOpts,
} from "../electron/resurrection/installer.js";

type Call = { cmd: string; args: string[] };

function fakeExec(responses: (call: Call) => { code: number; out: string }) {
  const calls: Call[] = [];
  const exec = async (cmd: string, args: string[]) => {
    const call = { cmd, args };
    calls.push(call);
    return responses(call);
  };
  return { calls, exec };
}

function withTempControlDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wt-inst-ctl-"));
  process.env.WINDYTALK_CONTROL_DIR = dir;
  return dir;
}

const APP_LAUNCH = { cmd: "/opt/wt/windytalk", args: [] as string[] };

test("linux: arm writes the timer+service units, enables, lingers; armed when all green", async () => {
  const dir = withTempControlDir();
  const { calls, exec } = fakeExec(({ cmd, args }) => {
    if (cmd === "loginctl" && args[0] === "show-user") return { code: 0, out: "Linger=yes\n" };
    if (cmd === "systemctl" && args.includes("is-enabled")) return { code: 0, out: "enabled\n" };
    if (cmd === "systemctl" && args.includes("is-active")) return { code: 0, out: "active\n" };
    return { code: 0, out: "" };
  });
  const unitDir = path.join(dir, "systemd-user");
  const status = await ensureResurrection({
    appLaunch: APP_LAUNCH,
    platform: "linux",
    exec,
    watcherPath: "/opt/wt/watcher.js",
    serviceDir: unitDir,
  });
  assert.equal(status.armed, true);
  const service = fs.readFileSync(path.join(unitDir, "windytalk-resurrection.service"), "utf8");
  const timer = fs.readFileSync(path.join(unitDir, "windytalk-resurrection.timer"), "utf8");
  assert.match(service, /ELECTRON_RUN_AS_NODE=1/);
  assert.match(service, /watcher\.js" --once|watcher\.js --once/);
  assert.match(timer, /OnUnitActiveSec=15/, "the pinned 15 s cadence");
  assert.ok(calls.some((c) => c.cmd === "loginctl" && c.args[0] === "enable-linger"),
    "the classic gotcha: must enable-linger or the service dies at logout");
  // The relaunch spec was written for the watcher.
  const spec = JSON.parse(fs.readFileSync(controlPaths().resurrectionSpec, "utf8"));
  assert.deepEqual(spec.launch.cmd, APP_LAUNCH.cmd);
  fs.rmSync(dir, { recursive: true, force: true });
  delete process.env.WINDYTALK_CONTROL_DIR;
});

test("linux: lingering off -> honestly UNARMED with the manual step in detail", async () => {
  const dir = withTempControlDir();
  const { exec } = fakeExec(({ cmd, args }) => {
    if (cmd === "loginctl" && args[0] === "show-user") return { code: 0, out: "Linger=no\n" };
    return { code: 0, out: args.includes("is-enabled") ? "enabled" : "active" };
  });
  const status = await checkArmed({ appLaunch: APP_LAUNCH, platform: "linux", exec });
  assert.equal(status.armed, false);
  assert.match(status.detail, /enable-linger/, "detail must carry the manual step");
  fs.rmSync(dir, { recursive: true, force: true });
  delete process.env.WINDYTALK_CONTROL_DIR;
});

test("linux: privilege-blocked systemctl -> unarmed, never a fake success (forced-honest)", async () => {
  const dir = withTempControlDir();
  const { exec } = fakeExec(({ cmd }) =>
    cmd === "systemctl" ? { code: 1, out: "Failed to connect to bus" } : { code: 0, out: "" },
  );
  const status = await ensureResurrection({
    appLaunch: APP_LAUNCH,
    platform: "linux",
    exec,
    serviceDir: path.join(dir, "systemd-user"),
  });
  assert.equal(status.armed, false);
  assert.match(status.detail, /Failed to connect to bus|not armed/);
  fs.rmSync(dir, { recursive: true, force: true });
  delete process.env.WINDYTALK_CONTROL_DIR;
});

test("darwin: plist has the 15 s StartInterval + ELECTRON_RUN_AS_NODE; xml-escapes paths", () => {
  const plist = launchdPlist("/Applications/Windy & Talk.app/watcher.js");
  assert.match(plist, /<key>StartInterval<\/key><integer>15<\/integer>/);
  assert.match(plist, /ELECTRON_RUN_AS_NODE/);
  assert.match(plist, /Windy &amp; Talk\.app/);
});

test("win32: arm writes the loop-mode wrapper and registers + kicks the logon task", async () => {
  const dir = withTempControlDir();
  const { calls, exec } = fakeExec(() => ({ code: 0, out: "" }));
  const status = await ensureResurrection({
    appLaunch: APP_LAUNCH,
    platform: "win32",
    exec,
    watcherPath: "C:\\wt\\watcher.js",
  });
  assert.equal(status.armed, true);
  const wrapper = fs.readFileSync(path.join(dir, "resurrection-watcher.cmd"), "utf8");
  assert.match(wrapper, /ELECTRON_RUN_AS_NODE=1/);
  assert.match(wrapper, /--loop/, "Scheduled Tasks can't fire every 15 s; the loop mode covers it");
  assert.ok(calls.some((c) => c.cmd === "schtasks" && c.args.includes("/Create")));
  assert.ok(calls.some((c) => c.cmd === "schtasks" && c.args.includes("/Run")), "armed now, not at next logon");
  fs.rmSync(dir, { recursive: true, force: true });
  delete process.env.WINDYTALK_CONTROL_DIR;
});

test("repair is serialized: concurrent calls converge, never interleave", async () => {
  const dir = withTempControlDir();
  let inFlight = 0;
  let maxInFlight = 0;
  const exec = async (_cmd: string, args: string[]) => {
    inFlight++;
    maxInFlight = Math.max(maxInFlight, inFlight);
    await new Promise((r) => setTimeout(r, 10));
    inFlight--;
    if (args[0] === "show-user") return { code: 0, out: "Linger=yes" };
    return { code: 0, out: args.includes("is-enabled") ? "enabled" : "active" };
  };
  const opts: InstallerOpts = { appLaunch: APP_LAUNCH, platform: "linux", exec, watcherPath: "/w.js" };
  const [a, b, c] = await Promise.all([
    ensureResurrection(opts),
    ensureResurrection(opts),
    ensureResurrection(opts),
  ]);
  assert.equal(maxInFlight, 1, "the single-flight chain must serialize repairs");
  assert.equal(a.armed && b.armed && c.armed, true);
  fs.rmSync(dir, { recursive: true, force: true });
  delete process.env.WINDYTALK_CONTROL_DIR;
});

test("relaunch spec: atomic write, exact shape the watcher consumes", () => {
  const dir = withTempControlDir();
  writeRelaunchSpec({ cmd: "/bin/app", args: ["--flag"], cwd: "/opt" });
  const spec = JSON.parse(fs.readFileSync(controlPaths().resurrectionSpec, "utf8"));
  assert.deepEqual(spec, { launch: { cmd: "/bin/app", args: ["--flag"], cwd: "/opt" } });
  fs.rmSync(dir, { recursive: true, force: true });
  delete process.env.WINDYTALK_CONTROL_DIR;
});
