// The control-surface tool registry + dispatch (contract `tools`, envelope per
// `result_shape`). Slice 1 ships the first three: get_health, reconnect,
// enter_safe_mode. Contract tools not yet built return ok:false
// error:'unsupported' with the reason in `result` (forced-honest, per
// platform_note) and are NOT advertised in /tools or MCP tools/list.
//
// Dispatch order per recovery_coordinator.lock.check_order: coordinator gate
// (lock -> debounce -> rate-limit) BEFORE the tier confirmer. Slice-1 tools are
// all auto_allow so the confirmer hook is a pass-through until slice 4 wires
// tier_resolution.
import { RecoveryCoordinator, EXEMPT_READS } from "./coordinator.js";
import type { ConfigStore } from "./config.js";
import type { CrashLoopDetector } from "./layer1.js";
import type { EngineAllowList } from "./engine-allow.js";
import { emitControlAction, type Emitter } from "./emit.js";
import type { LkgStore } from "./lkg.js";
import type { LogRing } from "./logring.js";
import { scrubDeviceName, scrubShortError } from "./scrub.js";
import { resolveTier } from "./tier.js";
import { applyUpdate, type ApplyResult, type ReleaseArtifact, type UpdateSource } from "./selfupdate.js";
import { updateConfigured } from "./update-key.js";

export interface Envelope {
  ok: boolean;
  result?: unknown;
  error?: string;
}

/** What the supervisor knows about the renderer/engine right now. */
export interface RendererStatus {
  connection: "connecting" | "online" | "offline" | "terminal";
  state: string;
  micOn: boolean;
  micError: string | null;
  sessionId: string | null;
  lastError: string | null;
  lastFrameAtMs: number | null;
}

export const OFFLINE_STATUS: RendererStatus = {
  connection: "offline",
  state: "offline",
  micOn: false,
  micError: null,
  sessionId: null,
  lastError: null,
  lastFrameAtMs: null,
};

/**
 * The confirmer (contract tiers + security.confirmer_fallback): a voice/tap
 * confirm when the app UI is up, a minimal NATIVE OS dialog from the
 * supervisor when it is not. 'unavailable' (truly headless) fails CLOSED.
 * 'allow_session' is the USER granting a session upgrade — never offered for
 * floor calls (the caller sets allowSessionGrant accordingly).
 */
export type Confirmer = (req: {
  tool: string;
  message: string;
  allowSessionGrant: boolean;
}) => Promise<"allow" | "allow_session" | "deny" | "unavailable">;

