// Last-known-good config store (contract last_known_good). LKG is the user's
// most recent WORKING customization, used ONLY by Layer-1 autonomic recovery —
// safe mode uses factory (safe_mode.nature), reset_to_defaults uses factory
// and INVALIDATES all generations so auto-recovery can never restore what the
// user explicitly discarded with the big red button.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import type { UserConfig } from "./config.js";

const GENERATIONS = 3;

export class LkgStore {
  private readonly dir: string;

  constructor(configDir: string) {
    this.dir = configDir;
  }

  private genPath(n: number): string {
    return path.join(this.dir, `lkg-${n}.json`);
  }

  /** Atomic (temp+rename), checksummed, rotates N generations (newest = 1). */
  write(config: UserConfig): void {
    const canonical = JSON.stringify(config);
    // Skip a write when gen-1 already holds this exact config (no churn).
    const current = this.readGen(1);
    if (current && JSON.stringify(current) === canonical) return;
    for (let n = GENERATIONS - 1; n >= 1; n--) {
      try {
        fs.renameSync(this.genPath(n), this.genPath(n + 1));
      } catch {
        // generation absent — fine
      }
    }
    const record = {
      checksum: crypto.createHash("sha256").update(canonical).digest("hex"),
      config,
    };
    fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
    const tmp = this.genPath(1) + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(record), { mode: 0o600 });
    fs.renameSync(tmp, this.genPath(1));
  }

  /**
   * Newest generation whose checksum verifies; null when every generation is
   * corrupt/absent — the caller then lands on the baked-in FACTORY constant
   * ('lands somewhere that works' is guaranteed by that constant, not a file).
   */
  loadBest(): UserConfig | null {
    for (let n = 1; n <= GENERATIONS; n++) {
      const config = this.readGen(n);
      if (config) return config;
    }
    return null;
  }

  /** reset_to_defaults: factory becomes the new seed; all generations die. */
  invalidateAll(): void {
    for (let n = 1; n <= GENERATIONS; n++) {
      try {
        fs.unlinkSync(this.genPath(n));
      } catch {
        // absent — fine
      }
    }
  }

  private readGen(n: number): UserConfig | null {
    try {
      const parsed = JSON.parse(fs.readFileSync(this.genPath(n), "utf8"));
      const canonical = JSON.stringify(parsed.config);
      const sum = crypto.createHash("sha256").update(canonical).digest("hex");
      if (sum !== parsed.checksum) return null; // rotted — skip this generation
      return parsed.config as UserConfig;
    } catch {
      return null;
    }
  }
}
