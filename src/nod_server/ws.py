"""WebSocket endpoints.

`/v1/stream` mirrors AssemblyAI's streaming contract so an existing client can
switch URL without changing code (PRD.md Mode A).
"""

from __future__ import annotations

from fastapi import WebSocket

from nod_core.arbiter import DEFAULT_CEILING_MS
from nod_core.types import NodMode


async def stream_endpoint(
    ws: WebSocket,
    *,
    nod_preset: str = "balanced",
    nod_mode: NodMode = NodMode.ADAPT,
    nod_ceiling_ms: int = DEFAULT_CEILING_MS,
    nod_events: bool = False,
) -> None:
    """Serve `WS /v1/stream` (ARCHITECTURE.md §7).

    Binary audio is forwarded verbatim. JSON control frames pass through
    unchanged except `UpdateConfiguration`, which is merged with Nod's own patch
    rather than fought: the host wins on any field it set explicitly in the last
    five seconds (EC-33).

    Args:
        ws: The client socket.
        nod_preset: Starting configuration.
        nod_mode: `adapt`, `observe` or `off`. `observe` sends no patches.
        nod_ceiling_ms: Latency ceiling for the arbiter.
        nod_events: Also send `NodEvent` frames to the client.
    """
    raise NotImplementedError


async def console_endpoint(ws: WebSocket, session_id: str) -> None:
    """Serve `WS /v1/console` (ARCHITECTURE.md §7).

    Server to client only, per-session subscription. A console client that falls
    behind is disconnected rather than allowed to buffer.

    Args:
        ws: The console socket.
        session_id: The session to subscribe to.
    """
    raise NotImplementedError
