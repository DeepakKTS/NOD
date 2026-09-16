"""Metrics, structured logging and the console telemetry hub.

The counters below are exactly those named in ARCHITECTURE.md §9.
`nod_decide_seconds` is the canary for INV-2; alert above 5 ms p99.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Final

from prometheus_client import Counter, Gauge, Histogram

from nod_core.types import JsonValue

TURNS_TOTAL: Final = Counter("nod_turns_total", "Turns observed.")
CUTS_TOTAL: Final = Counter("nod_cuts_total", "Premature cutoffs detected.")
CONFIG_PATCHES_TOTAL: Final = Counter(
    "nod_config_patches_total",
    "UpdateConfiguration frames emitted.",
)
DECIDE_SECONDS: Final = Histogram(
    "nod_decide_seconds",
    "Arbiter.decide latency. The canary for INV-2.",
)
QUEUE_DROPPED_TOTAL: Final = Counter(
    "nod_queue_dropped_total",
    "Entries dropped by a bounded queue.",
    ["queue"],
)
TTS_CACHE_HITS_TOTAL: Final = Counter(
    "nod_tts_cache_hits_total",
    "TTS audio cache hits.",
)
SESSION_ACTIVE: Final = Gauge("nod_session_active", "Sessions currently live.")
UPSTREAM_RECONNECTS_TOTAL: Final = Counter(
    "nod_upstream_reconnects_total",
    "Upstream reconnections and rotations.",
)


def configure_logging(level: str) -> None:
    """Configure structlog: one event per line, never in the hot loop.

    Every log line carries `session_id` and `turn_order` (ARCHITECTURE.md §9).

    Args:
        level: `NOD_LOG_LEVEL`.
    """
    raise NotImplementedError


def render_metrics() -> bytes:
    """Render the registry in Prometheus text format.

    Returns:
        The `/metrics` response body.
    """
    raise NotImplementedError


class TelemetryHub:
    """Per-session fan-out to console clients (ARCHITECTURE.md §7)."""

    def publish(
        self,
        session_id: str,
        kind: str,
        payload: Mapping[str, JsonValue],
    ) -> None:
        """Publish one console event. Non-blocking.

        Args:
            session_id: The session the event belongs to.
            kind: One of the message types in ARCHITECTURE.md §7.
            payload: The event body, carrying `t_ms` stream-relative.
        """
        raise NotImplementedError

    def subscribe(self, session_id: str) -> AsyncIterator[bytes]:
        """Subscribe one console client to one session.

        A client that falls behind is disconnected rather than allowed to buffer
        (ARCHITECTURE.md §7).

        Args:
            session_id: The session to follow.

        Returns:
            The serialised event stream for that session.
        """
        raise NotImplementedError