export interface ToolDeps {
  coordinator: RecoveryCoordinator;
  config: ConfigStore;
  allowList: EngineAllowList;
  detector: CrashLoopDetector;
  rendererStatus: () => RendererStatus;
  /** Command the renderer to re-dial; resolves true once online. */
  reconnectEngine: (timeoutMs: number) => Promise<boolean>;
  /** Push the active (overlay) config to the renderer (safe-mode entry). */
  applyActiveConfig: () => void;
  resurrectionArmed: () => boolean;
  /**
   * Is re-arming the resurrection service actually feasible on THIS box (for
   * get_capabilities.repair_resurrection)? False where a real arm attempt has
   * failed — the honest proxy for "privilege blocks it" (the boot self-check
   * always attempts a repair, so unarmed-after-attempt means re-arming isn't
   * working here). Defaults true before any attempt. Absent -> assume true.
   */
  resurrectionRepairable?: () => boolean;
  version: string;
  startedAtMs: number;
  emit: Emitter;
  logs: LogRing;
  /** Ask the renderer for audio devices / selftest stages (null on timeout). */
  probe: (kind: "audio-devices" | "selftest", timeoutMs: number) => Promise<unknown | null>;
  /** Is the engine a local child on THIS box (drives restart_engine's tri-state)? */
  engineIsLocal?: () => boolean;
  confirm: Confirmer;
  lkg: LkgStore;
  /** Deep reconnect: drop session state, new voice-session (restart_engine). */
  deepReconnectEngine: (timeoutMs: number) => Promise<boolean>;
  /** Clear transient caches (Electron session cache; models excluded). */
  clearCaches: () => Promise<void>;
  /** Re-arm the resurrection service (the single serialized repair routine). */
  repairResurrection: () => Promise<{ armed: boolean; detail: string }>;
  /**
   * Exit via the single resurrection path (response_ordering: called >=250 ms
   * after the response flushed; removes the heartbeat, exits distinguished).
   */
  restartApp: () => void;
  /** Reset the crash-loop counter (reset_to_defaults / exit_safe_mode). */
  resetCrashCounter: (why: string) => void;
  /** One plain sentence to the user (autonomy 0-2 notify-after + safe-mode). */
  notify?: (text: string) => void;
  /**
   * The last-synced brain-entitlement CACHE (works offline, when it's most
   * needed). 'default' is always accepted regardless. Empty = nothing synced
   * yet, so only 'default' passes (forced-honest, never fake entitlement).
   */
  entitledBrains: () => string[];
  /** The configured update source, or null when INERT (no key embedded). */
  updateSource?: () => UpdateSource | null;
  /** Free bytes on the install volume (apply_update disk precheck). */
  freeBytes?: () => number;
  /** Stage the verified artifact A/B + flip the pointer + write the marker. */
  stageUpdate?: (artifact: ReleaseArtifact) => Promise<void>;
  now?: () => number;
  /** reconnect's pinned block ceiling (10 s prod; injectable for tests). */
  reconnectTimeoutMs?: number;
  /** run_selftest's per-stage/total ceilings (5 s / 20 s prod; injectable). */
  selftestStageTimeoutMs?: number;
}

const MUTATING = new Set([
  "reconnect", "enter_safe_mode", "exit_safe_mode", "repair_resurrection",
  "restart_engine", "restart_app", "clear_cache", "reset_to_defaults",
  "set_audio_input", "set_audio_output", "set_volume", "set_engine_url",
  "set_brain", "set_wake_mode", "set_autonomy", "apply_update",
  "logout_account",
]);

const SET_TOOLS = new Set([
  "set_audio_input", "set_audio_output", "set_volume", "set_engine_url",
  "set_brain", "set_wake_mode", "set_autonomy",
]);

/** Plain-English confirm prompts (the confirmer shows these, voice or dialog). */
const CONFIRM_MESSAGES: Record<string, string> = {
  exit_safe_mode: "Leave safe mode and turn your saved settings (including hands) back on?",
  repair_resurrection: "Re-install Windy Talk's keep-alive protection?",
  restart_engine: "Restart the voice engine session?",
  restart_app: "Restart the whole Windy Talk app?",
  clear_cache: "Clear temporary files and reconnect? Settings and history are kept.",
  reset_to_defaults: "Reset ALL settings to factory defaults? Your conversation history is kept.",
  set_audio_input: "Switch which microphone Windy Talk listens with?",
  set_audio_output: "Switch which speaker Windy Talk talks through?",
  set_volume: "Mute the assistant's voice completely?",
  set_engine_url: "Point Windy Talk at a different voice engine?",
  set_brain: "Switch which brain answers you?",
  set_wake_mode: "Let Windy Talk listen for 'Hey Windy' all the time?",
  set_autonomy: "Let the assistant do more without asking first?",
  apply_update: "Install the latest Windy Talk update and restart?",
  open_account_portal: "Open your Windy account & billing page in the browser?",
  logout_account: "Sign out of your Windy account on this device? You can sign back in anytime.",
};

export class ControlTools {
  private readonly deps: ToolDeps;
  private readonly now: () => number;
  /** Session-scoped always-allow grants — granted by the USER, never an agent. */
  private sessionGrants = new Set<string>();

  constructor(deps: ToolDeps) {
    this.deps = deps;
    this.now = deps.now ?? Date.now;
  }

