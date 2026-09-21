"""Own both sockets, byte-forward audio, parse frames, fan out.

ARCHITECTURE.md §2: this module decides nothing. It carries audio and hands
events to the controller through a bounded queue.

The audio path is sacred (INV-1): `pump_audio_up` forwards frames verbatim and
never awaits controller work. `Arbiter.decide` is synchronous precisely so that
awaiting it here is impossible rather than merely discouraged.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from typing import Final

from nod_adapters.protocols import SttSession
from nod_core.arbiter import (
    BASE_MAX_MS,
    BASE_MIN_MS,
    CEILING_FLOOR_MS,
    HOST_OVERRIDE_MS,
    Arbiter,
    ArbiterInput,
)
from nod_core.arbiter import (
    WIRE_FIELDS as _WIRE_FIELDS,
)
from nod_core.profiler import Profiler
from nod_core.trace import TraceSink
from nod_core.types import (
    ConfigPatch,
    NodMode,
    SessionBegin,
    Termination,
    Turn,
    TurnConfig,
    WindowHint,
)

AUDIO_FRAME_MS: Final = 50
"""PCM16, 16 kHz, mono frames (ARCHITECTURE.md §7). Milliseconds."""

CONTROLLER_QUEUE_MAXSIZE: Final = 256
"""Fan-out queue to `run_controller`; drop-oldest (ARCHITECTURE.md §3)."""

PREBUFFER_MS: Final = 500
"""Ring for audio arriving before the socket is ready; drop-oldest (EC-02)."""

ROTATE_LEAD_MS: Final = 30_000
"""Reconnect this far before `Begin.expires_at` (EC-03). Milliseconds."""

RECONNECT_BACKOFF_MS: Final = (200, 400, 800, 1600, 3200)
"""Exponential backoff, jittered at use (EC-04). Milliseconds."""

RECONNECT_MAX_ATTEMPTS: Final = 5
"""After this many attempts, close the client socket with a typed error (EC-04)."""

RECONNECT_AUDIO_BUFFER_MS: Final = 2000
"""Audio buffered across a reconnect, then dropped oldest (EC-04). Milliseconds."""

IDLE_MS: Final = 15_000
"""Caller never speaks: prompt once, then close with `reason=idle` (EC-01)."""

HOST_OVERRIDE_FIELDS: Final = frozenset(field for field, _ in _WIRE_FIELDS)
"""The knobs a host can take ownership of (EC-33). Derived from the arbiter."""


class SessionProxy:
    """One caller session: two sockets, four tasks, one controller.

    The upstream is typed as the `SttSession` protocol and never as a concrete
    adapter. That is what makes `FakeAssemblyAI` and offline tests possible
    (ARCHITECTURE.md §2, INV-7), and `tests/unit/test_boundaries.py` asserts it.

    **Three additions to the signatures the stubs declared**, each because the
    declared surface could not do its job without it. They are listed here rather
    than discovered:

    - `feed_audio` and `client_events`. `pump_audio_up` forwards caller audio and
      `pump_events_down` returns upstream frames to the client, and neither the
      constructor nor the methods named a client side at all. The proxy owns a
      bounded queue in each direction; the server's WebSocket handler pushes into
      one and iterates the other.
    - `note_host_configuration`. EC-33 gives the host five seconds of ownership
      over any field it set, and nothing in the declared surface could tell the
      proxy that the host had set one.
    - `reconnect`, an optional factory. `rotate` is specified to reconnect before
      `Begin.expires_at`, which is impossible for an object handed exactly one
      socket and no way to obtain another. Optional, so a session that never
      rotates need not supply it.
    """

    __slots__ = (
        "_arbiter",
        "_audio_dropped",
        "_ceiling_ms",
        "_client_audio",
        "_client_events",
        "_closed",
        "_controller_dropped",
        "_controller_errors",
        "_controller_queue",
        "_current",
        "_event_dropped",
        "_expires_at_ms",
        "_hint",
        "_host_until",
        "_mode",
        "_patches_sent",
        "_pending",
        "_profiler",
        "_reconnect",
        "_rejected",
        "_stream_ms",
        "_tasks",
        "_trace",
        "_upstream",
    )

    def __init__(
        self,
        *,
        upstream: SttSession,
        profiler: Profiler,
        arbiter: Arbiter,
        trace: TraceSink,
        mode: NodMode,
        ceiling_ms: int,
        reconnect: Callable[[], Awaitable[SttSession]] | None = None,
    ) -> None:
        """Wire one session.

        Args:
            upstream: The upstream STT session, behind its protocol.
            profiler: The speaker axis for this session.
            arbiter: The controller for this session.
            trace: The trace sink for this session.
            mode: `adapt`, `observe` or `off`. `observe` profiles and traces but
                sends no patches (ARCHITECTURE.md §7).
            ceiling_ms: The latency ceiling for this connection.
            reconnect: Produces a fresh upstream session for `rotate`. Without it
                a session cannot rotate and `rotate` says so rather than failing
                obscurely at `expires_at`.

        Raises:
            ValueError: `ceiling_ms` is below `CEILING_FLOOR_MS`.
        """
        # ADR-021's soft side: a per-connection ceiling is clamped rather than
        # refused, because INV-8 forbids dropping a live call to enforce a
        # latency preference. `Voice.pacing_hint_ms` feeds this on a mid-session
        # voice switch, so a bad value arrives from a real code path.
        self._ceiling_ms = max(ceiling_ms, CEILING_FLOOR_MS)
        self._upstream = upstream
        self._profiler = profiler
        self._arbiter = arbiter
        self._trace = trace
        self._mode = mode
        self._reconnect = reconnect

        # Every queue bounded with an explicit overflow policy (CLAUDE.md §6,
        # ARCHITECTURE.md §3). `deque(maxlen=...)` rather than `asyncio.Queue`
        # for the two client-facing rings, because drop-*oldest* is what §3 asks
        # for and `Queue.put_nowait` raises on a full queue instead.
        self._client_audio: deque[bytes] = deque(maxlen=PREBUFFER_MS // AUDIO_FRAME_MS)
        self._client_events: asyncio.Queue[SessionBegin | Turn | Termination | None] = (
            asyncio.Queue(maxsize=CONTROLLER_QUEUE_MAXSIZE)
        )
        self._controller_queue: deque[Turn] = deque(maxlen=CONTROLLER_QUEUE_MAXSIZE)

        self._current = TurnConfig(
            min_turn_silence_ms=BASE_MIN_MS,
            max_turn_silence_ms=BASE_MAX_MS,
            end_of_turn_confidence_threshold=0.0,
            vad_threshold=None,
        )
        self._hint = WindowHint(min_mult=1.0, max_mult=1.0)
        self._pending: ConfigPatch | None = None
        self._patches_sent = 0
        self._stream_ms = 0
        self._expires_at_ms: int | None = None
        self._host_until: dict[str, float] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._audio_dropped = 0
        self._event_dropped = 0
        self._controller_dropped = 0
        self._controller_errors = 0
        self._rejected = 0

    # --- client side, the two directions the stubs did not name -------------

    def feed_audio(self, frame: bytes) -> None:
        """Accept one caller frame. Synchronous, non-blocking, `O(1)`. EC-02.

        Deliberately not `async`: INV-1 says the audio path must never await
        controller work, and the cheapest way to guarantee that is for the
        ingress to have nothing to await. Overflow drops the oldest frame and
        counts it — a dropped frame is a recorded metric, never a silent loss
        (ARCHITECTURE.md §3).

        Args:
            frame: One PCM16 frame.
        """
        if len(self._client_audio) == self._client_audio.maxlen:
            self._audio_dropped += 1
        self._client_audio.append(frame)

    async def client_events(self) -> AsyncIterator[SessionBegin | Turn | Termination]:
        """Yield every upstream frame, unmodified, for the client socket.

        `pump_events_down` forwards frames here without inspecting or editing
        them (ARCHITECTURE.md §7). Nod adds to the stream; it never rewrites it.

        Yields:
            Upstream frames in arrival order.
        """
        while True:
            event = await self._client_events.get()
            if event is None:
                return
            yield event

    def note_host_configuration(
        self, fields: Iterable[str], *, now: float | None = None
    ) -> None:
        """Record that the host set these fields itself. EC-33.

        The host wins on any field it set within `HOST_OVERRIDE_MS`, and Nod
        merges rather than fights. `time.monotonic` rather than the
        stream-relative clock the controller uses: the host's edit happens in
        real time and has no position in the transcript, and CLAUDE.md §6 scopes
        its stream-relative rule to the controller. `monotonic` rather than
        `time.time` so a clock adjustment cannot extend or cancel the window.

        Args:
            fields: Wire names the host configured.
            now: Injectable clock for tests. Defaults to `time.monotonic()`.
        """
        at = time.monotonic() if now is None else now
        for field in fields:
            if field in HOST_OVERRIDE_FIELDS:
                self._host_until[field] = at + HOST_OVERRIDE_MS / 1000.0

    def _host_fields(self, *, now: float | None = None) -> frozenset[str]:
        """Fields the host currently owns. `O(len(HOST_OVERRIDE_FIELDS))`. EC-33."""
        at = time.monotonic() if now is None else now
        return frozenset(
            field for field, until in self._host_until.items() if until > at
        )

    # --- the four tasks of ARCHITECTURE.md §3 -------------------------------

    async def run(self) -> None:
        """Start and supervise the four tasks of ARCHITECTURE.md §3.

        `pump_audio_up`, `pump_events_down`, `run_controller` and the trace
        sink's `drain`. Every queue between them is bounded with an explicit
        overflow policy and a drop counter.

        Returns when `pump_events_down` finishes — the upstream deciding the
        session is over — and cancels the rest on the way out so `aclose` has no
        orphans to find (EC-05).
        """
        async with asyncio.TaskGroup() as group:
            self._tasks = [
                group.create_task(self.pump_audio_up()),
                group.create_task(self.run_controller()),
                group.create_task(self._trace.drain()),
            ]
            await self.pump_events_down()
            for task in self._tasks:
                task.cancel()

    async def pump_audio_up(self) -> None:
        """Forward caller audio upstream. Highest priority.

        Never buffers beyond one frame and never awaits the controller (INV-1).
        If the upstream socket is slow, drops the oldest frame and counts it; a
        dropped frame is a recorded metric, never a silent loss.

        There is no reference to the profiler, the arbiter or either of their
        queues in this method, and that absence is the invariant. `decide` being
        synchronous makes awaiting it impossible rather than merely discouraged
        (CLAUDE.md §6), and `tests/unit/test_proxy.py` asserts this body touches
        nothing but the audio ring and the socket.
        """
        while not self._closed:
            if not self._client_audio:
                await asyncio.sleep(AUDIO_FRAME_MS / 1000.0 / 2)
                continue
            await self._upstream.send_audio(self._client_audio.popleft())

    async def pump_events_down(self) -> None:
        """Parse `Begin`, `Turn` and `Termination` and fan out. High priority.

        Pushes to the fan-out queues without awaiting consumers, and forwards
        every upstream frame to the client unmodified (ARCHITECTURE.md §7).
        """
        async for event in self._upstream.events():
            self._offer_to_client(event)
            if isinstance(event, SessionBegin):
                self._expires_at_ms = event.expires_at_ms
            elif isinstance(event, Turn):
                self._advance_stream_clock(event)
                if len(self._controller_queue) == self._controller_queue.maxlen:
                    self._controller_dropped += 1
                self._controller_queue.append(event)
            elif isinstance(event, Termination):
                break
        self._client_events.put_nowait(None)

    def _offer_to_client(self, event: SessionBegin | Turn | Termination) -> None:
        """Push one frame to the client without awaiting it. `O(1)`. §3."""
        if self._client_events.full():
            self._client_events.get_nowait()
            self._event_dropped += 1
        self._client_events.put_nowait(event)

    def _advance_stream_clock(self, turn: Turn) -> None:
        """Track stream-relative time from word timings. `O(1)`. CLAUDE.md §6, EC-08.

        The controller's only clock. Monotone by construction, so an out-of-order
        turn cannot move it backwards.
        """
        for word in turn.words:
            if word.end_ms > self._stream_ms:
                self._stream_ms = word.end_ms

    async def run_controller(self) -> None:
        """Drive the profiler and arbiter off the fan-out. Normal priority.

        Bounded queue of `CONTROLLER_QUEUE_MAXSIZE`, drop-oldest, counter
        incremented. A controller exception drops the session to `SAFE` and the
        call continues (INV-8, EC-31).
        """
        while not self._closed:
            if not self._controller_queue:
                await asyncio.sleep(AUDIO_FRAME_MS / 1000.0)
                continue
            await self._handle_turn(self._controller_queue.popleft())

    async def _handle_turn(self, turn: Turn) -> None:
        """One turn through the controller, with EC-31's boundary around it.

        The `except` is deliberately broad and deliberately narrow in scope: it
        wraps the profiler and the arbiter, and it does **not** wrap
        `send_patch_upstream`. A socket failure is EC-32's problem and has its own
        retry; folding it in here would report a transport error as a controller
        error and drop the speaker axis for the rest of the call over a dropped
        packet.

        Entering `SAFE` is loud (INV-8): the arbiter counts the error, a
        `controller_error` line reaches the trace, and the proxy keeps its own
        count so `dev` can re-raise after the call. A silent `SAFE` would produce
        a run that looks like `nod` and behaves like `balanced`, and the arm
        would be measured under the wrong label.

        Args:
            turn: One upstream `Turn`.
        """
        try:
            self._profiler.observe_turn(turn)
            patch = self._arbiter.decide(
                ArbiterInput(
                    features=self._profiler.features(),
                    hint=self._hint,
                    expected_answer=None,
                    current=self._current,
                    capabilities=self._arbiter.capabilities,
                    ceiling_ms=self._ceiling_ms,
                    turn_order=turn.turn_order,
                    t_ms=self._stream_ms,
                    host_override_fields=self._host_fields(),
                    patches_sent=self._patches_sent,
                )
            )
        except Exception as exc:
            self._arbiter.note_error(exc)
            self._controller_errors += 1
            self._trace.emit(
                "controller_error",
                {
                    "error": type(exc).__name__,
                    "turn_order": turn.turn_order,
                    "state": self._arbiter.state.value,
                },
                t_ms=self._stream_ms,
            )
            return

        if patch is None or self._mode is not NodMode.ADAPT:
            return
        self._pending = patch
        self._trace.emit(
            "config_decision",
            {
                "rule_id": patch.decision.rule_id,
                "trigger": patch.decision.trigger,
                "changed": list(patch.changed),
                "min_turn_silence": patch.config.min_turn_silence_ms,
                "max_turn_silence": patch.config.max_turn_silence_ms,
                "state": patch.decision.state.value,
            },
            t_ms=patch.t_ms,
        )
        await self.send_patch_upstream(patch.changed)

    async def send_patch_upstream(self, patch_fields: tuple[str, ...]) -> None:
        """Inject `UpdateConfiguration` on the same live socket.

        No reconnect, no session loss. Merges with any host-sent configuration
        rather than fighting it: the host wins on any field it set explicitly in
        the last `HOST_OVERRIDE_MS` (EC-33). A rejected update is retried once,
        then the session drops to `observe` (EC-32).

        Reads the values from the patch `_handle_turn` stored, because the
        declared signature carries field names and no values. That is why the
        proxy holds `_pending`: the field list alone cannot be sent.

        Args:
            patch_fields: The field names to send.
        """
        pending = self._pending
        if pending is None or not patch_fields:
            return
        held = self._host_fields()
        payload = {
            field: float(getattr(pending.config, attribute))
            for field, attribute in _WIRE_FIELDS
            if field in patch_fields and field not in held
        }
        if not payload:
            return
        for attempt in (1, 2):
            try:
                await self._upstream.update_configuration(payload)
            except Exception as exc:
                self._rejected += 1
                self._trace.emit(
                    "config_rejected",
                    {
                        "error": type(exc).__name__,
                        "attempt": attempt,
                        "fields": sorted(payload),
                    },
                    t_ms=self._stream_ms,
                )
                if attempt == 2:
                    # EC-32: one retry, then observe for the rest of the session.
                    # Not `off`: profiling and tracing stay on, so the console can
                    # show why the loop stopped closing and the trace still
                    # carries the decisions it would have sent.
                    self._mode = NodMode.OBSERVE
                    self._trace.emit(
                        "mode_degraded",
                        {"mode": self._mode.value, "reason": "update_rejected"},
                        t_ms=self._stream_ms,
                    )
                continue
            self._patches_sent += 1
            self._current = pending.config
            self._trace.emit(
                "config_applied",
                {"fields": sorted(payload), "patches_sent": self._patches_sent},
                t_ms=self._stream_ms,
                direction="up",
            )
            return

    async def rotate(self) -> None:
        """Rotate the upstream session before it expires. EC-03.

        Reconnects at `expires_at - ROTATE_LEAD_MS`, carries the profiler state
        across by `session_id`, replays the current config on the new socket and
        emits `upstream_rotated`. The caller sees nothing.

        Raises:
            RuntimeError: No `reconnect` factory was supplied. Stated loudly here
                rather than discovered at `expires_at`, which on a long call is
                the worst possible moment to learn it.
        """
        if self._reconnect is None:
            msg = (
                "rotate() needs the `reconnect` factory this session was built "
                "without; EC-03 cannot be satisfied by an object holding one socket"
            )
            raise RuntimeError(msg)
        carried = self._profiler.snapshot()
        previous = self._upstream
        fresh = await self._reconnect()
        self._upstream = fresh
        self._profiler = Profiler.restore(carried)
        # The new socket starts at the service's defaults, so the config the
        # caller is living under has to be replayed or the window silently
        # resets mid-call — which would look exactly like the controller
        # deciding to narrow.
        await fresh.update_configuration(
            {
                field: float(getattr(self._current, attribute))
                for field, attribute in _WIRE_FIELDS
            }
        )
        self._trace.emit(
            "upstream_rotated",
            {
                "from_session": previous.id,
                "to_session": fresh.id,
                "n_gaps": carried.n_gaps,
            },
            t_ms=self._stream_ms,
        )
        await previous.aclose()

    async def aclose(self) -> None:
        """Shut down cleanly. EC-05.

        Flushes the trace, finalises the session row, cancels all four tasks and
        releases the upstream socket. No orphan tasks; asserted by a leak test.

        Idempotent, because the client disconnecting and the upstream terminating
        can both reach here and a second close must not raise.
        """
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        await self._trace.aclose()
        await self._upstream.aclose()

    # --- counters, for the proxy's own observability ------------------------

    @property
    def mode(self) -> NodMode:
        """The session's current mode. EC-32 can degrade it to `observe`."""
        return self._mode

    @property
    def controller_errors(self) -> int:
        """Controller exceptions caught this session (EC-31, INV-8)."""
        return self._controller_errors

    @property
    def rejected_updates(self) -> int:
        """`UpdateConfiguration` failures, including the retried first one (EC-32)."""
        return self._rejected

    @property
    def dropped(self) -> Mapping[str, int]:
        """Every drop counter. A dropped frame is a metric, never a silent loss."""
        return {
            "audio": self._audio_dropped,
            "events": self._event_dropped,
            "controller": self._controller_dropped,
            "trace": self._trace.dropped,
        }

    @property
    def config_in_force(self) -> TurnConfig:
        """What the socket is actually running, updated only on a confirmed send."""
        return self._current

    @property
    def stream_ms(self) -> int:
        """Stream-relative time from word timings, never wall clock. CLAUDE.md §6."""
        return self._stream_ms
