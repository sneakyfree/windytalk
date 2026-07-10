"""Windy Connect pairing (ADR-058 D2).

Pairs the machine with the Windy ecosystem via `windy-connect` (CLI: `windy`),
then reads the resulting bundle so Windy Talk can address the user's own paired
account — chiefly the Brain: the bundle's `windy_mind` section is an
OpenAI-compatible endpoint + key that becomes a MindBrain, so a paired user
talks through THEIR Mind account instead of the dev key (the Phase-2 BYO-account
path, wired here at the socket).

Pairing itself (`windy connect`) needs a one-time browser device-code step — that
part is the user's. Everything after (reading the bundle, building the brain
handle) is what this adapter owns, and it's fully exercised via the CLI's
`--mock` bundle (no browser).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path


class ConnectError(RuntimeError):
    pass


def _state_path(home: str | None) -> Path:
    base = Path(home) if home else Path.home()
    return base / ".windy" / "state.json"


class WindyConnect:
    def __init__(self, home: str | None = None) -> None:
        self.home = home
        self.state_path = _state_path(home)

    # -- read the paired state -------------------------------------------------

    def is_connected(self) -> bool:
        b = self._bundle_or_none()
        if b is None:
            return False
        exp = b.get("expires_at")
        if exp:
            try:
                if _parse_iso(exp) < datetime.now(UTC):
                    return False
            except ValueError:
                pass
        return True

    def bundle(self) -> dict:
        b = self._bundle_or_none()
        if b is None:
            raise ConnectError(
                f"not connected (no {self.state_path}); run `windy connect` first"
            )
        return b

    def _bundle_or_none(self) -> dict | None:
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return state.get("bundle")

    # -- the Brain handle (the point of pairing, for Windy Talk) ---------------

    def mind_config(self) -> dict:
        """{base_url, api_key, model} from the paired bundle's windy_mind section.

        The base_url is pinned to https + an allowlisted host so a poisoned bundle
        (pairing MITM, or a process that wrote state.json) can't route the user's
        private transcripts + Mind key to an attacker."""
        mind = self.bundle().get("windy_mind")
        if not mind:
            raise ConnectError("bundle has no windy_mind section")
        base_url = mind.get("base_url", "https://api.windymind.ai/v1")
        _require_trusted_url(base_url)
        return {
            "base_url": base_url,
            "api_key": mind.get("api_key", ""),
            "model": mind.get("default_model") or None,
        }

    def mind_brain(self):
        """Construct a MindBrain addressed at the paired user's own Mind account."""
        from brains.mind import MindBrain
        cfg = self.mind_config()
        model = None if cfg["model"] in (None, "windy-mind-auto") else cfg["model"]
        return MindBrain(base_url=cfg["base_url"], api_key=cfg["api_key"], model=model)

    # -- pairing (invokes the CLI; real pairing needs a browser) ---------------

    def pair(self, agents: str = "generic", *, mock: bool = False,
             with_eternitas: bool = False, timeout: float = 120) -> dict:
        """Run `windy connect`. `mock=True` uses the CLI's in-memory bundle (no
        browser) — for tests/dev. Returns the resulting bundle."""
        # Always decide the Eternitas prompt (else the CLI blocks on it); --mock is
        # additive (skips the real OAuth/orchestrator call).
        cmd = ["windy", "connect", "--force",
               "--with-eternitas" if with_eternitas else "--no-eternitas"]
        if mock:
            cmd.append("--mock")
        env = dict(os.environ)
        if self.home:
            env["HOME"] = self.home
        try:
            subprocess.run(cmd, input=f"{agents}\n", text=True, env=env,
                           capture_output=True, timeout=timeout, check=True)
        except subprocess.CalledProcessError as e:
            raise ConnectError(f"windy connect failed: {e.stderr[-300:]}") from e
        except FileNotFoundError as e:
            raise ConnectError("`windy` CLI not found; pip install windy-connect") from e
        except subprocess.TimeoutExpired as e:
            raise ConnectError("windy connect timed out (browser step?)") from e
        return self.bundle()


_TRUSTED_HOST_SUFFIXES = (".windymind.ai", ".windyconnect.com")


def _require_trusted_url(url: str) -> None:
    """Enforce https + an allowlisted host on a bundle-supplied brain URL."""
    from urllib.parse import urlparse
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    trusted = host == "api.windymind.ai" or any(host.endswith(s) for s in _TRUSTED_HOST_SUFFIXES)
    if p.scheme != "https" or not trusted:
        raise ConnectError(
            f"refusing untrusted brain URL {url!r} (must be https + a windymind host)")


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
