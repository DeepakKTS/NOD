"""Paced real-time PCM16 feeder on a monotonic deadline schedule.

BENCH_SPEC.md §4. Audio must be fed in real time: dumping a wav into the socket
at once destroys every silence in it and makes the measurement meaningless. Since
the silences are the entire object of study, a feeder that drifts does not degrade
the measurement, it invalidates it (EC-37).

This module knows nothing about AssemblyAI. It takes an iterable of frames and an
async `send` callable, which is what lets P1's capability probe, P3's benchmark
and `FakeAssemblyAI` all drive the same code.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Final

FRAME_MS: Final = 50
"""Feeder frame size, PCM16 (BENCH_SPEC.md §4). Milliseconds."""

FRAME_S: Final = FRAME_MS / 1000.0
"""Feeder frame size. Seconds."""

DRIFT_SUSTAIN_FRAMES: Final = 4
"""Consecutive over-threshold frames required to void a run (EC-37, ADR-012).

Four frames is 200 ms of stream time: long enough that a single OS scheduler
preemption cannot reach it, short enough to catch a feeder that has genuinely
fallen behind within a fifth of a second. A 5-minute soak measured p99 lag of
1.5 ms with one 16 ms outlier, so max-lag alone would eventually void a long run
on a stall that displaced one frame out of tens of thousands.

Note the effective semantics. An absolute schedule drains a stall at one frame
period per frame, so the frames *recovering* from a stall are themselves over
threshold. With `MAX_LAG_MS = 25` and 50 ms frames, four consecutive means a
stall of roughly 150 ms or more voids the run, and anything shorter does not.
That is the intended bar: 150 ms is six times the worst outlier measured over
6000 frames, and well below anything that would shift a turn boundary.
"""

MAX_LAG_MS: Final = 25.0
"""Per-frame lag threshold; `DRIFT_SUSTAIN_FRAMES` in a row voids the run (EC-37).

BENCH_SPEC.md §4 and EC-37 both say "cumulative drift". Read literally, as the sum
of per-frame drifts, the quantity is near zero by construction under the absolute
schedule below — the guard would never fire and would be decorative. The quantity
that actually invalidates latency numbers is how far behind schedule the feeder
ever fell, so that is what is enforced here. The sum is recorded as well, so the
literal reading stays available in the trace (ADR-008).
"""

BYTES_PER_SAMPLE: Final = 2
"""PCM16."""


class FeederDriftError(RuntimeError):
    """The feeder fell further behind schedule than `MAX_LAG_MS`.

    A run that raises this is void. It is never reported with a caveat, because a
    latency measurement taken from a drifting feeder is not a worse number, it is
    a different number measuring something else.
    """

    def __init__(
        self, frame: int, deadline_ms: float, actual_ms: float, sustained: int
    ) -> None:
        """Record where the schedule broke.

        Args:
            frame: Index of the last late frame.
            deadline_ms: When it should have been sent, relative to `t0`.
            actual_ms: When it was actually sent, relative to `t0`.
            sustained: How many consecutive frames were over threshold.
        """
        self.frame = frame
        self.deadline_ms = deadline_ms
        self.actual_ms = actual_ms
        self.lag_ms = actual_ms - deadline_ms
        self.sustained = sustained
        super().__init__(
            f"feeder drifted {self.lag_ms:.2f} ms at frame {frame} "
            f"for {sustained} consecutive frames "
            f"(deadline {deadline_ms:.2f} ms, sent {actual_ms:.2f} ms); "
            f"run is void per EC-37"
        )


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """One frame's send timing, for the trace (BENCH_SPEC.md §4)."""

    n: int
    bytes: int
    deadline_ms: float
    actual_ms: float
    lag_ms: float
    max_lag_ms: float


@dataclass(frozen=True, slots=True)
class FeedReport:
    """What one paced run cost, in schedule terms."""

    frames: int
    audio_ms: int
    max_lag_ms: float
    sum_lag_ms: float
    p50_lag_ms: float
    p90_lag_ms: float
    p99_lag_ms: float


