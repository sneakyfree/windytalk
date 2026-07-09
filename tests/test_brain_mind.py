"""Task 1.1 unit tests for brains/mind.py — SSE parsing, tool-call assembly, and
the never-raise fault path, all with an injected fake stream (no network)."""
import urllib.error

import pytest

from brains import MindBrain, ToolCall, get_brain


def sse(*chunks: str):
    """Build SSE lines from raw JSON chunk strings + a [DONE] sentinel."""
    return [f"data: {c}" for c in chunks] + ["data: [DONE]"]


def fake_stream(lines):
    def _post_sse(self, body):
        yield from lines
    return _post_sse


def test_registry_and_defaults():
    b = get_brain("mind", api_key="k")
    assert isinstance(b, MindBrain) and b.name == "mind"
    assert b.base_url.endswith("/v1")
    assert b.model  # a concrete fast-TTFT default, not empty
    with pytest.raises(ValueError):
        get_brain("nope")


def test_text_streams_as_deltas(monkeypatch):
    lines = sse(
        '{"choices":[{"delta":{"content":"Opening "},"finish_reason":null}]}',
        '{"choices":[{"delta":{"content":"the calculator."},"finish_reason":null}]}',
        '{"choices":[{"delta":{},"finish_reason":"stop"}]}',
    )
    monkeypatch.setattr(MindBrain, "_post_sse", fake_stream(lines))
    events = list(MindBrain(api_key="k").stream([{"role": "user", "content": "hi"}]))
    texts = [e.text for e in events if e.kind == "text"]
    assert "".join(texts) == "Opening the calculator."
    assert events[-1].kind == "done" and events[-1].finish_reason == "stop"


def test_tool_calls_assemble_across_fragments(monkeypatch):
    # arguments stream as concatenated fragments across chunks
    lines = sse(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"open_app","arguments":"{\\"na"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"me\\":\\"calc\\"}"}}]}}]}',
        '{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    )
    monkeypatch.setattr(MindBrain, "_post_sse", fake_stream(lines))
    events = list(MindBrain(api_key="k").stream([{"role": "user", "content": "open calc"}]))
    tc_events = [e for e in events if e.kind == "tool_calls"]
    assert len(tc_events) == 1
    call = tc_events[0].tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.name == "open_app" and call.arguments == {"name": "calc"}
    assert events[-1].finish_reason == "tool_calls"


def test_unreachable_yields_error_not_raise(monkeypatch):
    def boom(self, body):
        raise urllib.error.URLError("no route")
        yield  # pragma: no cover
    monkeypatch.setattr(MindBrain, "_post_sse", boom)
    events = list(MindBrain(api_key="k").stream([{"role": "user", "content": "hi"}]))
    assert len(events) == 1 and events[0].kind == "error"
    assert "unreachable" in events[0].message.lower()


def test_malformed_chunks_are_skipped(monkeypatch):
    lines = ["data: not-json", "data: {}", *sse(
        '{"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}')]
    monkeypatch.setattr(MindBrain, "_post_sse", fake_stream(lines))
    events = list(MindBrain(api_key="k").stream([{"role": "user", "content": "hi"}]))
    assert "".join(e.text for e in events if e.kind == "text") == "ok"
    assert any(e.kind == "done" for e in events)
