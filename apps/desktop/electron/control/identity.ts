// Pid identity (contract resurrection.heartbeat.staleness_tiers.identity_aware):
// "pid absent/present" is identity-aware EVERYWHERE — a live pid whose executable
// path + start_time do not match the recorded {pid, started_at, exe} is ABSENT
// (the OS recycled the pid onto an innocent process). BOTH killers — the
// watcher's tier-2 SIGKILL and the single-instance takeover — verify through
// this one module before any kill, so the discipline cannot drift apart.
import { execFile } from "node:child_process";
import fs from "node:fs";

import { IDENTITY_START_TOLERANCE_S } from "./constants.js";

/** The identity record written to the heartbeat + instance.lock files. */
export interface IdentityRecord {
  pid: number;
  /** Process start, epoch seconds (as observed by procIdentity on this OS). */
  started_at: number;
  /** Executable path (process.execPath of the recorder). */
  exe: string;
}

export interface LiveIdentity {
  alive: boolean;
  exe: string | null;
  started_at: number | null;
}

type Exec = (cmd: string, args: string[]) => Promise<string>;

const realExec: Exec = (cmd, args) =>
  new Promise((resolve, reject) => {
    execFile(cmd, args, { timeout: 5_000 }, (err, stdout) => {
      if (err) reject(err);
      else resolve(String(stdout));
    });
  });

/**
 * What is live at `pid` right now? {alive:false} when nothing is.
 * exe/started_at may be null when the OS hides them (treated as mismatch).
 */
export async function procIdentity(
  pid: number,
  platform: NodeJS.Platform = process.platform,
  exec: Exec = realExec,
): Promise<LiveIdentity> {
  if (!pidAlive(pid)) return { alive: false, exe: null, started_at: null };
  try {
    if (platform === "linux") return await linuxIdentity(pid);
    if (platform === "darwin") return await darwinIdentity(pid, exec);
    if (platform === "win32") return await windowsIdentity(pid, exec);
  } catch {
    // Alive but unreadable (e.g. it exited mid-read): identity unknown.
  }
  return { alive: pidAlive(pid), exe: null, started_at: null };
}

/** Record our own identity, via the same reader the verifiers use. */
export async function selfIdentity(
  platform: NodeJS.Platform = process.platform,
): Promise<IdentityRecord> {
  const live = await procIdentity(process.pid, platform);
  return {
    pid: process.pid,
    // Fall back to self-measurement only if the OS reader failed on ourselves.
    started_at: live.started_at ?? (Date.now() - process.uptime() * 1000) / 1000,
    exe: process.execPath,
  };
}

/**
 * present-by-identity: alive AND exe matches AND start_time within tolerance.
 * Anything less (dead, recycled pid, unreadable identity) is ABSENT.
 */
export function identityMatches(record: IdentityRecord, live: LiveIdentity): boolean {
  if (!live.alive || live.exe == null || live.started_at == null) return false;
  if (normalizeExe(live.exe) !== normalizeExe(record.exe)) return false;
  return Math.abs(live.started_at - record.started_at) <= IDENTITY_START_TOLERANCE_S;
}

export function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    // ESRCH = gone. EPERM = exists but another user's — cannot be our app
    // (we always run as the login user), so it is absent for our purposes.
    return false;
  }
}

function normalizeExe(exe: string): string {
  // Linux reports "<path> (deleted)" after an on-disk swap of a running binary.
  let p = exe.replace(/ \(deleted\)$/, "");
  try {
    p = fs.realpathSync(p);
  } catch {
    // Path gone (deleted binary): compare the literal string.
  }
  return p;
}

// -- Linux: /proc (no subprocess needed) --------------------------------------

async function linuxIdentity(pid: number): Promise<LiveIdentity> {
  let exe: string | null = null;
  try {
    exe = fs.readlinkSync(`/proc/${pid}/exe`);
  } catch {
    exe = null;
  }
  const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
  const started_at = linuxStartTimeEpoch(stat, readBtime());
  return { alive: true, exe, started_at };
}

/**
 * Field 22 of /proc/<pid>/stat is starttime in clock ticks since boot. The comm
 * field (2) may contain spaces AND parentheses — parse after the LAST ')'.
 */
export function linuxStartTimeEpoch(statLine: string, btimeEpoch: number, hz = 100): number | null {
  const after = statLine.slice(statLine.lastIndexOf(")") + 1).trim().split(/\s+/);
  // `after` starts at field 3 (state), so starttime (field 22) is index 19.
  const ticks = Number(after[19]);
  if (!Number.isFinite(ticks)) return null;
  return btimeEpoch + ticks / hz;
}

function readBtime(): number {
  const stat = fs.readFileSync("/proc/stat", "utf8");
  const m = /^btime (\d+)$/m.exec(stat);
  return m ? Number(m[1]) : 0;
}

// -- macOS: ps (BSD ps prints lstart in a fixed English format) ---------------

async function darwinIdentity(pid: number, exec: Exec): Promise<LiveIdentity> {
  const out = await exec("ps", ["-p", String(pid), "-o", "lstart=,comm="]);
  return parseDarwinPs(out);
}

export function parseDarwinPs(out: string): LiveIdentity {
  const line = out.trim();
  if (!line) return { alive: false, exe: null, started_at: null };
  // "Thu Jul 11 10:00:00 2026 /path/to/exe with spaces"
  const m = /^(\w{3}\s+\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.+)$/.exec(line);
  if (!m) return { alive: true, exe: null, started_at: null };
  const ts = Date.parse(m[1]);
  return {
    alive: true,
    exe: m[2],
    started_at: Number.isFinite(ts) ? ts / 1000 : null,
  };
}

// -- Windows: CIM via PowerShell ----------------------------------------------

async function windowsIdentity(pid: number, exec: Exec): Promise<LiveIdentity> {
  const script =
    `$p = Get-CimInstance Win32_Process -Filter "ProcessId=${pid}"; ` +
    `if ($p) { @{exe=$p.ExecutablePath; start=[uint64](New-TimeSpan -Start (Get-Date "1970-01-01Z") ` +
    `-End $p.CreationDate.ToUniversalTime()).TotalSeconds} | ConvertTo-Json -Compress }`;
  const out = await exec("powershell", ["-NoProfile", "-NonInteractive", "-Command", script]);
  return parseWindowsCim(out);
}

export function parseWindowsCim(out: string): LiveIdentity {
  const line = out.trim();
  if (!line) return { alive: false, exe: null, started_at: null };
  try {
    const parsed = JSON.parse(line) as { exe?: string | null; start?: number };
    return {
      alive: true,
      exe: parsed.exe ?? null,
      started_at: typeof parsed.start === "number" ? parsed.start : null,
    };
  } catch {
    return { alive: true, exe: null, started_at: null };
  }
}
