"""Windy Mind brain — the one real LLM path (ADR-058 D1).

POST {base}/chat with stream:true (canonical route is /v1/chat; the
/v1/chat/completions alias is live too, so `base` ending in /v1 also works with
OpenAI SDKs). OpenAI-compatible request/response shape, SSE deltas
(chat.completion.chunk with delta.content / delta.tool_calls, `data: [DONE]`
sentinel — verified live 2026-07-09).

Auth (Phase 1 sequencing): the dev Mind key from Task 0.0 (env WINDY_MIND_DEV_KEY)
carries 1.1–1.6. Task 1.7 swaps it for brokered Eternitas tokens and scrubs the
dev key from code and disk — nothing here hardcodes a key.

Latency: first-token time is dominated by the upstream model's TTFT (PROBE_RESULTS),
so the default model is a fast-TTFT one, not `auto` (which routes to Opus, ~3 s).
Override with WINDYTALK_BRAIN_MODEL.

Transport is stdlib urllib (no new deps); `_post_sse` is factored out so tests
inject a fake stream. Faults never raise into the voice loop — they surface as a
terminal BrainEvent(kind="error").
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator

from .base import BrainEvent, BrainProvider, ToolCall

DEFAULT_BASE = "https://api.windymind.ai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"  # fast, consistent TTFT (PROBE_RESULTS)
DEFAULT_TIMEOUT = 30.0
USER_AGENT = "windytalk/1.0"  # never the urllib default (CF WAF 403s Python-urllib/*)

_SSL_CTX = None


def _https_ctx():
    """TLS context with a real CA bundle. Homebrew/macOS Python ships urllib
    with cafile=None, so every HTTPS call fails cert verification and the brain
    silently falls back to 'trouble reaching my brain' (found on the OC5 CPU
    engine 2026-07-16). Prefer certifi; fall back to the default (fine where
    system certs work, e.g. Linux/Windows)."""
    global _SSL_CTX
    if _SSL_CTX is None:
        import ssl
        try:
            import certifi
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


class MindBrain(BrainProvider):
    name = "mind"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = (base_url or os.environ.get("WINDYTALK_MIND_URL", DEFAULT_BASE)).rstrip("/")
        self.api_key = api_key or os.environ.get("WINDYTALK_MIND_KEY") \
            or os.environ.get("WINDY_MIND_DEV_KEY", "")
        self.model = model or os.environ.get("WINDYTALK_BRAIN_MODEL", DEFAULT_MODEL)
        self.timeout = timeout

    # -- transport (injectable for tests) --------------------------------------

    def _post_sse(self, body: dict) -> Iterator[str]:
        """POST and yield decoded SSE lines. Raises on transport/HTTP error
        (stream() converts that into an error event)."""
        req = urllib.request.Request(
            f"{self.base_url}/chat",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "Accept": "text/event-stream",
                     # explicit UA: CF WAF on api.windymind.ai 403s default Python-urllib/*
                     "User-Agent": USER_AGENT},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout, context=_https_ctx()) as resp:
            for raw in resp:
                yield raw.decode("utf-8", "replace").rstrip("\r\n")

    def _post_json(self, body: dict) -> dict:
        """POST a non-streaming request and return the parsed JSON reply.
        Raises on transport/HTTP error (callers convert to an error event)."""
        req = urllib.request.Request(
            f"{self.base_url}/chat",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout, context=_https_ctx()) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _nonstream_turn(self, body: dict) -> Iterator[BrainEvent]:
        """One whole-reply turn: same events as stream(), delivered in one burst."""
        try:
            data = self._post_json(body)
            message = ((data.get("choices") or [{}])[0]).get("message") or {}
            finish = ((data.get("choices") or [{}])[0]).get("finish_reason")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            yield BrainEvent(kind="error",
                             message=f"Mind unreachable: {type(e).__name__}")
            return
        except Exception as e:  # never let the voice loop die on the brain
            yield BrainEvent(kind="error", message=f"Mind error: {type(e).__name__}")
            return
        content = message.get("content")
        if content:
            yield BrainEvent(kind="text", text=content)
        frags: dict[int, dict] = {}
        for i, tc in enumerate(message.get("tool_calls") or []):
            _accumulate_tool_call(frags, {**tc, "index": tc.get("index", i)})
        calls = _assemble_tool_calls(frags)
        if calls:
            yield BrainEvent(kind="tool_calls", tool_calls=calls)
        yield BrainEvent(kind="done", finish_reason=finish or "stop")

    # -- streaming turn --------------------------------------------------------

    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               model: str | None = None) -> Iterator[BrainEvent]:
        body: dict = {"model": model or self.model, "messages": messages,
                      "stream": True}
        if tools:
            body["tools"] = tools
            # Mind's streaming path drops delta.tool_calls end-to-end (adapters
            # parse only delta.content; the SSE re-encoder forwards only content
            # — windy-mind#75), so a tool-armed turn must go non-streaming or
            # the model's calls never arrive. WINDYTALK_MIND_STREAM_TOOLS=1
            # re-enables streaming once Mind ships the fix.
            if os.environ.get("WINDYTALK_MIND_STREAM_TOOLS") != "1":
                body["stream"] = False
                yield from self._nonstream_turn(body)
                return

        # tool-call fragments accumulate by index across chunks (OpenAI streaming shape)
        tool_frags: dict[int, dict] = {}
        finish_reason = None
        try:
            for line in self._post_sse(body):
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    yield BrainEvent(kind="text", text=content)
                for tc in delta.get("tool_calls") or []:
                    _accumulate_tool_call(tool_frags, tc)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            yield BrainEvent(kind="error",
                             message=f"Mind unreachable: {type(e).__name__}")
            return
        except Exception as e:  # never let the voice loop die on the brain
            yield BrainEvent(kind="error", message=f"Mind error: {type(e).__name__}")
            return

        calls = _assemble_tool_calls(tool_frags)
        if calls:
            yield BrainEvent(kind="tool_calls", tool_calls=calls)
        yield BrainEvent(kind="done", finish_reason=finish_reason or "stop")


def _accumulate_tool_call(frags: dict[int, dict], tc: dict) -> None:
    idx = tc.get("index", 0)
    slot = frags.setdefault(idx, {"id": "", "name": "", "args": ""})
    if tc.get("id"):
        slot["id"] = tc["id"]
    fn = tc.get("function") or {}
    if fn.get("name"):
        slot["name"] = fn["name"]
    if fn.get("arguments"):
        slot["args"] += fn["arguments"]  # arguments stream as concatenated fragments


def _assemble_tool_calls(frags: dict[int, dict]) -> list[ToolCall]:
    calls = []
    for idx in sorted(frags):
        slot = frags[idx]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args"]) if slot["args"] else {}
        except json.JSONDecodeError:
            args = {}
        calls.append(ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"],
                              arguments=args))
    return calls
