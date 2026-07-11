// Layer 1 — the autonomic supervisor's crash-loop detector (contract
// `crash_loop`; design "the invisible ~95%"). Not an agent: dumb, reliable code.
//
// A "restart" is a connection/process that CAME UP and then DIED (engine session
// dropped after being online; renderer reloaded after a hang) — not a failed
// retry during an outage (an unreachable engine is an outage Layer 1's slow
// unbounded reconnect arm rides out; tripping safe mode there would fix nothing).
//
// Trip: >=3 restarts within 120 s -> enter safe mode (through the coordinator
// LOCK with the layer1 exemption — never rate-limited, may preempt a stuck
// holder — the exact failure the escape hatch exists to stop).
// Reset: 300 s of continuous healthy uptime, or reset_to_defaults /
// exit_safe_mode (those tools call resetCounter directly in slice 3).

export const TRIP_RESTARTS = 3;
export const TRIP_WINDOW_MS = 120_000;
export const RESET_HEALTHY_MS = 300_000;

export interface Layer1Deps {
  now?: () => number;
  /** Fires the safe-mode trip (goes through the coordinator as layer1). */
  tripSafeMode: (reason: string) => void;
  log?: (msg: string) => void;
}

export class CrashLoopDetector {
  private restartTimes: number[] = [];
  private restartsTotal = 0;
  private tripped = false;
  private healthySince: number | null = null;
  private readonly now: () => number;
  private readonly deps: Layer1Deps;

  constructor(deps: Layer1Deps) {
    this.deps = deps;
    this.now = deps.now ?? Date.now;
  }

  /** get_health.restarts — restarts since supervisor start. */
  get restarts(): number {
    return this.restartsTotal;
  }

  /** get_health.crash_loop — set at trip, clears when the counter resets. */
  get crashLoop(): boolean {
    return this.tripped;
  }

  /** An up-then-died event (engine session drop, renderer reload). */
  recordRestart(what: string): void {
    const nowMs = this.now();
    this.restartsTotal++;
    this.healthySince = null;
    this.restartTimes = this.restartTimes.filter((t) => nowMs - t <= TRIP_WINDOW_MS);
    this.restartTimes.push(nowMs);
    this.deps.log?.(`layer1: restart recorded (${what}); ${this.restartTimes.length} in window`);
    if (!this.tripped && this.restartTimes.length >= TRIP_RESTARTS) {
      this.tripped = true;
      this.deps.log?.("layer1: crash loop detected — tripping safe mode (stop thrashing)");
      this.deps.tripSafeMode(`${this.restartTimes.length} restarts in 120 s`);
    }
  }

  /** Call periodically (or on status updates) with current health. */
  observeHealthy(healthy: boolean): void {
    const nowMs = this.now();
    if (!healthy) {
      this.healthySince = null;
      return;
    }
    if (this.healthySince === null) this.healthySince = nowMs;
    if (nowMs - this.healthySince >= RESET_HEALTHY_MS) this.resetCounter("continuous healthy uptime");
  }

  /** Also invoked by reset_to_defaults / exit_safe_mode (slice 3). */
  resetCounter(why: string): void {
    if (this.restartTimes.length || this.tripped) {
      this.deps.log?.(`layer1: crash-loop counter reset (${why})`);
    }
    this.restartTimes = [];
    this.tripped = false;
  }
}
