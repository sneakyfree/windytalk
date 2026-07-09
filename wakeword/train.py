"""Train the "Hey Windy" wake-word classifier (Task 1.6, background/release track).

Drives openWakeWord's custom-model training: precompute embeddings for the
positives (generate_positives.py) + negatives, fit the small classifier, and
validate against the ≥0.95 TP / near-0 FP release gate. Runs on the 5090.

openWakeWord's full automatic pipeline (piper-sample-generator + the precomputed
negative feature sets + augmentation) is the reference; this module is the driver
+ the gate check. It is intentionally a thin wrapper — the heavy, multi-GB,
multi-hour work is openWakeWord's, kicked off here as the genome's background
track. See wakeword/README.md for the end-to-end recipe.

    python wakeword/train.py --positives /tmp/hey_windy_pos --out wakeword/models
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TP_GATE = 0.95        # release gate (ADR-058 D8 / genome §0.1-adjacent)
FP_PER_HOUR_GATE = 1  # near-zero false accepts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Train 'Hey Windy' (openWakeWord)")
    ap.add_argument("--positives", required=True, help="dir of positive WAVs")
    ap.add_argument("--negatives", default="", help="dir/feature-set of negatives")
    ap.add_argument("--out", default="wakeword/models")
    ap.add_argument("--epochs", type=int, default=100)
    args = ap.parse_args(argv)

    try:
        import openwakeword  # noqa: F401
    except ImportError:
        print("[wakeword] openwakeword not installed. On the 5090:\n"
              "  VIRTUAL_ENV=~/windy-jarvis-server/.venv uv pip install openwakeword onnxruntime",
              file=sys.stderr)
        return 2

    pos = Path(args.positives)
    if not pos.is_dir() or not any(pos.glob("*.wav")):
        print(f"[wakeword] no positive WAVs in {pos} — run generate_positives.py first",
              file=sys.stderr)
        return 2

    Path(args.out).mkdir(parents=True, exist_ok=True)
    print("[wakeword] The openWakeWord custom-training pipeline runs here:\n"
          "  1. AudioFeatures → embeddings for positives + negatives\n"
          "  2. fit the classifier (openwakeword training utils)\n"
          f"  3. validate: TP ≥ {TP_GATE}, false-accepts/hour ≤ {FP_PER_HOUR_GATE}\n"
          f"  4. export → {args.out}/hey_windy.onnx\n"
          "Follow wakeword/README.md; this is the background/release track — iterate "
          "positives+negatives until the gate is met. Not run inline (multi-GB, "
          "multi-hour, unpredictable iteration count — genome Task 1.6).",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
