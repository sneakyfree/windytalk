// The supervisor — Layer 1's home in the Electron main process. It owns the
// renderer<->main status bus, feeds the crash-loop detector, and implements the
// engine-facing actions the tools need (reconnect, apply-config). Transport is
// injected so the whole thing unit-tests without Electron.
//
// Layer 1's reconnect arm: the renderer keeps its own fast auto-retry (now with
// exponential backoff + jitter, capped, never giving up — the "unbounded
// slow-retry" the contract pins). The supervisor doesn't duplicate that loop;
// it supervises OUTCOMES: an engine session that comes up and then dies is a
// RESTART (crash-loop food); an engine that stays unreachable is an outage the
// renderer arm rides out.
import { OFFLINE_STATUS, type RendererStatus } from "./tools.js";
import type { CrashLoopDetector } from "./layer1.js";

export type RendererCommand =
  | { type: "reconnect" }
  | { type: "deep-reconnect" }
  | {
      type: "apply-config";
      hands_free: boolean;
      volume: number;
      audio_input_id: string | null;
      audio_output_id: string | null;
    }
  | { type: "notice"; text: string }
  | { type: "probe"; probe: "audio-devices" | "selftest"; reqId: number };

export interface SupervisorOpts {
  detector: CrashLoopDetector;
  sendCommand: (cmd: RendererCommand) => void;
  now?: () => number;
  log?: (msg: string) => void;
}

export class Supervisor {
  private status: RendererStatus = OFFLINE_STATUS;
  private listeners: ((s: RendererStatus) => void)[] = [];
  private pendingProbes = new Map<number, (result: unknown) => void>();
  private probeSeq = 0;
  private readonly opts: SupervisorOpts;

  constructor(opts: SupervisorOpts) {
    this.opts = opts;
  }

  /**
   * Ask the RENDERER for something only it can see (audio devices, selftest
   * stages). Resolves null on timeout / renderer down — callers report honest
   * failure, never fake data.
   */
  probe(kind: "audio-devices" | "selftest", timeoutMs: number): Promise<unknown | null> {
    const reqId = ++this.probeSeq;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pendingProbes.delete(reqId);
        resolve(null);
      }, timeoutMs);
      this.pendingProbes.set(reqId, (result) => {
        clearTimeout(timer);
        this.pendingProbes.delete(reqId);
        resolve(result);
      });
      this.opts.sendCommand({ type: "probe", probe: kind, reqId });
    });
  }

  /** Wire-in from the renderer's probe replies (IPC). */
  onProbeResult(reqId: number, result: unknown): void {
    this.pendingProbes.get(reqId)?.(result);
  }

  rendererStatus(): RendererStatus {
    return this.status;
  }

  /** Wire-in from the renderer's status pushes (IPC). */
  onRendererStatus(s: RendererStatus): void {
    const prev = this.status;
    this.status = s;
    if (prev.connection === "online" && s.connection !== "online") {
      // Came up, then died: a restart event (never counted for mere retries).
      this.opts.detector.recordRestart("engine session dropped");
    }
    this.opts.detector.observeHealthy(s.connection === "online");
    for (const cb of this.listeners) cb(s);
  }

  /** Renderer process hung/crashed: Layer 1 reloads it (autonomic) — a restart. */
  onRendererGone(what: string, reload: () => void): void {
    this.opts.log?.(`layer1: renderer ${what} — reloading`);
    this.opts.detector.recordRestart(`renderer ${what}`);
    this.status = OFFLINE_STATUS;
    try {
      reload();
    } catch {
      // window already destroyed; the resurrection watcher covers a dead main
    }
  }

  /**
   * restart_engine's degraded path: drop session state, close the socket, new
   * voice-session (distinct from reconnect, which re-dials the SAME session).
   */
  deepReconnectEngine(timeoutMs: number): Promise<boolean> {
    this.opts.sendCommand({ type: "deep-reconnect" });
    return this.awaitOnline(timeoutMs);
  }

  /** The reconnect tool's engine action: command a re-dial, await online. */
  reconnectEngine(timeoutMs: number): Promise<boolean> {
    if (this.status.connection === "online") {
      // Same-session re-dial: still issue the command (a half-dead socket can
      // read as online until the next liveness check), then await the outcome.
    }
    this.opts.sendCommand({ type: "reconnect" });
    return this.awaitOnline(timeoutMs);
  }

  private awaitOnline(timeoutMs: number): Promise<boolean> {
    const now = this.opts.now ?? Date.now;
    const deadline = now() + timeoutMs;
    return new Promise((resolve) => {
      const check = (s: RendererStatus) => {
        if (s.connection === "online") {
          unsub();
          resolve(true);
        }
      };
      // Bounded by the deadline; deliberately NOT unref'd (an unref'd timer
      // would let the process/event loop drain while the promise is pending).
      const timer = setInterval(() => {
        if (this.status.connection === "online") {
          unsub();
          resolve(true);
        } else if (now() >= deadline) {
          unsub();
          resolve(false);
        }
      }, 100);
      const unsub = () => {
        clearInterval(timer);
        this.listeners = this.listeners.filter((l) => l !== check);
      };
      this.listeners.push(check);
    });
  }

  /** Push the ACTIVE config's behavioral dials to the renderer. */
  applyActiveConfig(active: {
    hands_free: boolean;
    volume: number;
    audio_input_id: string | null;
    audio_output_id: string | null;
  }): void {
    this.opts.sendCommand({
      type: "apply-config",
      hands_free: active.hands_free,
      volume: active.volume,
      audio_input_id: active.audio_input_id,
      audio_output_id: active.audio_output_id,
    });
  }

  /** One calm plain sentence in the UI (design: grandma-readable without an agent). */
  notice(text: string): void {
    this.opts.sendCommand({ type: "notice", text });
  }
}
