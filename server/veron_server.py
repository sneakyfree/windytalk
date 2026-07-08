"""
Windy Jarvis — local brain server (runs on the Veron-1-5090 box).

All AI compute is local and free: faster-whisper (STT, CUDA) -> Ollama (LLM +
tool calling) -> kokoro-onnx (TTS). Speech-to-speech over a websocket. The thin
client streams microphone audio and its own tool schemas; the server transcribes,
reasons, calls tools (executed BACK on the client's desktop), and streams spoken
audio in return.

Protocol (one websocket per client):
  client -> server:
    - binary frame            = pcm16 mono 16 kHz microphone audio
    - {"type":"hello","tools":[...openai-style tool specs...],"prompt":"..."}
    - {"type":"tool_result","id":"...","output":"..."}
  server -> client:
    - {"type":"ready"}
    - {"type":"heard","text":"..."}                 (what STT understood)
    - {"type":"tool_call","id":"...","name":"...","args":{...}}
    - {"type":"say","text":"..."}                   (assistant reply text)
    - {"type":"audio_start"} / binary pcm16 24 kHz frames / {"type":"audio_end"}
    - {"type":"interrupted"}                         (barge-in: stop playback)

Env: WJ_MODEL (ollama model), WJ_WHISPER (base/small/...), WJ_VOICE, WJ_PORT.
"""
import asyncio
import json
import os
import re
import time
import urllib.request

import numpy as np
import webrtcvad
import websockets
from faster_whisper import WhisperModel
from kokoro_onnx import Kokoro

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = os.environ.get("WJ_MODEL", "qwen2.5:7b-instruct")
WHISPER_SIZE = os.environ.get("WJ_WHISPER", "base")
VOICE = os.environ.get("WJ_VOICE", "af_heart")
PORT = int(os.environ.get("WJ_PORT", "8765"))

IN_SR = 16000          # client mic rate (Whisper native)
OUT_SR = 24000         # Kokoro output rate
FRAME_MS = 30
FRAME_BYTES = IN_SR * FRAME_MS // 1000 * 2   # 30 ms pcm16
SILENCE_MS = 600       # trailing silence that ends an utterance
SPEECH_ONSET_MS = 150  # speech needed to (re)start / trigger barge-in

DEFAULT_PROMPT = (
    "You are Windy, a local voice assistant that controls the user's computer through "
    "tools. To do ANYTHING on the machine — open an app, search the web, type, press "
    "keys, click, read the screen, run a command — you MUST call the matching tool "
    "function. Never describe the action in words instead of calling the tool, and "
    "never output function syntax, JSON, XML, or code as your spoken reply. After a "
    "tool runs, give ONE short spoken confirmation. Keep every spoken reply brief and "
    "natural for text-to-speech.")

print("Loading Whisper on CUDA…", flush=True)
WHISPER = WhisperModel(WHISPER_SIZE, device="cuda", compute_type="float16")
print("Loading Kokoro…", flush=True)
KOKORO = Kokoro(os.path.join(HERE, "kokoro-v1.0.onnx"), os.path.join(HERE, "voices-v1.0.bin"))
print(f"Ready. model={MODEL} whisper={WHISPER_SIZE} voice={VOICE} port={PORT}", flush=True)


