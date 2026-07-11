// Install / self-check / auto-repair of the OS resurrection service (contract
// resurrection.self_check). ONE serialized, idempotent routine — the launch-time
// auto-repair and the repair_resurrection tool (slice 3) both call it, so they
// converge instead of racing. The floor cannot be merely observed-broken; it
// must self-repair, falling back to a plain warning ONLY when privilege
// genuinely blocks it.
//
// Architecture is heartbeat-watcher-spawner (NOT child-supervision): a tiny
// periodic unit per OS that runs watcher.js. The watcher runs under the app's
// own binary with ELECTRON_RUN_AS_NODE=1, so no system Node is assumed.
import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { controlPaths } from "../control/paths.js";
import type { RelaunchSpec } from "./watcher.js";

export interface ResurrectionStatus {
  armed: boolean;
  /** Human-readable: why unarmed, or the manual step when privilege blocks us. */
  detail: string;
}

export interface InstallerOpts {
  /** How the OS relaunches the app (packaged: the app binary; dev: electron + dir). */
  appLaunch: { cmd: string; args: string[]; cwd?: string };
  platform?: NodeJS.Platform;
  exec?: (cmd: string, args: string[]) => Promise<{ code: number; out: string }>;
  watcherPath?: string;
  /** Override for the OS service-definition dir (tests only). */
  serviceDir?: string;
  log?: (msg: string) => void;
}

const realExec = (cmd: string, args: string[]): Promise<{ code: number; out: string }> =>
  new Promise((resolve) => {
    execFile(cmd, args, { timeout: 15_000 }, (err, stdout, stderr) => {
      const code = err ? ((err as { code?: number | string }).code as number) ?? 1 : 0;
      resolve({ code: typeof code === "number" ? code : 1, out: `${stdout}${stderr}` });
    });
  });

// The single-flight chain: concurrent callers converge on sequential runs.
let repairChain: Promise<ResurrectionStatus> = Promise.resolve({ armed: false, detail: "never run" });

/** Serialized entry point — boot self-check AND repair_resurrection use this. */
export function ensureResurrection(opts: InstallerOpts): Promise<ResurrectionStatus> {
  repairChain = repairChain.then(
    () => repairOnce(opts),
    () => repairOnce(opts),
  );
  return repairChain;
}

async function repairOnce(opts: InstallerOpts): Promise<ResurrectionStatus> {
  const platform = opts.platform ?? process.platform;
  const exec = opts.exec ?? realExec;
  const log = opts.log ?? (() => {});
  const watcher = opts.watcherPath ?? defaultWatcherPath();
  try {
    writeRelaunchSpec(opts.appLaunch);
    if (platform === "linux") return await armSystemd(exec, watcher, log, opts.serviceDir);
    if (platform === "darwin") return await armLaunchd(exec, watcher, log, opts.serviceDir);
    if (platform === "win32") return await armScheduledTask(exec, watcher, log);
    return { armed: false, detail: `unsupported platform: ${platform}` };
  } catch (e) {
    return { armed: false, detail: `resurrection repair failed: ${String(e)}` };
  }
}

/** Read-only armed probe (get_health.resurrection_armed feeds from this). */
export async function checkArmed(opts: InstallerOpts): Promise<ResurrectionStatus> {
  const platform = opts.platform ?? process.platform;
  const exec = opts.exec ?? realExec;
  if (platform === "linux") return statusSystemd(exec);
  if (platform === "darwin") return statusLaunchd(exec);
  if (platform === "win32") return statusScheduledTask(exec);
  return { armed: false, detail: `unsupported platform: ${platform}` };
}

function defaultWatcherPath(): string {
  // Compiled layout: dist/electron/resurrection/installer.js sits next to watcher.js.
  return path.join(path.dirname(fileURLToPath(import.meta.url)), "watcher.js");
}

/** The spec the watcher relaunches from — written by the installer, never guessed. */
export function writeRelaunchSpec(appLaunch: InstallerOpts["appLaunch"]): void {
  const paths = controlPaths();
  const spec: RelaunchSpec = { launch: { ...appLaunch } };
  fs.mkdirSync(path.dirname(paths.resurrectionSpec), { recursive: true, mode: 0o700 });
  const tmp = paths.resurrectionSpec + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(spec, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, paths.resurrectionSpec);
}

// -- Linux: systemd --user timer + the enable-linger gotcha ---------------------

const UNIT = "windytalk-resurrection";

async function armSystemd(
  exec: InstallerOpts["exec"] & {},
  watcher: string,
  log: (m: string) => void,
  serviceDir?: string,
): Promise<ResurrectionStatus> {
  const unitDir = serviceDir ?? path.join(os.homedir(), ".config", "systemd", "user");
  fs.mkdirSync(unitDir, { recursive: true });
  fs.writeFileSync(
    path.join(unitDir, `${UNIT}.service`),
    [
      "[Unit]",
      "Description=Windy Talk resurrection watcher (is Windy Talk serving? if not, relaunch)",
      "[Service]",
      "Type=oneshot",
      "Environment=ELECTRON_RUN_AS_NODE=1",
      `ExecStart=${sdQuote(process.execPath)} ${sdQuote(watcher)} --once`,
      "",
    ].join("\n"),
  );
  fs.writeFileSync(
    path.join(unitDir, `${UNIT}.timer`),
    [
      "[Unit]",
      "Description=Windy Talk resurrection timer (15 s cadence per control.mcp.v1)",
      "[Timer]",
      "OnBootSec=15",
      "OnUnitActiveSec=15",
      "AccuracySec=1s",
      "[Install]",
      "WantedBy=timers.target",
      "",
    ].join("\n"),
  );
  await exec("systemctl", ["--user", "daemon-reload"]);
  const enable = await exec("systemctl", ["--user", "enable", "--now", `${UNIT}.timer`]);
  if (enable.code !== 0) {
    return { armed: false, detail: `systemctl enable failed: ${enable.out.trim()}` };
  }
  // The classic gotcha: a --user unit dies at logout unless lingering is on.
  const linger = await exec("loginctl", ["enable-linger", os.userInfo().username]);
  if (linger.code !== 0) {
    log(`enable-linger failed (${linger.out.trim()}) — resurrection stops at logout`);
  }
  return statusSystemd(exec);
}

