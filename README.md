# Windy Jarvis

Always-on, hands-free, interruptible **voice control of the Linux desktop** — the
Linux answer to the "GPT Realtime 2 Jarvis" demos (which were macOS-only). You talk;
Windy hears you, talks back, and actually operates the machine: opens apps, searches
the web, types, presses keys, clicks UI elements, reads the screen, runs commands.

No start/stop dictation. It listens continuously, you can talk over it, and there's no
copy-paste round trip — the voice model calls desktop tools directly.

**Bring any brain.** Windy Jarvis is provider-pluggable: pivot between Google Gemini
Live, OpenAI Realtime, and (coming) AWS Nova Sonic, your own PumpMe GPU cloud, and
fully-local models — same voice, same hands, one flag. You supply the key(s) for
whichever you want.

## Architecture

```
  microphone ─▶  BRAIN (swappable, providers/)                 ← speech-to-speech
                 ├─ gemini   Google Gemini Live  (free tier)      VAD · barge-in · tools
                 ├─ openai   OpenAI Realtime      (gpt-realtime-2.1-mini)
                 └─ …aws / pumpme / local (roadmap)
                        │  (function calls, provider-agnostic)
                        ▼
                    agent.py  ── 12 tools + dispatch
                        │
                        ▼
                     hands.py  ── the Linux "hands"
                     ├─ ydotool (uinput)      keyboard + mouse, Wayland-native
                     ├─ AT-SPI2               read screen + click elements semantically
                     ├─ gtk-launch / xdg-open apps, URLs, web search
                     └─ flameshot             screenshots (grim doesn't work on GNOME)
                        │
                        ▼
                  speaker ◀─ Windy's spoken reply
```

`audio.py` (mic + interruptible playback) and `hands.py` never change when you swap
brains. A brain is one file in `providers/` implementing the `Brain` interface in
`providers/base.py`; register it in `providers/__init__.py` and it's selectable.

## Setup

1. `cp .env.example .env`
2. Put a key in it — Gemini is free: get one at https://aistudio.google.com/apikey
3. `./run.sh`                     # or `./run.sh --provider openai`

Check readiness anytime: `python3 jarvis.py --list`

Deps (aiohttp, google-genai, PyAudio, PyGObject, ydotool, flameshot) are already on
Windy 0. `run.sh` starts the ydotool daemon, enables AT-SPI, and launches.

## Try saying

- "Open Firefox." / "Open the calculator."
- "Search the web for tomorrow's weather in Salt Lake City."
- "Type 'hello from Windy' and press enter."
- "What's on my screen right now?"
- "Press control shift T." / "Alt-tab."

## Verify the hands without a key or mic

```
python3 selftest.py    # opens Calculator and computes 7+5=12 by clicking via AT-SPI
```

## Provider notes

| Brain | Native S2S | Cost | Key |
|-------|-----------|------|-----|
| **gemini** | yes | free tier, then ~$0.30/hr in + $1.08/hr out | aistudio.google.com/apikey |
| **openai** | yes | ~1–3¢/command (gpt-realtime-2.1-mini) | platform.openai.com/api-keys |
| aws (roadmap) | yes | ~$0.85/hr | Bedrock (Nova 2 Sonic) |
| pumpme / local (roadmap) | no → chained STT+LLM+TTS via Pipecat | $0/hr | your GPU |

- Gemini wants 16 kHz mic in / 24 kHz out; OpenAI uses 24 kHz both ways. `audio.py`
  opens the mic/speaker at whatever the selected brain declares — no manual config.
- OpenAI Realtime caps sessions at 60 min; the main loop auto-reconnects.
- **Safety:** `run_shell` blocks destructive patterns unless `JARVIS_ALLOW_DANGEROUS=1`.
- **Electron/Chromium apps** expose empty accessibility trees unless launched with
  `ACCESSIBILITY_ENABLED=1`.

## Roadmap

- **Now:** Gemini + OpenAI pivot, Linux, bring-your-own-key.
- **Next:** AWS Nova Sonic adapter; PumpMe/local brains via a Pipecat STT+LLM+TTS
  pipeline (text models wrapped into voice); openWakeWord "Hey Windy" gate (zero idle
  cost); a settings UI + downloadable packaged app (Electron, matching Windy Word).
- **Later:** cross-platform hands (macOS `agent-desktop`, Windows UIAutomation).
