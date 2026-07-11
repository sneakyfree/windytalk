// Single-instance lock + takeover (contract resurrection.single_instance).
//
// The exclusivity primitive: Node has no flock(2), so the kernel-released lock
// is an exclusive LOCAL SOCKET — a unix socket (POSIX) / named pipe (Windows).
// It has exactly the property every pinned branch is derived from: the kernel
// releases it when the holder dies, so "the lock is held" always means "some
// LIVE process holds it", and a mismatched recorded pid cannot be the true
// holder. The pinned lock-file CONTENT {pid, started_at, exe} is written at
// acquire to instance.lock, exactly as specified, and is what the takeover
// verifies (same identity discipline as the watcher's tier-2 kill).
//
// Protocol (pinned): a second instance asks the holder to ack via a
// SERVING-ATTESTING :8782 round-trip (never a bare accept — a connect() to the
// lock socket succeeding proves nothing; the kernel completes handshakes into
// the backlog of a deadlocked process). Ack within 3 s (one retry) -> focus the
// holder and exit. No ack -> verify the recorded identity against the live pid:
// MATCH -> SIGKILL the wedged holder, take the lock, start serving. MISMATCH ->
// a live foreign process holds the socket (the recorded holder is gone) -> do
// NOT kill; surface the squatter case and exit.
import fs from "node:fs";
import net from "node:net";
import path from "node:path";

import { ACK_RETRIES, ACK_TIMEOUT_MS } from "./constants.js";
import {
  identityMatches,
  procIdentity,
  selfIdentity,
  type IdentityRecord,
  type LiveIdentity,
} from "./identity.js";
import { helloControlPort } from "./server.js";

export type AcquireResult =
  | { role: "holder"; release: () => void }
  | { role: "second-focused" } // healthy holder acked + focused; caller exits 0
  | { role: "squatter"; detail: string } // never killed; caller surfaces + exits
  | { role: "error"; detail: string };

export interface InstanceDeps {
  socketPath: string;
  lockFilePath: string;
  portFilePath: string;
  readToken: () => string | null;
  identity?: IdentityRecord;
  getIdentity?: (pid: number) => Promise<LiveIdentity>;
  kill?: (pid: number) => void;
  hello?: typeof helloControlPort;
  ackTimeoutMs?: number;
  ackRetries?: number;
  log?: (msg: string) => void;
}

export async function acquireInstanceLock(deps: InstanceDeps): Promise<AcquireResult> {
  const log = deps.log ?? (() => {});
  const getIdentity = deps.getIdentity ?? ((pid: number) => procIdentity(pid));
  const kill = deps.kill ?? ((pid: number) => process.kill(pid, "SIGKILL"));
  const hello = deps.hello ?? helloControlPort;
  const ackTimeout = deps.ackTimeoutMs ?? ACK_TIMEOUT_MS;
  const ackRetries = deps.ackRetries ?? ACK_RETRIES;
  const self = deps.identity ?? (await selfIdentity());

  // One takeover attempt at most: bind -> (held?) ack -> verify -> kill -> bind.
  for (let attempt = 0; attempt < 3; attempt++) {
    const bound = await tryBindLockSocket(deps.socketPath);
    if (bound.ok) {
      // Post-bind self-verify closes the unlink/bind race two claimers can hit
      // on a filesystem unix socket: round-trip the PATH and expect our own pid.
      const seen = await socketHolderPid(deps.socketPath, 1_000);
      if (seen !== self.pid) {
        bound.server.close();
        log(`instance: lost the claim race to pid ${seen}; deferring`);
        continue; // someone else won — go through the second-instance path
      }
      writeLockContent(deps.lockFilePath, self);
      return {
        role: "holder",
        release: () => {
          try {
            bound.server.close();
          } catch {
            /* already closed */
          }
        },
      };
    }

    // The socket is HELD by a live process. Ask the holder to ack + focus via
    // the serving-attesting :8782 round-trip (3 s, one retry). Discovery files
    // (port/token) are polled inside the same budget: a holder that JUST won a
    // boot race may be milliseconds from writing them.
    const acked = await tryAck(deps, hello, ackTimeout, ackRetries);
    if (acked) {
      log("instance: healthy holder acked — focused it; exiting");
      return { role: "second-focused" };
    }

    // No ack: wedged holder or a foreign squatter. The lock-file content decides.
    const record = readLockContent(deps.lockFilePath);
    const live = record ? await getIdentity(record.pid) : null;
    if (record && live && identityMatches(record, live)) {
      log(`instance: holder pid ${record.pid} is wedged (no ack) — SIGKILL + takeover`);
      try {
        kill(record.pid);
      } catch {
        // Died between verify and kill: the socket frees either way.
      }
      await waitForSocketFree(deps.socketPath, 3_000);
      continue; // retry the bind; the kernel released the dead holder's socket
    }
    // Mismatch = the recorded holder is gone yet something live holds the
    // socket. Before the squatter verdict, take one more full pass — a racing
    // legit winner may not have written its record yet. Never kill on mismatch.
    if (attempt === 0) continue;
    return {
      role: "squatter",
      detail:
        "a foreign process holds the Windy Talk instance lock " +
        "(recorded holder is gone) — not killing it; is something squatting the socket?",
    };
  }
  return { role: "error", detail: "could not acquire the instance lock after takeover attempts" };
}

