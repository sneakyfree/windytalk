// The OS resurrection watcher (contract `resurrection`) — the supervisor's
// supervisor. Run every 15 s by launchd / a systemd --user timer (--once) or as
// a logon-task loop on Windows (--loop). Its ONLY job: is Windy Talk serving?
// If not, relaunch it — via the two identity-aware staleness tiers, under the
// service's own backoff ceiling.
//
// Deliberately boring: no Electron imports, no :8782 probe in the kill decision
// (a bare TCP accept proves nothing about a deadlocked serving loop — the
// heartbeat's staleness ALREADY IS the serving verdict, per staleness_tiers),
// and the pid from the heartbeat file ONLY — process-name scanning is FORBIDDEN
// (heartbeat_content: wrong-kill risk).
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

function readFileSyncSafe(p: string): string | null {
  try {
    return fs.readFileSync(p, "utf8");
  } catch {
    return null;
  }
}

function writeFileAtomic(p: string, content: string): void {
  const tmp = p + ".tmp";
  fs.writeFileSync(tmp, content, { mode: 0o600 });
  fs.renameSync(tmp, p);
}

import {
  BACKOFF_FRESH_RESET_S,
  BACKOFF_MAX_RELAUNCHES,
  BACKOFF_SLOW_INTERVAL_S,
  BACKOFF_WINDOW_S,
  STALE_DEAD_S,
  STALE_WEDGE_S,
  WATCH_INTERVAL_MS,
} from "../control/constants.js";
import { readHeartbeat } from "../control/heartbeat.js";
import {
  clearUpdateState,
  readUpdateState,
  rollbackDecision,
  writeUpdateState,
  type UpdateState,
} from "../control/selfupdate.js";
import {
  identityMatches,
  procIdentity,
  type IdentityRecord,
  type LiveIdentity,
} from "../control/identity.js";
import { controlPaths } from "../control/paths.js";
import { notifyOs } from "./notify.js";

export type WatchAction =
  | "healthy"
  | "wait" // stale 30–90 s with a live-by-identity pid: the grace zone
  | "relaunch"
  | "kill-relaunch" // tier-2 wedge
  | "disk-full" // alive but can't write: never kill, never relaunch
  | "backoff" // relaunch wanted but the service ceiling defers it
  | "state-unwritable" // backoff counter can't persist: fail safe, no thrash
  | "no-spec"; // nothing to relaunch with (installer never wrote the spec)

export interface BackoffState {
  /** Relaunch timestamps (epoch s) since the counter last reset. */
  relaunches: number[];
  /** Once 3 relaunches land inside a 300 s span: 1 attempt per 5 min, sticky. */
  slow: boolean;
  /** Start of the current continuous-fresh streak (epoch s), if any. */
  freshSince: number | null;
}

export interface WatcherDeps {
  heartbeatPath: string;
  now?: () => number; // ms
  readHb?: typeof readHeartbeat;
  getIdentity?: (pid: number) => Promise<LiveIdentity>;
  kill?: (pid: number) => void;
  relaunch?: () => boolean;
  probeWritable?: (dir: string) => boolean;
  notify?: (title: string, body: string) => void;
  loadState?: () => BackoffState;
  saveState?: (s: BackoffState) => boolean;
  /** Read the A/B update marker (null when no update is being verified). */
  loadUpdateState?: () => UpdateState | null;
  /** Did the running build attest itself? (fresh heartbeat @ toVersion + bind). */
  updateAttested?: (state: UpdateState) => boolean;
  /** Flip the A/B pointer back to previousBinary and clear the marker. */
  rollbackUpdate?: (state: UpdateState) => void;
  clearUpdate?: () => void;
  log?: (msg: string) => void;
}

