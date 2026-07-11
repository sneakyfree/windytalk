// The recovery coordinator (contract `recovery_coordinator`) — ONE state machine
// owns all recovery so the healers (Layer 1, resident agent, external agent)
// can't fight into a thrash loop. Every number here is NORMATIVE in the contract.
//
// Gate order (recovery_coordinator.lock.check_order): lock check, then debounce,
// then rate-limit — all BEFORE the tier confirmer, so a blocked call returns
// already_recovering/rate_limited immediately and never pointlessly prompts.
//
// Layer-1 exemption: ALL of Layer 1's own autonomic actions (its reconnect AND
// its crash-loop safe-mode trip) go through the LOCK but are NEVER charged the
// rate limit or debounce — the escape hatch must not be rate_limited mid-thrash.

export const LOCK_HOLDERS = new Set([
  "reconnect",
  "enter_safe_mode",
  "exit_safe_mode",
  "restart_engine",
  "restart_app",
  "clear_cache",
  "reset_to_defaults",
  "apply_update",
]);

/** Exempt from the lock AND the rate limit — enumerated BY NAME, not "get_*". */
export const EXEMPT_READS = new Set([
  "get_health",
  "get_status",
  "get_config",
  "get_logs",
  "list_audio_devices",
  "get_capabilities",
  "check_for_update",
]);

/** set_* config dials: no lock taken, but blocked while a recovery is in flight. */
export const CONFIG_TOOLS = new Set([
  "set_audio_input",
  "set_audio_output",
  "set_volume",
  "set_engine_url",
  "set_brain",
  "set_wake_mode",
  "set_autonomy",
]);

/** Lock-exempt (or not lock-blocked) but still debounced/rate-limited. */
const LOCK_EXEMPT_RATE_LIMITED = new Set(["run_selftest", "repair_resurrection"]);

export const DEBOUNCE_MS = 5_000;
export const CEILING_CALLS = 5;
export const CEILING_WINDOW_MS = 300_000;
/** Lock auto-release ceiling — no permanent deadlock. */
export const LOCK_CEILING_MS = 30_000;
/** apply_update holds through its staging + verification window (slice 5). */
export const LOCK_CEILING_UPDATE_MS = 90_000;

export type GateOutcome =
  | { proceed: true; ticket: Ticket }
  | { proceed: false; error: "already_recovering" | "rate_limited"; reason: string };

export interface Ticket {
  tool: string;
  /** Lock epoch at grant time; 0 when the tool holds no lock. */
  epoch: number;
  /** True once enter_safe_mode preempted this ticket's lock. */
  readonly abandoned: boolean;
  /**
   * Charge the executed debounce/ceiling counters. Called AFTER the tier
   * confirmer allows and BEFORE the handler runs — a call the user DENIES was
   * never executed and must not charge (contract: rejected calls do not
   * increment the executed counter). Layer-1/exempt tickets no-op.
   */
  commit(): void;
  /** Release the lock (no-op if not a holder or already preempted). */
  release(): void;
}

interface LockState {
  tool: string;
  since: number;
  epoch: number;
  ceilingMs: number;
}

export class RecoveryCoordinator {
  private lock: LockState | null = null;
  private epochCounter = 0;
  private abandonedEpochs = new Set<number>();
  /** Executed-call timestamps per tool (the 5/300 s ceiling). */
  private executed = new Map<string, number[]>();
  /** Last EXECUTED time per tool+canonical-args key (the 5 s debounce). */
  private lastByKey = new Map<string, number>();
  private now: () => number;

  constructor(opts: { now?: () => number } = {}) {
    this.now = opts.now ?? Date.now;
  }

  /** Is a recovery in flight right now (drives get_health.mode='recovering')? */
  get recovering(): boolean {
    return this.liveLock() !== null;
  }

  get lockHolderTool(): string | null {
    return this.liveLock()?.tool ?? null;
  }

