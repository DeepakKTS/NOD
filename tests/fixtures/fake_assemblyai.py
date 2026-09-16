"""A local WebSocket server that replays recorded trace JSONL.

This is what makes INV-7 possible: unit and integration tests run against this
instead of the network, and `make bench` runs the whole suite offline on a clean
clone with no API key.

Implemented in Phase 1 part two (docs/PROMPTS.md P3): replays recorded events
with their original inter-event timing, and accepts `UpdateConfiguration` and
`ForceEndpoint`, recording both so tests can assert on what the controller sent.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


class FakeAssemblyAI:
    """A replay server bound to loopback only."""

    def __init__(self, trace: Path, *, host: str = "127.0.0.1", port: int = 0) -> None:
        """Prepare a replay server for one recorded trace.

        Args:
            trace: The recorded `.jsonl` trace to replay.
            host: Bind address. Loopback, so the INV-7 network guard allows it.
            port: Bind port; 0 picks a free one.
        """
        raise NotImplementedError

    async def __aenter__(self) -> FakeAssemblyAI:
        """Start serving and return the running server."""
        raise NotImplementedError

    async def __aexit__(self, *exc: object) -> None:
        """Stop serving."""
        raise NotImplementedError

    @property
    def url(self) -> str:
        """The `ws://` URL clients should connect to."""
        raise NotImplementedError

    @property
    def received_updates(self) -> Sequence[dict[str, float]]:
        """Every `UpdateConfiguration` this server accepted, in order."""
        raise NotImplementedError
