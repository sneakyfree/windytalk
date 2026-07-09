// Task 1.5 client-core tests — the voice-session.v1 protocol client + frame codec.
// Run: tsc -p tsconfig.build.json && node --test dist/test
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FLAG_FINAL,
  HEADER_LEN,
  MIC_TYPE,
  TTS_TYPE,
  buildFrame,
  parseFrame,
} from "../src/frames.js";
import { type Clock, VoiceClient } from "../src/protocol.js";

// -- a fake transport + controllable clock ----------------------------------

class FakeTransport {
  sent: (string | ArrayBuffer)[] = [];
  send(d: string | ArrayBuffer) {
    this.sent.push(d);
  }
  json(): Record<string, unknown>[] {
    return this.sent
      .filter((x): x is string => typeof x === "string")
      .map((x) => JSON.parse(x));
  }
  binary(): ArrayBuffer[] {
    return this.sent.filter((x): x is ArrayBuffer => typeof x !== "string");
  }
  last(): Record<string, unknown> {
    const j = this.json();
    return j[j.length - 1];
  }
}

class FakeClock implements Clock {
  t = 1000;
  private timers: { at: number; cb: () => void; live: boolean }[] = [];
  now() {
    return this.t;
  }
  setTimer(ms: number, cb: () => void) {
    const timer = { at: this.t + ms, cb, live: true };
    this.timers.push(timer);
    return () => {
      timer.live = false;
    };
  }
  advance(ms: number) {
    this.t += ms;
    for (const timer of this.timers) {
      if (timer.live && timer.at <= this.t) {
        timer.live = false;
        timer.cb();
      }
    }
  }
}

function ttsFrame(sayId: number, final: boolean, byte = 0xaa): ArrayBuffer {
  return buildFrame(TTS_TYPE, final ? FLAG_FINAL : 0, 0, 123, sayId, new Uint8Array([byte, byte]));
}

// -- frame codec -------------------------------------------------------------

test("frame roundtrip preserves every field", () => {
  const f = buildFrame(TTS_TYPE, FLAG_FINAL, 7, 1783593335657, 42, new Uint8Array([1, 2, 3, 4]));
  assert.equal(f.byteLength, HEADER_LEN + 4);
  const p = parseFrame(f)!;
  assert.equal(p.type, TTS_TYPE);
  assert.equal(p.flags & FLAG_FINAL, FLAG_FINAL);
  assert.equal(p.seq, 7);
  assert.equal(p.tsMs, 1783593335657); // u64 survives
  assert.equal(p.streamId, 42);
  assert.deepEqual([...p.payload], [1, 2, 3, 4]);
});

test("short buffer parses to null", () => {
  assert.equal(parseFrame(new ArrayBuffer(8)), null);
});

test("mic frame header matches engine layout (little-endian)", () => {
  const f = buildFrame(MIC_TYPE, 0, 0x0102, 0, 0, new Uint8Array(640));
  const v = new DataView(f);
  assert.equal(v.getUint8(0), MIC_TYPE);
  assert.equal(v.getUint16(2, true), 0x0102);
  assert.equal(f.byteLength, HEADER_LEN + 640);
});

// -- handshake + routing -----------------------------------------------------

test("hello then ready adopts session id; pong answers time_ping", () => {
  const tx = new FakeTransport();
  const states: string[] = [];
  const c = new VoiceClient(tx, { onState: (s) => states.push(s) });
  c.hello();
  assert.equal(tx.last().protocol, "voice-session.v1");
  c.onWireMessage(JSON.stringify({ type: "ready", session_id: "s1", resumed: false }));
  c.onWireMessage(JSON.stringify({ type: "time_ping", t0: 555 }));
  const pong = tx.json().find((m) => m.type === "pong")!;
  assert.equal(pong.t0, 555);
  c.onWireMessage(JSON.stringify({ type: "state", value: "listening", turn_id: 1 }));
  assert.deepEqual(states, ["listening"]);
});

test("audio frames forward to onAudio; final flag passed through", () => {
  const tx = new FakeTransport();
  const got: [number, boolean][] = [];
  const c = new VoiceClient(tx, { onAudio: (id, _p, fin) => got.push([id, fin]) });
  c.onWireMessage(JSON.stringify({ type: "say_start", say_id: 1, turn_id: 1, text: "hi" }));
  c.onWireMessage(ttsFrame(1, false));
  c.onWireMessage(ttsFrame(1, true));
  assert.deepEqual(got, [[1, false], [1, true]]);
});

test("cancelled say_id audio is discarded", () => {
  const tx = new FakeTransport();
  let plays = 0;
  const c = new VoiceClient(tx, { onAudio: () => plays++ });
  c.onWireMessage(JSON.stringify({ type: "say_cancel", say_id: 3, reason: "barge_in" }));
  c.onWireMessage(ttsFrame(3, false)); // stale → dropped
  assert.equal(plays, 0);
});

