"""The engine-box control surface (engine.mcp.v1 — ADR-060 §7 baseline).

The health of the VOICE ENGINE BOX itself (the 5090 running engine/server.py),
separate from control.mcp.v1 (the desktop app) and hands.mcp.v1 (drives other
apps). Before this, a wedged engine box could only be fallen away from
(control.mcp.v1 set_engine_url); now an agent can DIAGNOSE it and RESTART the
worker remotely.

Doctor-not-in-patient: this surface is hosted by a SUPERVISOR (server/
supervise.py) that owns the engine WORKER as a subprocess, injected here as an
`EngineController`. So restart_engine works even when the worker is hung — the
doctor is not in the patient. The engine-facing operations are all behind the
`EngineController` ABC, so the surface is fully testable with a fake.

Bilingual (ADR-060 §3.2): GET /tools, POST /invoke {name,arguments}, POST /mcp
(JSON-RPC), one registry. Same security wall as hands/surface.py: per-install
bearer token (constant-time), loopback bind, Origin/Host reject, no CORS.

Tier enforcement on a headless server (no local human): auto_allow runs; an
ask_first / always_confirm tool requires an explicit top-level `confirm: true`
in the call (the authenticated caller affirms the confirmation happened on
their side) — else `{ok:false, error:'confirm_required'}`. Enforced HERE, at
the surface, never by the caller's goodwill.
"""
from __future__ import annotations

import json
import os
import secrets
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

_CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "engine.mcp.v1.json"
_MCP_PROTOCOL = "2025-06-18"
_SERVER_VERSION = "1.0"

# Tools this v1 implements LIVE. Everything else in the contract is declared but
# reports tri-state 'unsupported' (ADR-060) until wired — never absent.
_LIVE_TOOLS = frozenset({
    "get_health", "get_status", "get_config", "get_logs", "run_selftest",
    "get_capabilities", "reconnect", "restart_engine", "reset_to_defaults",
})
_UNWIRED_TOOLS = frozenset({
    "enter_safe_mode", "exit_safe_mode", "set_engine_config",
    "check_for_update", "apply_update",
})
_UNWIRED_REASON = (
    "declared for a future agent to find, but not wired in engine.mcp.v1 yet"
)


class EngineController(ABC):
    """The engine-facing operations the surface drives. The production impl
    (server/supervise.py) owns the engine worker subprocess; tests inject a
    fake. Read methods must never raise into the surface — return a safe value."""

    @abstractmethod
    def health(self) -> dict:
        """{healthy, worker_alive, providers_warm, gpu, serving, active_sessions, mode}."""

    @abstractmethod
    def status(self) -> dict:
        """{state, active_sessions, uptime_s}."""

    @abstractmethod
    def config(self) -> dict:
        """{stt, tts, brain, device, port} — secrets redacted."""

    @abstractmethod
    def logs(self, lines: int) -> str:
        """Recent scrubbed engine logs."""

    @abstractmethod
    def selftest(self) -> dict:
        """{ok, stages:[{stage,pass,detail,ms}]} — exercise STT→TTS on the box."""

    @abstractmethod
    def reconnect(self) -> str:
        """Re-warm providers + re-establish the brain, without a worker restart."""

    @abstractmethod
    def restart_engine(self) -> str:
        """Restart the engine WORKER process (works even when it's hung)."""

    @abstractmethod
    def reset_to_defaults(self) -> str:
        """Reset engine config to defaults and restart the worker."""


