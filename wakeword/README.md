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

## Client integration (follow-up)

Wake is **client-side** (voice-session.v1 §13; the engine only sees post-wake
audio). In the Electron client, run the three ONNX stages (melspectrogram →
embedding → `hey_windy` classifier) in the renderer via **onnxruntime-web**, fed
by the same AudioWorklet mic frames. On a wake, the client shows the §4.6
listening affordance (≤300 ms budget) and starts streaming to the engine. Ship
`hey_jarvis` as the dev fallback until `hey_windy` clears the gate; remove the
fallback at release (D8).

## Status

Groundwork done: pipeline scripts + recipe here; openWakeWord runtime verified on
the 5090. **Not done (the background/release work):** the training run to ≥0.95
and the onnxruntime-web client integration. Kick training off with the scripts
below on the 5090 when ready — it runs independently of the rest of the build.
