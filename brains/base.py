"""Brain provider abstraction (ADR-058 D1 / ADR-044).

Every LLM turn goes through a BrainProvider. The one real path is Windy Mind
(brains/mind.py → POST api.windymind.ai/v1/chat); openai_compat covers non-Mind
OpenAI-compatible endpoints. The engine never calls a model vendor directly.

A brain turn is a *stream* of events so the engine can sentence-chunk TTS as
tokens arrive (voice-session.v1 §10). Providers MUST NOT raise into the voice
loop on a network fault — they yield a terminal `error` event instead.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class BrainEvent:
    """One event in a brain turn.

    kind:
      "text"       — `text` is an incremental assistant-text delta
      "tool_calls" — `tool_calls` holds the assembled calls for this turn
      "done"       — end of turn; `finish_reason` set
      "error"      — `message` explains; the turn is over (caller speaks a fallback)
    """

    kind: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    message: str = ""


class BrainProvider(ABC):
    name: str = "base"

    @abstractmethod
    def stream(self, messages: list[dict], tools: list[dict] | None = None,
               model: str | None = None) -> Iterator[BrainEvent]:
        """Stream a turn. Yields text deltas, then any tool_calls, then done —
        or a single error event. Never raises for transport faults."""
        raise NotImplementedError