export async function checkOnce(deps: WatcherDeps): Promise<WatchAction> {
  const now = deps.now ?? Date.now;
  const readHb = deps.readHb ?? readHeartbeat;
  const getIdentity = deps.getIdentity ?? ((pid: number) => procIdentity(pid));
  const kill = deps.kill ?? ((pid: number) => process.kill(pid, "SIGKILL"));
  const probeWritable = deps.probeWritable ?? defaultProbeWritable;
  const log = deps.log ?? (() => {});
  const nowS = now() / 1000;
  const state = (deps.loadState ?? (() => emptyState()))();

  // Out-of-process A/B rollback (self_update.out_of_process_rollback): if an
  // update is pending verification, the NEW build must attest within 60 s or we
  // flip back to the previous known-good binary. Checked BEFORE staleness so a
  // wedged new build rolls back (not just relaunches the broken binary).
  const update = deps.loadUpdateState?.() ?? null;
  if (update && update.pending) {
    const attested = deps.updateAttested?.(update) ?? false;
    const decision = rollbackDecision(update, attested, now());
    if (decision === "commit") {
      log(`update to ${update.toVersion} verified — committing`);
      deps.clearUpdate?.();
    } else if (decision === "rollback") {
      log(`update to ${update.toVersion} failed to attest in 60 s — rolling back to ${update.fromVersion}`);
      deps.rollbackUpdate?.(update);
      (deps.notify ?? notifyOs)(
        "Windy Talk restored a working version",
        "A recent update didn't start correctly, so Windy Talk went back to the version that works.",
      );
      // The rollback relaunches the previous binary; done this tick.
      return "kill-relaunch";
    }
    // 'wait': fall through to normal staleness handling (the new build may just
    // be slow to boot; tier1/tier2 still relaunch it if it died outright).
  }

  const hb = readHb(deps.heartbeatPath);

  if (hb === null) {
    // tier1_dead: file ABSENT -> relaunch immediately. Also restart_app's fast
    // path (it removes the heartbeat so we bring the app back at once).
    log("heartbeat absent — relaunching");
    return relaunchUnderBackoff(deps, state, nowS, "relaunch");
  }

  const ageS = nowS - hb.mtimeMs / 1000;

  if (ageS <= STALE_DEAD_S) {
    // Fresh. Track the continuous-fresh streak; 300 s of it resets the counter.
    if (state.freshSince === null) state.freshSince = nowS;
    if (nowS - state.freshSince >= BACKOFF_FRESH_RESET_S && (state.relaunches.length || state.slow)) {
      state.relaunches = [];
      state.slow = false;
      log("backoff counter reset after continuous fresh heartbeat");
    }
    deps.saveState?.(state);
    return "healthy";
  }

  state.freshSince = null; // the streak broke

  // identity-aware presence (staleness_tiers.identity_aware): dead, recycled
  // (exe/start_time mismatch) or unreadable identity ALL count as ABSENT.
  const present =
    hb.record !== null && identityMatches(hb.record, await getIdentity(hb.record.pid));

  if (!present) {
    // tier1_dead: mtime > 30 s AND pid absent-by-identity. Fires at the ≤45 s
    // budget even on a recycled pid — and never signals the innocent process.
    log(`heartbeat stale ${ageS.toFixed(0)}s, pid absent-by-identity — relaunching`);
    return relaunchUnderBackoff(deps, state, nowS, "relaunch");
  }

  if (ageS > STALE_WEDGE_S) {
    // tier2_wedged. Probe (a): can the heartbeat dir be written at all?
    if (!probeWritable(path.dirname(deps.heartbeatPath))) {
      // Disk full: the app is alive but can't write. Relaunch fixes nothing and
      // a SIGKILL punishes a live app — surface it OS-level (the in-app UI may
      // be down) and stand down.
      log("heartbeat stale but the disk is unwritable — disk-full, not killing");
      (deps.notify ?? notifyOs)(
        "Windy Talk: the disk is full",
        "Windy Talk can't save its state. Free some disk space to keep it healthy.",
      );
      deps.saveState?.(state);
      return "disk-full";
    }
    // Probe (b): writable + stale past 90 s already MEANS the serving loop
    // stopped (the writer is fate-coupled to serving). Genuine wedge: re-verify
    // identity IMMEDIATELY before the SIGKILL, then relaunch under backoff.
    const record = hb.record as IdentityRecord;
    if (identityMatches(record, await getIdentity(record.pid))) {
      log(`wedged supervisor pid ${record.pid} (stale ${ageS.toFixed(0)}s) — SIGKILL + relaunch`);
      try {
        kill(record.pid);
      } catch {
        // Died between verify and kill — tier1 territory now; relaunch anyway.
      }
    }
    return relaunchUnderBackoff(deps, state, nowS, "kill-relaunch");
  }

  // 30 s < age <= 90 s with a live-by-identity pid: not yet a verdict.
  deps.saveState?.(state);
  return "wait";
}

