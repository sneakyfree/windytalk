"""The Brain socket (ADR-058 D1). Every LLM turn goes through Windy Mind."""
from __future__ import annotations

from .base import BrainEvent, BrainProvider, ToolCall
from .mind import MindBrain

_PROVIDERS = {
    "mind": MindBrain,
}


def get_brain(name: str = "mind", **kwargs) -> BrainProvider:
    try:
        cls = _PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown brain provider {name!r}; have {sorted(_PROVIDERS)}"
        ) from None
    return cls(**kwargs)


__all__ = ["get_brain", "BrainProvider", "BrainEvent", "ToolCall", "MindBrain"]
