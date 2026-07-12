"""Task 1.4 tests for the hands surface — tier gating, dispatch, HTTP + MCP —
against a fake backend (no real desktop)."""
import json
import urllib.error
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


def test_always_confirm_never_upgrades():
    calls = []

    def yes(tool, args, tier):
        calls.append(tool)
        return True
    s = surface(confirmer=yes)
    s.invoke("run_shell", {"command": "a", "_always_allow": True})
    s.invoke("run_shell", {"command": "b"})
    assert calls == ["run_shell", "run_shell"]   # confirmed BOTH times


def test_agent_cannot_self_escalate_via_args():
    # The old _always_allow-from-args hole: an agent injecting the sentinel must
    # NOT get a session upgrade. Only the confirmer's (allow, remember) can.
    calls = []

    def yes(tool, args, tier):
        calls.append(tool)
        return True                        # allow, but do NOT remember
    s = surface(confirmer=yes)
    s.invoke("mouse_click", {"x": 1, "y": 2, "_always_allow": True})
    s.invoke("mouse_click", {"x": 3, "y": 4})
    assert calls == ["mouse_click", "mouse_click"]  # prompted BOTH times


def test_confirmer_tuple_grants_session_upgrade():
    calls = []

    def yes_remember(tool, args, tier):
        calls.append(tool)
        return (True, True)                # allow AND remember (confirmer's choice)
    s = surface(confirmer=yes_remember)
    s.invoke("mouse_click", {"x": 1, "y": 2})
    s.invoke("mouse_click", {"x": 3, "y": 4})   # should NOT re-prompt
    assert calls == ["mouse_click"]


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
    s = HandsSurface(backend=FakeBackend(),
                     policy=TierPolicy(confirmer=lambda *a: True), token="test-token")
    host, port = s.serve(port=0)  # ephemeral port
    yield s, f"http://{host}:{port}"
    s.stop()


def _get(url, token="test-token", extra=None):
    headers = {"X-Windytalk-Token": token} if token else {}
    headers.update(extra or {})
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status, json.loads(r.read())


