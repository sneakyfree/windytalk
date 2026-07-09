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
    def __init__(self, backend=None, policy: TierPolicy | None = None) -> None:
        self.backend = backend or get_backend()
        self.policy = policy or TierPolicy()
        self.schemas = _load_tool_schemas()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    # -- the shared dispatch (both tenants) ------------------------------------

    def invoke(self, tool: str, args: dict | None = None) -> dict:
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
        return [{"name": t["name"], "description": t["description"],
                 "tier": t["tier"], "inputSchema": t["inputSchema"]}
                for t in self.schemas.values()]

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

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/") == "/tools":
                    self._send(200, {"tools": surface.tool_list()})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"error": "bad json"})
                    return
                if self.path.rstrip("/") == "/invoke":
                    self._send(200, surface.invoke(body.get("tool"), body.get("args")))
                elif self.path.rstrip("/") == "/mcp":
                    self._send(200, surface.handle_mcp(body))
                else:
                    self._send(404, {"error": "not found"})

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        bound = self._httpd.server_address
        self._thread = Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return bound[0], bound[1]

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
