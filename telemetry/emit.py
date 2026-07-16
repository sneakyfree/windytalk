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
# Wire timeout for one send. The genome's ≤200ms budget is about never blocking
# the voice loop — sends run on daemon threads, so the CALLER budget stays ~0
# regardless. 200ms on the wire proved impossible post-VPS-migration (TLS
# handshake alone to admin.windyword.ai = 320-420ms from Fort Anne; measured
# 2026-07-15: 100/100 events dropped). 1.5s delivers while staying lossy-by-
# design on a truly dead network.
TIMEOUT_S = float(os.environ.get("WINDYTALK_TELEMETRY_TIMEOUT", "1.5"))
USER_AGENT = "windytalk/1.0"  # never the urllib default (CF WAF 403s Python-urllib/*)

_SSL_CTX = None


def _https_ctx():
    """CA-bundle TLS context — Homebrew/macOS urllib has cafile=None (every
    HTTPS call fails verification). Prefer certifi, else default."""
    global _SSL_CTX
    if _SSL_CTX is None:
        import ssl
        try:
            import certifi
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX

_SERVICE = "windytalk"
_PLATFORM = "windy-talk"

# Mirrors contracts/telemetry.v1.json $defs.event — the only keys that can leave.
_ALLOWED_FIELDS = frozenset({
    "event_type", "actor_type", "actor_id", "session_id", "user_id", "agent_id",
    "ts", "dur_ms", "turns", "model", "cost_microcents", "latency_ms", "tool",
    "tier_outcome", "error_code", "region", "metadata",
})

# metadata (INTEL-CONTRACT-V2) — non-content descriptors only; sub-keys whitelisted
# so a caller can never smuggle content through the metadata object either.
_ALLOWED_METADATA = frozenset({
    "app_version", "install_id", "os", "device", "region", "arch", "locale",
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
        if key not in _ALLOWED_FIELDS or value is None:
            continue
        if key == "metadata" and isinstance(value, dict):
            event[key] = {k: v for k, v in value.items() if k in _ALLOWED_METADATA}
        else:
            event[key] = value
    event.setdefault("actor_type", "system")
    event.setdefault("session_id", "none")
    # the live ingest requires ts (probed 2026-07-09); stamp it so callers never must
    event.setdefault(
        "ts",
        datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return event


def _send(url: str, token: str, body: bytes) -> int | None:
    """POST one batch. Returns the HTTP status (for tests/diagnostics); never raises."""
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_https_ctx()) as resp:
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