test("tool_call surfaces to the dispatcher", () => {
  const tx = new FakeTransport();
  let seen: unknown = null;
  const c = new VoiceClient(tx, {
    onToolCall: (id, _t, tool, args) => (seen = { id, tool, args }),
  });
  c.onWireMessage(
    JSON.stringify({ type: "tool_call", call_id: "c1", turn_id: 1, tool: "open_app", args: { name: "calc" } }),
  );
  assert.deepEqual(seen, { id: "c1", tool: "open_app", args: { name: "calc" } });
  c.sendToolResult("c1", true, "done");
  assert.deepEqual(tx.last(), { type: "tool_result", call_id: "c1", ok: true, result: "done", error: "" });
});

test("unknown message and unknown binary type are ignored, not errors", () => {
  const tx = new FakeTransport();
  let audio = 0;
  let err = 0;
  const c = new VoiceClient(tx, { onAudio: () => audio++, onError: () => err++ });
  c.onWireMessage(JSON.stringify({ type: "future_thing", x: 1 }));
  c.onWireMessage(buildFrame(0x09, 0, 0, 0, 1, new Uint8Array([1]))); // unknown binary type
  c.onWireMessage("not json at all");
  assert.equal(audio, 0);
  assert.equal(err, 0);
});

// -- barge-in state machine --------------------------------------------------

function speakingClient(clock: FakeClock, cb = {}) {
  const tx = new FakeTransport();
  const c = new VoiceClient(tx, cb, clock);
  c.onWireMessage(JSON.stringify({ type: "say_start", say_id: 5, turn_id: 2, text: "a long reply" }));
  c.onWireMessage(JSON.stringify({ type: "state", value: "speaking", turn_id: 2 }));
  return { tx, c };
}

test("barge trigger pauses playback and sends barge_in with active say_id", () => {
  const clock = new FakeClock();
  let paused = 0;
  const { tx, c } = speakingClient(clock, { onPausePlayback: () => paused++ });
  c.localBargeTrigger();
  assert.equal(paused, 1);
  const b = tx.json().find((m) => m.type === "barge_in")!;
  assert.equal(b.say_id, 5);
});

test("say_cancel verdict clears playback and ends the barge", () => {
  const clock = new FakeClock();
  let cleared = -1;
  const { c } = speakingClient(clock, { onClearPlayback: (id: number) => (cleared = id) });
  c.localBargeTrigger();
  c.onWireMessage(JSON.stringify({ type: "say_cancel", say_id: 5, reason: "barge_in" }));
  assert.equal(cleared, 5);
});

test("say_resume within the fence resumes playback", () => {
  const clock = new FakeClock();
  let resumed = 0;
  let cleared = 0;
  const { c } = speakingClient(clock, {
    onResumePlayback: () => resumed++,
    onClearPlayback: () => cleared++,
  });
  c.localBargeTrigger();
  clock.advance(200); // before the 400ms fence
  c.onWireMessage(JSON.stringify({ type: "say_resume", say_id: 5 }));
  assert.equal(resumed, 1);
  assert.equal(cleared, 0);
});

test("no verdict within 400ms fence clears playback", () => {
  const clock = new FakeClock();
  let cleared = 0;
  const { c } = speakingClient(clock, { onClearPlayback: () => cleared++ });
  c.localBargeTrigger();
  clock.advance(401);
  assert.equal(cleared, 1);
});

test("say_resume after the fence is ignored", () => {
  const clock = new FakeClock();
  let resumed = 0;
  const { c } = speakingClient(clock, { onResumePlayback: () => resumed++ });
  c.localBargeTrigger();
  clock.advance(401); // fence fires
  c.onWireMessage(JSON.stringify({ type: "say_resume", say_id: 5 }));
  assert.equal(resumed, 0);
});

test("refractory window blocks an immediate re-trigger", () => {
  const clock = new FakeClock();
  const tx = new FakeTransport();
  const c = new VoiceClient(tx, {}, clock);
  c.onWireMessage(JSON.stringify({ type: "say_start", say_id: 6, turn_id: 3, text: "x" }));
  c.onWireMessage(JSON.stringify({ type: "state", value: "speaking", turn_id: 3 }));
  c.localBargeTrigger();
  c.onWireMessage(JSON.stringify({ type: "say_resume", say_id: 6 })); // ends barge, starts refractory
  // still "speaking"; immediate re-trigger inside 300ms refractory is suppressed
  c.localBargeTrigger();
  const barges = tx.json().filter((m) => m.type === "barge_in");
  assert.equal(barges.length, 1);
  clock.advance(301);
  c.localBargeTrigger();
  assert.equal(tx.json().filter((m) => m.type === "barge_in").length, 2);
});

test("barge only fires while speaking", () => {
  const clock = new FakeClock();
  const tx = new FakeTransport();
  const c = new VoiceClient(tx, {}, clock);
  c.onWireMessage(JSON.stringify({ type: "state", value: "listening" }));
  c.localBargeTrigger();
  assert.equal(tx.json().filter((m) => m.type === "barge_in").length, 0);
});

test("reconnect floor discards pre-reconnect audio", () => {
  const clock = new FakeClock();
  const tx = new FakeTransport();
  let plays = 0;
  const c = new VoiceClient(tx, { onAudio: () => plays++ }, clock);
  c.onWireMessage(JSON.stringify({ type: "say_start", say_id: 8, turn_id: 4, text: "x" }));
  c.markReconnecting(); // floor = 9
  c.onWireMessage(ttsFrame(8, false)); // pre-reconnect → dropped
  assert.equal(plays, 0);
});
