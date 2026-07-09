"""
Brain = a swappable voice backend. Each provider (OpenAI Realtime, Gemini Live,
later AWS Nova Sonic / PumpMe / local) implements this one interface, so the main
loop and the desktop "hands" never change when you pivot between them.

A Brain owns its own transport and its own conversation loop. The main loop hands
it a Mic, a Speaker, and a `dispatch(name, args) -> str` callback for tool calls;
the Brain streams audio both ways, handles barge-in via speaker.clear(), and calls
dispatch() whenever the model invokes a desktop tool.
"""
from __future__ import annotations

import abc


class Brain(abc.ABC):
    #: human label shown in the UI / logs
    name: str = "brain"
    #: microphone sample rate this provider expects (Hz)
    input_rate: int = 24000
    #: sample rate of the audio this provider returns (Hz)
    output_rate: int = 24000

    @property
    @abc.abstractmethod
    def ready(self) -> tuple[bool, str]:
        """(usable?, reason) — e.g. (False, 'set GOOGLE_API_KEY') if creds missing."""

    @abc.abstractmethod
    async def run(self, mic, speaker, dispatch, log) -> None:
        """
        Connect and run one conversation session until the connection ends.
        Raise on transport error so the main loop can reconnect.
          mic      : audio.Mic     -> await mic.read() -> pcm16 bytes @ input_rate
          speaker  : audio.Speaker -> speaker.play(pcm) / speaker.clear() (barge-in)
          dispatch : callable(name:str, args:dict) -> str   (runs a desktop tool)
          log      : callable(str)                          (status/transcript line)
        """
