"""Windy Fly agent adapter (ADR-058 D3).

Talks to a running Windy Fly agent over its JSON-RPC bridge
(`src/windyfly/bridge/uds_server.py` in sneakyfree/windy-agent; UDS on Mac/Linux,
TCP on Windows). Method `agent.respond {message, session_id} → {response}`,
newline-delimited JSON, one response per request.

Two things this adapter owns, both found live in Task 0.0 (docs/PROBE_RESULTS.md):
  1. Bridge replies carry a status-banner prefix `[🪰 Windy Fly · … · 🟢 99%]` —
     stripped here so the agent never reads its own dashboard aloud.
  2. The bridge is request/response, not token-streaming. For sentence-by-sentence
     TTS (voice-session.v1 §10) we segment the reply client-side with the engine's
     §10 chunker. `respond_segments()` also probes `agent.respond_stream` and uses
     it if a future bridge offers it — auto-upgrading with no client change.

Transport faults raise `WindyFlyError`; the voice session loop catches it and
speaks a fallback line (the adapter does not decide UX).
"""
from __future__ import annotations

import json
import os
import re
import socket
import tempfile
from collections.abc import Iterator

from engine.segment import segment_stream

_BANNER = re.compile(r"^\s*\[🪰[^\]]*\]\s*")


class WindyFlyError(RuntimeError):
    """Bridge unreachable, timed out, or returned an error."""


def default_socket_path() -> str:
    return os.environ.get("WINDYFLY_IPC_PATH") \
        or os.path.join(tempfile.gettempdir(), "windyfly.sock")


class WindyFlyAgent:
    name = "windyfly"

    def __init__(self, socket_path: str | None = None, timeout: float = 120.0) -> None:
        self.socket_path = socket_path or default_socket_path()
        self.timeout = timeout
        self._stream_unsupported = False  # set once we learn the bridge lacks it

    # -- transport -------------------------------------------------------------

    def _call(self, method: str, params: dict) -> dict:
        """One JSON-RPC round-trip over the UDS bridge. Raises WindyFlyError."""
        req = json.dumps({"method": method, "params": params}).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.socket_path)
                s.sendall(req)
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
        except OSError as e:  # includes TimeoutError, connection refused, etc.
            raise WindyFlyError(f"bridge {self.socket_path}: {type(e).__name__}") from e
        if not buf:
            raise WindyFlyError("bridge closed with no response")
        try:
            resp = json.loads(buf.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise WindyFlyError("bridge sent malformed JSON") from e
        if resp.get("error"):
            raise WindyFlyError(str(resp["error"]))
        return resp.get("result") or {}

    # -- public API ------------------------------------------------------------

    def respond(self, message: str, session_id: str | None = None) -> str:
        """Blocking full-reply turn. Banner stripped. Raises WindyFlyError."""
        result = self._call("agent.respond",
                            {"message": message, "session_id": session_id or ""})
        return strip_banner(result.get("response", ""))

    def respond_segments(self, message: str,
                         session_id: str | None = None) -> Iterator[str]:
        """Yield the reply as TTS-ready sentence segments (voice-session.v1 §10).

        Prefers a streaming bridge (`agent.respond_stream` → {segments|response});
        falls back to `agent.respond` + client-side §10 chunking. Raises
        WindyFlyError on transport fault."""
        if not self._stream_unsupported:
            try:
                result = self._call("agent.respond_stream",
                                    {"message": message, "session_id": session_id or ""})
                segments = result.get("segments")
                if segments is not None:
                    for seg in segments:
                        cleaned = strip_banner(seg).strip()
                        if cleaned:
                            yield cleaned
                    return
                # bridge answered but without segments → segment its response text
                text = strip_banner(result.get("response", ""))
                yield from segment_stream([text])
                return
            except WindyFlyError as e:
                if "Unknown method" not in str(e):
                    raise
                self._stream_unsupported = True  # this bridge is respond-only; don't re-probe
        # fallback: single blocking call, segmented client-side
        yield from segment_stream([self.respond(message, session_id)])


def strip_banner(text: str) -> str:
    """Remove a leading `[🪰 Windy Fly · … ]` status banner if present."""
    return _BANNER.sub("", text or "", count=1).lstrip("\n")