  /** Built tools only — what /tools and MCP tools/list advertise. */
  builtTools(): string[] {
    return [
      "get_health", "get_status", "get_config", "get_logs", "list_audio_devices",
      "run_selftest", "get_capabilities", "check_for_update",
      "reconnect", "enter_safe_mode", "exit_safe_mode", "repair_resurrection",
      "restart_engine", "restart_app", "clear_cache", "reset_to_defaults",
      "set_audio_input", "set_audio_output", "set_volume", "set_engine_url",
      "set_brain", "set_wake_mode", "set_autonomy", "apply_update",
      "get_account", "get_billing_summary", "open_account_portal", "logout_account",
    ];
  }

  async dispatch(
    tool: string,
    args: Record<string, unknown> = {},
    opts: { layer1?: boolean; preconfirmed?: boolean } = {},
  ): Promise<Envelope> {
    if (!this.isContractTool(tool)) return { ok: false, error: `unknown tool: ${tool}` };
    if (!this.builtTools().includes(tool)) {
      return { ok: false, error: "unsupported", result: "not built yet (control.mcp.v1 build in progress)" };
    }
    // apply_update is forced-honest INERT until the signing key is embedded:
    // there is nothing to stage and nothing to authorize, so refuse before the
    // lock + confirmer rather than prompt for an update that cannot happen.
    if (tool === "apply_update" && !this.updateReady()) {
      return { ok: false, error: "no update source configured" };
    }
    const gate = this.deps.coordinator.gate(tool, args, opts);
    if (!gate.proceed) return { ok: false, error: gate.error, result: gate.reason };

    // Tier confirmer — AFTER the coordinator (check_order: a lock-blocked or
    // rate-limited call never pointlessly prompts). Layer 1 is not an agent;
    // its autonomic actions never prompt. preconfirmed = the physical Reset
    // button, whose own dialog IS the confirmation.
    // Everything from here holds the lock, so a single try/finally guarantees
    // exactly one release even if the CONFIRMER rejects (else the lock would
    // leak until its 30 s/90 s ceiling). `abandoned` is read INSIDE the finally,
    // BEFORE release(), because release() clears the abandoned flag.
    let notifyAfter = false;
    let res: Envelope;
    let abandoned = false;
    try {
      if (!opts.layer1 && !opts.preconfirmed) {
        const decision = resolveTier(tool, args, {
          currentAutonomy: this.deps.config.getActive().autonomy,
          sessionGrants: this.sessionGrants,
        });
        if (decision.action === "allow") {
          notifyAfter = decision.notify_after && MUTATING.has(tool);
        } else {
          const outcome = await this.deps.confirm({
            tool,
            message: CONFIRM_MESSAGES[tool] ?? `Allow the assistant to run ${tool}?`,
            allowSessionGrant: decision.session_grant_allowed,
          });
          if (outcome === "allow_session" && decision.session_grant_allowed) {
            this.sessionGrants.add(tool); // granted by the USER via the confirmer
          } else if (outcome !== "allow" && outcome !== "allow_session") {
            // deny, or fail-CLOSED when even a native dialog can't render. The
            // finally releases the lock; no counters were charged (no commit).
            return { ok: false, error: "denied" };
          }
        }
      }
      gate.ticket.commit(); // the call EXECUTES: charge debounce/ceiling now
      res = await this.execute(tool, args);
    } catch (e) {
      res = { ok: false, error: `${(e as Error)?.name ?? "Error"}: ${scrubShortError(String((e as Error)?.message ?? e))}` };
    } finally {
      abandoned = gate.ticket.abandoned;
      gate.ticket.release();
    }
    if (abandoned) {
      // Preempted by enter_safe_mode: the handler was abandoned, its result
      // discarded (recovery_coordinator.lock.preempt).
      if (MUTATING.has(tool)) {
        emitControlAction(this.deps.emit, { tool, ok: false, error: "preempted", mode: this.mode() });
      }
      return { ok: false, error: "already_recovering", result: "abandoned: preempted by enter_safe_mode" };
    }
    if (MUTATING.has(tool) && !opts.layer1) {
      emitControlAction(this.deps.emit, { tool, ok: res.ok, error: res.error, mode: this.mode() });
    }
    if (notifyAfter && res.ok) {
      // autonomy 0-2: even auto_allow recovery tools notify the user AFTER acting.
      this.deps.notify?.(`I ran ${tool.replace(/_/g, " ")} to keep things working.`);
    }
    return res;
  }

