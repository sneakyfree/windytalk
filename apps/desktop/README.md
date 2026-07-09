# apps/desktop — the Windy Talk voice client (Electron/TS · voice-session.v1)

The canonical client — the agent's body on the machine. Clean split, mirroring
the engine (logic vs transport):

- `src/frames.ts` — §2 binary frame codec (mirrors the engine's `<BBHQI` header).
- `src/protocol.ts` — **`VoiceClient`**: the tested protocol core (pure TS, no DOM):
  message routing, playback discard policy (§3/§10), and the full barge-in state
  machine (§7: local pause → `barge_in` → 250 ms engine verdict / 400 ms client
  fence / 300 ms refractory). Timers/clock are injected so it's deterministic.
- `src/playback.ts` — WebAudio 24 kHz playback (pause/resume/clear + lip-sync level).
- `src/renderer.ts` — the glue: `VoiceClient` ⇄ engine WebSocket, AudioWorklet mic,
  playback, face, and `tool_call` → hands surface (Task 1.4) → `tool_result`.
- `renderer/capture-worklet.js` — §4 AudioWorklet capture: device→16 kHz, 20 ms
  frames, energy speech-onset for local barge-in. (MediaRecorder is forbidden, §4.2.)
- `renderer/index.html` + `renderer/face.js` — the animated face (ported from the
  prototype), driven by state/level.
- `electron/main.js` — the transparent, always-on-top shell.

## Run

```
npm install            # TypeScript, @types/node, electron
npm test               # 16 protocol-core tests (frame codec + barge-in machine)
npm run typecheck      # tsc --noEmit
npm start              # launch the app (needs the engine running — see below)
```

Point it at the engine (default `ws://127.0.0.1:8788`) and the hands surface
(default `http://127.0.0.1:8781`) via `WINDYTALK_ENGINE_URL` / `WINDYTALK_HANDS_URL`.

The engine runs on the 5090 (`python -m engine.server`); the hands surface runs
locally (`hands/surface.py`).

## Verification status

- **Machine-verified (this repo):** the protocol core — 16 tests covering the
  frame codec and the complete barge-in state machine — plus a clean typecheck of
  every module.
- **Yours to run (needs a mic + speakers):** the audio E2E — the §0.1 gate is
  *"barge-in works through speakers, not just headphones," p90 over ≥20 spoken
  turns.* That is inherently human-in-the-loop, exactly like the prototype's
  live-mic test. `WINDYTALK_SHOT=/tmp/face.png WINDYTALK_DEMO=speaking npm start`
  will also screenshot the face (works in a real desktop session; it hangs under
  the headless Claude Code sandbox, a known Electron limitation).
