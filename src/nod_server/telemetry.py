"""Metrics, structured logging and the console telemetry hub.

The counters below are exactly those named in ARCHITECTURE.md §9.
`nod_decide_seconds` is the canary for INV-2; alert above 5 ms p99.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Final, override

import structlog
from prometheus_client import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from nod_core.trace import TraceSink
from nod_core.types import JsonValue

CONSOLE_QUEUE_MAXSIZE: Final = 256
"""Events a console client may fall behind by before it is dropped.

Bounded because §7 says a slow client is disconnected rather than buffered, and
because an unbounded queue here would grow with call duration (INV-3)."""

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
CONSOLE_DROPPED_TOTAL: Final = Counter(
    "nod_console_dropped_total",
    "Console clients disconnected for falling behind.",
)
CONTROLLER_ERRORS_TOTAL: Final = Counter(
    "nod_controller_errors_total",
    "Controller exceptions caught by the arbiter's SAFE path (INV-8).",
)


def configure_logging(level: str) -> None:
    """Configure structlog: one event per line, never in the hot loop.

    **This was a `NotImplementedError` stub for eleven gates.** ADR-050 recorded
    the consequence rather than fixing it: when the live patch census was being
    reconstructed there were no structured logs to fall back on, because there
    were no logs at all. The stdlib root logger has no handler by default, so
    every `_log.info` in this package went to the `lastResort` handler at
    WARNING and was dropped — which is how a server-side frame counter written
    specifically to diagnose a silent client produced nothing.

    Idempotent: `create_app` may be called more than once in a test session, and
    adding a second handler would double every line.

    Args:
        level: `NOD_LOG_LEVEL`, e.g. `info`.
    """
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not any(getattr(h, "_nod", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        handler._nod = True  # type: ignore[attr-defined]
        root.addHandler(handler)


def render_metrics() -> bytes:
    """Render the registry in Prometheus text format. `O(series)`.

    The default registry, which is where the module-level counters above
    registered themselves on import. No argument, because a second registry in
    this process would be a second answer to "what are the metrics".

    Returns:
        The `/metrics` response body, UTF-8 Prometheus text exposition.
    """
    return generate_latest(REGISTRY)


class TelemetryHub:
    """Per-session fan-out to console clients (ARCHITECTURE.md §7).

    One bounded queue per subscriber. A client that falls behind is
    **disconnected**, not buffered: §7 says so, and an unbounded console queue
    would be the one place in the process where memory grows with call duration
    (INV-3).
    """

    __slots__ = ("_subscribers",)

    def __init__(self) -> None:
        """Start with no sessions and no subscribers."""
        self._subscribers: dict[str, list[asyncio.Queue[bytes | None]]] = {}

    def publish(
        self,
        session_id: str,
        kind: str,
        payload: Mapping[str, JsonValue],
    ) -> None:
        """Publish one console event. Non-blocking, `O(subscribers)`.

        Never awaits and never raises: this is called from the trace tee and
        from the stream endpoint, both of which sit on paths that must not
        block on a slow browser (INV-1).

        Args:
            session_id: The session the event belongs to.
            kind: One of the message types in ARCHITECTURE.md §7.
            payload: The event body, carrying `t_ms` stream-relative.
        """
        queues = self._subscribers.get(session_id)
        if not queues:
            return
        line = json.dumps(
            {"kind": kind, "session_id": session_id, **dict(payload)},
            separators=(",", ":"),
            default=str,
        ).encode()
        for queue in list(queues):
            try:
                queue.put_nowait(line)
            except asyncio.QueueFull:
                # Disconnect rather than buffer (§7). `None` is the sentinel the
                # subscriber loop reads as "you fell behind"; dropping the
                # newest line instead would leave a console silently showing
                # stale config, which is worse than a closed socket.
                CONSOLE_DROPPED_TOTAL.inc()
                queues.remove(queue)
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(None)

    async def subscribe(self, session_id: str) -> AsyncIterator[bytes]:
        """Subscribe one console client to one session. `O(1)` per event.

        Args:
            session_id: The session to follow.

        Yields:
            Serialised events until the client is dropped or unsubscribes.
        """
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=CONSOLE_QUEUE_MAXSIZE
        )
        self._subscribers.setdefault(session_id, []).append(queue)
        try:
            while True:
                line = await queue.get()
                if line is None:
                    return
                yield line
        finally:
            remaining = self._subscribers.get(session_id, [])
            if queue in remaining:
                remaining.remove(queue)
            if not remaining:
                self._subscribers.pop(session_id, None)

    def subscribers(self, session_id: str) -> int:
        """How many console clients are following a session. `O(1)`."""
        return len(self._subscribers.get(session_id, ()))


class ConsoleTeeSink(TraceSink):
    """A `TraceSink` that also publishes to the console hub.

    **Chosen over a new `SessionProxy` parameter, deliberately.** Every record
    the live-call screen needs is already emitted to the trace, because INV-4
    requires a `ConfigDecision` on every patch and INV-8 requires a
    `controller_error` on every caught exception. Teeing the sink reuses those
    call sites instead of adding a second set that could drift from them, and
    it leaves `SessionProxy.__slots__` and the controller path untouched.

    What it does **not** carry is the turn stream: the proxy does not trace
    per-turn events, and adding them for the console's benefit would put the
    hot loop to work for a browser. `ws.stream_endpoint` publishes those as it
    forwards them, which is off the audio path already.

    One consequence worth stating: the tee publishes **post-redaction**
    payloads (INV-6). For the Floor Meter, the config strip and the reason line
    that is invisible — they carry numbers and rule ids. A transcript pane
    rendering from this sink would show masked digits, which is why the
    transcript is fed from the turn stream instead.
    """

    __slots__ = ("_hub", "_session")

    def __init__(self, session_id: str, *, hub: TelemetryHub, **kwargs: object) -> None:
        """Wrap a sink for one session.

        Args:
            session_id: Names the trace file and the console topic.
            hub: The fan-out to publish to.
            kwargs: Passed through to `TraceSink`.
        """
        super().__init__(session_id, **kwargs)  # type: ignore[arg-type]
        self._hub = hub
        self._session = session_id

    @override
    def emit(
        self,
        kind: str,
        payload: Mapping[str, JsonValue],
        *,
        t_ms: int,
        direction: str = "local",
    ) -> None:
        """Trace the record, publish the console view, and count it. `O(1)`.

        **The counters are incremented here because this is the one place the
        server already sees every controller event.** They were declared and
        never incremented for eleven gates (ADR-057): `/metrics` reported 0.0
        while a live session's trace read `patches_sent: 12`. A metric that
        cannot move is worse than no metric, because a zero from it reads as a
        measurement.

        `config_applied` is the one counted, not `config_decision`: the former
        is emitted after the frame reaches the socket, so the counter means
        "patches the upstream accepted" rather than "patches we thought of".
        """
        super().emit(kind, payload, t_ms=t_ms, direction=direction)
        if kind == "config_applied":
            CONFIG_PATCHES_TOTAL.inc()
        elif kind == "controller_error":
            CONTROLLER_ERRORS_TOTAL.inc()
        elif kind == "upstream_rotated":
            UPSTREAM_RECONNECTS_TOTAL.inc()
        console_kind = CONSOLE_KINDS.get(kind)
        if console_kind is None:
            return
        self._hub.publish(self._session, console_kind, {**dict(payload), "t_ms": t_ms})


CONSOLE_KINDS: Final = {
    "config_decision": "config.changed",
    "controller_error": "agent.state",
    "config_rejected": "agent.state",
    "mode_degraded": "agent.state",
}
"""Trace kinds the console renders, mapped to ARCHITECTURE §7's message types.

`config_decision` rather than `config_applied` is what moves the Floor Meter,
and the choice matters: `config_applied` carries only the field names, while
INV-4 requires the *explanation* — trigger, rule id, old and new — to reach the
dashboard, not just the trace. The reason line has nothing to render without it.
"""
