"""Protocol definitions every adapter sits behind.

ARCHITECTURE.md §2: `nod_core` imports these protocols and never a concrete
adapter. That is what makes `FakeAssemblyAI` and offline tests possible (INV-7),
and `tests/unit/test_boundaries.py` fails if it is ever violated.

Protocol method bodies are `...` rather than `raise NotImplementedError`: a
protocol body is unreachable by construction, so raising there would be
misleading rather than informative.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from nod_core.types import SessionBegin, Termination, Turn, Voice

type AudioChunkStream = AsyncIterator[bytes]
"""Synthesised audio, streamed so barge-in can cancel mid-utterance."""


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of conversation handed to an `LlmClient`."""

    role: Literal["system", "user", "assistant"]
    content: str


@runtime_checkable
class SttSession(Protocol):
    """A live streaming speech-to-text session.

    The only upstream surface `nod_core.proxy` is allowed to touch.
    """

    id: str

    async def send_audio(self, frame: bytes) -> None:
        """Forward one PCM16 frame verbatim. Must not block on anything else."""
        ...

    async def update_configuration(self, patch: Mapping[str, float]) -> None:
        """Apply a mid-stream config change on this same socket.

        No reconnect, no session loss (CONTROL_SPEC.md §0).
        """
        ...

    async def force_endpoint(self) -> None:
        """End the current turn now, rather than waiting out the silence."""
        ...

    def events(self) -> AsyncIterator[SessionBegin | Turn | Termination]:
        """Yield upstream frames, partials included."""
        ...

    async def aclose(self) -> None:
        """Release the socket."""
        ...


@runtime_checkable
class LlmClient(Protocol):
    """The agent's brain. Never on the turn-timing decision path (CLAUDE.md §7)."""

    id: str

    def stream(
        self,
        messages: Sequence[Message],
        *,
        timeout_ms: int,
    ) -> AsyncIterator[str]:
        """Stream a reply. On timeout the caller emits a filler (EC-26)."""
        ...

    def cancel(self) -> None:
        """Abandon the in-flight completion."""
        ...


@runtime_checkable
class TtsEngine(Protocol):
    """A speech synthesiser, as specified in ARCHITECTURE.md §8."""

    id: str

    async def voices(self) -> Sequence[Voice]:
        """List the voices this engine offers."""
        ...

    async def synthesize(
        self,
        text: str,
        voice: Voice,
        speed: float,
    ) -> AudioChunkStream:
        """Synthesise `text`, streamed so it can be cancelled mid-utterance."""
        ...

    def cancel(self) -> None:
        """Stop immediately. Must be immediate, for barge-in (EC-23)."""
        ...
