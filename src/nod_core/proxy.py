"""Own both sockets, byte-forward audio, parse frames, fan out.

ARCHITECTURE.md §2: this module decides nothing. It carries audio and hands
events to the controller through a bounded queue.

The audio path is sacred (INV-1): `pump_audio_up` forwards frames verbatim and
never awaits controller work. `Arbiter.decide` is synchronous precisely so that
awaiting it here is impossible rather than merely discouraged.
"""

from __future__ import annotations

from typing import Final

from nod_adapters.protocols import SttSession
from nod_core.arbiter import Arbiter
from nod_core.profiler import Profiler
from nod_core.trace import TraceSink
from nod_core.types import NodMode

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


class SessionProxy:
    """One caller session: two sockets, four tasks, one controller.

    The upstream is typed as the `SttSession` protocol and never as a concrete
    adapter. That is what makes `FakeAssemblyAI` and offline tests possible
    (ARCHITECTURE.md §2, INV-7), and `tests/unit/test_boundaries.py` asserts it.
    """

    def __init__(
        self,
        *,
        upstream: SttSession,
        profiler: Profiler,
        arbiter: Arbiter,
        trace: TraceSink,
        mode: NodMode,
        ceiling_ms: int,
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
        """
        raise NotImplementedError

    async def run(self) -> None:
        """Start and supervise the four tasks of ARCHITECTURE.md §3.

        `pump_audio_up`, `pump_events_down`, `run_controller` and the trace
        sink's `drain`. Every queue between them is bounded with an explicit
        overflow policy and a drop counter.
        """
        raise NotImplementedError

    async def pump_audio_up(self) -> None:
        """Forward caller audio upstream. Highest priority.

        Never buffers beyond one frame and never awaits the controller (INV-1).
        If the upstream socket is slow, drops the oldest frame and counts it; a
        dropped frame is a recorded metric, never a silent loss.
        """
        raise NotImplementedError

    async def pump_events_down(self) -> None:
        """Parse `Begin`, `Turn` and `Termination` and fan out. High priority.

        Pushes to the fan-out queues without awaiting consumers, and forwards
        every upstream frame to the client unmodified (ARCHITECTURE.md §7).
        """
        raise NotImplementedError

    async def run_controller(self) -> None:
        """Drive the profiler and arbiter off the fan-out. Normal priority.

        Bounded queue of `CONTROLLER_QUEUE_MAXSIZE`, drop-oldest, counter
        incremented. A controller exception drops the session to `SAFE` and the
        call continues (INV-8, EC-31).
        """
        raise NotImplementedError

    async def send_patch_upstream(self, patch_fields: tuple[str, ...]) -> None:
        """Inject `UpdateConfiguration` on the same live socket.

        No reconnect, no session loss. Merges with any host-sent configuration
        rather than fighting it: the host wins on any field it set explicitly in
        the last `HOST_OVERRIDE_MS` (EC-33). A rejected update is retried once,
        then the session drops to `observe` (EC-32).

        Args:
            patch_fields: The field names to send.
        """
        raise NotImplementedError

    async def rotate(self) -> None:
        """Rotate the upstream session before it expires. EC-03.

        Reconnects at `expires_at - ROTATE_LEAD_MS`, carries the profiler state
        across by `session_id`, replays the current config on the new socket and
        emits `upstream_rotated`. The caller sees nothing.
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        """Shut down cleanly. EC-05.

        Flushes the trace, finalises the session row, cancels all four tasks and
        releases the upstream socket. No orphan tasks; asserted by a leak test.
        """
        raise NotImplementedError
