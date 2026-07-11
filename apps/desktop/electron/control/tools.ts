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
import { scrubShortError } from "./scrub.js";

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
  version: string;
  startedAtMs: number;
  emit: Emitter;
  now?: () => number;
  /** reconnect's pinned block ceiling (10 s prod; injectable for tests). */
  reconnectTimeoutMs?: number;
}

const MUTATING = new Set(["reconnect", "enter_safe_mode"]);

export class ControlTools {
  private readonly deps: ToolDeps;
  private readonly now: () => number;

  constructor(deps: ToolDeps) {
    this.deps = deps;
    this.now = deps.now ?? Date.now;
  }

  /** Built tools only — what /tools and MCP tools/list advertise. */
  builtTools(): string[] {
    return ["get_health", "reconnect", "enter_safe_mode"];
  }

  async dispatch(tool: string, args: Record<string, unknown> = {}, opts: { layer1?: boolean } = {}): Promise<Envelope> {
    if (!this.isContractTool(tool)) return { ok: false, error: `unknown tool: ${tool}` };
    if (!this.builtTools().includes(tool)) {
      return { ok: false, error: "unsupported", result: "not built yet (control.mcp.v1 build in progress)" };
    }
    const gate = this.deps.coordinator.gate(tool, args, opts);
    if (!gate.proceed) return { ok: false, error: gate.error, result: gate.reason };
    // (tier confirmer would run HERE — after the coordinator, per check_order.
    //  Slice-1 tools are auto_allow; slice 4 wires tier_resolution.)
    let res: Envelope;
    try {
      res = await this.execute(tool);
    } catch (e) {
      res = { ok: false, error: `${(e as Error)?.name ?? "Error"}: ${scrubShortError(String((e as Error)?.message ?? e))}` };
    } finally {
      gate.ticket.release();
    }
    if (gate.ticket.abandoned) {
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
    return res;
  }

  private async execute(tool: string): Promise<Envelope> {
    switch (tool) {
      case "get_health":
        return { ok: true, result: this.health() };
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
      default:
        return { ok: false, error: "unsupported" };
    }
  }

  /** Layer 1's crash-loop trip: through the lock, exempt from rate limits. */
  async layer1TripSafeMode(): Promise<Envelope> {
    return this.dispatch("enter_safe_mode", {}, { layer1: true });
  }

  mode(): "normal" | "safe" | "recovering" | "updating" {
    // slice 5 adds 'updating'. A recovery in flight is the most informative
    // transient state; safe mode is the salient resting state.
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
      ].includes(tool)
    );
  }
}
