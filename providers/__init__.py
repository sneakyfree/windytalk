"""Provider registry — add a brain here and it becomes selectable everywhere."""
from providers.base import Brain


def _openai(**kw):
    from providers.openai_realtime import OpenAIRealtimeBrain
    return OpenAIRealtimeBrain(**kw)


def _gemini(**kw):
    from providers.gemini_live import GeminiLiveBrain
    return GeminiLiveBrain(**kw)


def _local(**kw):
    from providers.local import LocalBrain
    return LocalBrain(**kw)


# name -> factory (lazy so we don't import an SDK unless that provider is used)
REGISTRY = {
    "local": _local,     # Veron-1-5090 brain server (faster-whisper + Ollama + Kokoro)
    "gemini": _gemini,   # Google Gemini Live (cloud)
    "openai": _openai,   # OpenAI Realtime (cloud)
    # future: "aws" (Nova Sonic)
}


def get(name: str, **kw) -> Brain:
    name = (name or "").lower()
    if name not in REGISTRY:
        raise ValueError(f"Unknown provider {name!r}. Options: {', '.join(REGISTRY)}")
    return REGISTRY[name](**kw)


def names():
    return list(REGISTRY)
