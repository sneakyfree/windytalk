"""The engine-box supervisor — the doctor that is NOT in the patient.

Owns the voice-engine WORKER (engine/server.py) as a subprocess and hosts the
engine.mcp.v1 control surface (engine/control.py). Because the supervisor owns
the worker, restart_engine recovers a HUNG worker — the whole point of a
control surface that lives outside the thing it heals.

Run on the engine box:
    python -m server.supervise --port 8783 --engine-port 8788

The worker spawn command is injectable so this is testable with a dummy process
(no GPU/engine needed) — the supervision logic is what matters, not the model.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from threading import Lock, Thread

from engine.control import EngineController, EngineControlSurface

_DEFAULTS = {"stt": "whisper", "tts": "kokoro", "brain": "mind", "device": "cuda"}


class EngineSupervisor(EngineController):
    """Owns the engine worker subprocess + implements the control operations."""

    def __init__(self, spawn_cmd: list[str] | None = None, *, engine_port: int = 8788,
                 config: dict | None = None, log_capacity: int = 500) -> None:
        self._engine_port = engine_port
        self._config = {**_DEFAULTS, **(config or {}), "port": engine_port}
        self._default_config = dict(self._config)
        self._spawn_cmd = spawn_cmd or [
            sys.executable, "-m", "engine.server",
            "--host", "0.0.0.0", "--port", str(engine_port),
        ]
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._mode = "down"
        self._logs: deque[str] = deque(maxlen=log_capacity)
        self._lock = Lock()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            self._spawn_locked()

    def _spawn_locked(self) -> None:
        self._proc = subprocess.Popen(
            self._spawn_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**os.environ})
        self._started_at = time.time()
        self._mode = "normal"
        if self._proc.stdout is not None:
            Thread(target=self._drain_logs, args=(self._proc,), daemon=True).start()

    def _drain_logs(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self._logs.append(line.rstrip("\n"))

    def stop(self) -> None:
        with self._lock:
            self._terminate_locked()
            self._mode = "down"

    def _terminate_locked(self) -> None:
        p = self._proc
        if p is None:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()  # a HUNG worker: the supervisor still recovers it
                p.wait(timeout=5)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
        self._proc = None

    # -- EngineController: reads ----------------------------------------------

    def _worker_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _serving(self) -> bool:
        """Best-effort: can we open a TCP connection to the engine's ws port?"""
        try:
            with socket.create_connection(("127.0.0.1", self._engine_port), timeout=0.5):
                return True
        except OSError:
            return False

    @staticmethod
    def _gpu() -> bool | None:
        """Best-effort GPU presence via nvidia-smi (no heavy torch import — the
        control surface must stay light and dependency-free). None = can't tell
        (no nvidia-smi ⇒ maybe a non-NVIDIA / CPU box), not a definitive False."""
        import shutil
        if shutil.which("nvidia-smi") is None:
            return None
        try:
            r = subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                               text=True, timeout=5)
            return r.returncode == 0 and "GPU" in (r.stdout or "")
        except Exception:  # noqa: BLE001
            return False

    def health(self) -> dict:
        alive = self._worker_alive()
        serving = alive and self._serving()
        return {
            "healthy": bool(alive and serving and self._mode == "normal"),
            "worker_alive": alive,
            "providers_warm": serving,  # a serving ws implies warmed providers
            "gpu": self._gpu(),
            "serving": serving,
            "active_sessions": 0,  # the worker owns the true count; 0 until wired
            "mode": self._mode,
        }

    def status(self) -> dict:
        alive = self._worker_alive()
        state = "restarting" if self._mode == "restarting" else (
            "serving" if (alive and self._serving()) else ("warming" if alive else "down"))
        uptime = (time.time() - self._started_at) if (alive and self._started_at) else None
        return {"state": state, "active_sessions": 0,
                "uptime_s": round(uptime, 1) if uptime is not None else None}

    def config(self) -> dict:
        return dict(self._config)

    def logs(self, lines: int) -> str:
        tail = list(self._logs)[-lines:]
        return "\n".join(tail) if tail else "(no engine logs captured yet)"

    def selftest(self) -> dict:
        """Exercise the real STT→TTS round-trip via engine/pipeline.py in a
        subprocess (isolates a crash from the supervisor). Pass = exit 0 + a
        non-empty wav produced."""
        t0 = time.time()
        with tempfile.TemporaryDirectory(prefix="wt-engine-selftest-") as td:
            out = Path(td) / "reply.wav"
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "engine.pipeline",
                     "--phrase", "engine self test", "--out", str(out)],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ})
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "stages": [
                    {"stage": "roundtrip", "pass": False, "detail": str(e)[:200],
                     "ms": round((time.time() - t0) * 1000, 1)}]}
            produced = out.exists() and out.stat().st_size > 0
            ok = r.returncode == 0 and produced
            detail = ("stt→tts round-trip produced audio" if ok
                      else (r.stderr or r.stdout or "no audio produced").strip()[-200:])
            return {"ok": ok, "stages": [
                {"stage": "roundtrip", "pass": ok, "detail": detail,
                 "ms": round((time.time() - t0) * 1000, 1)}]}

    # -- EngineController: actions --------------------------------------------

    def reconnect(self) -> str:
        with self._lock:
            if not self._worker_alive():
                self._spawn_locked()
                return "engine worker was down — respawned it (providers re-warm on next session)"
        return "engine worker is alive; providers re-warm on the next session"

    def restart_engine(self) -> str:
        with self._lock:
            self._mode = "restarting"
            self._terminate_locked()
            self._spawn_locked()
        return "restarting the engine worker"

    def reset_to_defaults(self) -> str:
        with self._lock:
            self._config = dict(self._default_config)
            self._mode = "restarting"
            self._terminate_locked()
            self._spawn_locked()
        return "reset engine config to defaults; restarting the worker"


