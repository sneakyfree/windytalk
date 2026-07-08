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

## Desktop app (face + button)

A little Electron window with an animated face (blinks, glances up when thinking,
mouth lip-syncs to the reply), a big mic on/off button, and status lights
(listening / thinking / speaking / waiting / offline, plus a "Veron online" dot).

```
bash scripts/install-launcher.sh    # adds "Windy Jarvis" to the GNOME app grid
gtk-launch windy-jarvis             # or just double-click it in Activities
# dev: cd desktop && npm install && npm start
```

The app is a thin shell: it spawns the Python agent (`run.sh --ui`) and reflects
its state over a localhost websocket (`ui_bridge.py`, port 8770). It reimplements
nothing — audio, hands, and the brain are unchanged.

## Hands-free ("Hey Jarvis")

```
./run.sh --wake        # or JARVIS_WAKE=1, or --ui --wake with the app
```
The client stays asleep and streams nothing until it hears **"Hey Jarvis"** locally
(openWakeWord, on CPU) — then it listens for your command and drifts back to sleep.
Zero idle streaming. The face shows a dozing "Say Hey Jarvis" state while armed.

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

## Sharing copies (licensed, remotely controlled)

The brain runs on the Veron-5090, reachable at a public, license-gated endpoint
(`wss://jarvis.thewindstorm.uk`) — so a copy needs a key you issue, and you control
it live from the box. On Veron:

```
python3 server/admin.py new "Bill"          # mint a key
python3 server/admin.py list                # see licenses + who's connected NOW
python3 server/admin.py lock  WINDY-XXXXXX "Text Grant to unlock."   # freeze a copy
python3 server/admin.py unlock WINDY-XXXXXX
python3 server/admin.py expire WINDY-XXXXXX 2026-08-01               # time-limit it
```

Build + hand out a copy:
```
scripts/package-client.sh                   # -> ~/windy-jarvis-client.tar.gz (no server/, no keys)
# send the tarball + their key; they run:
tar xzf windy-jarvis-client.tar.gz && cd windy-jarvis && scripts/install-client.sh WINDY-XXXXXX
```
The installer pulls system deps (ydotool, flameshot, AT-SPI, portaudio, node), Python
deps, the Electron app, writes their `.env` (key + endpoint), and adds the app-grid
launcher. When they run it, you see them online and hold the kill switch. A locked
copy shows a red face and Windy speaks your unlock message aloud.

## Roadmap

- **Done:** local Veron-5090 brain (free); provider pivot (local / Gemini / OpenAI);
  desktop face app; "Hey Jarvis" wake word; GNOME launcher; **public licensed endpoint
  + remote lock/expire/who's-online + client packaging/installer**.
- **Next:** a single-binary bundle (PyInstaller/AppImage) so users skip even the
  installer; concurrent-session limits per key (anti-sharing); a web admin dashboard.
- **Later:** AWS Nova Sonic adapter; cross-platform hands (macOS `agent-desktop`,
  Windows UIAutomation) so non-Linux friends can run it; custom "Hey Windy" wake model.
