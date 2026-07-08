"""
Headless loopback test for veron_server.py (run ON Veron 1, no mic needed).

Synthesizes a spoken command with Kokoro, streams it to the brain server as if it
were microphone audio, answers any tool call, and saves the spoken reply. Proves
the STT -> LLM(tools) -> TTS loop end-to-end.

  .venv/bin/python server/test_client.py "open the calculator"
"""
import asyncio
import json
import os
import sys

import numpy as np
import soundfile as sf
import websockets
from kokoro_onnx import Kokoro

HERE = os.path.dirname(os.path.abspath(__file__))
URL = os.environ.get("WJ_URL", "ws://localhost:8765")
COMMAND = sys.argv[1] if len(sys.argv) > 1 else "open the calculator"

TOOLS = [
    {"type": "function", "function": {"name": "open_app",
     "description": "Launch a desktop application by name.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                    "required": ["name"]}}},
    {"type": "function", "function": {"name": "web_search",
     "description": "Search the web for a query.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"]}}},
]


def make_speech_16k(text: str) -> bytes:
    k = Kokoro(os.path.join(HERE, "kokoro-v1.0.onnx"), os.path.join(HERE, "voices-v1.0.bin"))
    samples, sr = k.create(text, voice="am_michael", speed=1.0, lang="en-us")  # 24k
    # linear resample 24k -> 16k for the mic path
    n_out = int(len(samples) * 16000 / sr)
    x = np.linspace(0, len(samples), n_out, endpoint=False)
    res = np.interp(x, np.arange(len(samples)), samples)
    return (np.clip(res, -1, 1) * 32767).astype(np.int16).tobytes()


async def main():
    print(f"→ speaking to server: {COMMAND!r}")
    speech = make_speech_16k(COMMAND)
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "hello", "tools": TOOLS}))

        async def send_audio():
            frame = 16000 * 30 // 1000 * 2  # 30 ms
            for i in range(0, len(speech), frame):
                await ws.send(speech[i:i + frame]); await asyncio.sleep(0.02)
            await ws.send(b"\x00" * frame * 30)  # ~900 ms silence -> endpoint
        asyncio.create_task(send_audio())

        reply = bytearray()
        got_tool = got_say = False
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=45)
            except asyncio.TimeoutError:
                print("timeout waiting for server"); break
            if isinstance(msg, (bytes, bytearray)):
                reply.extend(msg); continue
            ev = json.loads(msg)
            t = ev.get("type")
            if t == "ready":
                print("server ready")
            elif t == "heard":
                print(f"  STT heard: {ev['text']!r}")
            elif t == "tool_call":
                got_tool = True
                print(f"  TOOL_CALL: {ev['name']}({ev['args']})")
                await ws.send(json.dumps({"type": "tool_result", "id": ev["id"],
                                          "output": f"Opening {ev['args'].get('name', '')}"}))
            elif t == "say":
                got_say = True
                print(f"  Windy says: {ev['text']!r}")
            elif t == "audio_end":
                break
        if reply:
            sf.write("/tmp/wj_reply.wav", np.frombuffer(bytes(reply), dtype=np.int16), 24000)
            print(f"  saved reply audio: /tmp/wj_reply.wav ({len(reply)/2/24000:.1f}s)")
        print(f"\nRESULT: tool_call={got_tool}  spoken_reply={got_say}  "
              f"audio_bytes={len(reply)}")


if __name__ == "__main__":
    asyncio.run(main())
