"""Task 1.4 tests for the hands surface — tier gating, dispatch, HTTP + MCP —
against a fake backend (no real desktop)."""
import json
import urllib.request

import pytest

from hands import HandsSurface, TierPolicy
from hands.backends.base import HandsBackend, UnsupportedTool
from hands.tiers import deny_all, load_tiers


class FakeBackend(HandsBackend):
    name = "fake"

    def __init__(self):
        self.calls = []

    def _rec(self, _tool, **kw):
        self.calls.append((_tool, kw))
        return f"did {_tool} {kw}"

    def open_app(self, name): return self._rec("open_app", name=name)
    def web_search(self, query): return self._rec("web_search", query=query)
    def open_url(self, url): return self._rec("open_url", url=url)
    def type_text(self, text): return self._rec("type_text", text=text)
    def press_keys(self, combo): return self._rec("press_keys", combo=combo)
    def click_element(self, label): return self._rec("click_element", label=label)
    def mouse_click(self, x, y, button="left"): return self._rec("mouse_click", x=x, y=y, button=button)
    def scroll(self, amount): return self._rec("scroll", amount=amount)
    def read_screen(self): return self._rec("read_screen")
    def list_apps(self): return self._rec("list_apps")
    def screenshot(self, path=None): return self._rec("screenshot", path=path)
    def run_shell(self, command):
        return self._rec("run_shell", command=command)


def surface(confirmer=deny_all):
    return HandsSurface(backend=FakeBackend(), policy=TierPolicy(confirmer=confirmer))


def test_contract_defines_all_twelve_tiers():
    tiers = load_tiers()
    assert set(tiers) == {
        "open_app", "web_search", "open_url", "type_text", "press_keys",
        "click_element", "mouse_click", "scroll", "read_screen", "list_apps",
        "screenshot", "run_shell"}


def test_auto_allow_runs_without_confirmer():
    s = surface()  # deny_all confirmer, but open_app is auto_allow
    res = s.invoke("open_app", {"name": "calculator"})
    assert res == {"ok": True, "result": "did open_app {'name': 'calculator'}"}


def test_always_confirm_denied_by_default():
    s = surface()  # deny_all
    res = s.invoke("run_shell", {"command": "echo hi"})
    assert res == {"ok": False, "error": "denied"}
    assert s.backend.calls == []  # never reached the backend


def test_always_confirm_runs_when_approved():
    seen = []

    def yes(tool, args, tier):
        seen.append((tool, tier))
        return True
    s = surface(confirmer=yes)
    res = s.invoke("run_shell", {"command": "echo hi"})
    assert res["ok"] and "run_shell" in res["result"]
    assert seen == [("run_shell", "always_confirm")]


def test_ask_first_session_upgrade():
    calls = []

    def yes(tool, args, tier):
        calls.append(tool)
        return True
    s = surface(confirmer=yes)
    # mouse_click is ask_first; grant a session upgrade via the sentinel
    s.invoke("mouse_click", {"x": 1, "y": 2, "_always_allow": True})
    s.invoke("mouse_click", {"x": 3, "y": 4})   # should NOT re-prompt
    assert calls == ["mouse_click"]             # confirmer hit once
    # sentinel was filtered out of the backend call
    assert all("_always_allow" not in kw for _, kw in s.backend.calls)


def test_always_confirm_never_upgrades():
    calls = []

    def yes(tool, args, tier):
        calls.append(tool)
        return True
    s = surface(confirmer=yes)
    s.invoke("run_shell", {"command": "a", "_always_allow": True})
    s.invoke("run_shell", {"command": "b"})
    assert calls == ["run_shell", "run_shell"]   # confirmed BOTH times


def test_unknown_tool():
    assert surface().invoke("frobnicate", {})["error"].startswith("unknown tool")


def test_unsupported_maps_cleanly():
    class NoScreenshot(FakeBackend):
        def screenshot(self, path=None):
            raise UnsupportedTool()
    s = HandsSurface(backend=NoScreenshot(),
                     policy=TierPolicy(confirmer=lambda *a: True))
    assert s.invoke("screenshot", {}) == {"ok": False, "error": "unsupported"}


# ---------- HTTP + MCP transports ----------

@pytest.fixture
def served():
    s = surface(confirmer=lambda *a: True)
    host, port = s.serve(port=0)  # ephemeral port
    yield s, f"http://{host}:{port}"
    s.stop()


def _get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read())


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def test_http_tools_and_invoke(served):
    _, base = served
    tools = _get(base + "/tools")["tools"]
    assert len(tools) == 12 and any(t["tier"] == "always_confirm" for t in tools)
    res = _post(base + "/invoke", {"tool": "open_app", "args": {"name": "calc"}})
    assert res["ok"] and "open_app" in res["result"]


def test_mcp_list_and_call(served):
    _, base = served
    lst = _post(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in lst["result"]["tools"]]
    assert "run_shell" in names and "inputSchema" in lst["result"]["tools"][0]
    call = _post(base + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "list_apps", "arguments": {}}})
    assert call["result"]["isError"] is False
    assert "list_apps" in call["result"]["content"][0]["text"]
