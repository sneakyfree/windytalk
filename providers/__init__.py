"""Provider registry — add a brain here and it becomes selectable everywhere."""
from providers.base import Brain


def _openai(**kw):
    from providers.openai_realtime import OpenAIRealtimeBrain
    return OpenAIRealtimeBrain(**kw)


def _gemini(**kw):
    from providers.gemini_live import GeminiLiveBrain
    return GeminiLiveBrain(**kw)


# name -> factory (lazy so we don't import an SDK unless that provider is used)
REGISTRY = {
    "gemini": _gemini,
    "openai": _openai,
    # future: "aws" (Nova Sonic), "pumpme"/"local" (Pipecat STT+LLM+TTS pipeline)
}


def get(name: str, **kw) -> Brain:
    name = (name or "").lower()
    if name not in REGISTRY:
        raise ValueError(f"Unknown provider {name!r}. Options: {', '.join(REGISTRY)}")
    return REGISTRY[name](**kw)


def names():
    return list(REGISTRY)