function emptyState(): BackoffState {
  return { relaunches: [], slow: false, freshSince: null };
}

/**
 * service_backoff: max 3 relaunches per 300 s, then 1 attempt per 5 min, until
 * 300 s of continuous fresh heartbeat resets the counter. If the counter can't
 * PERSIST (a full disk in oneshot mode), relaunching anyway would be unbounded
 * thrash — fail safe: surface it and stand down this tick.
 */
function relaunchUnderBackoff(
  deps: WatcherDeps,
  state: BackoffState,
  nowS: number,
  action: "relaunch" | "kill-relaunch",
): WatchAction {
  const log = deps.log ?? (() => {});
  const last = state.relaunches.at(-1);
  if (state.slow && last !== undefined && nowS - last < BACKOFF_SLOW_INTERVAL_S) {
    deps.saveState?.(state);
    return "backoff";
  }
  const inWindow = state.relaunches.filter((t) => nowS - t <= BACKOFF_WINDOW_S);
  if (!state.slow && inWindow.length >= BACKOFF_MAX_RELAUNCHES) {
    state.slow = true;
    log("relaunch ceiling hit (3 in 300 s) — dropping to 1 attempt per 5 min");
    if (last !== undefined && nowS - last < BACKOFF_SLOW_INTERVAL_S) {
      deps.saveState?.(state);
      return "backoff";
    }
  }

  state.relaunches = [...state.relaunches.slice(-19), nowS];
  const persisted = deps.saveState ? deps.saveState(state) : true;
  if (!persisted) {
    (deps.notify ?? notifyOs)(
      "Windy Talk: the disk is full",
      "Windy Talk stopped and can't be restarted safely until disk space is freed.",
    );
    return "state-unwritable";
  }
  const launched = (deps.relaunch ?? (() => relaunchFromSpec(controlPaths().resurrectionSpec, log)))();
  return launched ? action : "no-spec";
}

function defaultProbeWritable(dir: string): boolean {
  // Random, unpredictable name: a same-user process pre-creating a predictable
  // probe path as a DIRECTORY would force writable=false and trick the watcher
  // into standing down on a genuinely-wedged app (a self-heal DoS).
  const probe = path.join(dir, `.wt-probe-${randomBytes(8).toString("hex")}`);
  try {
    fs.writeFileSync(probe, "x");
    fs.unlinkSync(probe);
    return true;
  } catch {
    return false;
  }
}

// -- relaunch spec (written by the resurrection installer, never guessed) -------

export interface RelaunchSpec {
  launch: { cmd: string; args: string[]; cwd?: string; env?: Record<string, string> };
}

export function relaunchFromSpec(specPath: string, log: (m: string) => void): boolean {
  let spec: RelaunchSpec;
  try {
    spec = JSON.parse(fs.readFileSync(specPath, "utf8"));
    if (typeof spec?.launch?.cmd !== "string" || !Array.isArray(spec.launch.args)) {
      throw new Error("malformed spec");
    }
  } catch (e) {
    log(`cannot relaunch: resurrection spec unreadable (${String(e)})`);
    notifyOs(
      "Windy Talk needs attention",
      "Windy Talk stopped and its restart settings are missing. Open Windy Talk to repair it.",
    );
    return false;
  }
  const child = spawn(spec.launch.cmd, spec.launch.args, {
    cwd: spec.launch.cwd,
    env: { ...process.env, ...(spec.launch.env ?? {}) },
    detached: true,
    stdio: "ignore",
  });
  child.unref();
  log(`relaunched: ${spec.launch.cmd} ${spec.launch.args.join(" ")}`);
  return true;
}

