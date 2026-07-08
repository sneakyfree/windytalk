"""
Local brain — talks to the Windy Jarvis brain server on the Veron-1-5090 box
(faster-whisper + Ollama + Kokoro on the GPU). Free, open-source, no cloud.

Transport is our own websocket protocol (see server/veron_server.py). The desktop
tools still execute HERE on the client via hands.py; the server only decides which
to call. Point it at the server with JARVIS_LOCAL_URL (default: an SSH tunnel on
localhost, so `ssh -N -L 8765:localhost:8765 wg-veron` just works).
"""
import asyncio
import json

import aiohttp

import agent
import config
from providers.base import Brain


def _chat_tools():
    """agent.TOOLS is Realtime-flat; Ollama/chat wants {type,function:{...}}."""
    out = []
    for t in agent.TOOLS:
        out.append({"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("parameters", {"type": "object", "properties": {}})}})
    return out


class LocalBrain(Brain):
    name = "local"
    input_rate = 16000   # server/Whisper native
    output_rate = 24000  # Kokoro output

    def __init__(self, url: str | None = None):
        self.url = url or config.LOCAL_SERVER_URL
        self.locked = False

    @property
    def ready(self):
        if not self.url:
            return (False, "set JARVIS_LOCAL_URL (or SSH-tunnel the Veron server)")
        return (True, "")

    async def run(self, mic, speaker, dispatch, log):
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(self.url, max_msg_size=0, heartbeat=20) as ws:
                await ws.send_json({"type": "hello", "tools": _chat_tools(),
                                    "prompt": config.SYSTEM_PROMPT,
                                    "license": config.LICENSE})

                async def pump():
                    while True:
                        data = await mic.read()
                        if self.locked:               # locked → stream silence
                            data = b"\x00" * len(data)
                        await ws.send_bytes(data)

                pump_task = asyncio.create_task(pump())
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            speaker.play(msg.data)              # 24 kHz reply audio
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            ev = json.loads(msg.data)
                            t = ev.get("type")
                            if t == "tool_call":
                                result = dispatch(ev.get("name", ""), ev.get("args") or {})
                                log(f"[tool] {ev.get('name')}({ev.get('args')}) -> {result[:80]}")
                                await ws.send_json({"type": "tool_result",
                                                    "id": ev.get("id"), "output": result})
                            elif t == "interrupted":
                                speaker.clear()                 # barge-in
                            elif t == "heard":
                                log(f"You: {ev.get('text', '').strip()}")
                            elif t == "say":
                                log(f"Windy: {ev.get('text', '').strip()}")
                            elif t == "ready":
                                log("connected to Veron brain")
                            elif t == "locked":
                                self.locked = True
                                log(f"🔒 {ev.get('message', 'Locked by Grant.')}")
                            elif t == "unlocked":
                                self.locked = False
                                log("🔓 Unlocked.")
                            elif t == "denied":
                                log(f"⛔ {ev.get('message', 'No valid license.')}")
                                import asyncio as _a
                                await _a.sleep(8)               # back off, don't hammer
                                return
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                finally:
                    pump_task.cancel()
