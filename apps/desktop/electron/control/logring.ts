// get_logs' backing store: a bounded in-memory ring of technical events
// (connections, restarts, errors). Content-free + scrubbed AT APPEND TIME — a
// message is scrubbed before it is stored, so nothing unscrubbed can ever be
// read back out. Newest LAST, per the pinned returns.
import { scrubShortError } from "./scrub.js";

export interface LogLine {
  ts: string; // ISO 8601
  level: "info" | "warn" | "error";
  msg: string; // scrubbed
}

const MAX_LINES = 500; // matches get_logs.inputSchema.lines.maximum

export class LogRing {
  private lines: LogLine[] = [];
  private readonly now: () => number;

  constructor(opts: { now?: () => number } = {}) {
    this.now = opts.now ?? Date.now;
  }

  append(level: LogLine["level"], msg: string): void {
    this.lines.push({
      ts: new Date(this.now()).toISOString().replace(/\.\d{3}Z$/, "Z"),
      level,
      msg: scrubShortError(msg) ?? "",
    });
    if (this.lines.length > MAX_LINES) this.lines.splice(0, this.lines.length - MAX_LINES);
  }

  /** Newest LAST; truncated=true when older lines were dropped by the request. */
  tail(count: number): { lines: LogLine[]; truncated: boolean } {
    const n = Math.max(1, Math.min(MAX_LINES, count));
    return {
      lines: this.lines.slice(-n),
      truncated: this.lines.length > n,
    };
  }
}
