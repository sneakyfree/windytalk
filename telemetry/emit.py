"""Content-free telemetry emitter (ADR-WA-001, contracts/telemetry.v1.json).

Fire-and-forget: emit() never raises, never blocks the caller beyond building a
dict, and is a silent no-op unless configured. The content-free guarantee is
structural — only keys in _ALLOWED_FIELDS survive, so a caller passing
transcript="..." leaks nothing even by accident.

Config (env):
  WINDYTALK_TELEMETRY_TOKEN  — per-emitter bearer token; absent → no-op
  WINDYTALK_TELEMETRY_URL    — default https://admin.windyword.ai/v1/events
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import urllib.request

DEFAULT_URL = "https://admin.windyword.ai/v1/events"
TIMEOUT_S = 0.2  # hard ceiling per the genome: ≤200 ms, then give up silently

_SERVICE = "windytalk"
_PLATFORM = "windy-talk"

# Mirrors contracts/telemetry.v1.json $defs.event — the only keys that can leave.
_ALLOWED_FIELDS = frozenset({
    "event_type", "actor_type", "session_id", "user_id", "agent_id", "ts",
    "dur_ms", "turns", "model", "cost_microcents", "latency_ms", "tool",
    "tier_outcome", "error_code", "region",
})

_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()


def _config() -> tuple[str, str] | None:
    token = os.environ.get("WINDYTALK_TELEMETRY_TOKEN", "").strip()
    if not token:
        return None
    return os.environ.get("WINDYTALK_TELEMETRY_URL", DEFAULT_URL), token


def _clean(fields: dict) -> dict:
    event = {"service": _SERVICE, "platform": _PLATFORM}
    for key, value in fields.items():
        if key in _ALLOWED_FIELDS and value is not None:
            event[key] = value
    event.setdefault("actor_type", "system")
    event.setdefault("session_id", "none")
    # the live ingest requires ts (probed 2026-07-09); stamp it so callers never must
    event.setdefault(
        "ts",
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return event


def _send(url: str, token: str, body: bytes) -> int | None:
    """POST one batch. Returns the HTTP status (for tests/diagnostics); never raises."""
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status
    except Exception:
        return None


def emit(event_type: str, **fields) -> None:
    """Emit one telemetry event. Never raises; no-op unless configured."""
    try:
        cfg = _config()
        if cfg is None:
            return
        url, token = cfg
        event = _clean({"event_type": event_type, **fields})
        body = json.dumps({"events": [event]}, default=str).encode("utf-8")
        thread = threading.Thread(
            target=_send, args=(url, token, body), daemon=True,
            name="windytalk-telemetry",
        )
        with _threads_lock:
            _threads[:] = [t for t in _threads if t.is_alive()]
            _threads.append(thread)
        thread.start()
    except Exception:
        pass  # missing telemetry is a bug, but raising into the voice path is worse


def flush(timeout_s: float = 1.0) -> None:
    """Best-effort join of in-flight sends (engine shutdown). Never raises."""
    try:
        with _threads_lock:
            pending = list(_threads)
        for thread in pending:
            thread.join(timeout=timeout_s)
    except Exception:
        pass
