// Launch the BUNDLED hands surface with the payload's frozen python — the
// bring-your-own-runtime doctrine at runtime (docs/PACKAGING.md): a packaged
// app NEVER consults the machine's python. Dev checkouts have no payload, so
// this returns null and the dev flow (`python -m hands` by hand) is unchanged.
import { spawn, type ChildProcess } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export interface HandsLaunch {
  child: ChildProcess;
  /** Bearer for X-Windytalk-Token — shared with main's hands proxy. */
  token: string;
}

/** The payload's frozen interpreter, or null when unpackaged. */
export function payloadPython(
  resourcesPath: string,
  platform: NodeJS.Platform = process.platform,
): string | null {
  const p = platform === "win32"
    ? path.join(resourcesPath, "payload", "python", "python.exe")
    : path.join(resourcesPath, "payload", "python", "bin", "python3");
  return fs.existsSync(p) ? p : null;
}

export function launchBundledHands(
  resourcesPath: string,
  opts: { env?: NodeJS.ProcessEnv; log?: (msg: string) => void; spawnImpl?: typeof spawn } = {},
): HandsLaunch | null {
  const python = payloadPython(resourcesPath);
  if (!python) return null;
  const doSpawn = opts.spawnImpl ?? spawn;
  const env: NodeJS.ProcessEnv = { ...(opts.env ?? process.env) };
  // One shared secret: generated here, handed to the child (surface.py reads
  // WINDYTALK_HANDS_TOKEN) and returned so main's proxy sends the same value.
  const token = (env.WINDYTALK_HANDS_TOKEN ?? "").trim() || crypto.randomBytes(24).toString("hex");
  env.WINDYTALK_HANDS_TOKEN = token;
  env.PYTHONPATH = path.join(resourcesPath, "payload", "app-py");
  env.PYTHONDONTWRITEBYTECODE = "1";
  // Bundled input tools ahead of PATH so the fallback chains find them even on
  // a box that has none (the cocktail travels with the app).
  env.PATH = `${path.join(resourcesPath, "payload", "tools")}${path.delimiter}${env.PATH ?? ""}`;
  // stdin ignored on purpose: the console confirmer EOFs -> gated tiers DENY
  // (fail closed). The in-app tap confirmer is the pinned v1.1 refinement.
  const child = doSpawn(python, ["-m", "hands"], {
    env,
    stdio: ["ignore", "ignore", "ignore"],
  });
  child.on("error", () => opts.log?.("bundled hands failed to spawn"));
  child.on("exit", (code, signal) =>
    opts.log?.(`bundled hands exited (code=${code} signal=${signal})`));
  return { child, token };
}