  private async execute(tool: string, args: Record<string, unknown> = {}): Promise<Envelope> {
    switch (tool) {
      case "get_health":
        return { ok: true, result: this.health() };
      case "get_status": {
        const s = this.deps.rendererStatus();
        const state = s.connection === "online" ? s.state : "offline";
        const known = ["idle", "listening", "thinking", "speaking", "paused", "offline"];
        return {
          ok: true,
          result: {
            state: known.includes(state) ? state : "idle",
            mic_on: s.micOn,
            session_id: s.sessionId,
          },
        };
      }
      case "get_config": {
        // Positive allow-list: exactly the $defs/config fields, engine_url
        // scrubbed, ids passed through (ids are the API surface, not names).
        const shape = (c: ReturnType<ConfigStore["getActive"]>) => ({
          engine_url: this.deps.allowList.scrubForDiagnostics(c.engine_url),
          brain: c.brain,
          audio_input_id: c.audio_input_id,
          audio_output_id: c.audio_output_id,
          volume: c.volume,
          hands_free: c.hands_free,
          autonomy: c.autonomy,
        });
        return {
          ok: true,
          result: {
            active: shape(this.deps.config.getActive()),
            saved: shape(this.deps.config.getSaved()),
          },
        };
      }
      case "get_logs": {
        const lines = typeof args.lines === "number" ? args.lines : 100;
        return { ok: true, result: this.deps.logs.tail(lines) };
      }
      case "list_audio_devices": {
        const raw = await this.deps.probe("audio-devices", 5_000);
        if (raw == null) {
          return { ok: false, error: "timeout", result: "the app's audio layer did not answer" };
        }
        const scrub = (list: unknown, kind: "input" | "output") =>
          (Array.isArray(list) ? list : [])
            .filter((d) => d && typeof d.id === "string")
            .map((d) => ({
              id: d.id as string,
              name: scrubDeviceName(String(d.name ?? ""), d.id as string, kind),
              selected: d.selected === true,
            }));
        const devices = raw as { inputs?: unknown; outputs?: unknown };
        return {
          ok: true,
          result: { inputs: scrub(devices.inputs, "input"), outputs: scrub(devices.outputs, "output") },
        };
      }
      case "run_selftest":
        return { ok: true, result: await this.selftest() };
      case "get_capabilities":
        return { ok: true, result: this.capabilities() };
      case "check_for_update":
        return { ok: true, result: await this.checkForUpdate() };
      case "reconnect": {
        const ok = await this.deps.reconnectEngine(this.deps.reconnectTimeoutMs ?? 10_000);
        return ok ? { ok: true, result: "reconnected" } : { ok: false, error: "timeout" };
      }
      case "enter_safe_mode": {
        if (this.deps.config.inSafeMode) return { ok: true, result: "already in safe mode" };
        this.deps.config.setSafeMode(true);
        this.deps.applyActiveConfig();
        return { ok: true, result: "entered safe mode" };
      }
      case "exit_safe_mode": {
        if (!this.deps.config.inSafeMode) return { ok: true, result: "not in safe mode" };
        // Drop the overlay: the persisted config — INCLUDING set_* saves made
        // during safe mode — becomes active. Never an entry-time snapshot.
        this.deps.config.setSafeMode(false);
        this.deps.applyActiveConfig();
        this.deps.resetCrashCounter("exit_safe_mode");
        return { ok: true, result: "left safe mode — your saved settings are active again" };
      }
      case "repair_resurrection": {
        const status = await this.deps.repairResurrection();
        if (status.armed) return { ok: true, result: status.detail };
        // Privilege genuinely blocks it: honest unsupported, manual step rides
        // in result (platform_note / the tool's pinned description).
        return { ok: false, error: "unsupported", result: status.detail };
      }
      case "restart_engine": {
        // The desktop client never owns the engine as a child process (it's a
        // separate process — loopback, the LAN 5090, or the cloud), so a deep
        // reconnect (drop session state, new voice-session) is the real
        // behavior everywhere; get_capabilities reports 'degraded' accordingly.
        const ok = await this.deps.deepReconnectEngine(this.deps.reconnectTimeoutMs ?? 10_000);
        if (!ok) return { ok: false, error: "timeout" };
        // Only claim "remote" when the engine genuinely is off-box — on the
        // flagship loopback engine "engine is remote" would be factually wrong.
        const local = this.deps.engineIsLocal?.() ?? false;
        return {
          ok: true,
          result: local
            ? "restarted the voice session (deep reconnect)"
            : "engine is remote — performed deep reconnect",
        };
      }
      case "clear_cache": {
        await this.deps.clearCaches();
        void this.deps.reconnectEngine(this.deps.reconnectTimeoutMs ?? 10_000);
        return { ok: true, result: "cache cleared — reconnecting (settings and history kept)" };
      }
      case "restart_app": {
        // Capability-gated (platform_note): without an armed resurrection
        // service the exit would STRAND the user — nothing relaunches. Refuse
        // honestly rather than exit into a black hole (get_capabilities reports
        // this same false, but an agent may skip it).
        if (!this.deps.resurrectionArmed()) {
          return { ok: false, error: "unsupported", result: "keep-alive protection is off — restarting could strand the app; repair it first" };
        }
        // response_ordering: reply {ok:true,'restarting'}, FLUSH, act >=250 ms
        // later — never in-handler. The exit removes the heartbeat so the OS
        // service relaunches immediately (the ONE relaunch path).
        setTimeout(() => this.deps.restartApp(), 350);
        return { ok: true, result: "restarting" };
      }
      case "reset_to_defaults": {
        // The big red button: FACTORY (the immutable constant), settings-only.
        // SURVIVES: token, engine allow-list, resurrection registration (we
        // simply never touch them). Invalidates LKG; clears the safe-mode
        // flag; autonomy lands on the fresh-install cap via factory.
        this.deps.config.reset();
        this.deps.lkg.invalidateAll();
        this.deps.resetCrashCounter("reset_to_defaults");
        setTimeout(() => this.deps.restartApp(), 350); // response_ordering
        return { ok: true, result: "restarting" };
      }
      case "set_audio_input": {
        if (typeof args.device_id !== "string" || !args.device_id) {
          return { ok: false, error: "invalid device_id: expected a non-empty string from list_audio_devices" };
        }
        return this.setDial({ audio_input_id: args.device_id });
      }
      case "set_audio_output": {
        if (typeof args.device_id !== "string" || !args.device_id) {
          return { ok: false, error: "invalid device_id: expected a non-empty string from list_audio_devices" };
        }
        return this.setDial({ audio_output_id: args.device_id });
      }
      case "set_volume": {
        const level = Number(args.level);
        if (!Number.isInteger(level) || level < 0 || level > 100) {
          return { ok: false, error: "invalid level: must be an integer 0-100" };
        }
        return this.setDial({ volume: level });
      }
      case "set_engine_url": {
        const url = String(args.url ?? "");
        const verdict = this.deps.allowList.check(url);
        if (!verdict.allowed) {
          // The pinned template — NEVER 'denied' (that is tier refusal).
          return { ok: false, error: `untrusted host: ${verdict.host}`, result: verdict.reason };
        }
        return this.setDial({ engine_url: url }, () => {
          // A new engine only matters if we redial it (same-session re-dial).
          void this.deps.reconnectEngine(this.deps.reconnectTimeoutMs ?? 10_000);
        });
      }
      case "set_brain": {
        const brain = String(args.brain ?? "");
        if (brain !== "default" && !this.deps.entitledBrains().includes(brain)) {
          return { ok: false, error: `not entitled to brain: ${brain}` };
        }
        return this.setDial({ brain });
      }
      case "set_wake_mode":
        return this.setDial({ hands_free: args.hands_free === true });
      case "set_autonomy": {
        const level = Number(args.level);
        if (!Number.isInteger(level) || level < 0 || level > 10) {
          return { ok: false, error: "invalid level: must be an integer 0-10" };
        }
        return this.setDial({ autonomy: level });
      }
      case "apply_update":
        return this.applyUpdateTool(args);
      // account/billing (rev.8, ADR-060 §7): declared + served so a future
      // agent DISCOVERS them, but forced-honest 'unsupported' until Talk's
      // Windy-account layer (windy-connect pairing / Eternitas wi_-EPT-wk_
      // chain) is wired. get_capabilities reports these 'unsupported' too.
      case "get_account":
      case "get_billing_summary":
      case "open_account_portal":
      case "logout_account":
        return {
          ok: false,
          error: "unsupported",
          result:
            "account/billing isn't wired into Windy Talk yet — sign-in and " +
            "entitlement run through the shared Windy account (windy-connect " +
            "pairing). This knob is declared so it can be driven once that lands.",
        };
      default:
        return { ok: false, error: "unsupported" };
    }
  }

