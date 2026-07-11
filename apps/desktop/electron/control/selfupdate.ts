// Safe self-update guards (contract self_update) — apply_update is RCE-by-design,
// so every guard here is NORMATIVE and must pass BEFORE an artifact is staged.
// The pure functions are the correctness core (fully unit-tested); the file
// operations run only behind a configured trust root (see update-key.ts).
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { EMBEDDED_UPDATE_PUBLIC_KEY, updateConfigured } from "./update-key.js";

export interface ReleaseArtifact {
  version: string;
  data: Buffer;
  /** Detached signature over `data` by the update-signing private key. */
  signature: Buffer;
}

export interface UpdateSource {
  /** Newest NON-prerelease version on the channel (the ONLY installable one). */
  channelHead(): Promise<string | null>;
  fetchArtifact(version: string): Promise<ReleaseArtifact>;
}

export type ApplyResult =
  | { ok: true; result: string }
  | {
      ok: false;
      error:
        | "no update source configured"
        | "unsigned or untrusted update"
        | "downgrade refused"
        | "insufficient disk"
        | "timeout";
      result?: string;
    };

// -- semver (strict; prerelease sorts BELOW its release, but head excludes them)

export function parseSemver(v: string): { major: number; minor: number; patch: number; pre: string | null } | null {
  const m = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/.exec(v.trim());
  if (!m) return null;
  return { major: +m[1], minor: +m[2], patch: +m[3], pre: m[4] ?? null };
}

/** -1 | 0 | 1; throws on unparseable input (a malformed version never compares equal). */
export function compareSemver(a: string, b: string): -1 | 0 | 1 {
  const pa = parseSemver(a);
  const pb = parseSemver(b);
  if (!pa || !pb) throw new Error(`unparseable semver: ${!pa ? a : b}`);
  for (const k of ["major", "minor", "patch"] as const) {
    if (pa[k] !== pb[k]) return pa[k] < pb[k] ? -1 : 1;
  }
  if (pa.pre === pb.pre) return 0;
  if (pa.pre === null) return 1; // release > prerelease
  if (pb.pre === null) return -1;
  return pa.pre < pb.pre ? -1 : 1;
}

// -- signature (Ed25519 detached; verify-before-stage) ---------------------------

export function verifySignature(publicKeyPem: string, data: Buffer, signature: Buffer): boolean {
  if (!updateConfigured(publicKeyPem)) return false; // no trust root -> never verifies
  try {
    const key = crypto.createPublicKey(publicKeyPem);
    return crypto.verify(null, data, key, signature); // Ed25519: algorithm=null
  } catch {
    return false;
  }
}

// -- anti-rollback (signature proves AUTHENTICITY, not FRESHNESS) ------------------

/**
 * Only the channel-head installs. Reject version <= current AND reject any
 * signed intermediate that is > current but < head (both -> 'downgrade refused').
 */
export function checkAntiRollback(
  current: string,
  target: string,
  head: string,
): { ok: true } | { ok: false; error: "downgrade refused" } {
  if (compareSemver(target, current) <= 0) return { ok: false, error: "downgrade refused" };
  if (compareSemver(target, head) !== 0) return { ok: false, error: "downgrade refused" };
  return { ok: true };
}

// -- disk precheck (fail CLOSED, never half-stage) --------------------------------

/** Need room for the A/B PAIR (new alongside old) + margin. */
export function precheckDisk(freeBytes: number, artifactBytes: number): boolean {
  const needed = artifactBytes * 2 + 64 * 1024 * 1024; // A/B pair + 64 MB margin
  return freeBytes >= needed;
}

// -- the guard pipeline (verify -> anti-rollback -> disk), source-gated -----------

export interface ApplyDeps {
  source: UpdateSource | null;
  publicKeyPem?: string;
  currentVersion: string;
  requestedVersion?: string; // omitted = 'latest' (channel-head)
  freeBytes: () => number;
  /** Stage the verified artifact A/B and flip the pointer; returns the marker. */
  stage: (artifact: ReleaseArtifact) => Promise<void>;
}

/**
 * Run all NORMATIVE guards, then stage. INERT (no configured source) ->
 * the honest 'no update source configured'. Never stages an artifact that
 * failed any guard.
 */
