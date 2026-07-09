// voice-session.v1 client protocol core (pure TS — no DOM, no Electron).
//
// Owns the message routing, the playback discard policy (§3/§10), and the
// barge-in state machine (§7: local pause → barge_in → 250ms engine verdict /
// 400ms client fence / 300ms refractory). Audio playback *mechanism* (WebAudio)
// and the WebSocket live in the renderer; this module decides WHAT to play and
// WHEN to pause/clear, so the tricky rules are unit-testable without hardware.

import { FLAG_FINAL, MIC_TYPE, buildFrame, parseFrame } from "./frames.js";

export const PROTOCOL = "voice-session.v1";
const BARGE_FENCE_MS = 400; // §7.4
const REFRACTORY_MS = 300; // §7.4

export interface Transport {
  send(data: string | ArrayBuffer): void;
}

export interface Clock {
  now(): number;
  setTimer(ms: number, cb: () => void): () => void; // returns a cancel fn
}

export const realClock: Clock = {
  now: () => Date.now(),
  setTimer: (ms, cb) => {
    const id = setTimeout(cb, ms);
    return () => clearTimeout(id);
  },
};

export interface Callbacks {
  onReady?(sessionId: string, resumed: boolean, limits: unknown): void;
  onState?(value: string, turnId?: number): void;
  onHeard?(text: string, final: boolean, turnId: number): void;
  onSayStart?(sayId: number, turnId: number, text: string): void;
  onAudio?(sayId: number, pcm: Uint8Array, final: boolean): void; // play it
  onSayEnd?(sayId: number): void;
  onPausePlayback?(): void; // barge: pause, don't clear (§7.2)
  onResumePlayback?(): void; // false-positive verdict (§7.3)
  onClearPlayback?(sayId: number): void; // cancel: stop + clear (§7.3)
  onLevel?(value: number): void;
  onToolCall?(callId: string, turnId: number, tool: string, args: unknown): void;
  onError?(code: string, message: string, fatal: boolean): void;
}

export class VoiceClient {
  private seqOut = 0;
  private sessionId: string | null = null;
  private state = "idle";
  private activeSayId = 0;
  private cancelled = new Set<number>();
  private connectEpochFloor = 0; // say_ids below this are pre-reconnect → stale
  private bargePending = false;
  private clearFence: (() => void) | null = null;
  private refractoryUntil = 0;
  private client: { app: string; version: string; platform: string };

  constructor(
    private transport: Transport,
    private cb: Callbacks = {},
    private clock: Clock = realClock,
    client = { app: "WindyTalk", version: "1", platform: "unknown" },
  ) {
    this.client = client;
  }

  currentState(): string {
    return this.state;
  }

  // -- outbound --------------------------------------------------------------

  hello(resume = false): void {
    this.transport.send(
      JSON.stringify({
        type: "hello",
        protocol: PROTOCOL,
        session_id: this.sessionId ?? undefined,
        resume,
        client: this.client,
      }),
    );
  }

  setMic(on: boolean): void {
    this.transport.send(JSON.stringify({ type: "mic", on, ts: this.clock.now() }));
  }

  sendText(message: string): void {
    this.transport.send(JSON.stringify({ type: "text", message }));
  }

  sendToolResult(callId: string, ok: boolean, result = "", error = ""): void {
    this.transport.send(
      JSON.stringify({ type: "tool_result", call_id: callId, ok, result, error }),
    );
  }

  pushMicFrame(pcm: Uint8Array): void {
    const frame = buildFrame(MIC_TYPE, 0, this.seqOut, this.clock.now(), 0, pcm);
    this.seqOut = (this.seqOut + 1) & 0xffff;
    this.transport.send(frame);
  }

