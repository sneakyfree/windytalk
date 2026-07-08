"""OpenAI Realtime brain — native speech-to-speech over a websocket."""
import base64
import json

import aiohttp

import agent
import config
from providers.base import Brain


class OpenAIRealtimeBrain(Brain):
    name = "openai"
    input_rate = 24000
    output_rate = 24000

    def __init__(self, model: str | None = None, voice: str | None = None):
        self.model = model or config.OPENAI_MODEL
        self.voice = voice or config.OPENAI_VOICE

    @property
    def ready(self):
        if not config.OPENAI_API_KEY:
            return (False, "set OPENAI_API_KEY in .env")
        return (True, "")

    def _session_update(self):
        return {"type": "session.update", "session": {
            "modalities": ["audio", "text"],
            "instructions": config.SYSTEM_PROMPT,
            "voice": self.voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {"type": "server_vad", "threshold": 0.5,
                               "prefix_padding_ms": 300, "silence_duration_ms": 600},
            "tools": agent.TOOLS,
            "tool_choice": "auto",
        }}

    async def run(self, mic, speaker, dispatch, log):
        url = f"wss://api.openai.com/v1/realtime?model={self.model}"
        headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}",
                   "OpenAI-Beta": "realtime=v1"}
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(url, headers=headers, max_msg_size=0) as ws:
                await ws.send_json(self._session_update())

                async def pump():
                    while True:
                        data = await mic.read()
                        await ws.send_json({"type": "input_audio_buffer.append",
                                            "audio": base64.b64encode(data).decode()})

                import asyncio
                pump_task = asyncio.create_task(pump())
                try:
                    async for msg in ws:
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        ev = json.loads(msg.data)
                        t = ev.get("type", "")
                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            speaker.play(base64.b64decode(ev["delta"]))
                        elif t == "input_audio_buffer.speech_started":
                            speaker.clear()  # barge-in
                        elif t == "response.function_call_arguments.done":
                            try:
                                args = json.loads(ev.get("arguments") or "{}")
                            except json.JSONDecodeError:
                                args = {}
                            result = dispatch(ev.get("name", ""), args)
                            log(f"[tool] {ev.get('name')}({args}) -> {result[:80]}")
                            await ws.send_json({"type": "conversation.item.create",
                                                "item": {"type": "function_call_output",
                                                         "call_id": ev.get("call_id", ""),
                                                         "output": result}})
                            await ws.send_json({"type": "response.create"})
                        elif t in ("response.audio_transcript.done",
                                   "response.output_audio_transcript.done"):
                            log(f"Windy: {ev.get('transcript', '').strip()}")
                        elif t == "conversation.item.input_audio_transcription.completed":
                            log(f"You: {ev.get('transcript', '').strip()}")
                        elif t == "error":
                            log(f"[API error] {ev.get('error')}")
                finally:
                    pump_task.cancel()
