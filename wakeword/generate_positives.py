"""Generate synthetic "Hey Windy" positive samples for wake-word training (Task 1.6).

Uses piper-sample-generator (or a plain piper voice) to synthesize many spoken
"Hey Windy" utterances across speaking rates, then leaves augmentation (RIR +
noise) to the openWakeWord training step. Output: 16 kHz mono WAVs in --out.

Run on the 5090 (models/tooling live in ~/windy-jarvis-server/). This is the
front end of the pipeline in wakeword/README.md; it does NOT train — see train.py.

    python wakeword/generate_positives.py --n 2000 --out /tmp/hey_windy_pos
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PHRASE = "Hey Windy"
PIPER_VOICE_DEFAULT = str(Path.home() / "windy-jarvis-server" / "en_US-lessac-medium.onnx")


def have(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Synthesize 'Hey Windy' positives")
    ap.add_argument("--n", type=int, default=2000, help="number of samples")
    ap.add_argument("--out", default="/tmp/hey_windy_pos")
    ap.add_argument("--voice", default=PIPER_VOICE_DEFAULT)
    ap.add_argument("--phrase", default=PHRASE)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Preferred: piper-sample-generator gives voice/rate/pitch diversity in one call.
    if have("piper_sample_generator") or _bin_exists("piper-sample-generator"):
        cmd = ["piper-sample-generator", "--text", args.phrase,
               "--max-samples", str(args.n), "--output-dir", str(out)]
        print("[wakeword] piper-sample-generator:", " ".join(cmd), flush=True)
        subprocess.run(cmd, check=True)
        return 0

    # Fallback: a single piper voice across a spread of speaking rates. Less
    # diverse — good enough to smoke the pipeline; use the sample-generator for
    # a release-quality set.
    if not _bin_exists("piper") and not have("piper"):
        print("[wakeword] neither piper-sample-generator nor piper found. Install "
              "piper-sample-generator (see wakeword/README.md) on the 5090.",
              file=sys.stderr)
        return 2
    rates = [0.85, 0.95, 1.0, 1.1, 1.25]
    per = max(1, args.n // len(rates))
    made = 0
    for rate in rates:
        for i in range(per):
            wav = out / f"heywindy_{rate:.2f}_{i:05d}.wav"
            cmd = ["piper", "--model", args.voice, "--length_scale", str(1.0 / rate),
                   "--output_file", str(wav)]
            subprocess.run(cmd, input=args.phrase, text=True, check=True,
                           capture_output=True)
            made += 1
    print(f"[wakeword] wrote {made} positives → {out}", flush=True)
    return 0


def _bin_exists(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


if __name__ == "__main__":
    raise SystemExit(main())
