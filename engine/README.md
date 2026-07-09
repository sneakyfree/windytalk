# engine/ — the voice brain-stem · **Python, server-side (GPU)**

STT/TTS provider ABCs (ADR-044), VAD endpointing, latency measurement, sentence
segmentation, and the headless turn spine. Speaks `contracts/voice-session.v1.md`
to clients. Never contains client code.

## Modules (Task 0.5)

- `providers/stt/` — `STTProvider` ABC + `whisper` (local faster-whisper, 5090)
  and `transcribe` (AWS Transcribe Streaming, forced-honest stub). `get_stt(name)`.
- `providers/tts/` — `TTSProvider` ABC + `kokoro` (local kokoro-onnx) and `cloud`
  (forced-honest stub). Output invariant: PCM16 mono 24 kHz. `get_tts(name)`.
- `vad.py` — `Segmenter`, 20 ms/16 kHz frames, endpointing per voice-session.v1 §6
  (150/700 ms defaults). Injectable detector for testing without webrtcvad.
- `segment.py` — §10 sentence chunking (`first_segment`, `segment_stream`).
- `latency.py` — the frozen §0.1 budget, measured; composes EOS→first-audio and
  produces the content-free `latency_ms` telemetry shape.
- `pipeline.py` — the headless spine: wav → STT → echo brain → TTS with §0.1
  latency logging. `python -m engine.pipeline --phrase "…"` or `--wav in.wav`.

`session.py` (voice-session.v1 turn loop) and `server.py` (the websocket server)
arrive in **Phase 1** with the client — Phase 0 is spine + contracts only.

## Runtime (the 5090 path)

Deps: `faster-whisper`, `kokoro-onnx`, `webrtcvad`, `numpy`. The model files
(`kokoro-v1.0.onnx`, `voices-v1.0.bin`) live outside the repo; point at them with
`WINDYTALK_KOKORO_MODEL` / `WINDYTALK_KOKORO_VOICES` (default `~/windy-jarvis-server/`).

**cuDNN gotcha (carried from the prototype):** CTranslate2/faster-whisper aborts
unless `LD_LIBRARY_PATH` includes the venv's `nvidia/*/lib`. Launch via a wrapper
that sets it (see `reference/server/run_server.sh`).

Verified on the Veron 5090 (Task 0.5): self-contained turn EOS→first-audio
**524 ms**; `--wav` file input **454 ms** — both well under the 1200 ms §0.1 gate.
`whisper` imports lazily, so this package imports and unit-tests on any machine.
