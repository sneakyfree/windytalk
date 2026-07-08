"""
Google Gemini Live brain — native speech-to-speech via the google-genai SDK.

Gemini Live wants 16 kHz pcm16 input and returns 24 kHz pcm16. VAD/barge-in are
handled server-side; on interruption the SDK sends server_content.interrupted.
"""
import asyncio

import agent
import config
from providers.base import Brain


def _gemini_tools():
    """Convert the canonical (OpenAI-shaped) tool specs to Gemini FunctionDeclarations."""
    from google.genai import types
    decls = []
    for t in agent.TOOLS:
        params = t.get("parameters") or {"type": "object", "properties": {}}
        try:
            decls.append(types.FunctionDeclaration(
                name=t["name"], description=t.get("description", ""),
                parameters_json_schema=params))
        except Exception:
            decls.append(types.FunctionDeclaration(
                name=t["name"], description=t.get("description", "")))
    return [types.Tool(function_declarations=decls)]


class GeminiLiveBrain(Brain):
    name = "gemini"
    input_rate = 16000
    output_rate = 24000

    def __init__(self, model: str | None = None, voice: str | None = None):
        self.model = model or config.GEMINI_MODEL
        self.voice = voice or config.GEMINI_VOICE

    @property
    def ready(self):
        if not config.GOOGLE_API_KEY:
            return (False, "set GOOGLE_API_KEY in .env (free key: aistudio.google.com)")
        return (True, "")

    def _config(self):
        from google.genai import types
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(parts=[types.Part(text=config.SYSTEM_PROMPT)]),
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice))),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            tools=_gemini_tools(),
        )

    async def run(self, mic, speaker, dispatch, log):
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=config.GOOGLE_API_KEY)

        async with client.aio.live.connect(model=self.model, config=self._config()) as session:
            async def pump():
                while True:
                    data = await mic.read()
                    await session.send_realtime_input(
                        audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={self.input_rate}"))

            pump_task = asyncio.create_task(pump())
            try:
                async for msg in session.receive():
                    # audio out
                    if getattr(msg, "data", None):
                        speaker.play(msg.data)
                    sc = getattr(msg, "server_content", None)
                    if sc is not None:
                        if getattr(sc, "interrupted", False):
                            speaker.clear()  # barge-in
                        it = getattr(sc, "input_transcription", None)
                        if it and getattr(it, "text", None):
                            log(f"You: {it.text.strip()}")
                        ot = getattr(sc, "output_transcription", None)
                        if ot and getattr(ot, "text", None):
                            log(f"Windy: {ot.text.strip()}")
                    # tool calls
                    tc = getattr(msg, "tool_call", None)
                    if tc and getattr(tc, "function_calls", None):
                        responses = []
                        for fc in tc.function_calls:
                            args = dict(fc.args or {})
                            result = dispatch(fc.name, args)
                            log(f"[tool] {fc.name}({args}) -> {result[:80]}")
                            responses.append(types.FunctionResponse(
                                id=fc.id, name=fc.name, response={"result": result}))
                        await session.send_tool_response(function_responses=responses)
            finally:
                pump_task.cancel()