/**
 * The pinned ack: up to (1 + retries) hello round-trips of `ackTimeout` each.
 * Missing discovery files consume the budget in short polls instead of failing
 * instantly, so a just-booting holder isn't misread as wedged.
 */
async function tryAck(
  deps: InstanceDeps,
  hello: typeof helloControlPort,
  ackTimeout: number,
  ackRetries: number,
): Promise<boolean> {
  const deadline = Date.now() + ackTimeout * (ackRetries + 1);
  let attempts = 0;
  while (Date.now() < deadline && attempts <= ackRetries) {
    const port = readPortFile(deps.portFilePath);
    const token = deps.readToken();
    if (port == null || token == null) {
      await new Promise((r) => setTimeout(r, Math.min(200, ackTimeout / 4)));
      continue;
    }
    attempts++;
    const remaining = Math.max(100, deadline - Date.now());
    const ack = await hello(port, token, Math.min(ackTimeout, remaining));
    if (ack.ok) return true;
  }
  return false;
}

// -- the socket primitive ------------------------------------------------------

type BindOutcome = { ok: true; server: net.Server } | { ok: false };

async function tryBindLockSocket(socketPath: string): Promise<BindOutcome> {
  for (let pass = 0; pass < 2; pass++) {
    const outcome = await bindOnce(socketPath);
    if (outcome !== "stale") return outcome;
    // Stale filesystem socket (holder SIGKILLed): clear it and retry once.
    try {
      fs.unlinkSync(socketPath);
    } catch {
      /* already gone */
    }
  }
  return { ok: false };
}

function bindOnce(socketPath: string): Promise<BindOutcome | "stale"> {
  return new Promise((resolve) => {
    const server = net.createServer((conn) => {
      // The lock socket's only protocol: identify the holder, then hang up.
      conn.end(JSON.stringify({ pid: process.pid }));
    });
    server.on("error", async (err: NodeJS.ErrnoException) => {
      if (err.code !== "EADDRINUSE") {
        resolve({ ok: false });
        return;
      }
      // In use: live holder, or a stale file left by a dead one?
      const holder = await socketHolderPid(socketPath, 1_000);
      resolve(holder != null ? { ok: false } : "stale");
    });
    server.listen(socketPath, () => {
      if (process.platform !== "win32") {
        try {
          fs.chmodSync(socketPath, 0o600);
        } catch {
          /* best-effort; the dir is 0700 */
        }
      }
      resolve({ ok: true, server });
    });
  });
}

/** Connect to the lock socket and read the holder's pid; null = nobody live. */
function socketHolderPid(socketPath: string, timeoutMs: number): Promise<number | null> {
  return new Promise((resolve) => {
    const conn = net.connect(socketPath);
    let data = "";
    const done = (pid: number | null) => {
      conn.destroy();
      resolve(pid);
    };
    conn.setTimeout(timeoutMs, () => done(null));
    conn.on("error", () => done(null));
    conn.on("data", (c) => (data += c));
    conn.on("end", () => {
      try {
        const parsed = JSON.parse(data);
        done(typeof parsed?.pid === "number" ? parsed.pid : null);
      } catch {
        done(null);
      }
    });
  });
}

async function waitForSocketFree(socketPath: string, budgetMs: number): Promise<void> {
  const deadline = Date.now() + budgetMs;
  while (Date.now() < deadline) {
    if ((await socketHolderPid(socketPath, 300)) == null) return;
    await new Promise((r) => setTimeout(r, 100));
  }
}

// -- lock-file content ----------------------------------------------------------

function writeLockContent(lockFilePath: string, identity: IdentityRecord): void {
  fs.mkdirSync(path.dirname(lockFilePath), { recursive: true, mode: 0o700 });
  const tmp = lockFilePath + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(identity), { mode: 0o600 });
  fs.renameSync(tmp, lockFilePath);
}

export function readLockContent(lockFilePath: string): IdentityRecord | null {
  try {
    const parsed = JSON.parse(fs.readFileSync(lockFilePath, "utf8"));
    if (
      typeof parsed?.pid === "number" &&
      typeof parsed?.started_at === "number" &&
      typeof parsed?.exe === "string"
    ) {
      return parsed as IdentityRecord;
    }
  } catch {
    /* absent or torn */
  }
  return null;
}

function readPortFile(portFilePath: string): number | null {
  try {
    const port = Number(fs.readFileSync(portFilePath, "utf8").trim());
    return Number.isInteger(port) && port > 0 ? port : null;
  } catch {
    return null;
  }
}