export async function applyUpdate(deps: ApplyDeps): Promise<ApplyResult> {
  const pem = deps.publicKeyPem ?? EMBEDDED_UPDATE_PUBLIC_KEY;
  if (!deps.source || !updateConfigured(pem)) {
    return { ok: false, error: "no update source configured" };
  }
  const head = await deps.source.channelHead();
  if (!head) return { ok: false, error: "no update source configured" };
  const targetVersion = deps.requestedVersion && deps.requestedVersion !== "latest"
    ? deps.requestedVersion
    : head;

  // anti-rollback is cheap and needs no download — check it first.
  const fresh = checkAntiRollback(deps.currentVersion, targetVersion, head);
  if (!fresh.ok) return fresh;

  const artifact = await deps.source.fetchArtifact(targetVersion);
  // Signature-verify BEFORE staging (run_selftest is NOT the integrity gate).
  if (!verifySignature(pem, artifact.data, artifact.signature)) {
    return { ok: false, error: "unsigned or untrusted update" };
  }
  // Belt-and-suspenders: the fetched artifact's own version must match target.
  if (artifact.version !== targetVersion) {
    return { ok: false, error: "unsigned or untrusted update", result: "artifact version mismatch" };
  }
  if (!precheckDisk(deps.freeBytes(), artifact.data.length)) {
    return { ok: false, error: "insufficient disk" };
  }
  await deps.stage(artifact);
  return { ok: true, result: "restarting" };
}

// -- A/B state + the OUT-OF-PROCESS rollback decision (lives in the watcher) -------

export interface UpdateState {
  pending: boolean;
  fromVersion: string;
  toVersion: string;
  /** The previous known-good binary to flip back to on failure. */
  previousBinary: string;
  newBinary: string;
  /** epoch ms by which the new build must have attested. */
  deadlineMs: number;
  /**
   * Set TRUE by the new build once it self-verifies (launch + fresh heartbeat
   * + bind + engine reachability). The watcher NEVER trusts this alone — it
   * also requires an independent fresh-heartbeat + serving signal — but a build
   * that crashes on boot can never set it, which is the case out-of-process
   * rollback exists to catch. Only the watcher CLEARS the marker (on commit),
   * so the app cannot fake success by deleting it.
   */
  attested?: boolean;
}

export const UPDATE_STATE_FILE = "update-state.json";

export function writeUpdateState(stateDir: string, state: UpdateState): void {
  fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  const p = path.join(stateDir, UPDATE_STATE_FILE);
  const tmp = p + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(state), { mode: 0o600 });
  fs.renameSync(tmp, p);
}

export function readUpdateState(stateDir: string): UpdateState | null {
  try {
    const parsed = JSON.parse(fs.readFileSync(path.join(stateDir, UPDATE_STATE_FILE), "utf8"));
    if (parsed && typeof parsed.toVersion === "string" && typeof parsed.previousBinary === "string") {
      return parsed as UpdateState;
    }
  } catch {
    // absent — no update in flight
  }
  return null;
}

export function clearUpdateState(stateDir: string): void {
  try {
    fs.unlinkSync(path.join(stateDir, UPDATE_STATE_FILE));
  } catch {
    // already gone
  }
}

export type RollbackDecision = "wait" | "commit" | "rollback";

/**
 * The out-of-process rollback trigger (self_update.out_of_process_rollback):
 * the NEW build must, within 60 s, launch + write a fresh heartbeat + bind
 * :8782 + pass engine reachability. The watcher owns this decision so a hostile
 * new build cannot suppress its own rollback.
 *
 * attested = the running build proved itself: a fresh heartbeat whose version
 * matches toVersion AND (bind + engine-reachability) succeeded. Audio stages
 * are NOT inputs here (grandma may update with her headset unplugged).
 */
export function rollbackDecision(
  state: UpdateState,
  attested: boolean,
  nowMs: number,
): RollbackDecision {
  if (attested) return "commit"; // the new build is healthy: keep it
  if (nowMs < state.deadlineMs) return "wait"; // still inside the 60 s window
  return "rollback"; // deadline passed without attestation: flip back
}
