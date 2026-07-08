"""Connect with a license key and log control events for ~28s (for lock/unlock tests).
   PYTHONPATH=. python3 server/license_test.py <LICENSE_KEY>"""
import asyncio, json, sys, time
import aiohttp

URL = "ws://localhost:8765"
KEY = sys.argv[1] if len(sys.argv) > 1 else ""
TOOLS = [{"type": "function", "function": {"name": "open_app",
          "description": "Launch an app", "parameters": {"type": "object",
          "properties": {"name": {"type": "string"}}, "required": ["name"]}}}]


async def main():
    t0 = time.time()
    def ts(): return f"{time.time()-t0:5.1f}s"
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(URL, max_msg_size=0) as ws:
            await ws.send_json({"type": "hello", "tools": TOOLS, "license": KEY})
            audio = 0
            end = time.time() + 28
            while time.time() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg.type == aiohttp.WSMsgType.BINARY:
                    audio += len(msg.data); continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                ev = json.loads(msg.data); t = ev.get("type")
                if t == "audio_end":
                    print(f"  {ts()} (spoke {audio/2/24000:.1f}s of audio)"); audio = 0
                elif t in ("ready", "denied", "locked", "unlocked", "say"):
                    print(f"  {ts()} → {t.upper()}: {ev.get('message') or ev.get('text') or ''}")
    print(f"  {ts()} disconnected")

asyncio.run(main())