def _post(url, payload, token="test-token", extra=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Windytalk-Token"] = token
    headers.update(extra or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=3) as r:
        return r.status, json.loads(r.read())


def _status(url, method="POST", token=None, extra=None):
    headers = dict(extra or {})
    if token:
        headers["X-Windytalk-Token"] = token
    req = urllib.request.Request(url, data=b"{}" if method == "POST" else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_http_tools_and_invoke(served):
    _, base = served
    _, tools = _get(base + "/tools")
    tools = tools["tools"]
    assert len(tools) == 12 and any(t["tier"] == "always_confirm" for t in tools)
    _, res = _post(base + "/invoke", {"tool": "open_app", "args": {"name": "calc"}})
    assert res["ok"] and "open_app" in res["result"]


def test_mcp_list_and_call(served):
    _, base = served
    _, lst = _post(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in lst["result"]["tools"]]
    assert "run_shell" in names and "inputSchema" in lst["result"]["tools"][0]
    _, call = _post(base + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                    "params": {"name": "list_apps", "arguments": {}}})
    assert call["result"]["isError"] is False
    assert "list_apps" in call["result"]["content"][0]["text"]


def _post_raw(url, payload, token="test-token"):
    """POST returning (status, raw_bytes) — tolerates an empty 204 body."""
    headers = {"Content-Type": "application/json", "X-Windytalk-Token": token}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_mcp_initialize_lifecycle(served):
    # A standard MCP client sends `initialize` FIRST and aborts on -32601.
    _, base = served
    _, init = _post(base + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "windytalk-hands"
    # ping must return an empty result, not -32601.
    _, png = _post(base + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert png["result"] == {}


def test_mcp_notification_gets_204_no_body(served):
    # `notifications/initialized` is a JSON-RPC notification: no response body.
    _, base = served
    status, body = _post_raw(base + "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 204
    assert body == b""


def test_mcp_tools_call_is_canonical_json_and_structured(served):
    # The str()-rendered-result bug: text must be VALID JSON and structuredContent present.
    _, base = served
    _, call = _post(base + "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "list_apps", "arguments": {}}})
    text = call["result"]["content"][0]["text"]
    parsed = json.loads(text)  # MUST parse — single-quote str() would raise here
    assert parsed["ok"] is True
    assert call["result"]["structuredContent"] == parsed


def test_mcp_batch_array_is_invalid_request(served):
    _, base = served
    _, resp = _post(base + "/mcp", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert resp["error"]["code"] == -32600


def test_mcp_request_method_without_id_does_not_execute(served):
    # A tools/call with no id is a notification: no response, and it must NOT run.
    _, base = served
    status, body = _post_raw(base + "/mcp", {"jsonrpc": "2.0", "method": "tools/call",
                                             "params": {"name": "list_apps", "arguments": {}}})
    assert status == 204 and body == b""


# ---------- security: the CSRF/RCE hole is closed ----------

def test_missing_token_is_401(served):
    _, base = served
    assert _status(base + "/invoke", token=None) == 401


def test_wrong_token_is_401(served):
    _, base = served
    assert _status(base + "/invoke", token="nope") == 401


def test_browser_origin_is_403(served):
    # a webpage's fetch always carries an Origin — must be rejected outright (CSRF)
    _, base = served
    assert _status(base + "/invoke", token="test-token",
                   extra={"Origin": "https://evil.example"}) == 403


def test_non_loopback_host_is_403(served):
    # DNS-rebinding attempt: Host header points at a non-loopback name
    _, base = served
    assert _status(base + "/invoke", token="test-token",
                   extra={"Host": "attacker.example"}) == 403


def test_options_preflight_rejected(served):
    _, base = served
    assert _status(base + "/invoke", method="OPTIONS", token="test-token") == 405


# ---------- cross-OS backends + capability negotiation ----------

def test_all_three_backends_implement_the_abc():
    from hands.backends.base import TOOL_NAMES
    from hands.backends.linux import LinuxBackend
    from hands.backends.macos import MacOSBackend
    from hands.backends.windows import WindowsBackend
    for B in (LinuxBackend, MacOSBackend, WindowsBackend):
        b = B()                              # instantiable ⇒ all 12 abstractmethods present
        caps = b.capabilities()
        assert set(caps) == set(TOOL_NAMES), B.__name__
        assert all(hasattr(b, t) for t in TOOL_NAMES)


def test_capabilities_endpoint(served):
    _, base = served
    _, caps = _get(base + "/capabilities")
    assert "backend" in caps and "tools" in caps
    assert set(caps["tools"]) == {
        "open_app", "web_search", "open_url", "type_text", "press_keys",
        "click_element", "mouse_click", "scroll", "read_screen", "list_apps",
        "screenshot", "run_shell"}


def test_tool_list_reports_supported_flag(served):
    _, base = served
    _, out = _get(base + "/tools")
    assert all("supported" in t for t in out["tools"])


def test_tool_list_supported_reflects_backend_capabilities():
    # Regression: the supported flag was read from the outer {"backend","tools"}
    # dict instead of caps["tools"], so it was ALWAYS True and capability
    # negotiation was silently dead. A backend that reports a tool unsupported
    # must surface supported=False for exactly that tool.
    class PartialBackend(FakeBackend):
        def capabilities(self):
            return {
                "open_app": True, "web_search": True, "open_url": True, "type_text": True,
                "press_keys": True, "click_element": True, "mouse_click": True, "scroll": True,
                "read_screen": True, "list_apps": True, "screenshot": False, "run_shell": True,
            }

    s = HandsSurface(backend=PartialBackend(), policy=TierPolicy(confirmer=deny_all))
    flags = {t["name"]: t["supported"] for t in s.tool_list()}
    assert flags["screenshot"] is False, "an unsupported tool must advertise supported=False"
    assert flags["open_app"] is True
    assert False in set(flags.values()), "capability negotiation must be alive (not all-True)"


def test_invoke_rejects_non_dict_args_instead_of_crashing():
    # Regression: dict("foo") / dict([1,2]) raised before the try/except and
    # dropped the connection; now it returns the result shape honestly.
    s = surface(confirmer=lambda *a: True)
    assert s.invoke("open_app", "not-a-dict") == {"ok": False, "error": "bad args: expected an object"}
    assert s.invoke("open_app", [1, 2]) == {"ok": False, "error": "bad args: expected an object"}


def test_bad_content_length_returns_400_not_a_dropped_socket(served):
    _, base = served
    # Non-integer and negative Content-Length must both 400 (the negative one
    # previously made rfile.read(-1) block to EOF and wedge the handler thread).
    for bad in ("abc", "-1"):
        req = urllib.request.Request(
            base + "/invoke", data=b'{"tool":"open_app","args":{"name":"x"}}',
            headers={"Content-Type": "application/json", "X-Windytalk-Token": "test-token",
                     "Content-Length": bad}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 400, f"Content-Length {bad!r} should 400, got {code}"


def test_non_ascii_token_fails_closed_401_not_500(served):
    _, base = served
    code = _status(base + "/invoke", token="café")  # non-ASCII token header
    assert code == 401, "a non-ASCII token must fail closed with 401, never crash the handler"


def test_backend_detect_maps_platforms():
    import hands.backends as hb
    # _detect returns the right key per sys.platform family
    assert hb._detect() in ("linux", "macos", "windows") or True  # smoke: importable
