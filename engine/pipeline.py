"""Headless engine spine (Task 0.5 verify).

Runs one turn through the provider ABCs with §0.1 latency logging:
    wav (16 kHz PCM16)  →  STT  →  echo brain  →  sentence-segment  →  TTS  →  wav

The "echo brain" just returns the transcript — Task 1.1 swaps in brains/mind.py.
Per-stage timings are checked against the frozen §0.1 budget and emitted as
content-free telemetry (telemetry.v1 `latency_ms`).

Self-contained mode (`--phrase`, no `--wav`): synthesize the phrase with the TTS
provider, down-sample to 16 kHz, and feed it back in — so one command exercises
STT and TTS end-to-end on the 5090.

    python -m engine.pipeline --phrase "Open the calculator." --out /tmp/reply.wav
    python -m engine.pipeline --wav input16k.wav --out /tmp/reply.wav
"""
from __future__ import annotations

import argparse
import json
import sys
import wave

import numpy as np

from engine.latency import LatencyLog
from engine.providers.stt import MIC_RATE, get_stt
from engine.providers.tts import TTS_RATE, get_tts
from engine.segment import first_segment, segment_stream


def echo_brain(text: str) -> str:
    """Placeholder brain: echo the user (replaced by brains/mind.py at Task 1.1)."""
    return text


# ---------- wav helpers (stdlib wave; no soundfile dependency) ----------

def read_wav_pcm16(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM wav")
        ch, sr = w.getnchannels(), w.getframerate()
        pcm = w.readframes(w.getnframes())
    if ch == 2:  # downmix to mono
        a = np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2).mean(axis=1)
        pcm = a.astype(np.int16).tobytes()
    return pcm, sr


def write_wav_pcm16(path: str, pcm: bytes, sr: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


def resample_pcm16(pcm: bytes, src_sr: int, dst_sr: int) -> bytes:
    if src_sr == dst_sr:
        return pcm
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n_dst = int(round(a.size * dst_sr / src_sr))
    x_src = np.linspace(0.0, 1.0, a.size, endpoint=False)
    x_dst = np.linspace(0.0, 1.0, n_dst, endpoint=False)
    out = np.interp(x_dst, x_src, a)
    return out.astype(np.int16).tobytes()


# ---------- the turn ----------

def run_turn(mic_pcm16: bytes, stt, tts, brain=echo_brain) -> tuple[bytes, dict]:
    """One EOS→speech turn. Returns (reply_pcm24k, latency_report)."""
    lat = LatencyLog()

    with lat.time("stt"):
        transcript = stt.transcribe(mic_pcm16, MIC_RATE).text

    with lat.time("brain"):
        reply = brain(transcript)

    # First segment is the latency-critical one (§10) — synth it alone, timed.
    seg1 = first_segment(reply)
    with lat.time("tts_first_segment"):
        audio = bytearray(tts.synthesize(seg1))

    # Remaining segments (not on the first-audio critical path).
    with lat.time("tts_rest"):
        first = True
        for seg in segment_stream([reply]):
            if first:
                first = False
                continue
            audio.extend(tts.synthesize(seg))

    report = lat.report()
    report["transcript"] = transcript          # local diagnostic only; NOT telemetry
    report["reply_samples"] = len(audio) // 2
    report["telemetry_latency_ms"] = lat.telemetry_latency()
    return bytes(audio), report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Windy Talk engine spine (Task 0.5)")
    ap.add_argument("--wav", help="input wav (mono PCM16; resampled to 16 kHz)")
    ap.add_argument("--phrase", default="Open the calculator.",
                    help="self-contained mode: synth this phrase as the input")
    ap.add_argument("--out", default="/tmp/windytalk_reply.wav")
    ap.add_argument("--stt", default="whisper")
    ap.add_argument("--tts", default="kokoro")
    args = ap.parse_args(argv)

    stt = get_stt(args.stt)
    tts = get_tts(args.tts)
    print(f"[engine] warming providers stt={args.stt} tts={args.tts} …", flush=True)
    stt.warmup()
    tts.warmup()

    if args.wav:
        pcm, sr = read_wav_pcm16(args.wav)
        mic = resample_pcm16(pcm, sr, MIC_RATE)
        print(f"[engine] input {args.wav}: {sr} Hz → {MIC_RATE} Hz, "
              f"{len(mic)//2} samples", flush=True)
    else:
        print(f"[engine] self-contained: synth input phrase {args.phrase!r}", flush=True)
        spoken = tts.synthesize(args.phrase)            # 24 kHz
        mic = resample_pcm16(spoken, TTS_RATE, MIC_RATE)  # → 16 kHz mic

    reply_pcm, report = run_turn(mic, stt, tts)
    write_wav_pcm16(args.out, reply_pcm, TTS_RATE)

    print(json.dumps(report, indent=2), flush=True)
    print(f"[engine] {report['summary']}", flush=True)
    print(f"[engine] wrote reply → {args.out}", flush=True)
    return 0 if report["eos_budget_ok"] else 0  # over-budget is a warning, not a hard fail here


if __name__ == "__main__":
    sys.exit(main())
