"""
Windy Jarvis — always-on conversational voice control for the Fedora desktop.

Pipeline:  mic -> OpenAI Realtime (speech-to-speech + tools) -> hands.py -> desktop
The model listens continuously (server VAD), speaks back, lets you interrupt it,
and calls the Linux desktop-control tools in agent.py to actually do things.

Run:  ./run.sh          (or: OPENAI_API_KEY=sk-... python3 jarvis.py)
Stop: Ctrl-C
"""
import asyncio
import base64
import json
import signal
import sys
import threading

import aiohttp
import pyaudio

import agent
import config

WS_URL = f"wss://api.openai.com/v1/realtime?model={config.MODEL}"
HEADERS = {"Authorization": f"Bearer {config.OPENAI_API_KEY}",
           "OpenAI-Beta": "realtime=v1"}


class Speaker:
    """Non-blocking PCM16 playback with instant flush for barge-in."""

    def __init__(self, pa: pyaudio.PyAudio):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.stream = pa.open(format=pyaudio.paInt16, channels=config.CHANNELS,
                              rate=config.SAMPLE_RATE, output=True,
                              frames_per_buffer=1024, stream_callback=self._cb)

    def _cb(self, in_data, frame_count, time_info, status):
        need = frame_count * 2  # 16-bit mono
        with self.lock:
            out = bytes(self.buf[:need])
            del self.buf[:need]
        if len(out) < need:
            out += b"\x00" * (need - len(out))
        return (out, pyaudio.paContinue)

    def play(self, pcm: bytes):
        with self.lock:
            self.buf.extend(pcm)

    def clear(self):
        with self.lock:
            self.buf.clear()


async def mic_pump(ws, mic, loop, state):
    """Stream microphone audio to the model as 40 ms pcm16 chunks."""
    while not state["stop"]:
        try:
            data = await loop.run_in_executor(
                None, lambda: mic.read(config.CHUNK_FRAMES, exception_on_overflow=False))
        except Exception as e:
            print("  [mic] read error:", e)
            break
        try:
            await ws.send_json({"type": "input_audio_buffer.append",
                                "audio": base64.b64encode(data).decode()})
        except ConnectionResetError:
            break


async def handle_events(ws, speaker, state):
    """Consume server events: play audio, barge-in, run tools, log transcripts."""
    async for msg in ws:
        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            break
        if msg.type is not aiohttp.WSMsgType.TEXT:
            continue
        ev = json.loads(msg.data)
        t = ev.get("type", "")

        # --- audio out (handle both current + next-gen event names) ----------
        if t in ("response.audio.delta", "response.output_audio.delta"):
            speaker.play(base64.b64decode(ev["delta"]))

        # --- barge-in: user started talking over Windy -> stop playback ------
        elif t == "input_audio_buffer.speech_started":
            speaker.clear()

        # --- tool call -------------------------------------------------------
        elif t == "response.function_call_arguments.done":
            name = ev.get("name", "")
            call_id = ev.get("call_id", "")
            try:
                args = json.loads(ev.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = agent.call_tool(name, args)
            print(f"  \033[36m[tool]\033[0m {name}({args}) -> {result[:90]}")
            await ws.send_json({"type": "conversation.item.create",
                                "item": {"type": "function_call_output",
                                         "call_id": call_id, "output": result}})
            await ws.send_json({"type": "response.create"})

        # --- transcripts (nice to watch in the terminal) ---------------------
        elif t in ("response.audio_transcript.done",
                   "response.output_audio_transcript.done"):
            print(f"  \033[35mWindy:\033[0m {ev.get('transcript', '').strip()}")
        elif t == "conversation.item.input_audio_transcription.completed":
            print(f"  \033[32mYou:\033[0m {ev.get('transcript', '').strip()}")

        elif t == "error":
            print("  \033[31m[API error]\033[0m", ev.get("error"))

        elif t == "session.created":
            print("  \033[90msession ready\033[0m")


def _session_update():
    return {"type": "session.update", "session": {
        "modalities": ["audio", "text"],
        "instructions": config.SYSTEM_PROMPT,
        "voice": config.VOICE,
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "input_audio_transcription": {"model": "whisper-1"},
        "turn_detection": {"type": "server_vad", "threshold": 0.5,
                           "prefix_padding_ms": 300, "silence_duration_ms": 600},
        "tools": agent.TOOLS,
        "tool_choice": "auto",
    }}


async def run_once(pa, mic, speaker, state):
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL, headers=HEADERS, max_msg_size=0) as ws:
            await ws.send_json(_session_update())
            loop = asyncio.get_running_loop()
            await asyncio.gather(mic_pump(ws, mic, loop, state),
                                 handle_events(ws, speaker, state))


async def main():
    if not config.OPENAI_API_KEY:
        sys.exit("No OPENAI_API_KEY. Put it in windy-jarvis/.env or export it, then rerun.")

    pa = pyaudio.PyAudio()
    try:
        mic = pa.open(format=pyaudio.paInt16, channels=config.CHANNELS,
                      rate=config.SAMPLE_RATE, input=True,
                      frames_per_buffer=config.CHUNK_FRAMES)
    except Exception as e:
        sys.exit(f"Could not open the microphone at {config.SAMPLE_RATE} Hz: {e}")
    speaker = Speaker(pa)
    state = {"stop": False}

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: state.update(stop=True))

    print(f"\n  \033[1mWindy Jarvis\033[0m — model={config.MODEL} voice={config.VOICE}")
    print("  Listening. Just talk. Say things like \"open Firefox\" or \"search the "
          "web for the weather\". Ctrl-C to quit.\n")

    while not state["stop"]:
        try:
            await run_once(pa, mic, speaker, state)
        except aiohttp.WSServerHandshakeError as e:
            sys.exit(f"Handshake failed ({e.status}). Check the API key and model name "
                     f"({config.MODEL}).")
        except Exception as e:
            if state["stop"]:
                break
            print(f"  \033[33m[reconnecting after: {e}]\033[0m")
            await asyncio.sleep(1)

    mic.stop_stream(); mic.close(); pa.terminate()
    print("\n  Bye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
