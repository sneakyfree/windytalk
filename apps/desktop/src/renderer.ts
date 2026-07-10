// Renderer glue — wires the tested VoiceClient to real audio + face + control
// panel + hands. Runs in the Electron renderer (contextIsolation on). No protocol
// logic lives here; this is transport + I/O + UI wiring. Exposes `window.wt` so the
// page (control panel + mic button) drives the REAL mic/connection, not a cosmetic flag.

import { Playback } from "./playback.js";
import { type Callbacks, VoiceClient } from "./protocol.js";

interface WindytalkBridge {
  cfg: { engineUrl: string; handsUrl: string; appVersion: string; demo: string; autoMic: boolean };
  hands: { invoke(tool: string, args: unknown): Promise<{ ok: boolean; result?: string; error?: string }> };
  quit(): void;
}

export interface Status {
  connection: "connecting" | "online" | "offline" | "terminal";
  state: string; // engine state: idle|listening|thinking|speaking|paused
  micOn: boolean;
  micError: string | null;
  sessionId: string | null;
  lastError: string | null;
  heard: string;
  saying: string;
}

type StatusListener = (s: Status) => void;

declare global {
  interface Window {
    windytalk?: WindytalkBridge;
    wt?: WindowWT;
    face?: {
      setState(s: string): void;
      setLevel(v: number): void;
      setCaption(text: string, kind: "heard" | "say" | "none"): void;
    };
    windyTalkStart?: () => void;
  }
}

export interface WindowWT {
  toggleMic(): void;
  setMic(on: boolean): void;
  status(): Status;
  onStatus(cb: StatusListener): void;
  quit(): void;
}

const BRIDGE: WindytalkBridge | undefined = (globalThis as unknown as { window: Window }).window?.windytalk;
const ENGINE_URL = BRIDGE?.cfg.engineUrl ?? "ws://127.0.0.1:8788";
const RECONNECT_MS = 1500;
const LIVENESS_MS = 25000; // §9: >25s with no engine frame ⇒ treat as abnormal close

class RendererApp {
  private ws: WebSocket | null = null;
  private client: VoiceClient;
  private playback = new Playback();
  private audioCtx: AudioContext | null = null;
  private worklet: AudioWorkletNode | null = null;
  private micStream: MediaStream | null = null;
  private micWanted = false; // the user's intent (button)
  private ready = false; // engine sent `ready` (gate binary until then, §11.1)
  private terminal = false; // bye/fatal ⇒ never auto-reconnect (§9)
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private livenessTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners: StatusListener[] = [];
  private s: Status = {
    connection: "connecting", state: "idle", micOn: false, micError: null,
    sessionId: null, lastError: null, heard: "", saying: "",
  };

  constructor() {
    this.client = new VoiceClient(this.transport(), this.callbacks());
  }

  async start(): Promise<void> {
    await this.setupMic(); // never throws — surfaces mic errors into status
    this.connect();
    setInterval(() => window.face?.setLevel(this.playback.level()), 60);
    if (BRIDGE?.cfg.autoMic) this.setMic(true);
  }

  // -- status broadcast ------------------------------------------------------

  onStatus(cb: StatusListener): void {
    this.listeners.push(cb);
    cb(this.s);
  }
  private emit(patch: Partial<Status>): void {
    this.s = { ...this.s, ...patch };
    for (const cb of this.listeners) cb(this.s);
  }

  // -- transport (rebuilt per socket, client reused) -------------------------

  private transport() {
    return {
      send: (d: string | ArrayBuffer) => {
        // §11.1: no binary before `ready`
        if (typeof d !== "string" && !this.ready) return;
        if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(d);
      },
    };
  }

