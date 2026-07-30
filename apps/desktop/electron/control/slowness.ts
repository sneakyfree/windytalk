// Layer 1's second sense: DEGRADATION, not just death.
//
// The crash-loop detector next door counts restarts — a process that came up and
// died. Every failure that actually reached Grant in the 2026-07 hand tests did
// neither:
//
//   * the engine got ~3x slower over ~13 hours (13.2s per turn vs 4.5s fresh) and
//     never crashed once. He asked seven questions over four and a half minutes and
//     heard nothing back.
//   * Mind silently substituted a 7B local model for Opus. Never crashed.
//   * the vision model kept getting evicted, turning a 13s click into 90s. Never
//     crashed.
//
// A supervisor that only watches for corpses will sit there while the thing rots.
// This watches the one number the user actually feels — how long a turn takes —
// and asks for a recovery when it drifts away from what this install has proven it
// can do.
//
// Deliberately dumb (P5): medians, no statistics library, no model. It compares a
// recent window against a baseline the install earned, and it is honest about not
// knowing yet.

/** Turns observed before a baseline is trusted. Below this we do nothing. */
export const BASELINE_MIN_SAMPLES = 5;
/** Recent turns compared against the baseline. */
export const RECENT_WINDOW = 3;
/** Recent median must exceed baseline by this factor to count as degraded.
 *  2.5, not 3. The real event this exists to catch measured 13.2s against a 4.5s
 *  baseline — 2.93x — so a factor of 3 would have sat through the exact failure it
 *  was written for. The test caught it; the number comes from the measurement. */
export const SLOW_FACTOR = 2.5;
/** ...and by this absolute floor, so a fast baseline can't trip on noise. */
export const SLOW_FLOOR_MS = 12_000;
/** Never ask for recovery more often than this. */
export const COOLDOWN_MS = 300_000;
/** Samples retained; the baseline is the median of the healthy early ones. */
const KEEP = 50;

export interface SlownessDeps {
  now?: () => number;
  /** Ask the supervisor for a recovery (deep-reconnect). Must be idempotent. */
  requestRecovery: (reason: string) => void;
  log?: (msg: string) => void;
}

function median(xs: number[]): number {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

export class SlownessDetector {
  private samples: number[] = [];
  private baseline = 0;
  // -Infinity, not 0: with 0 the cooldown check swallows the very FIRST recovery
  // request whenever the clock is near zero. Real Date.now() is large enough to
  // mask it, so this would have been a bug that only ever appeared under an
  // injected clock — i.e. invisible in production and wrong the whole time.
  private lastRecoveryAt = Number.NEGATIVE_INFINITY;
  private degraded = false;

  constructor(private deps: SlownessDeps) {}

  private now(): number {
    return this.deps.now ? this.deps.now() : Date.now();
  }

  /** get_health.degraded — true once we've asked for a recovery and not recovered. */
  isDegraded(): boolean {
    return this.degraded;
  }

  /** The install's earned normal, in ms. 0 until enough turns have been seen. */
  baselineMs(): number {
    return this.baseline;
  }

  /** One completed turn: user stopped speaking -> she started speaking. */
  noteTurn(latencyMs: number): void {
    if (!Number.isFinite(latencyMs) || latencyMs <= 0) return;
    this.samples.push(latencyMs);
    if (this.samples.length > KEEP) this.samples.shift();

    if (this.samples.length < BASELINE_MIN_SAMPLES) return;
    if (!this.baseline) {
      // The baseline is what this machine proved it could do on its first healthy
      // turns — not a number hardcoded by someone with a different CPU, GPU, brain
      // and room. A 5090 and a laptop should each measure themselves.
      this.baseline = median(this.samples.slice(0, BASELINE_MIN_SAMPLES));
      this.deps.log?.(`slowness: baseline ${Math.round(this.baseline)}ms`);
      return;
    }

    const recent = median(this.samples.slice(-RECENT_WINDOW));
    const slow = recent > this.baseline * SLOW_FACTOR && recent > SLOW_FLOOR_MS;
    if (!slow) {
      // Sustained health clears the flag; recovery is judged by turns, not a timer.
      if (this.degraded && recent <= this.baseline * SLOW_FACTOR) {
        this.degraded = false;
        this.deps.log?.("slowness: back to normal");
      }
      return;
    }

    const t = this.now();
    if (t - this.lastRecoveryAt < COOLDOWN_MS) return;  // already asked recently
    this.lastRecoveryAt = t;
    this.degraded = true;
    const reason =
      `turns are ${(recent / this.baseline).toFixed(1)}x slower than normal ` +
      `(${Math.round(recent)}ms vs ${Math.round(this.baseline)}ms baseline)`;
    this.deps.log?.(`slowness: ${reason} — requesting recovery`);
    this.deps.requestRecovery(reason);
  }

  /** After a recovery: forget the degraded run, keep the earned baseline. */
  resetAfterRecovery(): void {
    this.samples = [];
    this.degraded = false;
  }
}
