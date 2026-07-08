"""
Windy Jarvis — always-on conversational voice control for the Linux desktop.

Provider-agnostic main loop: it picks a "brain" (Gemini Live, OpenAI Realtime, …),
wires it to the microphone, speaker, and the desktop-control tools, and reconnects
if the session drops. Pivot brains with --provider or JARVIS_PROVIDER.

  ./run.sh                       # uses JARVIS_PROVIDER (default: gemini)
  ./run.sh --provider openai
  python3 jarvis.py --list       # show providers and whether each is ready
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


async def main(provider_name):
    brain = providers.get(provider_name)
    ok, why = brain.ready
    if not ok:
        sys.exit(f"Provider '{brain.name}' isn't ready: {why}")

    mic = audio.Mic(brain.input_rate, config.CHUNK_MS)
    speaker = audio.Speaker(brain.output_rate)
    state = {"stop": False}

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: state.update(stop=True))

    def log(line):
        print(f"  {line}")

    print(f"\n  \033[1mWindy Jarvis\033[0m — brain=\033[36m{brain.name}\033[0m "
          f"(mic {brain.input_rate}Hz / out {brain.output_rate}Hz)")
    print("  Listening. Just talk: \"open Firefox\", \"search the web for the weather\", "
          "\"what's on my screen?\". Ctrl-C to quit.\n")

    while not state["stop"]:
        try:
            await brain.run(mic, speaker, _dispatch, log)
        except Exception as e:
            if state["stop"]:
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
    ap.add_argument("--list", action="store_true", help="list providers and readiness")
    a = ap.parse_args()
    if a.list:
        _list(); sys.exit(0)
    try:
        asyncio.run(main(a.provider))
    except KeyboardInterrupt:
        pass
