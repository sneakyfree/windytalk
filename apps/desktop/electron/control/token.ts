// The PER-INSTALL control bearer token (contract security.token). Deliberately
// NOT the hands token and NOT per-launch: it is generated once, persisted 0600,
// survives restart_app and reset_to_defaults, and rotates only via an explicit
// UI action — a rotating token would strand a registered MCP client (the
// grandma path) on every restart.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { TOKEN_ENV_OVERRIDE } from "./constants.js";

export function loadOrCreateToken(
  tokenPath: string,
  env: NodeJS.ProcessEnv = process.env,
): string {
  const override = env[TOKEN_ENV_OVERRIDE];
  if (override) return override; // tests only, per the contract
  try {
    const existing = fs.readFileSync(tokenPath, "utf8").trim();
    if (existing) return existing;
  } catch {
    // absent or unreadable: mint below
  }
  const token = crypto.randomBytes(24).toString("hex");
  fs.mkdirSync(path.dirname(tokenPath), { recursive: true, mode: 0o700 });
  // 0600 (owner-only). On Windows the profile dir ACL covers it; mode is a no-op.
  fs.writeFileSync(tokenPath, token + "\n", { mode: 0o600 });
  return token;
}

/**
 * Constant-time compare (the hands wall's secrets.compare_digest, in Node).
 * Hash both sides to fixed length first so length never leaks via timing.
 */
export function tokenEquals(presented: string, expected: string): boolean {
  const a = crypto.createHash("sha256").update(presented, "utf8").digest();
  const b = crypto.createHash("sha256").update(expected, "utf8").digest();
  return crypto.timingSafeEqual(a, b);
}
