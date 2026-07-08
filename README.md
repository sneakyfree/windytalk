# Windy Jarvis

Always-on, hands-free, interruptible **voice control of this Fedora desktop** — the
Linux answer to the "GPT Realtime 2 Jarvis" demos (which were macOS-only). You talk;
Windy hears you, talks back, and actually operates the machine: opens apps, searches
the web, types, presses keys, clicks UI elements, reads the screen, runs commands.

No start/stop dictation. It listens continuously, you can talk over it, and there's no
copy-paste round trip — the voice model calls desktop tools directly.

## Architecture

```
  microphone ─▶ OpenAI Realtime API (gpt-realtime-2.1-mini)   ← the "brain"
                speech-to-speech · server VAD · barge-in · tool calling
                        │  (function calls)
                        ▼
                    agent.py  ── tool schemas + dispatch
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

## Setup

1. `cp .env.example .env` and put your OpenAI key in it.
2. `./run.sh`

That's it — deps (aiohttp, PyAudio, numpy, PyGObject, ydotool, flameshot) are already
on Windy 0. `run.sh` starts the ydotool daemon, enables AT-SPI, and launches.

## Try saying

- "Open Firefox." / "Open the calculator."
- "Search the web for tomorrow's weather in Salt Lake City."
- "Type 'hello from Windy' and press enter."
- "What's on my screen right now?"
- "Press control shift T." / "Alt-tab."

## Verify the hands without a key or mic

```
python3 selftest.py                          # safe: read/launch/screenshot/mouse
JARVIS_SELFTEST_TYPING=1 python3 selftest.py # also types into a text editor
```

## Notes / knobs

- **Cost:** `gpt-realtime-2.1-mini` ≈ $10/$20 per 1M audio in/out (~1–3¢ per command).
  Streaming the mic continuously bills input while listening — a wake-word gate
  (openWakeWord) is the planned Phase-2 add to make idle cost zero.
- **Session cap:** OpenAI Realtime sessions max out at 60 min; `jarvis.py` auto-reconnects.
- **Swap the brain:** set `JARVIS_MODEL=gpt-realtime-2.1` for best quality, or point
  `WS_URL`/session config at Google Gemini Live (free tier) — see the research memo.
- **Safety:** `run_shell` blocks destructive patterns unless `JARVIS_ALLOW_DANGEROUS=1`.
- **Electron/Chromium apps** expose empty accessibility trees unless launched with
  `ACCESSIBILITY_ENABLED=1` — `read_screen`/`click_element` won't see inside them otherwise.

## Roadmap

- **Phase 2:** openWakeWord "Hey Windy" gate (zero idle cost) + push-to-talk toggle.
- **Phase 1.5:** optional `agent-sh/computer-use-linux` MCP for richer semantic control.
- **Phase 3:** fully-local brain (Kyutai Unmute / Pipecat) on the PumpMe GPU box, $0/hr.
