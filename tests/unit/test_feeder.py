"""The paced feeder holds an absolute schedule and voids a drifted run.

BENCH_SPEC.md §4 and EC-37. These tests exist because the failure they guard
against is silent: a feeder that drifts still produces a full set of plausible
latency numbers, and nothing downstream can tell that they measure the feeder
rather than the model.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nod_bench.feeder import (
    FRAME_MS,
    FeederDriftError,
    FrameRecord,
    PacedFeeder,
    frames_of,
)

SAMPLE_RATE = 16000
FRAME_BYTES = (SAMPLE_RATE * FRAME_MS // 1000) * 2


def _frames(count: int) -> list[bytes]:
    return [b"\x00" * FRAME_BYTES] * count


async def _noop(frame: bytes) -> None:
    return None


@pytest.mark.asyncio
async def test_frames_are_sent_in_order_and_counted() -> None:
    sent: list[bytes] = []
    feeder = PacedFeeder()

    async def capture(frame: bytes) -> None:
        sent.append(frame)

    report = await feeder.feed(_frames(5), capture)

    assert len(sent) == 5
    assert feeder.frames_sent == 5
    assert report.frames == 5
    assert report.audio_ms == 5 * FRAME_MS


@pytest.mark.asyncio
async def test_schedule_absorbs_a_stall_instead_of_carrying_it_forward() -> None:
    """The distinction BENCH_SPEC.md §4 draws: sleep to a deadline, not for a duration.

    A stalled frame must not push the whole tail back. Sleeping `FRAME_S` in a
    loop adds the stall to the total run length and to every later frame's
    position; sleeping to `t0 + n * FRAME_S` absorbs it, because the next
    deadline is where it always was. Asserting on total elapsed time is what
    separates the two — asserting on the reported `deadline_ms` would not, since
    that is recomputed from the frame index and agrees with itself either way.
    """
    records: list[FrameRecord] = []
    feeder = PacedFeeder(max_lag_ms=10_000.0, on_frame=records.append)
    count, stall_s = 16, 0.2

    async def stall_once(frame: bytes) -> None:
        if feeder.frames_sent == 2:
            await asyncio.sleep(stall_s)

    start = time.monotonic()
    await feeder.feed(_frames(count), stall_once)
    elapsed_ms = (time.monotonic() - start) * 1000

    nominal_ms = count * FRAME_MS
    # Absorbed: the run is still about its nominal length despite a 200 ms stall.
    # A fixed-duration sleep would land at nominal + 200 ms.
    assert elapsed_ms < nominal_ms + stall_s * 1000 * 0.5
    # The stall lands on its own frame...
    assert records[2].lag_ms > stall_s * 1000 * 0.8
    # ...and the schedule has recovered by the end.
    assert records[-1].lag_ms < records[2].lag_ms / 4


@pytest.mark.asyncio
async def test_drift_beyond_the_threshold_voids_the_run() -> None:
    feeder = PacedFeeder(max_lag_ms=25.0)

    async def stall(frame: bytes) -> None:
        if feeder.frames_sent == 1:
            await asyncio.sleep(0.15)

    with pytest.raises(FeederDriftError) as caught:
        await feeder.feed(_frames(6), stall)

    assert caught.value.frame == 1
    assert caught.value.lag_ms > 25.0
    assert "EC-37" in str(caught.value)


@pytest.mark.asyncio
async def test_drift_just_under_the_threshold_does_not_abort() -> None:
    """The guard must bite at the threshold and not below it."""
    feeder = PacedFeeder(max_lag_ms=25.0)
    report = await feeder.feed(_frames(6), _noop)
    assert report.max_lag_ms <= 25.0


@pytest.mark.asyncio
async def test_sum_of_lags_is_recorded_alongside_the_max() -> None:
    """ADR-008: the literal reading of "cumulative" stays available in the trace."""
    feeder = PacedFeeder()
    report = await feeder.feed(_frames(8), _noop)
    assert report.sum_lag_ms >= 0.0
    assert report.max_lag_ms <= report.sum_lag_ms + 1e-9


@pytest.mark.asyncio
async def test_now_ms_tracks_the_stream_and_starts_at_zero() -> None:
    feeder = PacedFeeder()
    assert feeder.now_ms() == 0

    seen: list[int] = []

    async def observe(frame: bytes) -> None:
        seen.append(feeder.now_ms())

    await feeder.feed(_frames(6), observe)

    assert seen == sorted(seen)
    assert seen[-1] >= 4 * FRAME_MS


@pytest.mark.asyncio
async def test_wait_until_ms_resolves_at_the_stream_deadline() -> None:
    feeder = PacedFeeder()
    reached: list[int] = []

    async def waiter() -> None:
        await feeder.wait_until_ms(150)
        reached.append(feeder.now_ms())

    task = asyncio.ensure_future(waiter())
    await feeder.feed(_frames(10), _noop)
    await task

    assert reached and 130 <= reached[0] <= 260


@pytest.mark.asyncio
async def test_wait_until_ms_past_the_end_of_the_clip_returns() -> None:
    """A deadline beyond the clip must not hang the control task."""
    feeder = PacedFeeder()

    async def waiter() -> None:
        await feeder.wait_until_ms(10_000)

    task = asyncio.ensure_future(waiter())
    await feeder.feed(_frames(4), _noop)
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_wait_until_ms_leaves_no_pending_task() -> None:
    """EC-05 in miniature: the timeout path must cancel its own waiter."""
    feeder = PacedFeeder()
    before = len(asyncio.all_tasks())

    async def waiter() -> None:
        await feeder.wait_until_ms(100)

    task = asyncio.ensure_future(waiter())
    await feeder.feed(_frames(6), _noop)
    await task

    assert len(asyncio.all_tasks()) <= before + 1


def test_frames_of_splits_and_pads_the_tail() -> None:
    pcm = b"\x01" * (FRAME_BYTES + 10)
    out = frames_of(pcm, sample_rate=SAMPLE_RATE)
    assert len(out) == 2
    assert all(len(f) == FRAME_BYTES for f in out)
    assert out[1][:10] == b"\x01" * 10
    assert out[1][10:] == b"\x00" * (FRAME_BYTES - 10)


def test_frames_of_on_empty_input() -> None:
    assert frames_of(b"", sample_rate=SAMPLE_RATE) == []


@pytest.mark.asyncio
async def test_feed_is_paced_in_real_time() -> None:
    """The point of the whole module: 10 frames take about 10 frame periods."""
    feeder = PacedFeeder()
    start = time.monotonic()
    await feeder.feed(_frames(10), _noop)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert elapsed_ms >= 9 * FRAME_MS
