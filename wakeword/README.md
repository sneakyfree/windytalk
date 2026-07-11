# wakeword/ — "Hey Windy" (ADR-058 D8 · genome Task 1.6)

The custom wake word. openWakeWord custom-model pipeline: synthetic positives +
negatives → train a small classifier on precomputed audio embeddings → validate
**≥ 0.95 true-positive / near-0 false-positive** → export ONNX → run client-side.

**This is a background/release track, by design** (genome §5, Task 1.6): the
≥0.95 gate holds for *release*, not for progress. Stock `hey_jarvis` is the
dev-only fallback so nothing blocks on training. Runtime confirmed on the 5090:
openWakeWord 0.4.0 installed; `Model(wakeword_model_paths=[...],
inference_framework="onnx")` is the load API (0.4.0 — note it's *not* the
`wakeword_models=` kwarg from newer docs).

## The recipe (reproducible, on the 5090)

Deps (Veron `~/windy-jarvis-server/.venv`): `openwakeword==0.4.0`, `onnxruntime`,
plus the training extras and [piper-sample-generator](https://github.com/rhasspy/piper-sample-generator)
for positives. A piper voice (`en_US-lessac-medium.onnx`) is already on the box.

1. **Positives** — `python wakeword/generate_positives.py` synthesizes many
   "Hey Windy" utterances across piper voices/speeds/pitches, then augments with
   room impulse responses + background noise (openWakeWord's augmentation set).
   Diversity here is what buys generalization.
2. **Negatives** — hard negatives (phonetically near "Hey Windy": "hey wendy",
   "hey mindy", "a windy day") + openWakeWord's precomputed negative feature
   sets (ACAV100M etc., multi-GB) so the model sees hours of not-the-wake-word.
3. **Features** — precompute melspectrogram → embedding features for both sets
   (openWakeWord's `AudioFeatures`); training is on embeddings, not raw audio.
4. **Train** — `python wakeword/train.py` drives openWakeWord's training to fit
   the small classifier; early-stop on the validation TP/FP gate.
5. **Validate** — held-out positives (TP ≥ 0.95) + a long negative stream
   (false-accepts/hour ≈ 0). Iterate 1–4 until the gate is met (unpredictable
   count — this is why it's a background track).
6. **Export** — `hey_windy.onnx`; drop it in `wakeword/models/`.

## Client integration

Wake is **client-side** (voice-session.v1 §13; the engine only sees post-wake
audio). The **gate is now built and wired**: `apps/desktop/src/wake.ts` holds the
pure, unit-tested `WakeGate` state machine (asleep → send nothing; wake →
forward real mic frames; grace-timeout → back to sleep — ported from
`reference/wake.py`), plus a `WakeDetector` interface and `loadWakeDetector()`.
`renderer.ts` routes AudioWorklet mic frames through the gate when hands-free is
on (`window.wt.setWake(true)`), off by default (push-to-talk). The gate is
verified with an injected fake detector + clock in `apps/desktop/test/wake.test.ts`.

**The one remaining step** is the detector itself: run the three ONNX stages
(melspectrogram → embedding → `hey_windy` classifier) via **onnxruntime-web** in
`loadWakeDetector()`. It's a drop-in — the gate needs no change. Until the model
is trained + bundled, `loadWakeDetector()` returns `null` and the app honestly
stays in push-to-talk (surfacing "model not bundled yet") rather than faking it.

## Status

Groundwork + the client gate done: pipeline scripts + recipe here; openWakeWord
runtime verified on the 5090; `WakeGate` built, wired, and tested in the client.
**Not done (the background/release work):** the training run to ≥0.95, exporting
`hey_windy.onnx`, and the onnxruntime-web detector inside `loadWakeDetector()`.
Kick training off with the scripts below on the 5090 when ready — it runs
independently of the rest of the build.
