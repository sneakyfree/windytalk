"""
Windy Jarvis — always-on conversational voice control for the Linux desktop.

Provider-agnostic main loop: it picks a "brain" (local Veron-5090, Gemini, OpenAI),
wires it to the microphone, speaker, and the desktop-control tools, and reconnects
if the session drops. Pivot brains with --provider or JARVIS_PROVIDER.

  ./run.sh                       # local (Veron) brain, default
  ./run.sh --provider gemini
  python3 jarvis.py --list       # show providers and whether each is ready
  python3 jarvis.py --ui         # also serve the Electron face app's status bridge
Stop: Ctrl-C
"""
import argparse
import asyncio
import signal
import sys

import agent
import audio
import config
import providers


def _dispatch(name, args):
    return agent.call_tool(name, args)


async def main(provider_name, use_ui=False):
    brain = providers.get(provider_name)
    ok, why = brain.ready
    if not ok:
        sys.exit(f"Provider '{brain.name}' isn't ready: {why}")

    mic = audio.Mic(brain.input_rate, config.CHUNK_MS)
    speaker = audio.Speaker(brain.output_rate)
    st = {"stop": False, "connected": False, "thinking": False}

    bridge = None
    if use_ui:
        from ui_bridge import UIBridge
        bridge = UIBridge()
        await bridge.start()

        def on_cmd(c):
            if c.get("type") == "mic":
                mic.paused = not c.get("on", True)
        bridge.on_command = on_cmd

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: st.update(stop=True))

    def log(line):
        print(f"  {line}")
        if not bridge:
            return
        if line.startswith("You:"):
            st["thinking"] = True; st["connected"] = True
            bridge.emit({"type": "heard", "text": line[4:].strip()})
        elif line.startswith("Windy:"):
            bridge.emit({"type": "say", "text": line[6:].strip()})
        elif "connected" in line.lower() or "ready" in line.lower():
            st["connected"] = True

    async def ui_poll():
        cur = None
        while not st["stop"]:
            if speaker.speaking:
                st["thinking"] = False
            state = ("offline" if not st["connected"] else
                     "paused" if mic.paused else
                     "speaking" if speaker.speaking else
                     "thinking" if st["thinking"] else "listening")
            if state != cur:
                bridge.status(state); cur = state
            if speaker.speaking:
                bridge.level(min(speaker.level * 4.0, 1.0))
            await asyncio.sleep(0.05)

    if bridge:
        asyncio.create_task(ui_poll())

    print(f"\n  \033[1mWindy Jarvis\033[0m — brain=\033[36m{brain.name}\033[0m "
          f"(mic {brain.input_rate}Hz / out {brain.output_rate}Hz)"
          f"{'  [UI bridge :8770]' if bridge else ''}")
    print("  Listening. Just talk: \"open Firefox\", \"search the web for the weather\", "
          "\"what's on my screen?\". Ctrl-C to quit.\n")

    while not st["stop"]:
        try:
            await brain.run(mic, speaker, _dispatch, log)
        except Exception as e:
            st["connected"] = False
            if st["stop"]:
                break
            print(f"  \033[33m[reconnecting after: {type(e).__name__}: {e}]\033[0m")
            await asyncio.sleep(1)

    mic.close(); speaker.close(); audio.shutdown()
    print("\n  Bye.")


def _list():
    print("Providers:")
    for n in providers.names():
        try:
            ok, why = providers.get(n).ready
            print(f"  {'✓' if ok else '✗'} {n:8s} {'' if ok else '— ' + why}")
        except Exception as e:
            print(f"  ? {n:8s} — {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=config.PROVIDER,
                    help=f"brain to use ({', '.join(providers.names())})")
    ap.add_argument("--ui", action="store_true", help="serve the Electron face bridge on :8770")
    ap.add_argument("--list", action="store_true", help="list providers and readiness")
    a = ap.parse_args()
    if a.list:
        _list(); sys.exit(0)
    try:
        asyncio.run(main(a.provider, use_ui=a.ui))
    except KeyboardInterrupt:
        pass
