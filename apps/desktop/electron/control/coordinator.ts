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
/**
 * Pre-commit hold ceiling: a lock acquired at gate() but not yet committed is
 * waiting on the tier confirmer. The confirmer bounds itself (the native dialog
 * denies after 60 s), so this only backstops a confirmer that never settles —
 * 75 s comfortably clears a real 60 s confirm while still freeing a truly-hung
 * one. The HANDLER's own ceiling (30 s / 90 s) starts fresh at commit(), so a
 * slow confirm can never let the lock self-release mid-prompt and admit a
 * concurrent recovery (the "one recovery at a time" invariant).
 */
export const LOCK_CONFIRM_HOLD_MS = 75_000;

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
  /** When the CURRENT phase started: gate time until commit, then commit time. */
  since: number;
  epoch: number;
  /** The handler's ceiling (30 s, or 90 s for apply_update); applies post-commit. */
  ceilingMs: number;
  /** False while the tier confirmer is pending; true once the handler runs. */
  committed: boolean;
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

    // 2) DEBOUNCE + 3) CEILING — measured on EXECUTED calls with the same
    //    tool+args key. RESERVE-then-REFUND: the counters are charged HERE, at
    //    gate time, so a second same-tool call that arrives while the first is
    //    still awaiting its confirmer already sees the reservation (a deferred
    //    charge let two overlapping calls both slip past debounce/ceiling). The
    //    reservation is REFUNDED if the call never executes (denied / released
    //    without commit) — so a rejected call still never counts, as pinned.
    let refund: (() => void) | null = null;
    if (!opts.layer1) {
      const key = this.key(tool, args);
      const last = this.lastByKey.get(key);
      if (last !== undefined && nowMs - last < DEBOUNCE_MS) {
        return { proceed: false, error: "rate_limited", reason: "debounced (5 s same-call window)" };
      }

      const times = (this.executed.get(tool) ?? []).filter((t) => nowMs - t <= CEILING_WINDOW_MS);
      if (times.length >= CEILING_CALLS) {
        this.executed.set(tool, times);
        return { proceed: false, error: "rate_limited", reason: "over the 5-per-300s ceiling" };
      }

      // Reserve immediately (visible to any concurrent gate for this tool/key).
      times.push(nowMs);
      this.executed.set(tool, times);
      const prevLast = this.lastByKey.get(key);
      this.lastByKey.set(key, nowMs);

      refund = () => {
        const arr = this.executed.get(tool);
        if (arr) {
          const i = arr.indexOf(nowMs);
          if (i >= 0) arr.splice(i, 1);
        }
        // Restore the debounce marker only if no later call has overwritten it
        // (else that call's reservation stands and must not be clobbered).
        if (this.lastByKey.get(key) === nowMs) {
          if (prevLast === undefined) this.lastByKey.delete(key);
          else this.lastByKey.set(key, prevLast);
        }
      };
    }

    return this.grant(tool, isHolder ? nowMs : 0, refund, preemptEpoch);
  }

  private grant(
    tool: string,
    lockAt: number,
    refund: (() => void) | null = null,
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
        since: lockAt, // gate time; the confirm-hold ceiling applies until commit
        epoch,
        ceilingMs: tool === "apply_update" ? LOCK_CEILING_UPDATE_MS : LOCK_CEILING_MS,
        committed: false,
      };
    }
    const coordinator = this;
    let committed = false;
    let released = false;
    const ticket: Ticket = {
      tool,
      epoch,
      get abandoned(): boolean {
        return epoch !== 0 && coordinator.abandonedEpochs.has(epoch);
      },
      commit(): void {
        if (committed) return;
        committed = true;
        // The reservation charged at gate is now PERMANENT (the call executes).
        // The handler starts NOW (the confirmer, if any, has resolved): reset the
        // ceiling clock so a slow confirm never ate into the handler's window,
        // and switch the lock to its post-commit ceiling.
        if (epoch !== 0 && coordinator.lock?.epoch === epoch) {
          coordinator.lock.since = coordinator.now();
          coordinator.lock.committed = true;
        }
      },
      release(): void {
        if (released) return;
        released = true;
        // Refund the reservation unless the call genuinely executed to a kept
        // result — i.e. refund when it never committed (denied / errored
        // pre-commit) OR when it was preempted (abandoned -> returns
        // already_recovering, a rejected outcome that must not count). So a
        // ticket counts toward debounce/ceiling only if it committed AND was
        // not abandoned. Applies to lock-less set_*/run_selftest tickets too
        // (epoch 0 but still reserved).
        const wasAbandoned = epoch !== 0 && coordinator.abandonedEpochs.has(epoch);
        if (!committed || wasAbandoned) refund?.();
        if (epoch !== 0) {
          coordinator.abandonedEpochs.delete(epoch);
          if (coordinator.lock?.epoch === epoch) coordinator.lock = null;
        }
      },
    };
    return { proceed: true, ticket };
  }

  /**
   * The lock self-releases at its hard ceiling — no permanent deadlock. Before
   * commit the effective ceiling is the confirm-hold bound (a pending confirmer
   * must not let the lock lapse and admit a concurrent recovery); after commit
   * it is the handler's own 30 s / 90 s ceiling, measured from commit time.
   */
  private liveLock(): LockState | null {
    if (this.lock) {
      const ceiling = this.lock.committed ? this.lock.ceilingMs : LOCK_CONFIRM_HOLD_MS;
      if (this.now() - this.lock.since >= ceiling) this.lock = null;
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