  private updateReady(): boolean {
    return updateConfigured() && !!this.deps.updateSource?.();
  }

  private async checkForUpdate(): Promise<Record<string, unknown>> {
    // Read-only: is a newer known-good build available? Installs nothing.
    // INERT (self_update.source) -> honest 'no update source configured'.
    const base = { update_available: false, current: this.deps.version, latest: null as string | null };
    if (!this.updateReady()) return { ...base, reason: "no update source configured" };
    try {
      const head = await this.deps.updateSource!()!.channelHead();
      if (!head) return { ...base, reason: "no update source configured" };
      const { compareSemver } = await import("./selfupdate.js");
      const newer = compareSemver(head, this.deps.version) > 0;
      return { update_available: newer, current: this.deps.version, latest: head, reason: null };
    } catch (e) {
      return { ...base, reason: `update check failed: ${scrubShortError(String(e))}` };
    }
  }

  private async applyUpdateTool(args: Record<string, unknown>): Promise<Envelope> {
    // The coordinator already holds apply_update's lock through the 90 s window.
    this.markUpdating(true);
    try {
      const res: ApplyResult = await applyUpdate({
        source: this.deps.updateSource?.() ?? null,
        currentVersion: this.deps.version,
        requestedVersion: typeof args.version === "string" ? args.version : undefined,
        freeBytes: this.deps.freeBytes ?? (() => Number.MAX_SAFE_INTEGER),
        stage: this.deps.stageUpdate ?? (async () => {}),
      });
      if (res.ok) {
        // response_ordering: reply 'restarting', flush, act >=250 ms later.
        setTimeout(() => this.deps.restartApp(), 350);
        return { ok: true, result: res.result };
      }
      this.markUpdating(false); // no staging happened: leave 'updating'
      return { ok: false, error: res.error, result: res.result };
    } catch (e) {
      this.markUpdating(false);
      return { ok: false, error: `update failed: ${scrubShortError(String(e))}` };
    }
  }

