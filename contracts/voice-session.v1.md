# voice-session.v1 — client ⇄ engine protocol

**Status: FROZEN 2026-07-09** (rev 3; survived two rounds of the Task 0.2 two-agent
adversarial read — round 2 produced zero divergent interpretations between independent
client and engine implementations; its remaining findings are codified below).
Version string: `voice-session.v1`, string grammar `voice-session.v<major>[.<minor>]`
(major parsed as the integer after `.v`; an absent or unparseable `protocol` value is a
fatal `version_mismatch`). Change control per `contracts/README.md`:
additive → v1.1 via PR; breaking → new v2 file + tell Grant.

This contract exists to make the §0.1 felt-latency budget *achievable and measurable*:
EOS→first-audio ≤ 1.2 s · barge-in→silence ≤ 150 ms · wake→listening-UI ≤ 300 ms ·
mic-frame transport ≤ 60 ms (all p90 over ≥20 real turns). Every design choice below
serves those four numbers. RFC 2119 keywords (MUST/SHOULD/MAY) are used literally.

---

## 1 · Transport

- One **WebSocket** connection per session. Engine is the server.
- **Binary frames** carry audio (both directions). **Text frames** carry UTF-8 JSON
  messages (control + events). A receiver MUST ignore (not error on) any JSON message
  whose `type` it does not recognize, and MUST drop (not error on) any binary frame
  whose header `type` it does not recognize — this is what makes v1.1 additive changes safe.
- All multi-byte integers in binary headers are **little-endian**.
- WebSocket permessage-deflate SHOULD be disabled for binary frames (pcm doesn't
  compress usefully and it adds latency).
- Anything received by the engine before `hello` (JSON or binary) MUST be silently
  ignored/dropped — there is no session yet to error on.

## 2 · Binary frame layout (uniform 16-byte header, both directions)

| Offset | Size | Field | Meaning |
|---|---|---|---|
| 0 | u8 | `type` | `0x01` mic audio (client→engine) · `0x02` TTS audio (engine→client) |
| 1 | u8 | `flags` | bit 0 = `final` (TTS only: last chunk of this `say_id`; **mic frames never set it** — mic lifecycle is the `mic` JSON message); other bits 0 in v1 |
| 2 | u16 | `seq` | per-direction counter, starts 0 at (re)connect, wraps mod 65536. **Diagnostic only**: TCP already guarantees order — a receiver MUST NOT reorder or delay frames by `seq`; on a gap or duplicate it SHOULD log and MUST still process the frame. |
| 4 | u64 | `ts_ms` | sender's session clock (§8.0), ms since Unix epoch, at frame *capture* (mic, first sample) or *emission* (TTS) |
| 12 | u32 | `stream_id` | mic: MUST be 0 · TTS: the `say_id` this audio belongs to |
| 16 | — | payload | PCM, per §3 |

**Malformed frames** (engine side): payload not a positive multiple of 2; mic payload
≠ 640 bytes; mic `stream_id` ≠ 0. The engine MUST drop such a frame and SHOULD send a
non-fatal `error` (code `bad_frame`). The **client never sends `error`** (it is not in
the client→engine catalog): on a malformed inbound frame the client MUST drop it and
SHOULD log locally.

## 3 · Audio formats (fixed in v1 — not negotiable at runtime)

