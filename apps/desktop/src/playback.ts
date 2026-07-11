// TTS playback (voice-session.v1 §3/§10) — schedules 24 kHz PCM16 chunks on a
// WebAudio timeline, gapless within a say_id, and supports the barge-in
// primitives the protocol drives: pause (keep buffer), resume, clearAll (cut).
//
// DOM/WebAudio-dependent, so it lives in the renderer layer (not the tested
// protocol core). `level()` feeds the face's lip-sync.

const TTS_RATE = 24000;

export class Playback {
  private ctx: AudioContext;
  private playHead = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private gain: GainNode;
  private lastRms = 0;
  private paused = false;
  private volume = 1; // the assistant's OWN output gain (never the OS mixer)

  constructor(ctx?: AudioContext) {
    this.ctx = ctx ?? new AudioContext({ sampleRate: TTS_RATE });
    this.gain = this.ctx.createGain();
    this.gain.connect(this.ctx.destination);
  }

  /** Enqueue one PCM16 chunk for gapless playback. */
  enqueue(pcm16: Uint8Array): void {
    if (pcm16.byteLength < 2 || pcm16.byteLength % 2 !== 0) return; // guard: empty/odd → skip
    const samples = new Int16Array(pcm16.buffer, pcm16.byteOffset, pcm16.byteLength >> 1);
    const buf = this.ctx.createBuffer(1, samples.length, TTS_RATE);
    const out = buf.getChannelData(0);
    let sumsq = 0;
    for (let i = 0; i < samples.length; i++) {
      const v = samples[i] / 32768;
      out[i] = v;
      sumsq += v * v;
    }
    this.lastRms = samples.length ? Math.sqrt(sumsq / samples.length) : 0;
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gain);
    const startAt = Math.max(this.ctx.currentTime, this.playHead);
    src.start(startAt);
    this.playHead = startAt + buf.duration;
    this.sources.add(src);
    src.onended = () => this.sources.delete(src);
  }

  /** Barge pause — silence quickly but keep the timeline for a possible resume. */
  pause(): void {
    this.paused = true;
    this.gain.gain.setValueAtTime(0, this.ctx.currentTime);
  }

  resume(): void {
    this.paused = false;
    this.gain.gain.setValueAtTime(this.volume, this.ctx.currentTime);
  }

  /** control.mcp.v1 set_volume: 0-100 mapped to gain (0 = mute). */
  setVolume(level: number): void {
    this.volume = Math.min(100, Math.max(0, level)) / 100;
    if (!this.paused) this.gain.gain.setValueAtTime(this.volume, this.ctx.currentTime);
  }

  /** control.mcp.v1 set_audio_output: route to a specific device (best-effort). */
  async setSink(deviceId: string): Promise<void> {
    const ctx = this.ctx as AudioContext & { setSinkId?: (id: string) => Promise<void> };
    if (typeof ctx.setSinkId === "function") {
      await ctx.setSinkId(deviceId === "default" ? "" : deviceId);
    }
  }

  /** Hard cut — stop everything and drop the buffer (barge confirmed / cancel). */
  clearAll(): void {
    for (const s of this.sources) {
      try {
        s.stop();
      } catch {
        /* already stopped */
      }
    }
    this.sources.clear();
    this.playHead = this.ctx.currentTime;
    this.paused = false;
    this.gain.gain.setValueAtTime(this.volume, this.ctx.currentTime);
    this.lastRms = 0;
  }

  /** Current output loudness 0..1 for lip-sync (approx, from the last chunk). */
  level(): number {
    if (this.paused) return 0; // §7.4: freeze lip-sync while barge-paused
    return this.sources.size ? Math.min(1, this.lastRms * 3) : 0;
  }
}