  // Local barge-in fast path (§7.1/§7.2): the renderer's worklet detector calls
  // this the instant it hears speech while the agent is speaking.
  localBargeTrigger(): void {
    if (this.state !== "speaking" || this.bargePending) return;
    if (this.clock.now() < this.refractoryUntil) return;
    this.bargePending = true;
    this.cb.onPausePlayback?.(); // pause ≤50ms, do NOT clear (§7.2)
    this.transport.send(
      JSON.stringify({ type: "barge_in", ts: this.clock.now(), say_id: this.activeSayId }),
    );
    this.clearFence = this.clock.setTimer(BARGE_FENCE_MS, () => {
      // §7.4: no verdict in 400ms → treat as cancelled, clear buffers
      this.cb.onClearPlayback?.(this.activeSayId);
      this.cancelled.add(this.activeSayId);
      this.endBarge();
    });
  }

  // -- inbound ---------------------------------------------------------------

  onWireMessage(data: string | ArrayBuffer): void {
    if (typeof data !== "string") {
      this.onBinary(data);
      return;
    }
    let m: Record<string, unknown>;
    try {
      m = JSON.parse(data);
    } catch {
      return;
    }
    this.onJson(m);
  }

  private onBinary(buf: ArrayBuffer): void {
    const f = parseFrame(buf);
    if (f === null) return; // §2: drop short frame
    if (f.type !== 0x02) return; // §2: only TTS binary from engine; drop unknown types
    // §3: discard only cancelled / superseded / pre-reconnect say_ids
    if (this.cancelled.has(f.streamId) || f.streamId < this.connectEpochFloor) return;
    this.cb.onAudio?.(f.streamId, f.payload, (f.flags & FLAG_FINAL) !== 0);
  }

  private onJson(m: Record<string, unknown>): void {
    switch (m.type) {
      case "ready":
        this.sessionId = String(m.session_id);
        // any pre-reconnect say_ids are now stale; current say_ids restart from engine
        this.connectEpochFloor = 0;
        this.cancelled.clear();
        this.cb.onReady?.(this.sessionId, Boolean(m.resumed), m.limits);
        break;
      case "state":
        this.state = String(m.value);
        if (this.state !== "speaking") this.endBarge(); // leaving speaking resolves barge (§7.4)
        this.cb.onState?.(this.state, m.turn_id as number | undefined);
        break;
      case "heard":
        this.cb.onHeard?.(String(m.text), Boolean(m.final), Number(m.turn_id));
        break;
      case "say_start":
        this.activeSayId = Number(m.say_id);
        this.cb.onSayStart?.(this.activeSayId, Number(m.turn_id), String(m.text));
        break;
      case "say_end":
        this.cb.onSayEnd?.(Number(m.say_id));
        break;
      case "say_cancel":
        this.cancelled.add(Number(m.say_id));
        this.cb.onClearPlayback?.(Number(m.say_id)); // stop ≤50ms + clear whole turn (§11.5)
        this.endBarge();
        break;
      case "say_resume":
        if (this.bargePending) {
          this.cb.onResumePlayback?.();
          this.endBarge();
        }
        // a resume arriving after the fence already fired is ignored (§7.4)
        break;
      case "level":
        this.cb.onLevel?.(Number(m.value));
        break;
      case "tool_call":
        this.cb.onToolCall?.(
          String(m.call_id),
          Number(m.turn_id),
          String(m.tool),
          m.args,
        );
        break;
      case "time_ping":
        this.transport.send(
          JSON.stringify({ type: "pong", t0: m.t0, t_client: this.clock.now() }),
        );
        break;
      case "error":
        this.cb.onError?.(String(m.code), String(m.message), Boolean(m.fatal));
        break;
      // unknown types ignored (§1 additive-safety)
    }
  }

  // Renderer calls this before reconnecting so pre-reconnect audio is discarded.
  markReconnecting(): void {
    this.connectEpochFloor = this.activeSayId + 1;
    this.seqOut = 0;
    this.endBarge();
  }

  private endBarge(): void {
    if (this.clearFence) {
      this.clearFence();
      this.clearFence = null;
    }
    if (this.bargePending) {
      this.refractoryUntil = this.clock.now() + REFRACTORY_MS;
    }
    this.bargePending = false;
  }
}
