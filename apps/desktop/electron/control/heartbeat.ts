// The serving-liveness heartbeat (contract resurrection.heartbeat). The file is
// JSON {pid, started_at, exe} — mtime carries staleness, content carries the
// identity BOTH kill paths verify before any SIGKILL.
//
// liveness_semantics: the writer is FATE-COUPLED to the serving loop — bumped
// only after an attestation proves the app is actually serving (a served :8782
// serving-path round-trip, a renderer<->main echo, or a recent engine frame).
// A free-running timer that keeps ticking while serving is dead is exactly the
// "looks-alive-but-dead" failure this design exists to kill; here the timer
// callback ASKS the attestor every tick and skips the bump when it says no.
import fs from "node:fs";
import path from "node:path";

import { HEARTBEAT_INTERVAL_MS } from "./constants.js";
import type { IdentityRecord } from "./identity.js";

/** Resolves true only when a serving-path round-trip succeeded (see attest.ts). */
export type Attestor = () => Promise<boolean>;

export interface HeartbeatWriterOpts {
  heartbeatPath: string;
  identity: IdentityRecord;
  attest: Attestor;
  intervalMs?: number;
  onError?: (err: unknown) => void;
}

export class HeartbeatWriter {
  private timer: NodeJS.Timeout | null = null;
  private ticking = false;
  readonly opts: Required<Pick<HeartbeatWriterOpts, "intervalMs">> & HeartbeatWriterOpts;

  constructor(opts: HeartbeatWriterOpts) {
    this.opts = { intervalMs: HEARTBEAT_INTERVAL_MS, ...opts };
  }

  start(): void {
    if (this.timer) return;
    // Immediate first bump attempt so a fresh launch is fresh on disk fast
    // (rollback_criteria: a new build must heartbeat within its 60 s window).
    void this.tick();
    this.timer = setInterval(() => void this.tick(), this.opts.intervalMs);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  /** One writer cycle: attest, then bump. Exposed for tests (injected clocking). */
  async tick(): Promise<boolean> {
    if (this.ticking) return false; // a wedged attestor must not stack ticks
    this.ticking = true;
    try {
      const serving = await this.opts.attest();
      if (!serving) return false; // no attestation -> no bump -> file goes stale
      writeHeartbeat(this.opts.heartbeatPath, this.opts.identity);
      return true;
    } catch (err) {
      // Can't write (e.g. disk full): the file goes stale; the watcher's
      // fs-writability probe tells disk-full apart from a wedge. Never throw.
      this.opts.onError?.(err);
      return false;
    } finally {
      this.ticking = false;
    }
  }
}

export function writeHeartbeat(heartbeatPath: string, identity: IdentityRecord): void {
  fs.mkdirSync(path.dirname(heartbeatPath), { recursive: true, mode: 0o700 });
  // temp + rename so a reader never sees a torn record.
  const tmp = heartbeatPath + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(identity), { mode: 0o600 });
  fs.renameSync(tmp, heartbeatPath);
}

/**
 * restart_app's fast path (resurrection.restart_app_path): removing the file is
 * tier1 "ABSENT -> relaunch immediately" — the ONE relaunch path, no race.
 */
export function removeHeartbeat(heartbeatPath: string): void {
  try {
    fs.unlinkSync(heartbeatPath);
  } catch {
    // already absent — fine
  }
}

export function readHeartbeat(
  heartbeatPath: string,
): { record: IdentityRecord | null; mtimeMs: number } | null {
  let st: fs.Stats;
  try {
    st = fs.statSync(heartbeatPath);
  } catch {
    return null; // absent
  }
  let record: IdentityRecord | null = null;
  try {
    const parsed = JSON.parse(fs.readFileSync(heartbeatPath, "utf8"));
    if (
      typeof parsed?.pid === "number" &&
      typeof parsed?.started_at === "number" &&
      typeof parsed?.exe === "string"
    ) {
      record = parsed as IdentityRecord;
    }
  } catch {
    // Unparseable content: identity unknown -> treated as pid-absent by the
    // watcher (no kill is possible without a verified identity; safe default).
  }
  return { record, mtimeMs: st.mtimeMs };
}
