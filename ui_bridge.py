"""
UI bridge — a tiny local websocket (localhost:8770) that the Electron face app
connects to. The agent pushes status + speaking-level events; the app sends the
on/off (mic) command back. Pure localhost, JSON, a few hundred bytes/sec.

  agent -> app : {"type":"status","state":"offline|listening|thinking|speaking|paused"}
                 {"type":"level","v":0.0..1.0}          (mouth openness while speaking)
                 {"type":"heard","text":"..."} / {"type":"say","text":"..."}
  app -> agent : {"type":"mic","on":true|false}         (the big button)
"""
import asyncio
import json

from aiohttp import WSMsgType, web


class UIBridge:
    def __init__(self, port: int = 8770):
        self.port = port
        self.clients: set = set()
        self.on_command = None          # callable(dict) set by the agent
        self._last = {"type": "status", "state": "offline"}

    async def start(self):
        app = web.Application()
        app.router.add_get("/ws", self._handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()

    async def _handler(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            await ws.send_json(self._last)          # sync the new client immediately
            async for msg in ws:
                if msg.type == WSMsgType.TEXT and self.on_command:
                    try:
                        self.on_command(json.loads(msg.data))
                    except Exception:
                        pass
        finally:
            self.clients.discard(ws)
        return ws

    def emit(self, obj: dict):
        if obj.get("type") == "status":
            self._last = obj
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.create_task(ws.send_json(obj))

    def status(self, state: str, **kw):
        self.emit({"type": "status", "state": state, **kw})

    def level(self, v: float):
        self.emit({"type": "level", "v": round(v, 3)})
