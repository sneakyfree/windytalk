"""The Agent socket (ADR-058 D2/D3). Pair any runtime; talk to a Windy Fly agent."""
from __future__ import annotations

from .connect import ConnectError, WindyConnect
from .windyfly import WindyFlyAgent, WindyFlyError, strip_banner

__all__ = ["WindyFlyAgent", "WindyFlyError", "strip_banner",
           "WindyConnect", "ConnectError"]
