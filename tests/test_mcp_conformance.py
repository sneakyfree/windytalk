"""Shared MCP conformance — the HANDS (Python) driver.

Feeds every case in contracts/mcp-conformance.v1.json to hands.surface.handle_mcp
and asserts the result. The behaviors live in that one shared file; the TS
control surface runs the SAME cases through its own driver
(apps/desktop/test/mcp-conformance.test.ts). Neither rail can drift from the
rulebook without this failing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hands import HandsSurface, TierPolicy
from hands.backends.base import HandsBackend

_RULEBOOK = Path(__file__).resolve().parent.parent / "contracts" / "mcp-conformance.v1.json"
_SAFE_READ_TOOL = "list_apps"  # side-effect-free read tool on the hands surface
_MISSING = object()


class _FakeBackend(HandsBackend):
    name = "conformance-fake"

    def open_app(self, name): return f"open {name}"
    def web_search(self, query): return f"search {query}"
    def open_url(self, url): return f"url {url}"
    def type_text(self, text, target=None): return "typed"
    def press_keys(self, combo): return "pressed"
    def click_element(self, label): return "clicked"
    def mouse_click(self, x, y, button="left"): return "moused"
    def scroll(self, amount): return "scrolled"
    def read_screen(self): return "screen"
    def list_apps(self): return ["App A", "App B"]
    def screenshot(self, path=None): return "shot"
    def run_shell(self, command): return "ran"


def _surface() -> HandsSurface:
    return HandsSurface(backend=_FakeBackend(),
                        policy=TierPolicy(confirmer=lambda *a: True),
                        token="conformance")


# -- the shared evaluator (kept byte-for-byte equivalent to the TS driver's) ----

def _subst(obj, tool):
    if isinstance(obj, dict):
        return {k: _subst(v, tool) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_subst(v, tool) for v in obj]
    if obj == "$SAFE_READ_TOOL":
        return tool
    return obj


def _get(obj, path):
    cur = obj
    for seg in path.split("."):
        neg = seg.startswith("-")
        if seg.isdigit() or (neg and seg[1:].isdigit()):
            if not isinstance(cur, list):
                return _MISSING
            i = int(seg)
            if not (-len(cur) <= i < len(cur)):
                return _MISSING
            cur = cur[i]
        elif isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return _MISSING
    return cur


def _type_name(v):
    if v is _MISSING or v is None:
        return "null" if v is None else "missing"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "unknown"


def _check(resp, assertion):
    op = assertion[0]
    if op == "equal":
        assert _get(resp, assertion[1]) == assertion[2], f"equal {assertion[1]}"
    elif op == "type":
        assert _type_name(_get(resp, assertion[1])) == assertion[2], f"type {assertion[1]}"
    elif op == "nonempty_array":
        v = _get(resp, assertion[1])
        assert isinstance(v, list) and len(v) >= 1, f"nonempty_array {assertion[1]}"
    elif op == "structured_matches_text":
        text = _get(resp, "result.content.0.text")
        assert isinstance(text, str), "content text must be a string"
        parsed = json.loads(text)  # MUST be valid JSON (never str()-rendered)
        assert parsed == _get(resp, "result.structuredContent"), "structuredContent must match the text"
    else:
        raise AssertionError(f"unknown assert op: {op}")


def _load_cases():
    doc = json.loads(_RULEBOOK.read_text())
    return doc["cases"]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["name"])
def test_hands_mcp_conformance(case):
    surface = _surface()
    request = _subst(case["request"], _SAFE_READ_TOOL)
    resp = surface.handle_mcp(request)
    expect = case["expect"]
    if expect.get("no_response"):
        assert resp is None, "a notification must produce no response"
        return
    assert resp is not None, "a request must produce a response"
    for assertion in expect["asserts"]:
        _check(resp, assertion)


def test_rulebook_is_shared_and_nonempty():
    # Guard: the same file the TS driver reads, and it actually has cases.
    assert _RULEBOOK.exists()
    assert len(_load_cases()) >= 8