  /**
   * The coordinator's gate. `layer1: true` marks Layer 1's own autonomic
   * actions (lock yes; debounce/rate-limit never).
   */
  gate(tool: string, args: Record<string, unknown> = {}, opts: { layer1?: boolean } = {}): GateOutcome {
    // Exempt reads: no lock, no debounce, no ceiling.
    if (EXEMPT_READS.has(tool)) return this.grant(tool, 0);

    const nowMs = this.now();
    const lock = this.liveLock();
    const isHolder = LOCK_HOLDERS.has(tool);

    // 1) LOCK. enter_safe_mode may PREEMPT a held lock (the escape hatch for a
    //    stuck state): the preempted handler is abandoned, its result discarded.
    //    The reclaim is DEFERRED to grant() so a preempt that is then
    //    rate-limited/debounced does NOT clear the lock (which would abandon the
    //    holder AND free the lock for a concurrent recovery without ever
    //    entering safe mode).
    let preemptEpoch: number | null = null;
    if (lock) {
      if (tool === "enter_safe_mode") {
        preemptEpoch = lock.epoch;
      } else if (isHolder || CONFIG_TOOLS.has(tool)) {
        // Holders fail fast (never queue, never run concurrently); config set_*
        // tools are blocked too (a recovery would overwrite their change).
        return {
          proceed: false,
          error: "already_recovering",
          reason: `${lock.tool} is in flight`,
        };
      }
      // run_selftest / repair_resurrection fall through: not blocked by the lock.
    }

    // 2) DEBOUNCE — measured on EXECUTED calls with the same tool+args key.
    let charge: (() => void) | null = null;
    if (!opts.layer1) {
      const key = this.key(tool, args);
      const last = this.lastByKey.get(key);
      if (last !== undefined && nowMs - last < DEBOUNCE_MS) {
        return { proceed: false, error: "rate_limited", reason: "debounced (5 s same-call window)" };
      }

      // 3) CEILING — 5 executed calls per tool per rolling 300 s.
      const times = (this.executed.get(tool) ?? []).filter((t) => nowMs - t <= CEILING_WINDOW_MS);
      this.executed.set(tool, times);
      if (times.length >= CEILING_CALLS) {
        return { proceed: false, error: "rate_limited", reason: "over the 5-per-300s ceiling" };
      }

      // The counters charge at ticket.commit() — after the tier confirmer —
      // so a user-DENIED call never counts as executed (as pinned).
      charge = () => {
        times.push(this.now());
        this.lastByKey.set(key, this.now());
      };
    }

    return this.grant(tool, isHolder ? nowMs : 0, charge, preemptEpoch);
  }

  private grant(
    tool: string,
    lockAt: number,
    charge: (() => void) | null = null,
    preemptEpoch: number | null = null,
  ): GateOutcome {
    // Reclaim only now that the call has passed every gate check (lock +
    // debounce + ceiling): a rejected preempt above never reaches here, so it
    // leaves the held lock — and its holder — untouched.
    if (preemptEpoch !== null) {
      this.abandonedEpochs.add(preemptEpoch);
      if (this.lock?.epoch === preemptEpoch) this.lock = null;
    }
    let epoch = 0;
    if (lockAt > 0) {
      epoch = ++this.epochCounter;
      this.lock = {
        tool,
        since: lockAt,
        epoch,
        ceilingMs: tool === "apply_update" ? LOCK_CEILING_UPDATE_MS : LOCK_CEILING_MS,
      };
    }
    const coordinator = this;
    let charged = false;
    const ticket: Ticket = {
      tool,
      epoch,
      get abandoned(): boolean {
        return epoch !== 0 && coordinator.abandonedEpochs.has(epoch);
      },
      commit(): void {
        if (!charged) {
          charged = true;
          charge?.();
        }
      },
      release(): void {
        if (epoch === 0) return;
        coordinator.abandonedEpochs.delete(epoch); // done with this epoch either way
        if (coordinator.lock?.epoch === epoch) coordinator.lock = null;
      },
    };
    return { proceed: true, ticket };
  }

  /** The lock self-releases at its hard ceiling — no permanent deadlock. */
  private liveLock(): LockState | null {
    if (this.lock && this.now() - this.lock.since >= this.lock.ceilingMs) {
      this.lock = null;
    }
    return this.lock;
  }

  private key(tool: string, args: Record<string, unknown>): string {
    // Canonicalize args so set_audio_input(A) vs (B) are DIFFERENT keys (the
    // legit "try the other mic" flow) while key order can't split a key.
    const sorted = Object.keys(args)
      .sort()
      .map((k) => `${k}=${JSON.stringify(args[k])}`)
      .join(",");
    return `${tool}(${sorted})`;
  }
}

export { LOCK_EXEMPT_RATE_LIMITED };
