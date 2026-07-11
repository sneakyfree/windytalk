"""The hands control surface (ADR-058 D4 — the §6/§7 co-tenant API).

One `invoke()` path serves both tenants: the human (via the local HTTP endpoint)
and the agent (via MCP / voice-session.v1 tool_call). Both share the same tier
gate and the same backend state — modeled on the proven windyword.py 127.0.0.1
capability pattern. Returns the frozen hands.mcp.v1 result shape {ok,result,error}.

Transports (all localhost):
  GET  /tools           → the tool list (names, tiers, schemas)
  POST /invoke          → {tool, args} → result shape (human/programmatic path)
  POST /mcp             → MCP JSON-RPC 2.0: tools/list, tools/call (agent path)
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from .backends import get_backend
from .backends.base import TOOL_NAMES, UnsupportedTool
from .tiers import TierPolicy

_CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "hands.mcp.v1.json"


def _load_tool_schemas() -> dict[str, dict]:
    doc = json.loads(_CONTRACT.read_text())
    return {t["name"]: t for t in doc["tools"]}


class HandsSurface:
    def __init__(self, backend=None, policy: TierPolicy | None = None,
                 token: str | None = None) -> None:
        import os
        import secrets
        self.backend = backend or get_backend()
        self.policy = policy or TierPolicy()
        self.schemas = _load_tool_schemas()
        # Per-launch bearer token. Read from env (the launcher shares it with the
        # client) or minted here. Any /invoke or /mcp call must present it. This +
        # Origin/Host rejection is what stops a webpage from driving the desktop.
        self.token = token or os.environ.get("WINDYTALK_HANDS_TOKEN") or secrets.token_hex(24)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    # -- the shared dispatch (both tenants) ------------------------------------

    def invoke(self, tool: str, args: dict | None = None) -> dict:
        # A hostile/buggy client can send `arguments` as a string or array;
        # dict("foo") / dict([1,2]) would raise BEFORE the try/except below and
        # drop the connection instead of returning the result shape. Reject
        # non-dict args honestly.
        if args is not None and not isinstance(args, dict):
            return {"ok": False, "error": "bad args: expected an object"}
        args = dict(args or {})
        if tool not in TOOL_NAMES:
            return {"ok": False, "error": f"unknown tool: {tool}"}
        if not self.policy.allowed(tool, args):
            return {"ok": False, "error": "denied"}
        call_args = self._filter_args(tool, args)
        fn = getattr(self.backend, tool)
        try:
            result = fn(**call_args)
            return {"ok": True, "result": result}
        except UnsupportedTool:
            return {"ok": False, "error": "unsupported"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _filter_args(self, tool: str, args: dict) -> dict:
        """Keep only the tool's schema properties (drops sentinels like _always_allow)."""
        props = self.schemas[tool].get("inputSchema", {}).get("properties", {})
        return {k: v for k, v in args.items() if k in props}

    def tool_list(self) -> list[dict]:
        # capabilities() returns {"backend": ..., "tools": {name: bool}} — the
        # per-tool map lives under "tools", NOT at the top level. Reading the
        # top level always missed and defaulted to True, so every tool was
        # advertised as supported on every box (capability negotiation was dead).
        caps = self.capabilities().get("tools", {})
        return [{"name": t["name"], "description": t["description"],
                 "tier": t["tier"], "inputSchema": t["inputSchema"],
                 "supported": caps.get(t["name"], True)}
                for t in self.schemas.values()]

    def capabilities(self) -> dict:
        """Per-OS support map + backend name (the Swiss-army-knife's blade list)."""
        try:
            caps = self.backend.capabilities()
        except Exception:
            caps = {t: True for t in self.schemas}
        return {"backend": self.backend.name, "tools": caps}

    # -- MCP JSON-RPC ----------------------------------------------------------

    def handle_mcp(self, req: dict) -> dict:
        rid = req.get("id")
        method = req.get("method")
        if method == "tools/list":
            tools = [{"name": t["name"], "description": t["description"],
                      "inputSchema": t["inputSchema"]} for t in self.tool_list()]
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}
        if method == "tools/call":
            params = req.get("params") or {}
            res = self.invoke(params.get("name"), params.get("arguments") or {})
            text = res.get("result") if res["ok"] else res.get("error", "error")
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": str(text)}],
                               "isError": not res["ok"]}}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}

    # -- local HTTP server -----------------------------------------------------

    def serve(self, host: str = "127.0.0.1", port: int = 8781) -> tuple[str, int]:
        surface = self
        max_body = 64 * 1024  # cap request size (DoS guard)
        _loopback = {"127.0.0.1", "localhost", "::1"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                # deliberately NO Access-Control-Allow-Origin: no site may read us.
                self.end_headers()
                self.wfile.write(body)

            def _guard(self) -> bool:
                """Reject anything a browser/remote could send. A legit local
                (Electron main / CLI) caller sends no Origin and a loopback Host."""
                # 1) any Origin header ⇒ a browser cross-origin request ⇒ reject.
                if self.headers.get("Origin"):
                    self._send(403, {"ok": False, "error": "forbidden"})
                    return False
                # 2) Host must be loopback (blocks DNS-rebinding).
                host_hdr = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
                if host_hdr and host_hdr not in _loopback:
                    self._send(403, {"ok": False, "error": "forbidden host"})
                    return False
                # 3) bearer token must match (constant-time — on a shared host the
                #    loopback port is reachable by other local users, and the token
                #    is their only barrier; don't leak length/prefix via timing).
                import secrets
                presented = self.headers.get("X-Windytalk-Token") or ""
                # compare_digest raises TypeError on non-ASCII str operands;
                # compare on bytes so a `café` token header fails closed with a
                # clean 401 instead of an uncaught 500 / dropped socket.
                if not secrets.compare_digest(presented.encode(), surface.token.encode()):
                    self._send(401, {"ok": False, "error": "unauthorized"})
                    return False
                return True

            def do_OPTIONS(self):
                # No CORS preflight is ever honored — a browser cannot use this API.
                self._send(405, {"error": "method not allowed"})

            def do_GET(self):
                if not self._guard():
                    return
                if self.path.rstrip("/") == "/tools":
                    self._send(200, {"tools": surface.tool_list()})
                elif self.path.rstrip("/") == "/capabilities":
                    self._send(200, surface.capabilities())
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._guard():
                    return
                # A non-integer Content-Length would raise; a NEGATIVE one would
                # pass the size cap and make rfile.read(-1) block reading to EOF,
                # wedging this thread forever (a ThreadingHTTPServer DoS). Parse
                # defensively and reject anything not in [0, max_body].
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except (TypeError, ValueError):
                    self._send(400, {"ok": False, "error": "bad content-length"})
                    return
                if length < 0:
                    self._send(400, {"ok": False, "error": "bad content-length"})
                    return
                if length > max_body:
                    self._send(413, {"ok": False, "error": "too large"})
                    return
                # No single un-guarded input should ever drop the connection —
                # the contract's "never silence" guarantee. Any unexpected error
                # becomes a 500 with a result shape, not a socket reset.
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"error": "bad json"})
                    return
                if not isinstance(body, dict):
                    self._send(400, {"error": "bad body"})
                    return
                try:
                    if self.path.rstrip("/") == "/invoke":
                        self._send(200, surface.invoke(body.get("tool"), body.get("args")))
                    elif self.path.rstrip("/") == "/mcp":
                        self._send(200, surface.handle_mcp(body))
                    else:
                        self._send(404, {"error": "not found"})
                except Exception as e:  # noqa: BLE001 — never leak a raw traceback / drop the socket
                    self._send(500, {"ok": False, "error": f"internal: {type(e).__name__}"})

        # Bind loopback only — never expose the desktop-control port off-box.
        bind_host = host if host in _loopback else "127.0.0.1"
        self._httpd = ThreadingHTTPServer((bind_host, port), Handler)
        bound = self._httpd.server_address
        self._thread = Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return bound[0], bound[1]

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