def _percentile(ordered: list[float], q: float) -> float:
    """Return the `q` quantile of an already-sorted list. `O(1)`."""
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def frames_of(pcm: bytes, *, frame_ms: int = FRAME_MS, sample_rate: int) -> list[bytes]:
    """Split raw PCM16 into fixed-size frames, zero-padding the tail. `O(n)`.

    Args:
        pcm: Raw little-endian PCM16 bytes, mono.
        frame_ms: Frame size in milliseconds.
        sample_rate: Samples per second.

    Returns:
        Frames of exactly `frame_ms` each; the last is zero-padded if needed.
    """
    size = (sample_rate * frame_ms // 1000) * BYTES_PER_SAMPLE
    out = [pcm[i : i + size] for i in range(0, len(pcm), size)]
    if out and len(out[-1]) < size:
        out[-1] = out[-1].ljust(size, b"\x00")
    return out


class PacedFeeder:
    """Emits frames on an absolute monotonic schedule and reports its own drift.

    The schedule is `deadline_n = t0 + n * FRAME_S`, computed by multiplication
    from `t0` rather than by accumulating `+= FRAME_S`. That distinction is the
    whole design: an absolute schedule is self-correcting, because a late frame
    sleeps zero and the *next* deadline is still exactly where it always was. An
    accumulated one folds every oversleep into every subsequent deadline, which is
    precisely the compounding jitter BENCH_SPEC.md §4 forbids.

    The feeder also serves as the session's stream clock, since it is the only
    component that knows how much audio has actually gone out.
    """

    def __init__(
        self,
        *,
        frame_ms: int = FRAME_MS,
        max_lag_ms: float = MAX_LAG_MS,
        sustain_frames: int = DRIFT_SUSTAIN_FRAMES,
        on_frame: Callable[[FrameRecord], None] | None = None,
    ) -> None:
        """Prepare a feeder.

        Args:
            frame_ms: Frame size in milliseconds.
            max_lag_ms: Per-frame lag threshold (EC-37).
            sustain_frames: Consecutive over-threshold frames that void the run.
            on_frame: Called synchronously per frame, for the trace. Must not
                block: it runs inside the frame's own deadline budget.
        """
        self._frame_ms = frame_ms
        self._frame_s = frame_ms / 1000.0
        self._max_lag_ms = max_lag_ms
        self._sustain_frames = sustain_frames
        self._on_frame = on_frame
        self._t0: float | None = None
        self._started = asyncio.Event()
        self._finished = asyncio.Event()
        self._frames_sent = 0
        self._max_lag_seen = 0.0

    @property
    def frames_sent(self) -> int:
        """Frames handed to `send` so far."""
        return self._frames_sent

    def now_ms(self) -> int:
        """Stream-relative milliseconds since the first frame. `O(1)`.

        Derived from `time.monotonic()` rather than from the frame counter, so it
        carries sub-frame resolution: the frame counter would quantise every
        measurement to `frame_ms` and blur the very boundaries being measured. It
        is still stream-relative and still monotonic (CLAUDE.md §6, EC-46).

        Returns:
            Milliseconds since `t0`, or 0 before the first frame.
        """
        if self._t0 is None:
            return 0
        return int((time.monotonic() - self._t0) * 1000.0)

    async def wait_until_ms(self, stream_ms: int) -> None:
        """Resolve when the stream reaches `stream_ms`, or when the clip ends.

        Sleeps to the same absolute deadline the frame schedule uses, so a control
        frame scheduled for a given stream position lands there without polling
        and without inheriting the caller's jitter.

        Args:
            stream_ms: Stream-relative milliseconds to wait for.
        """
        await self._started.wait()
        if self._t0 is None:  # pragma: no cover - set before _started is raised
            return
        remaining = (self._t0 + stream_ms / 1000.0) - time.monotonic()
        if remaining <= 0:
            return
        try:
            # `wait_for` cancels the waiter on timeout, so the common path (the
            # deadline arrives before the clip ends) leaves no task behind.
            await asyncio.wait_for(self._finished.wait(), timeout=remaining)
        except TimeoutError:
            return

    async def feed(
        self,
        frames: Iterable[bytes],
        send: Callable[[bytes], Awaitable[None]],
    ) -> FeedReport:
        """Send every frame on the absolute schedule, or raise. `O(1)` per frame.

        Time spent inside `send` is charged against that frame's own deadline, so
        a slow upstream surfaces as drift rather than hiding inside the sleep.

        Args:
            frames: PCM16 frames of `frame_ms` each.
            send: Forwards one frame upstream.

        Returns:
            The run's schedule report.

        Raises:
            FeederDriftError: `sustain_frames` consecutive frames each lagged more
                than `max_lag_ms`. One isolated stall does not void a run: it
                displaces a single 50 ms frame, not the timeline (ADR-012).
        """
        lags: list[float] = []
        over_threshold = 0
        self._t0 = time.monotonic()
        self._started.set()
        try:
            for n, frame in enumerate(frames):
                deadline = self._t0 + n * self._frame_s
                delay = deadline - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

                await send(frame)
                self._frames_sent = n + 1

                actual_ms = (time.monotonic() - self._t0) * 1000.0
                deadline_ms = n * self._frame_ms
                lag_ms = actual_ms - deadline_ms
                lags.append(lag_ms)
                self._max_lag_seen = max(self._max_lag_seen, lag_ms)

                if self._on_frame is not None:
                    self._on_frame(
                        FrameRecord(
                            n=n,
                            bytes=len(frame),
                            deadline_ms=deadline_ms,
                            actual_ms=actual_ms,
                            lag_ms=lag_ms,
                            max_lag_ms=self._max_lag_seen,
                        )
                    )

                if lag_ms > self._max_lag_ms:
                    over_threshold += 1
                    if over_threshold >= self._sustain_frames:
                        raise FeederDriftError(
                            n, deadline_ms, actual_ms, over_threshold
                        )
                else:
                    over_threshold = 0
        finally:
            self._finished.set()

        ordered = sorted(lags)
        return FeedReport(
            frames=len(lags),
            audio_ms=len(lags) * self._frame_ms,
            max_lag_ms=self._max_lag_seen,
            sum_lag_ms=sum(lags),
            p50_lag_ms=_percentile(ordered, 0.50),
            p90_lag_ms=_percentile(ordered, 0.90),
            p99_lag_ms=_percentile(ordered, 0.99),
        )
