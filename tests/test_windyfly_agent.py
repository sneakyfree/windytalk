"""Task 1.2 tests for agents/windyfly.py — banner strip, §10 segmentation, the
agent.respond_stream probe + fallback, and error handling. Uses a fake UDS bridge
in a background thread (no real agent needed)."""
import json
import os
import socket
import tempfile
import threading

import pytest

from agents import WindyFlyAgent, WindyFlyError, strip_banner

BANNER = "[🪰 Windy Fly · Jul 09, 12:00 AM · 🟢 99%]\n\n"


class FakeBridge:
    """Minimal UDS server that answers one method per connection."""

    def __init__(self, responder):
        self.responder = responder
        self.path = os.path.join(tempfile.mkdtemp(), "fake.sock")
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(8)
        self._stop = False
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        self._srv.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            with conn:
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    d = conn.recv(65536)
                    if not d:
                        break
                    buf.extend(d)
                req = json.loads(buf.decode())
                resp = self.responder(req.get("method"), req.get("params", {}))
                conn.sendall(json.dumps(resp).encode() + b"\n")

    def close(self):
        self._stop = True
        self._t.join(timeout=1)


@pytest.fixture
def bridge_factory():
    made = []

    def make(responder):
        b = FakeBridge(responder)
        made.append(b)
        return b
    yield make
    for b in made:
        b.close()


def test_strip_banner_removes_only_the_fly_banner():
    assert strip_banner(BANNER + "Hello there.") == "Hello there."
    assert strip_banner("No banner here.") == "No banner here."
    # a legitimate bracketed aside that isn't the fly banner is preserved
    assert strip_banner("[note] keep this") == "[note] keep this"


def test_respond_strips_banner(bridge_factory):
    def responder(method, params):
        assert method == "agent.respond"
        return {"id": "1", "result": {"response": BANNER + "Opening the calculator."},
                "error": None}
    b = bridge_factory(responder)
    agent = WindyFlyAgent(socket_path=b.path)
    assert agent.respond("open calc") == "Opening the calculator."


def test_respond_segments_uses_stream_when_available(bridge_factory):
    def responder(method, params):
        assert method == "agent.respond_stream"
        return {"id": "1", "result":
                {"segments": ["Opening the calculator.", "It is ready."]}, "error": None}
    b = bridge_factory(responder)
    agent = WindyFlyAgent(socket_path=b.path)
    assert list(agent.respond_segments("open calc")) == \
        ["Opening the calculator.", "It is ready."]


def test_respond_segments_falls_back_to_respond(bridge_factory):
    calls = []

    def responder(method, params):
        calls.append(method)
        if method == "agent.respond_stream":
            return {"id": "1", "result": None, "error": "Unknown method: agent.respond_stream"}
        return {"id": "1", "result":
                {"response": "Opening the calculator now. It is ready to use."}, "error": None}
    b = bridge_factory(responder)
    agent = WindyFlyAgent(socket_path=b.path)
    segs = list(agent.respond_segments("open calc"))
    assert segs == ["Opening the calculator now.", "It is ready to use."]
    assert calls == ["agent.respond_stream", "agent.respond"]
    # second call must not re-probe the missing stream method
    calls.clear()
    list(agent.respond_segments("again"))
    assert calls == ["agent.respond"]


def test_bridge_error_raises(bridge_factory):
    def responder(method, params):
        return {"id": "1", "result": None, "error": "boom"}
    b = bridge_factory(responder)
    with pytest.raises(WindyFlyError, match="boom"):
        WindyFlyAgent(socket_path=b.path).respond("x")


def test_unreachable_socket_raises():
    agent = WindyFlyAgent(socket_path="/nonexistent/nope.sock", timeout=1)
    with pytest.raises(WindyFlyError):
        agent.respond("x")