  /** Layer 1's crash-loop trip: through the lock, exempt from rate limits. */
  async layer1TripSafeMode(): Promise<Envelope> {
    return this.dispatch("enter_safe_mode", {}, { layer1: true });
  }

  private updatingFlag = false;
  markUpdating(on: boolean): void {
    this.updatingFlag = on;
  }

  mode(): "normal" | "safe" | "recovering" | "updating" {
    if (this.updatingFlag) return "updating"; // staging in flight (highest priority)
    if (this.deps.coordinator.recovering) return "recovering";
    if (this.deps.config.inSafeMode) return "safe";
    return "normal";
  }

  /** The get_health snapshot — exactly the pinned `returns` shape. */
  health(): Record<string, unknown> {
    const d = this.deps;
    const s = d.rendererStatus();
    const nowMs = this.now();
    const mode = this.mode();
    const connected = s.connection === "online";
    const active = d.config.getActive();

    const engine = {
      connected,
      url: d.allowList.scrubForDiagnostics(active.engine_url),
      last_frame_s_ago: s.lastFrameAtMs == null ? null : Math.max(0, (nowMs - s.lastFrameAtMs) / 1000),
    };
    // Honest heuristic until run_selftest's active probe lands (slice 2): the
    // engine mediates the brain, so brain reachability tracks the session.
    const brain = { reachable: connected, model: null as string | null };
    const mic = { present: s.micError == null, device: null as string | null, capturing: s.micOn };
    const speaker = { device: null as string | null };
    const crashLoop = d.detector.crashLoop;
    const healthy = connected && brain.reachable && mic.present && !crashLoop && mode === "normal";

    const { summary, suggestedFix } = this.summarize({ connected, crashLoop, mode, micPresent: mic.present, healthy });
    return {
      healthy,
      mode,
      engine,
      brain,
      mic,
      speaker,
      uptime_s: Math.max(0, (nowMs - d.startedAtMs) / 1000),
      restarts: d.detector.restarts,
      crash_loop: crashLoop,
      resurrection_armed: d.resurrectionArmed(),
      last_error: scrubShortError(s.lastError),
      version: d.version,
      summary,
      suggested_fix: suggestedFix,
    };
  }