- **Mic (client→engine):** PCM signed 16-bit LE, mono, **16 000 Hz**, framed at exactly
  **20 ms = 320 samples = 640 bytes** payload. Frames MUST be contiguous capture; the
  client MUST NOT skip silence (the engine's VAD needs it) and MUST keep streaming
  while the mic is on **including while TTS is playing** (barge-in detection depends on it).
- **TTS (engine→client):** PCM signed 16-bit LE, mono, **24 000 Hz**. Chunk duration is
  engine-chosen; each chunk SHOULD be 20–60 ms (payload 960–2880 bytes). The final
  chunk of an utterance sets header flag `final`. The client is responsible for
  resampling 24 kHz → its output-device rate.
- **Pipelining is allowed and expected:** the engine MAY send audio for `say_id` N+1
  while N is still playing (playback of segments is still strictly sequential, §10).
  Therefore the client buffers per-`say_id` and MUST discard only TTS frames whose
  `say_id` is **cancelled, superseded, or from before the current (re)connect** — not
  merely "not currently playing".
- **Wire-order guarantees:** `say_start` always precedes the first audio frame of its
  `say_id` (same TCP stream, so this holds trivially); the `final`-flagged chunk is the
  last audio of the `say_id`; `say_end` follows it. Audio for a `say_id` the client has
  no `say_start` for is therefore stale (post-cancel straggler) — drop it. The client
  advances playout from `say_id` N to N+1 when N's buffer is drained and *either*
  end-signal (`final` flag or `say_end`) has been seen; it tolerates either arriving
  first. The client applies no length checks to TTS payloads beyond §2's
  even-and-nonempty rule (the 20–60 ms chunking is a SHOULD).

## 4 · Client capture requirements (normative — this is product, not advice)

1. **Echo cancellation MUST be on**: `getUserMedia({audio: {echoCancellation: true}})`
   (plus `noiseSuppression: true`, `autoGainControl: true` SHOULD be on). The mic hears
   the speakers; without AEC the engine barges in on its own TTS. Headphone-only
   correctness is a Task 1.5 verification FAILURE mode, not a pass.
2. Capture MUST use an **AudioWorklet** (or equivalent raw-callback API on native
   platforms). **MediaRecorder / chunked-container capture is forbidden** — it alone
   can eat 100–200 ms of the budget.
3. The client resamples device rate → 16 kHz and frames to exactly 20 ms.
4. `ts_ms` MUST be stamped at capture time of the frame's *first sample*, from the
   **session clock** (§8.0) — the same clock used in `pong` — and MUST be monotonically
   non-decreasing **within a connection** (the clock re-anchors at each (re)connect,
   like `seq`; cross-connection monotonicity is not required).
5. Target ≤ 40 ms from acoustic sound → frame handed to the WebSocket (leaves 60 ms
   transport + engine time inside the wake/barge budgets).
6. **Wake affordance (sanctioned local exception):** wake-word detection is client-local
   in v1 (§13). On wake the client MAY immediately render a local "listening"
   affordance — the §0.1 wake→listening-UI ≤ 300 ms budget is measured to this local
   affordance, not to the engine's `state` round trip. The engine's next `state` event
   reconciles the UI.

## 5 · JSON messages — catalog

Every JSON message has a `type` field. Unknown *fields* MUST be ignored (additive-change
safety). `ts` fields are sender session-clock ms (§8.0).

### Client → engine

| type | fields | semantics |
|---|---|---|
| `hello` | `protocol:"voice-session.v1"`, `session_id?`, `resume?:bool`, `auth?:{scheme,token}`, `client:{app,version,platform}`, `options?:{vad?:{silence_ms?,min_speech_ms?}, level_events?:bool}` | MUST be the first message after connect. No binary frames before `ready`. `level_events` defaults to `true` when absent. |
| `mic` | `on:bool, ts` | Mic state. After any (re)connect the engine considers the mic **off** until it receives `mic {on:true}`. Redundant `mic` messages (same state) are idempotent no-ops — so `mic {on:false}` transitions to `paused` only from a mic-on state (§6's "any" means any mic-on state). |
| `barge_in` | `ts, say_id` | Client-side speech-start detected while `speaking` (§7). `say_id` = the segment at the playout head at trigger time, or the most recently received `say_id` if none is playing. It is advisory: the engine resolves the barge against its **active turn**, whatever `say_id` was reported. |
| `tool_result` | `call_id, ok:bool, result?:string, error?:string` | Answer to `tool_call`. Exactly one per `call_id` (§11.3 for timeout + cancelled-turn rules). |
| `text` | `message:string` | Dev/test path: inject a user turn as text (no audio). On receipt the engine mints a new `turn_id`, emits `heard {final:true}` with the text, then `state {thinking}` — mirroring the EOS path. |
| `pong` | `t0:number, t_client:number` | Echo of `time_ping.t0` plus client session clock at receipt. MUST be sent within 50 ms of receiving `time_ping`. |
| `bye` | — | Graceful close; engine may discard session state immediately. |

### Engine → client

| type | fields | semantics |
|---|---|---|
| `ready` | `protocol:"voice-session.v1"`, `session_id`, `resumed:bool`, `audio_out:{rate:24000}`, `limits:{session_ttl_s:60, vad:{silence_ms, min_speech_ms}}` | Response to `hello`. `limits.vad` echoes the clamped, in-force endpointing values (§6) — **informational for the client** (all endpointing is engine-side; the client's §7 barge detector is independent of these values). Client MAY now send binary; it need not wait for the clock-sync burst, and mic activation is user/wake-driven, never automatic on connect. |
| `state` | `value, ts, turn_id?` | Authoritative UI state (§6). |
| `heard` | `text, final:bool, ts, turn_id` | STT partial (`final:false`, replaces previous partial of the same `turn_id`) or final transcript. |
| `say_start` | `say_id, turn_id, text` | An utterance segment begins; `text` = exactly what this `say_id` will speak. |
| `say_end` | `say_id` | All audio for `say_id` sent (client may still be playing it). |
| `say_cancel` | `say_id, reason:"barge_in"\|"error"\|"superseded"` | Client MUST stop playback ≤ 50 ms and clear all buffered audio for the whole turn (§11.5). |
| `say_resume` | `say_id` | Verdict on a `barge_in` the engine judged false (§7): resume paused playback. |
| `level` | `value:0..1, ts` | Output loudness for lip-sync, ≤ 25 Hz, emitted only while the engine is emitting TTS audio (including a post-`paused` tail, §6). Suppressed if `options.level_events == false`. Client MAY ignore and compute locally. |
| `tool_call` | `call_id, turn_id, tool, args:object` | Execute on the local hands surface (hands.mcp.v1); reply with `tool_result`. |
| `time_ping` | `t0:number` | Clock-sync probe, sent right after `ready` (3× burst ~100 ms apart) then every 10 s (§8). |
| `error` | `code, message, fatal:bool` | `fatal:true` → engine closes the socket after sending. Codes: §9. |
| `bye` | `reason` | Engine-initiated **graceful** close (shutdown, TTL, superseded). The client MUST NOT auto-reconnect after `bye` (reconnect is for abnormal closes only, §9). |

## 6 · Session state machine (engine-owned, authoritative)

States: `idle` · `listening` · `thinking` · `speaking` · `paused`.

- `ready` → `idle` (also after a resumed reconnect: post-resume state is always `idle`,
  because the mic is considered off until a fresh `mic {on:true}`).
- `idle` --mic on--> `listening`.
- `listening` --EOS detected--> `thinking`. **EOS rule, exact:** the utterance opens
  when *cumulative* voiced time within the current listening period reaches
  `min_speech_ms` (voiced frames need not be contiguous); EOS fires when, after the
  utterance has opened, `silence_ms` of *contiguous* unvoiced time elapses. Interior
  silence shorter than `silence_ms` resets the silence counter but keeps the utterance
  open. Silence before the utterance opens accumulates nothing. Defaults
  `min_speech_ms` 150, `silence_ms` 700; `hello.options.vad` overrides, clamped by the
  engine to `min_speech_ms ∈ [50, 1000]`, `silence_ms ∈ [200, 2000]`; the in-force
  values are reported in `ready.limits.vad` and the client MUST honor them.
- At EOS the engine emits `heard {final:true}` **first**, then `state {thinking}`.
- `thinking` --first TTS chunk emitted--> `speaking`.
- `speaking` --`say_end` of the turn's last `say_id`--> `listening`.
- `speaking` --confirmed barge-in--> `listening` (new turn begins with the barging speech).
- any --`mic {on:false}`--> `paused` (immediately). If this happens mid-turn
  (`thinking`/`speaking`), the engine SHOULD complete the in-flight turn's speech —
  it keeps sending TTS (and `level`) for the current turn's queued segments; the client
  keeps playing them. Barge-in is unavailable while `paused` (no mic). A pending
  `barge_in` verdict is voided by `mic {on:false}`: mic-off wins — the engine sends
  **neither** `say_resume` nor `say_cancel` for that barge and completes the in-flight
  turn's speech; the client, having sent `mic {on:false}` with a barge pending, resumes
  its paused playback.
- `paused` --`mic {on:true}`--> `listening`.
- **Speech during `thinking` is ignored in v1**: mic frames keep flowing but feed VAD
  only; the engine MUST NOT queue a second turn and MUST NOT treat it as barge-in
  (barge-in exists only in `speaking`, §7). This is a known v1 limitation (§13) —
  user speech between EOS and first audio is dropped.
- The engine MUST emit a `state` event on every transition. The client renders states;
  it MUST NOT invent transitions. Its only sanctioned local exceptions: the §7
  playback-pause fast path and the §4.6 wake affordance.
- **`turn_id`**: u32, starts at 1, increments per user utterance, minted when the
  utterance *opens* (first qualifying speech) so `heard` partials carry it. A turn MAY
  contain multiple sequential `say_id`s (§10) and interleaved `tool_call`s.
- **`say_id`**: u32, starts at 1, strictly increasing per session, never reused
  (it is also the binary `stream_id`).

## 7 · Barge-in (the 150 ms budget, decomposed)

While `speaking`, the user may talk over the agent. Round-tripping to the engine before
silencing playback cannot meet 150 ms; therefore:

1. The client MUST run a **local speech-start detector** on its (AEC-cleaned) mic
   while state is `speaking` (energy/VAD in the worklet; detection SHOULD trigger
   within 80 ms of speech onset).
2. On trigger the client MUST, within 50 ms: **pause** playback (do not clear), and
   send `barge_in {ts, say_id}` (at most one in flight at a time).
3. The engine, which is independently running VAD on the still-flowing mic frames,
   MUST reply within **250 ms** of receiving the `barge_in` with either
   `say_cancel {reason:"barge_in"}` (confirmed — client clears the turn's buffers;
   engine aborts brain/TTS for that turn and treats the barging speech as the start of
   a new turn) or `say_resume` (false positive — client resumes playback; a ≤ 300 ms
   dip, not a cut). **Engine-side confirmation rule:** confirm once ≥ 60 ms of
   cumulative voiced mic time is observed within the decision window. If the engine
   produces a timely verdict (at or before 250 ms) with < 60 ms voiced, it sends
   `say_resume`. Only if it emits **no** verdict by the deadline does it resolve as
   `say_cancel` (fail toward the user having the floor) — it MUST NOT send
   `say_resume` after its deadline.
4. If the client receives neither verdict within **400 ms** of sending `barge_in`, it
   MUST treat the barge as cancelled (clear buffers). A `say_resume` arriving after
   the client's 400 ms fence MUST be ignored (the audio is gone). A `state` transition
   away from `speaking` also resolves a pending barge as cancelled. After a
   `say_resume`, the client MUST apply a **300 ms refractory window** before sending
   another `barge_in` (prevents detector thrash on residual voicing; genuine continued
   speech is still caught by the engine's autonomous path, rule 5). While a barge is
   pending, the engine MAY keep emitting `level`; the client freezes lip-sync while
   its playback is locally paused and resumes it on `say_resume`.
5. The engine MUST apply the same barge-in path when *it* detects speech during
   `speaking` (same ≥ 60 ms voiced rule) even if the client never sent `barge_in`:
   it sends `say_cancel` directly.
6. A `barge_in` received when the engine is not `speaking` (stale/late) is silently
   ignored — the client's 400 ms fence and rule 4 make this safe.

Measured metric: the §0.1 ≤ 150 ms budget is anchored at **acoustic speech onset** and
decomposes as ≤ 80 ms detection (rule 1) + ≤ 50 ms pause action (rule 2) + audio-stack
flush. `barge_in.ts` records the detector-trigger instant (no back-dating); the engine
logs trigger→silent as the measurable proxy and the detection allowance is budgeted by
rule 1's SHOULD. Confirm/clear is cleanup, outside the budget.

## 8 · Clock sync & latency measurement (how §0.1 gets *measured, not vibed*)

**8.0 The session clock.** Each side derives its timestamps from a clock constructed as
*wall-clock epoch anchored once at connect + monotonic delta* — i.e. it reads as ms
since Unix epoch but can never step backward (NTP/DST steps don't move it). All `ts`,
`ts_ms`, `t0`, `t_client` values in this contract come from that clock. Latency
arithmetic is done in floating-point ms (JSON numbers are IEEE-754 doubles; safe at
epoch-ms magnitudes).

- The engine owns measurement. After `ready` it sends 3 `time_ping`s ~100 ms apart,
  then one every 10 s. From each `pong` it computes RTT = `t_engine_recv − t0` and
  offset = `t_client − (t0 + RTT/2)`; it keeps the estimate from the **lowest-RTT**
  exchange (min-filter over the session).
- **Transport latency** (≤ 60 ms gate) per mic frame: `t_engine_recv − (frame.ts_ms − offset)`,
  logged as a p90 over the session.
- **EOS→first-audio** (≤ 1.2 s gate): engine timestamps EOS decision → first TTS chunk
  handed to the socket; the client-side playout tail is bounded by §4.5/§3/§10 rules.
- Latency figures leave the engine as **out-of-band telemetry** (telemetry.v1,
  `latency_ms` — numbers only, never text). Telemetry is NOT carried on this WebSocket
  in either direction.

## 9 · Errors, auth, reconnect

- **Error codes** (non-exhaustive; unknown codes are non-fatal unless `fatal:true`):
  `version_mismatch` (fatal — `hello.protocol` major ≠ engine's), `auth_required`,
  `auth_invalid`, `not_entitled` (all fatal), `bad_frame`, `rate_limited`, `internal`.
  **v1 auth posture:** engines MUST NOT reject a `hello` merely for absent `auth`;
  an engine not configured to validate tokens MUST ignore a present `auth` field
  rather than reject it. Real enforcement arrives at Phase 1.7 without a protocol change.
- **Close taxonomy:** a close preceded by `bye` (either side) or by `error {fatal:true}`
  is **terminal** — the client MUST NOT auto-reconnect. Only a close with neither
  (socket drop, network loss) is **abnormal** and triggers reconnect. The client SHOULD
  also treat > 25 s without any engine frame (≈ 2 missed `time_ping`s) as an abnormal
  close and reconnect.
- **Reconnect:** the engine keeps session state (conversation context, `turn_id`/`say_id`
  counters) for `session_ttl_s` = 60 after an **abnormal** close (this is the reconnect
  grace window; a terminal close lets the engine discard immediately).
  Client reconnects with `hello {session_id, resume:true}` → `ready {resumed:true}`
  followed by `state {idle}` (§6: mic is off until re-asserted). `seq` counters reset
  to 0 both ways. Any in-flight `say_id` is implicitly cancelled — the client drops all
  playback buffers at reconnect; the engine MUST NOT replay audio. An unknown/expired
  `session_id` — or `resume:true` with no `session_id` at all — yields a **new**
  session (`resumed:false`, new id), not an error.
  If a second connection presents a live `session_id` (with or without `resume:true`),
  the engine sends `bye {reason:"superseded"}` on the old socket and continues on the
  new one; the `resume` flag then governs whether context is kept (`true`) or a fresh
  session is minted (`false`). v1's lax auth means possession of a live `session_id`
  is the only ownership proof — Phase 1.7 MUST gate supersession on authenticated
  ownership of the session.
- A `hello` on an already-established session (double-hello) is `bad_frame`-class:
  the engine responds with a non-fatal `error` and otherwise ignores it. The client
  never sends a second `hello` on an open socket.

## 10 · Sentence-chunked streaming TTS (who buffers what)

- **The engine buffers brain tokens → sentences.** It cuts a TTS segment at the first
  sentence boundary (`.`, `!`, `?`, `:`, newline — followed by whitespace/EOS) **at
  which the pending segment already contains ≥ 3 words** (an earlier boundary is
  ignored and its text carried forward), or at 24 words if no qualifying boundary
  appeared (run-on guard), or at brain-stream end. Each segment = one `say_id` (`say_start` → audio → `say_end`),
  same `turn_id`, strictly sequential **playback** (audio delivery may pipeline, §3).
- The first segment is the latency-critical one: the engine MUST start TTS synthesis
  for segment 1 as soon as its boundary is cut — it MUST NOT wait for the full brain
  reply. (This is what makes EOS→first-audio ≤ 1.2 s reachable: fast-TTFT brain +
  first-sentence synthesis, per PROBE_RESULTS.)
- Text sanitation (stripping the Windy Fly status banner, markdown, emoji) is the
  **engine's** job before synthesis; `say_start.text` is the exact text being spoken.
  A segment that is empty after sanitation is skipped entirely — it consumes no
  `say_id` and emits no messages.
- The client plays `say_id`s in ascending order; it MUST NOT insert gaps between chunks
  of one `say_id` (jitter-buffer ≤ 40 ms) and SHOULD NOT add silence between
  consecutive `say_id`s of a turn.

## 11 · Ordering & concurrency rules (explicit, so nobody guesses)

1. Client sends nothing before `hello`; no binary before `ready`. The engine silently
   ignores anything earlier (§1).
2. `heard` partials for a `turn_id` are superseded by later `heard` with the same
   `turn_id`; a `final:true` closes that transcript.
3. `tool_call`s within a turn are sequential from the engine (`call_id`s unique per
   session); the engine MAY speak (`say_*`) between tool calls of the same turn.
   **Timeout:** the client SHOULD bound tool execution at 30 s and then send
   `tool_result {ok:false, error:"timeout"}`; the engine MUST treat a `call_id` with
   no result after 45 s as failed and proceed. **Cancelled turns:** a `tool_call`
   already dispatched when its turn is cancelled MUST still be answered by the client
   (the exactly-one invariant holds); the engine MUST accept and discard results for
   cancelled turns. Duplicate or unknown-`call_id` `tool_result`s are ignored.
4. While `thinking`/`speaking`, incoming mic frames keep flowing and feed VAD only —
   the engine MUST NOT transcribe-and-queue a second turn behind the current one
   except via the barge-in path (§7). (No turn backlog in v1.)
5. After `say_cancel`, the engine flushes any queued segments of the same turn —
   a cancel kills the whole turn's remaining speech, not just one `say_id`. Late
   messages bearing a cancelled `turn_id` MAY still arrive at the client. Staleness
   rule: `turn_id` is monotonic, so any `heard`/`say_start`/`say_*` bearing a
   `turn_id` **lower than the highest the client has seen** is stale and MUST be
   dropped — except `tool_call`, which is always answered (rule 3).

## 12 · Worked timeline (informative)

```
C→E  text ws   hello {protocol, client}
E→C  text ws   ready {session_id:"s1", limits:{session_ttl_s:60, vad:{...}}}
E→C  text ws   state idle · time_ping ×3 / C pong ×3   (offset locked)
C→E  text ws   mic {on:true}                    → state listening
C→E  binary    mic frames (20 ms, seq 0,1,2…)
E→C  text ws   heard {"open the calc…", final:false, turn_id:1}
     …700 ms silence…                           → heard final:true, then state thinking
E→C  text ws   say_start {say_id:1, turn_id:1, text:"Opening the calculator."}
E→C  binary    tts chunks (say_id 1) …          → state speaking (at first chunk)
E→C  text ws   tool_call {call_id:"c1", tool:"open_app", args:{name:"calculator"}}
C→E  text ws   tool_result {call_id:"c1", ok:true}
E→C  text ws   say_end {say_id:1}               → state listening
      — user talks over a later reply (turn 2, speaking say_id 3) —
C→E  text ws   barge_in {say_id:3}              (playback paused locally ≤50 ms)
E→C  text ws   say_cancel {say_id:3, reason:"barge_in"} → state listening, turn_id:3 opens
```

## 13 · Non-goals in v1 (so they're not "missing", they're deferred)

Multi-party audio, opus/codec negotiation, engine-side playback-position tracking &
backpressure beyond TCP, mid-session sample-rate changes, wake-word events over this
protocol (wake is client-local in v1; the engine sees only post-wake audio; §4.6),
speaker identification, server-initiated recording consent flows, **barge-in during
`thinking`** (speech between EOS and first audio is dropped in v1 — candidate for v1.1),
client→engine `error` reporting, idle-session expiry policy beyond the reconnect grace.
