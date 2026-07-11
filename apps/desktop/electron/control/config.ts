// The supervisor's config store + safe-mode overlay (contract `safe_mode` +
// $defs.config). Safe mode is a runtime OVERLAY over the UNTOUCHED persisted
// config: FACTORY defaults for the behavioral values — EXCEPT engine_url, which
// keeps the current/paired engine (on the flagship local-5090-over-LAN topology
// a factory engine URL reaches no engine by construction; safe mode must not go
// voiceless). The mode is PERSISTED so a crash-looping machine relaunches INTO
// safe mode, not the loop config.
import fs from "node:fs";
import path from "node:path";

/** The user-changeable settings shape (contract $defs.config). */
export interface UserConfig {
  engine_url: string;
  brain: string;
  audio_input_id: string | null;
  audio_output_id: string | null;
  volume: number;
  hands_free: boolean;
  autonomy: number;
}

/**
 * FACTORY defaults — the immutable compiled-in constant (last_known_good
 * .corrupt_fallback: never a file that can rot). Autonomy = the pinned
 * fresh-install cap (3).
 */
export const FACTORY_CONFIG: Readonly<UserConfig> = Object.freeze({
  engine_url: "ws://127.0.0.1:8788",
  brain: "default",
  audio_input_id: null,
  audio_output_id: null,
  volume: 80,
  hands_free: false,
  autonomy: 3,
});

export class ConfigStore {
  private saved: UserConfig;
  private safeMode: boolean;
  private readonly configPath: string;
  private readonly safeFlagPath: string;
  /** How the config loaded — drives Layer-1's corrupt-config LKG recovery. */
  readonly loadedFrom: "config" | "fallback" | "factory";

  constructor(configDir: string, opts: { fallback?: () => UserConfig | null } = {}) {
    this.configPath = path.join(configDir, "config.json");
    this.safeFlagPath = path.join(configDir, "safe-mode");
    const loaded = this.load();
    if (loaded) {
      this.saved = loaded;
      this.loadedFrom = "config";
    } else {
      // Corrupt/absent config: Layer-1 recovery — last-known-good first, then
      // the immutable factory constant (last_known_good.corrupt_fallback).
      const lkg = opts.fallback?.() ?? null;
      this.saved = lkg ?? { ...FACTORY_CONFIG };
      this.loadedFrom = lkg ? "fallback" : "factory";
      if (lkg) this.persist(); // re-materialize the recovered config
    }
    this.safeMode = fs.existsSync(this.safeFlagPath);
  }

  /**
   * reset_to_defaults: factory (the immutable constant), settings-only, and
   * CLEARS the persisted safe-mode flag — a reset lands in mode 'normal'.
   */
  reset(): void {
    this.saved = { ...FACTORY_CONFIG };
    this.persist();
    this.setSafeMode(false);
  }

  /** The persisted (underlying) config — untouched by the safe-mode overlay. */
  getSaved(): UserConfig {
    return { ...this.saved };
  }

  /**
   * The ACTIVE config: in safe mode, factory for the behavioral values but the
   * saved engine_url (safe_mode.nature's pinned exception).
   */
  getActive(): UserConfig {
    if (!this.safeMode) return { ...this.saved };
    return { ...FACTORY_CONFIG, engine_url: this.saved.engine_url };
  }

  get inSafeMode(): boolean {
    return this.safeMode;
  }

  /** Persisted flag: a crash-looping machine relaunches INTO safe mode. */
  setSafeMode(on: boolean): void {
    this.safeMode = on;
    try {
      if (on) fs.writeFileSync(this.safeFlagPath, "1", { mode: 0o600 });
      else fs.unlinkSync(this.safeFlagPath);
    } catch {
      // Flag already in the desired state (or disk trouble the watcher surfaces).
    }
  }

  /**
   * Write the UNDERLYING config (in safe mode this is the "saved — will apply
   * when you leave safe mode" path; the overlay stays factory until exit).
   */
  setSaved(patch: Partial<UserConfig>): UserConfig {
    this.saved = { ...this.saved, ...patch };
    this.persist();
    return { ...this.saved };
  }

  private load(): UserConfig | null {
    try {
      const parsed = JSON.parse(fs.readFileSync(this.configPath, "utf8"));
      return this.sanitize(parsed);
    } catch {
      return null; // absent or corrupt — the constructor picks the fallback
    }
  }

  /** Unknown/invalid fields never survive a load — factory per field. */
  private sanitize(raw: Record<string, unknown>): UserConfig {
    const out = { ...FACTORY_CONFIG } as UserConfig;
    if (typeof raw.engine_url === "string") out.engine_url = raw.engine_url;
    if (typeof raw.brain === "string") out.brain = raw.brain;
    if (typeof raw.audio_input_id === "string") out.audio_input_id = raw.audio_input_id;
    if (typeof raw.audio_output_id === "string") out.audio_output_id = raw.audio_output_id;
    if (typeof raw.volume === "number" && raw.volume >= 0 && raw.volume <= 100) {
      out.volume = Math.round(raw.volume);
    }
    if (typeof raw.hands_free === "boolean") out.hands_free = raw.hands_free;
    if (typeof raw.autonomy === "number" && raw.autonomy >= 0 && raw.autonomy <= 10) {
      out.autonomy = Math.round(raw.autonomy);
    }
    return out;
  }

  private persist(): void {
    fs.mkdirSync(path.dirname(this.configPath), { recursive: true, mode: 0o700 });
    const tmp = this.configPath + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(this.saved, null, 2), { mode: 0o600 });
    fs.renameSync(tmp, this.configPath);
  }
}