  /**
   * One config-dial write (contract safe_mode.config_writes_in_safe_mode):
   * always writes the UNDERLYING config; in safe mode the overlay stays
   * factory and the pinned result string says so; otherwise the active
   * config is pushed to the renderer (and afterApply runs).
   */
  private setDial(patch: Record<string, unknown>, afterApply?: () => void): Envelope {
    this.deps.config.setSaved(patch);
    if (this.deps.config.inSafeMode) {
      return { ok: true, result: "saved — will apply when you leave safe mode" };
    }
    this.deps.applyActiveConfig();
    afterApply?.();
    return { ok: true, result: "saved" };
  }

  /**
   * run_selftest (pinned): actively probe the chain. Per-stage timeout 5 s,
   * whole tool bounded at 20 s; a timed-out stage is pass:false detail:'timeout'.
   * The mic/speaker stages run IN the renderer (only it owns the audio graph);
   * engine reachability is judged on live status + frame recency.
   */
  private async selftest(): Promise<Record<string, unknown>> {
    const stageMs = this.deps.selftestStageTimeoutMs ?? 5_000;
    const s = this.deps.rendererStatus();
    const enginePass = s.connection === "online";
    const engine = {
      pass: enginePass,
      detail: enginePass ? "session up" : `connection is ${s.connection}`,
    };
    // The engine mediates the brain: with a live session the brain answered
    // hello; without one it is unreachable by construction.
    const brain = {
      pass: enginePass,
      detail: enginePass ? "reachable via engine session" : "unreachable (engine session down)",
    };
    // Renderer-owned stages; the probe's own ceiling covers both (2 stages).
    const probed = (await this.deps.probe("selftest", stageMs * 2)) as {
      mic?: { pass: boolean; detail: string };
      speaker?: { pass: boolean; detail: string };
    } | null;
    const timeout = { pass: false, detail: "timeout" };
    return {
      stages: {
        engine,
        brain,
        mic: probed?.mic ?? timeout,
        speaker: probed?.speaker ?? timeout,
      },
    };
  }