  private connect(): void {
    if (this.terminal) return;
    this.emit({ connection: "connecting" });
    this.ready = false;
    const ws = new WebSocket(ENGINE_URL);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.client.hello(this.s.sessionId != null); // resume if we have a session (§9)
      this.armLiveness();
    };
    ws.onmessage = (e) => {
      this.armLiveness();
      this.client.onWireMessage(e.data);
    };
    ws.onclose = () => {
      this.ready = false;
      window.face?.setState("offline");
      this.playback.clearAll(); // §9: drop buffers at reconnect
      this.client.markReconnecting();
      if (this.terminal) {
        this.emit({ connection: "terminal" });
        return;
      }
      this.emit({ connection: "offline" });
      this.reconnectTimer = setTimeout(() => this.connect(), RECONNECT_MS);
    };
    ws.onerror = () => ws.close();
  }

  private armLiveness(): void {
    if (this.livenessTimer) clearTimeout(this.livenessTimer);
    this.livenessTimer = setTimeout(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.close(); // triggers reconnect
    }, LIVENESS_MS);
  }

  private callbacks(): Callbacks {
    const face = () => window.face;
    return {
      onReady: (sid) => {
        this.ready = true;
        this.emit({ connection: "online", sessionId: sid, lastError: null });
        if (this.micWanted) this.client.setMic(true); // re-assert mic on (re)connect
      },
      onState: (v) => {
        face()?.setState(v);
        // §7.1: tell the capture worklet when the agent is speaking (barge detection)
        this.worklet?.port.postMessage({ type: "speaking", on: v === "speaking" });
        this.emit({ state: v });
      },
      onHeard: (text) => { face()?.setCaption(`"${text}"`, "heard"); this.emit({ heard: text }); },
      onSayStart: (_id, _t, text) => { face()?.setCaption(text, "say"); this.emit({ saying: text }); },
      onAudio: (_id, pcm) => this.playback.enqueue(pcm),
      onSayEnd: () => {},
      onPausePlayback: () => this.playback.pause(),
      onResumePlayback: () => this.playback.resume(),
      onClearPlayback: () => this.playback.clearAll(),
      onLevel: (v) => face()?.setLevel(v),
      onToolCall: (callId, _turn, tool, args) => this.dispatchTool(callId, tool, args),
      onError: (code, msg, fatal) => {
        this.emit({ lastError: `${code}: ${msg}` });
        if (fatal) {
          this.terminal = true; // §9: fatal ⇒ do not reconnect
          this.emit({ connection: "terminal" });
          face()?.setState("offline");
        }
      },
      onBye: () => {
        this.terminal = true; // §9: bye ⇒ do not reconnect
        this.emit({ connection: "terminal" });
      },
    };
  }

  // -- mic (the real toggle the UI now drives) -------------------------------

  private async setupMic(): Promise<void> {
    try {
      const ctx = new AudioContext();
      this.audioCtx = ctx;
      await ctx.audioWorklet.addModule("capture-worklet.js");
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, // §4.1
      });
      this.micStream = stream;
      const src = ctx.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(ctx, "capture-processor");
      this.worklet = worklet;
      src.connect(worklet);
      worklet.connect(ctx.destination); // pull the graph so process() runs (emits silence)
      worklet.port.onmessage = (e) => {
        const d = e.data;
        if (d.type === "frame" && this.micWanted && this.ready) {
          this.client.pushMicFrame(new Uint8Array(d.pcm));
        } else if (d.type === "barge") {
          this.client.localBargeTrigger();
        }
      };
    } catch (err) {
      // No mic / permission denied: surface it, but still connect so TTS + status work.
      this.emit({ micError: String((err as Error)?.message || err) });
    }
  }

  setMic(on: boolean): void {
    if (on && this.s.micError) return; // can't listen without a mic
    this.micWanted = on;
    if (this.audioCtx?.state === "suspended") void this.audioCtx.resume();
    this.client.setMic(on);
    this.emit({ micOn: on });
    window.face?.setState(on ? this.s.state : "paused");
  }

  toggleMic(): void {
    this.setMic(!this.micWanted);
  }

  private async dispatchTool(callId: string, tool: string, args: unknown): Promise<void> {
    // Route through the main process (no CORS, token added there).
    try {
      const res = BRIDGE
        ? await BRIDGE.hands.invoke(tool, args)
        : { ok: false, error: "hands bridge unavailable" };
      this.client.sendToolResult(callId, res.ok, res.result ?? "", res.error ?? "");
    } catch (e) {
      this.client.sendToolResult(callId, false, "", `hands error: ${String(e)}`);
    }
  }

  status(): Status { return this.s; }
  quit(): void { BRIDGE?.quit(); }
}

const app = new RendererApp();
const wt: WindowWT = {
  toggleMic: () => app.toggleMic(),
  setMic: (on) => app.setMic(on),
  status: () => app.status(),
  onStatus: (cb) => app.onStatus(cb),
  quit: () => app.quit(),
};
(window as Window).wt = wt;
window.windyTalkStart = () => void app.start();
