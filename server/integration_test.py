"""
End-to-end integration test (run ON the client, e.g. Windy 0).

Streams a pre-recorded 16 kHz utterance to the Veron brain server (through the SSH
tunnel on localhost:8765), executes any tool call with the REAL hands, and plays
the spoken reply. Proves the full distributed loop:
  utterance -> Veron 5090 (STT+LLM+TTS) -> real desktop action here -> spoken reply.

  PYTHONPATH=. python3 server/integration_test.py /tmp/utter16k.pcm
"""
import asyncio
import json
import sys

import aiohttp

import agent
import audio
import config
from providers.local import _chat_tools

PCM = sys.argv[1] if len(sys.argv) > 1 else "/tmp/utter16k.pcm"


async def main():
    data = open(PCM, "rb").read()
    speaker = audio.Speaker(24000)
    url = config.LOCAL_SERVER_URL
    print(f"connecting to {url} …")
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(url, max_msg_size=0) as ws:
            await ws.send_json({"type": "hello", "tools": _chat_tools(),
                                "prompt": config.SYSTEM_PROMPT})

            async def feed():
                frame = 16000 * 30 // 1000 * 2
                for i in range(0, len(data), frame):
                    await ws.send_bytes(data[i:i + frame]); await asyncio.sleep(0.02)
                await ws.send_bytes(b"\x00" * frame * 30)   # trailing silence -> endpoint
            asyncio.create_task(feed())

            audio_bytes = 0
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    speaker.play(msg.data); audio_bytes += len(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    ev = json.loads(msg.data); t = ev.get("type")
                    if t == "heard":
                        print(f"  STT heard: {ev['text']!r}")
                    elif t == "tool_call":
                        print(f"  TOOL_CALL -> executing on THIS desktop: "
                              f"{ev['name']}({ev.get('args')})")
                        result = agent.call_tool(ev["name"], ev.get("args") or {})
                        print(f"    hands result: {result[:80]}")
                        await ws.send_json({"type": "tool_result", "id": ev["id"],
                                            "output": result})
                    elif t == "say":
                        print(f"  Windy says: {ev['text']!r}")
                    elif t == "audio_end":
                        break
            await asyncio.sleep(min(audio_bytes / 2 / 24000 + 0.5, 8))  # let reply play
    speaker.close(); audio.shutdown()
    print(f"\ndone — reply audio {audio_bytes/2/24000:.1f}s played through the speaker")


if __name__ == "__main__":
    asyncio.run(main())