  /**
   * get_capabilities (pinned): tri-state per tool for THIS box. An unbuilt
   * slice reads FALSE (it genuinely cannot run here yet — forced-honest;
   * slices 3-5 flip these as they land). restart_engine will report
   * 'degraded' on remote-engine boxes once built.
   */
  private capabilities(): Record<string, unknown> {
    const built = new Set(this.builtTools());
    const all = [
      "get_health", "get_status", "get_config", "get_logs", "list_audio_devices",
      "run_selftest", "get_capabilities", "check_for_update",
      "reconnect", "enter_safe_mode", "exit_safe_mode", "repair_resurrection",
      "restart_engine", "restart_app", "clear_cache", "reset_to_defaults", "apply_update",
      "set_audio_input", "set_audio_output", "set_volume", "set_engine_url",
      "set_brain", "set_wake_mode", "set_autonomy",
      "get_account", "get_billing_summary", "open_account_portal", "logout_account",
    ];
    const ACCOUNT_UNWIRED = new Set([
      "get_account", "get_billing_summary", "open_account_portal", "logout_account",
    ]);
    const tools: Record<string, boolean | string> = {};
    for (const t of all) {
      if (!built.has(t)) {
        tools[t] = false;
        continue;
      }
      if (ACCOUNT_UNWIRED.has(t)) {
        // Declared + served, but the Windy-account layer isn't wired yet:
        // honest tri-state 'unsupported' (ADR-060 §7), not a false 'true'.
        tools[t] = "unsupported";
        continue;
      }
      if (t === "restart_engine") {
        // The desktop client never owns a child engine process, so the true
        // path is the pinned DEGRADED one (deep reconnect) everywhere.
        tools[t] = "degraded";
        continue;
      }
      if (t === "restart_app") {
        // Without an armed resurrection service the exit would strand her.
        tools[t] = this.deps.resurrectionArmed();
        continue;
      }
      if (t === "repair_resurrection") {
        // FALSE where re-arming genuinely can't work here (privilege-blocked),
        // per platform_note — not unconditionally true. The tool still returns
        // an honest 'unsupported' + manual step at invoke time.
        tools[t] = this.deps.resurrectionRepairable ? this.deps.resurrectionRepairable() : true;
        continue;
      }
      tools[t] = true;
    }
    return { os: process.platform, tools };
  }

  private summarize(x: {
    connected: boolean;
    crashLoop: boolean;
    mode: string;
    micPresent: boolean;
    healthy: boolean;
  }): { summary: string; suggestedFix: string | null } {
    // suggested_fix: ALWAYS the least-destructive plausible tool, and one that
    // is usable here (the built set, until get_capabilities lands) — else null.
    const usable = new Set(this.builtTools());
    if (x.crashLoop && x.mode === "safe") {
      return {
        summary:
          "It kept crashing, so I switched to safe mode to keep things stable. A reset can clear a bad setting if it keeps happening.",
        suggestedFix: null,
      };
    }
    if (!x.connected) {
      return {
        summary: "The connection to your voice engine is down — reconnecting usually fixes it.",
        suggestedFix: usable.has("reconnect") ? "reconnect" : null,
      };
    }
    if (x.crashLoop) {
      return {
        summary: "It has been restarting a lot. Switching to safe mode will stop the churn.",
        suggestedFix: usable.has("enter_safe_mode") ? "enter_safe_mode" : null,
      };
    }
    if (!x.micPresent) {
      return {
        summary: "I can't find a working microphone, so it can't hear you.",
        suggestedFix: null, // set_audio_input needs a device id; slice 2's list_audio_devices guides it
      };
    }
    if (x.mode === "safe") {
      return { summary: "Running in safe mode — the reliable floor. Everything else looks fine.", suggestedFix: null };
    }
    return { summary: "Everything looks healthy.", suggestedFix: null };
  }

  private isContractTool(tool: string): boolean {
    return (
      EXEMPT_READS.has(tool) ||
      [
        "run_selftest",
        "reconnect",
        "enter_safe_mode",
        "exit_safe_mode",
        "repair_resurrection",
        "restart_engine",
        "restart_app",
        "clear_cache",
        "set_audio_input",
        "set_audio_output",
        "set_volume",
        "set_engine_url",
        "set_brain",
        "set_wake_mode",
        "set_autonomy",
        "reset_to_defaults",
        "apply_update",
        // account/billing actions (rev.8); the two reads are in EXEMPT_READS.
        "open_account_portal",
        "logout_account",
      ].includes(tool)
    );
  }
}
