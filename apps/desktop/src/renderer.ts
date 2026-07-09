// Renderer glue — wires the tested VoiceClient to real audio + face + hands.
// Runs in the Electron renderer (DOM/WebAudio). No protocol logic lives here;
// this is transport + I/O + the tool_call → hands-surface dispatch.

import { Playback } from "./playback.js";
import { type Callbacks, VoiceClient } from "./protocol.js";

const ENGINE_URL = readEnv("WINDYTALK_ENGINE_URL", "ws://127.0.0.1:8788");
const HANDS_URL = readEnv("WINDYTALK_HANDS_URL", "http://127.0.0.1:8781");

function readEnv(name: string, fallback: string): string {
  const w = window as unknown as { WINDYTALK?: Record<string, string> };
  return w.WINDYTALK?.[name] ?? fallback;
}

// The face is a plain global (face.js on the page) exposing setState/setLevel/setCaption.
interface Face {
  setState(s: string): void;
  setLevel(v: number): void;
  setCaption(text: string, kind: "heard" | "say" | "none"): void;
  setConnected(on: boolean): void;
}
declare global {
  interface Window {
    face: Face;
    windyTalkStart?: () => void;
  }
}

class RendererApp {
  private ws: WebSocket | null = null;
  private client!: VoiceClient;
  private playback = new Playback();
  private worklet: AudioWorkletNode | null = null;
  private micOn = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  async start(): Promise<void> {
    await this.setupMic();
    this.connect();
    // drive lip-sync from real output loudness
    setInterval(() => window.face?.setLevel(this.playback.level()), 60);
  }

  private callbacks(): Callbacks {
    const face = () => window.face;
    return {
      onReady: () => face()?.setConnected(true),
      onState: (v) => face()?.setState(v),
      onHeard: (text) => face()?.setCaption(`"${text}"`, "heard"),
      onSayStart: (_id, _t, text) => face()?.setCaption(text, "say"),
      onAudio: (_id, pcm) => this.playback.enqueue(pcm),
      onSayEnd: () => {},
      onPausePlayback: () => this.playback.pause(),
      onResumePlayback: () => this.playback.resume(),
      onClearPlayback: () => this.playback.clearAll(),
      onLevel: (v) => face()?.setLevel(v),
      onToolCall: (callId, _turn, tool, args) => this.dispatchTool(callId, tool, args),
      onError: (_c, msg, fatal) => {
        if (fatal) face()?.setConnected(false);
        console.warn("engine error:", msg);
      },
    };
  }

  private connect(): void {
    this.ws = new WebSocket(ENGINE_URL);
    this.ws.binaryType = "arraybuffer";
    const transport = {
      send: (d: string | ArrayBuffer) => {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(d);
      },
    };
    this.client = new VoiceClient(transport, this.callbacks());
    this.ws.onopen = () => {
      this.client.hello();
      this.client.setMic(this.micOn);
    };
    this.ws.onmessage = (e) => this.client.onWireMessage(e.data);
    this.ws.onclose = () => {
      window.face?.setConnected(false);
      this.client.markReconnecting();
      this.reconnectTimer = setTimeout(() => this.connect(), 1200); // abnormal → resume (§9)
    };
    this.ws.onerror = () => this.ws?.close();
  }

  private async setupMic(): Promise<void> {
    const ctx = new AudioContext();
    await ctx.audioWorklet.addModule("capture-worklet.js");
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, // §4.1
    });
    const src = ctx.createMediaStreamSource(stream);
    this.worklet = new AudioWorkletNode(ctx, "capture-processor");
    src.connect(this.worklet);
    this.worklet.port.onmessage = (e) => {
      const d = e.data;
      if (d.type === "frame" && this.micOn) {
        this.client.pushMicFrame(new Uint8Array(d.pcm));
      } else if (d.type === "barge") {
        this.client.localBargeTrigger();
      }
    };
  }

  setMic(on: boolean): void {
    this.micOn = on;
    this.client?.setMic(on);
    this.worklet?.port.postMessage({ type: "speaking", on: false });
  }

  private async dispatchTool(callId: string, tool: string, args: unknown): Promise<void> {
    // tool_call → the local hands surface (Task 1.4) → tool_result back to the engine.
    try {
      const resp = await fetch(`${HANDS_URL}/invoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tool, args }),
      });
      const res = (await resp.json()) as { ok: boolean; result?: string; error?: string };
      this.client.sendToolResult(callId, res.ok, res.result ?? "", res.error ?? "");
    } catch (e) {
      this.client.sendToolResult(callId, false, "", `hands unreachable: ${String(e)}`);
    }
  }
}

const app = new RendererApp();
window.windyTalkStart = () => void app.start();
