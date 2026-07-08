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

## Setup — local brain (default, free, on the Veron-1-5090)

The default brain runs entirely on the RTX 5090 in the Veron 1 box: faster-whisper
(STT) → Ollama `qwen2.5:7b-instruct` (LLM + tool calling) → kokoro-onnx (TTS). No
cloud, no API key, ~$0/hour. The server is a persistent systemd service on Veron;
the client reaches it through an SSH tunnel that `run.sh` opens automatically.

```
./run.sh          # opens the tunnel to wg-veron, then starts listening
```

That's it. `run.sh` starts ydotoold, enables AT-SPI, tunnels `localhost:8765` to the
Veron server, and launches. Deps (aiohttp, PyAudio, PyGObject, ydotool, flameshot)
are already on Windy 0. Check brains: `python3 jarvis.py --list`.

**The brain server** lives in `server/` and runs on Veron 1 at
`~/windy-jarvis-server/` as the `windy-jarvis` user service:
```
systemctl --user status windy-jarvis      # on Veron
server/veron_server.py                     # the STT→LLM→TTS websocket server
server/test_client.py "open the calculator"    # headless loopback test (on Veron)
server/integration_test.py /tmp/utter16k.pcm   # full distributed test (on a client)
```

### Cloud brains (optional)

`./run.sh --provider gemini` or `--provider openai` — put a key in `.env` first
(Gemini is free: https://aistudio.google.com/apikey).

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