async function statusSystemd(exec: InstallerOpts["exec"] & {}): Promise<ResurrectionStatus> {
  const enabled = await exec("systemctl", ["--user", "is-enabled", `${UNIT}.timer`]);
  const active = await exec("systemctl", ["--user", "is-active", `${UNIT}.timer`]);
  if (enabled.code !== 0 || active.code !== 0) {
    return {
      armed: false,
      detail: `timer not armed (enabled=${enabled.out.trim() || "no"}, active=${active.out.trim() || "no"})`,
    };
  }
  const linger = await exec("loginctl", ["show-user", os.userInfo().username, "-p", "Linger"]);
  if (!linger.out.includes("Linger=yes")) {
    return {
      armed: false,
      detail:
        "timer active but lingering is off — resurrection dies at logout. " +
        `Manual step: run "loginctl enable-linger ${os.userInfo().username}".`,
    };
  }
  return { armed: true, detail: "systemd --user timer active, lingering on" };
}

function sdQuote(p: string): string {
  return /[\s"']/.test(p) ? `"${p.replace(/"/g, '\\"')}"` : p;
}

// -- macOS: launchd LaunchAgent ---------------------------------------------------

const AGENT_LABEL = "com.windytalk.resurrection";

async function armLaunchd(
  exec: InstallerOpts["exec"] & {},
  watcher: string,
  _log: (m: string) => void,
  serviceDir?: string,
): Promise<ResurrectionStatus> {
  const agentDir = serviceDir ?? path.join(os.homedir(), "Library", "LaunchAgents");
  const plistPath = path.join(agentDir, `${AGENT_LABEL}.plist`);
  fs.mkdirSync(agentDir, { recursive: true });
  fs.writeFileSync(plistPath, launchdPlist(watcher));
  const uid = typeof process.getuid === "function" ? process.getuid() : 501;
  // bootout old registration first so a changed plist re-registers (idempotent).
  await exec("launchctl", ["bootout", `gui/${uid}/${AGENT_LABEL}`]);
  const boot = await exec("launchctl", ["bootstrap", `gui/${uid}`, plistPath]);
  if (boot.code !== 0) {
    // Older macOS fallback.
    const load = await exec("launchctl", ["load", "-w", plistPath]);
    if (load.code !== 0) {
      return { armed: false, detail: `launchctl bootstrap failed: ${boot.out.trim()}` };
    }
  }
  return statusLaunchd(exec);
}

async function statusLaunchd(exec: InstallerOpts["exec"] & {}): Promise<ResurrectionStatus> {
  const uid = typeof process.getuid === "function" ? process.getuid() : 501;
  const print = await exec("launchctl", ["print", `gui/${uid}/${AGENT_LABEL}`]);
  return print.code === 0
    ? { armed: true, detail: "LaunchAgent registered" }
    : { armed: false, detail: "LaunchAgent not registered" };
}

export function launchdPlist(watcher: string): string {
  const xe = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${xe(process.execPath)}</string>
    <string>${xe(watcher)}</string>
    <string>--once</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>ELECTRON_RUN_AS_NODE</key><string>1</string></dict>
  <key>StartInterval</key><integer>15</integer>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
`;
}

// -- Windows: Scheduled Task at logon, watcher in --loop mode ---------------------

const TASK_NAME = "WindyTalkResurrection";

async function armScheduledTask(
  exec: InstallerOpts["exec"] & {},
  watcher: string,
  _log: (m: string) => void,
): Promise<ResurrectionStatus> {
  // Scheduled Tasks can't fire every 15 s — the task starts the watcher LOOP at
  // logon; a companion .cmd wrapper carries the ELECTRON_RUN_AS_NODE env.
  const paths = controlPaths();
  fs.mkdirSync(paths.stateDir, { recursive: true });
  const wrapper = path.join(paths.stateDir, "resurrection-watcher.cmd");
  fs.writeFileSync(
    wrapper,
    `@echo off\r\nset ELECTRON_RUN_AS_NODE=1\r\n"${process.execPath}" "${watcher}" --loop\r\n`,
  );
  const create = await exec("schtasks", [
    "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED",
    "/TR", `"${wrapper}"`,
  ]);
  if (create.code !== 0) {
    return { armed: false, detail: `schtasks create failed: ${create.out.trim()}` };
  }
  // Kick it now so the floor is armed without waiting for the next logon.
  await exec("schtasks", ["/Run", "/TN", TASK_NAME]);
  return statusScheduledTask(exec);
}

async function statusScheduledTask(exec: InstallerOpts["exec"] & {}): Promise<ResurrectionStatus> {
  const query = await exec("schtasks", ["/Query", "/TN", TASK_NAME]);
  return query.code === 0
    ? { armed: true, detail: "Scheduled Task registered" }
    : { armed: false, detail: "Scheduled Task not registered" };
}