def ollama_chat(messages, tools):
    body = json.dumps({"model": MODEL, "messages": messages, "tools": tools,
                       "stream": False, "options": {"temperature": 0.2}}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def transcribe(pcm16: bytes) -> str:
    audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = WHISPER.transcribe(audio, language="en", beam_size=1,
                                     vad_filter=True, condition_on_previous_text=False)
    return " ".join(s.text for s in segments).strip()


def extract_tool_calls(content: str, tool_names):
    """Fallback: recover tool calls a local model leaked as text instead of
    emitting via Ollama's structured tool_calls field. Handles the common formats."""
    calls = []
    # <function=NAME><parameter=P>V</parameter>...</function>  (qwen-coder style)
    for m in re.finditer(r"<function=([\w.-]+)>(.*?)</function>", content, re.DOTALL):
        args = {p: v.strip() for p, v in
                re.findall(r"<parameter=([\w.-]+)>\s*(.*?)\s*</parameter>", m.group(2), re.DOTALL)}
        calls.append({"name": m.group(1), "arguments": args})
    if calls:
        return calls
    # {"name":"NAME","arguments":{...}}  possibly wrapped in <tool_call>…</tool_call>
    for m in re.finditer(r'\{[^{}]*"name"\s*:\s*"([\w.-]+)"[^{}]*(?:\{[^{}]*\})?[^{}]*\}', content):
        try:
            obj = json.loads(m.group(0))
            calls.append({"name": obj["name"],
                          "arguments": obj.get("arguments") or obj.get("parameters") or {}})
        except Exception:
            pass
    if calls:
        return calls
    # NAME({json})  or  NAME {json}  for a known tool name
    for name in tool_names:
        m = re.search(re.escape(name) + r"\s*[(\[]?\s*(\{.*?\})", content, re.DOTALL)
        if m:
            try:
                calls.append({"name": name, "arguments": json.loads(m.group(1))})
            except Exception:
                pass
    return calls


def clean_for_speech(text: str) -> str:
    """Strip any leaked tool-call markup so we never read <function=…> aloud."""
    text = re.sub(r"<function=.*?</function>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?tool_call>|<\|.*?\|>", "", text)
    text = re.sub(r"<parameter=.*?</parameter>", "", text, flags=re.DOTALL)
    return re.sub(r"\s+", " ", text).strip()


def synth_pcm(text: str) -> bytes:
    samples, sr = KOKORO.create(text, voice=VOICE, speed=1.0, lang="en-us")
    return (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()


class Segmenter:
    """webrtcvad endpointing: feed pcm16, get complete utterances; tracks speech."""

    def __init__(self):
        self.vad = webrtcvad.Vad(2)
        self.buf = bytearray()
        self.utter = bytearray()
        self.in_speech = False
        self.silence = 0
        self.speech = 0

    def push(self, pcm: bytes):
        """Yield ('utterance', bytes) when one completes; update .in_speech."""
        self.buf.extend(pcm)
        out = []
        while len(self.buf) >= FRAME_BYTES:
            frame = bytes(self.buf[:FRAME_BYTES]); del self.buf[:FRAME_BYTES]
            voiced = self.vad.is_speech(frame, IN_SR)
            if voiced:
                self.speech += FRAME_MS
                self.silence = 0
                if not self.in_speech and self.speech >= SPEECH_ONSET_MS:
                    self.in_speech = True
                if self.in_speech:
                    self.utter.extend(frame)
            else:
                self.speech = 0
                if self.in_speech:
                    self.utter.extend(frame)
                    self.silence += FRAME_MS
                    if self.silence >= SILENCE_MS:
                        out.append(("utterance", bytes(self.utter)))
                        self.utter = bytearray()
                        self.in_speech = False
                        self.silence = 0
        return out

    def onset(self, pcm: bytes) -> bool:
        """Lightweight check: does this chunk contain speech onset (for barge-in)?"""
        n = 0
        for i in range(0, len(pcm) - FRAME_BYTES, FRAME_BYTES):
            if self.vad.is_speech(pcm[i:i + FRAME_BYTES], IN_SR):
                n += FRAME_MS
        return n >= SPEECH_ONSET_MS


async def handle(ws):
    peer = ws.remote_address
    print(f"[+] client {peer}", flush=True)
    tools, prompt = [], DEFAULT_PROMPT
    history = [{"role": "system", "content": prompt}]
    seg = Segmenter()
    pending: dict[str, asyncio.Future] = {}
    speaking = asyncio.Event()          # set while we're streaming TTS
    interrupt = asyncio.Event()         # set on barge-in
    utter_q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    async def reader():
        nonlocal tools, prompt, history
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                if speaking.is_set() and seg.onset(bytes(msg)):
                    interrupt.set()
                for kind, data in seg.push(bytes(msg)):
                    await utter_q.put(data)
            else:
                ev = json.loads(msg)
                if ev.get("type") == "hello":
                    tools = ev.get("tools", [])
                    prompt = ev.get("prompt") or DEFAULT_PROMPT
                    history = [{"role": "system", "content": prompt}]
                    await ws.send(json.dumps({"type": "ready"}))
                elif ev.get("type") == "tool_result":
                    fut = pending.get(ev.get("id"))
                    if fut and not fut.done():
                        fut.set_result(ev.get("output", ""))

    async def stream_audio(text):
        pcm = await loop.run_in_executor(None, synth_pcm, text)
        await ws.send(json.dumps({"type": "audio_start"}))
        speaking.set(); interrupt.clear()
        step = OUT_SR * 2 // 10   # 100 ms frames
        for i in range(0, len(pcm), step):
            if interrupt.is_set():
                await ws.send(json.dumps({"type": "interrupted"}))
                break
            await ws.send(pcm[i:i + step])
            await asyncio.sleep(0.09)
        speaking.clear()
        await ws.send(json.dumps({"type": "audio_end"}))

    async def think(text):
        history.append({"role": "user", "content": text})
        for _ in range(6):  # allow a few tool rounds
            resp = await loop.run_in_executor(None, ollama_chat, history, tools)
            m = resp.get("message", {})
            calls = m.get("tool_calls") or []
            if not calls:  # fallback: recover tool calls leaked as text
                tool_names = [t.get("function", {}).get("name") for t in tools]
                for c in extract_tool_calls(m.get("content") or "", tool_names):
                    calls.append({"function": c})
            if calls:
                history.append(m)
                for tc in calls:
                    fn = tc.get("function", {})
                    tid = tc.get("id") or f"c{int(time.time()*1000)}"
                    fut = loop.create_future(); pending[tid] = fut
                    await ws.send(json.dumps({"type": "tool_call", "id": tid,
                                              "name": fn.get("name"),
                                              "args": fn.get("arguments", {})}))
                    try:
                        result = await asyncio.wait_for(fut, timeout=30)
                    except asyncio.TimeoutError:
                        result = "(no response from client)"
                    history.append({"role": "tool", "name": fn.get("name"),
                                    "content": str(result)})
                continue
            answer = clean_for_speech(m.get("content") or "")
            history.append({"role": "assistant", "content": answer})
            if answer:
                await ws.send(json.dumps({"type": "say", "text": answer}))
                await stream_audio(answer)
            return

    async def worker():
        while True:
            data = await utter_q.get()
            text = await loop.run_in_executor(None, transcribe, data)
            if not text or len(text) < 2:
                continue
            print(f"    heard: {text}", flush=True)
            await ws.send(json.dumps({"type": "heard", "text": text}))
            try:
                await think(text)
            except Exception as e:
                print("    think error:", e, flush=True)

    try:
        await asyncio.gather(reader(), worker())
    except websockets.ConnectionClosed:
        pass
    finally:
        print(f"[-] client {peer} gone", flush=True)


async def main():
    async with websockets.serve(handle, "0.0.0.0", PORT, max_size=None):
        print(f"Windy Jarvis brain server listening on :{PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