# -- surfaces.json registration (Python side, ADR-060 §3.8) --------------------

def _registry_path(home: str | None = None) -> Path:
    return Path(home or Path.home()) / ".windy" / "surfaces.json"


def register_surface(entry: dict, home: str | None = None) -> None:
    import json
    try:
        f = _registry_path(home)
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            doc = json.loads(f.read_text())
            surfaces = doc.get("surfaces", []) if isinstance(doc, dict) else []
        except (OSError, json.JSONDecodeError):
            surfaces = []
        surfaces = [s for s in surfaces if s.get("product") != entry["product"]]
        surfaces.append(entry)
        tmp = f.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"surfaces": surfaces}, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(f)
    except Exception:  # noqa: BLE001 — discovery is best-effort
        pass


def unregister_surface(product: str, home: str | None = None) -> None:
    import json
    try:
        f = _registry_path(home)
        if not f.exists():
            return
        doc = json.loads(f.read_text())
        surfaces = [s for s in doc.get("surfaces", []) if s.get("product") != product]
        f.write_text(json.dumps({"surfaces": surfaces}, indent=2) + "\n")
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Windy Talk engine-box supervisor")
    ap.add_argument("--port", type=int, default=8783, help="control surface port")
    ap.add_argument("--engine-port", type=int, default=8788, help="engine worker ws port")
    args = ap.parse_args(argv)

    sup = EngineSupervisor(engine_port=args.engine_port)
    sup.start()
    surface = EngineControlSurface(sup)
    host, port = surface.serve("127.0.0.1", args.port)
    token_path = Path.home() / ".windytalk" / "engine-control-token"
    try:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(surface.token)
        token_path.chmod(0o600)
    except OSError:
        pass
    register_surface({
        "product": "windytalk-engine",
        "version": "0.1.0",
        "class": "cloud",
        "contract": "engine.mcp.v1",
        "doctrine": "ADR-060 v1.0",
        "http": f"http://127.0.0.1:{port}",
        "mcp": f"http://127.0.0.1:{port}/mcp",
        "token_path": str(token_path),
        "health": "/invoke get_health",
        "pid": os.getpid(),
    })
    print(f"[supervise] engine control surface on http://{host}:{port} "
          f"(worker on :{args.engine_port})", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        unregister_surface("windytalk-engine")
        surface.stop()
        sup.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