// -- state store ----------------------------------------------------------------

export function loadStateFile(statePath: string): BackoffState {
  try {
    const parsed = JSON.parse(fs.readFileSync(statePath, "utf8"));
    return {
      relaunches: Array.isArray(parsed?.relaunches)
        ? parsed.relaunches.filter((t: unknown) => typeof t === "number")
        : [],
      slow: parsed?.slow === true,
      freshSince: typeof parsed?.freshSince === "number" ? parsed.freshSince : null,
    };
  } catch {
    return emptyState();
  }
}

export function saveStateFile(statePath: string, state: BackoffState): boolean {
  try {
    fs.mkdirSync(path.dirname(statePath), { recursive: true, mode: 0o700 });
    const tmp = statePath + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(state), { mode: 0o600 });
    fs.renameSync(tmp, statePath);
    return true;
  } catch {
    return false;
  }
}

// -- CLI shell --------------------------------------------------------------------

export async function runWatcherCli(argv: string[]): Promise<void> {
  const paths = controlPaths();
  const log = (m: string) => console.log(`[windytalk-watcher] ${m}`);
  const deps: WatcherDeps = {
    heartbeatPath: paths.heartbeat,
    relaunch: () => relaunchFromSpec(paths.resurrectionSpec, log),
    loadState: () => loadStateFile(paths.resurrectionState),
    saveState: (s) => saveStateFile(paths.resurrectionState, s),
    loadUpdateState: () => readUpdateState(paths.stateDir),
    updateAttested: (st) => {
      // Independent of the app's own claim: the new build must have set its
      // attested flag AND be writing a fresh heartbeat right now.
      if (st.attested !== true) return false;
      const hb = readHeartbeat(paths.heartbeat);
      if (!hb) return false;
      return Date.now() - hb.mtimeMs <= STALE_DEAD_S * 1000;
    },
    rollbackUpdate: (st) => {
      // Flip the A/B pointer back to the previous known-good binary, then
      // relaunch it. The spec was written pointing at the active binary; the
      // installer owns the pointer, so here we rewrite the relaunch spec's cmd
      // to previousBinary and relaunch, then clear the marker.
      try {
        const specPath = paths.resurrectionSpec;
        const spec = JSON.parse(readFileSyncSafe(specPath) || "{}");
        if (spec.launch) {
          spec.launch.cmd = st.previousBinary;
          writeFileAtomic(specPath, JSON.stringify(spec, null, 2));
        }
      } catch (e) {
        log(`rollback: could not rewrite relaunch spec (${String(e)})`);
      }
      clearUpdateState(paths.stateDir);
      relaunchFromSpec(paths.resurrectionSpec, log);
    },
    clearUpdate: () => clearUpdateState(paths.stateDir),
    log,
  };
  if (argv.includes("--loop")) {
    // Windows logon-task mode (Scheduled Tasks can't fire every 15 s).
    for (;;) {
      await checkOnce(deps);
      await new Promise((r) => setTimeout(r, WATCH_INTERVAL_MS));
    }
  }
  const action = await checkOnce(deps);
  log(`check: ${action}`);
}

const invokedDirectly =
  typeof process.argv[1] === "string" &&
  (process.argv[1].endsWith("watcher.js") || process.argv[1].endsWith("watcher.ts"));
if (invokedDirectly) {
  runWatcherCli(process.argv.slice(2)).catch((e) => {
    console.error(`[windytalk-watcher] fatal: ${String(e)}`);
    process.exitCode = 1;
  });
}