class EngineControlSurface:
    def __init__(self, controller: EngineController, token: str | None = None) -> None:
        self.controller = controller
        self.schemas = {t["name"]: t for t in json.loads(_CONTRACT.read_text())["tools"]}
        self.token = (token or os.environ.get("WINDYTALK_ENGINE_CONTROL_TOKEN")
                      or secrets.token_hex(24))
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    # -- shared dispatch (both tenants) ----------------------------------------

    def invoke(self, tool: str, args: dict | None = None) -> dict:
        if args is not None and not isinstance(args, dict):
            return {"ok": False, "error": "bad args: expected an object"}
        args = dict(args or {})
        meta = self.schemas.get(tool)
        if meta is None:
            return {"ok": False, "error": f"unknown tool: {tool}"}
        # Surface-side tier enforcement (headless server): non-auto_allow tools
        # require an explicit confirm:true (the authenticated caller affirms it).
        if meta["tier"] != "auto_allow" and args.get("confirm") is not True:
            return {"ok": False, "error": "confirm_required",
                    "result": f"'{tool}' is {meta['tier']}; resend with confirm=true"}
        if tool in _UNWIRED_TOOLS:
            return {"ok": False, "error": "unsupported", "result": _UNWIRED_REASON}
        try:
            return self._execute(tool, args)
        except Exception as e:  # noqa: BLE001 — never drop the socket / leak a trace
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _execute(self, tool: str, args: dict) -> dict:
        c = self.controller
        if tool == "get_health":
            return {"ok": True, "result": c.health()}
        if tool == "get_status":
            return {"ok": True, "result": c.status()}
        if tool == "get_config":
            return {"ok": True, "result": c.config()}
        if tool == "get_logs":
            lines = args.get("lines")
            n = lines if isinstance(lines, int) and 1 <= lines <= 1000 else 100
            return {"ok": True, "result": c.logs(n)}
        if tool == "run_selftest":
            return {"ok": True, "result": c.selftest()}
        if tool == "get_capabilities":
            return {"ok": True, "result": {"tools": self._capabilities()}}
        if tool == "reconnect":
            return {"ok": True, "result": c.reconnect()}
        if tool == "restart_engine":
            return {"ok": True, "result": c.restart_engine()}
        if tool == "reset_to_defaults":
            return {"ok": True, "result": c.reset_to_defaults()}
        return {"ok": False, "error": "unsupported", "result": _UNWIRED_REASON}

    def _capabilities(self) -> dict:
        """Tri-state per tool (ADR-060): live tools true; unwired 'unsupported'."""
        caps: dict[str, bool | str] = {}
        for name in self.schemas:
            if name in _UNWIRED_TOOLS:
                caps[name] = "unsupported"
            else:
                caps[name] = name in _LIVE_TOOLS
        return caps

    def tool_list(self) -> list[dict]:
        caps = self._capabilities()
        return [{"name": t["name"], "description": t["description"], "tier": t["tier"],
                 "inputSchema": t["inputSchema"], "supported": caps.get(t["name"], True)}
                for t in self.schemas.values()]

    # -- MCP JSON-RPC ----------------------------------------------------------

    def handle_mcp(self, req: dict) -> dict | None:
        if not isinstance(req, dict):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        method, rid = req.get("method"), req.get("id")
        if "id" not in req:
            return None  # notification: no response
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": _MCP_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "windytalk-engine", "version": _SERVER_VERSION}}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "tools/list":
            tools = [{"name": t["name"], "description": t["description"],
                      "inputSchema": t["inputSchema"]} for t in self.tool_list()]
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}
        if method == "tools/call":
            params = req.get("params") or {}
            res = self.invoke(params.get("name"), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(res)}],
                "structuredContent": res, "isError": not res["ok"]}}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}

    # -- local HTTP server (same wall as hands/surface.py) ---------------------

    def serve(self, host: str = "127.0.0.1", port: int = 8783) -> tuple[str, int]:
        surface = self
        max_body = 64 * 1024
        loopback = {"127.0.0.1", "localhost", "::1"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_no_body(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _guard(self) -> bool:
                if self.headers.get("Origin"):
                    self._send(403, {"ok": False, "error": "forbidden"})
                    return False
                host_hdr = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
                if host_hdr and host_hdr not in loopback:
                    self._send(403, {"ok": False, "error": "forbidden host"})
                    return False
                presented = self.headers.get("X-Windytalk-Engine-Token") or ""
                if not secrets.compare_digest(presented.encode(), surface.token.encode()):
                    self._send(401, {"ok": False, "error": "unauthorized"})
                    return False
                return True

            def do_OPTIONS(self):
                self._send(405, {"error": "method not allowed"})

            def do_GET(self):
                if not self._guard():
                    return
                if self.path.rstrip("/") == "/tools":
                    self._send(200, {"tools": surface.tool_list()})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._guard():
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                except (TypeError, ValueError):
                    self._send(400, {"ok": False, "error": "bad content-length"})
                    return
                if length < 0 or length > max_body:
                    self._send(413 if length > max_body else 400,
                               {"ok": False, "error": "bad content-length"})
                    return
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"error": "bad json"})
                    return
                try:
                    path = self.path.rstrip("/")
                    if path == "/invoke":
                        if not isinstance(body, dict):
                            self._send(400, {"error": "bad body"})
                            return
                        # ADR-060 §3.2 canonical {name, arguments}; {tool, args} legacy.
                        name = body.get("name", body.get("tool"))
                        arguments = body.get("arguments", body.get("args"))
                        self._send(200, surface.invoke(name, arguments))
                    elif path == "/mcp":
                        resp = surface.handle_mcp(body)
                        if resp is None:
                            self._send_no_body(204)
                        else:
                            self._send(200, resp)
                    else:
                        self._send(404, {"error": "not found"})
                except Exception as e:  # noqa: BLE001
                    self._send(500, {"ok": False, "error": f"internal: {type(e).__name__}"})

        bind_host = host if host in loopback else "127.0.0.1"
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
