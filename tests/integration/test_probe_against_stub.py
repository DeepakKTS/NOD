"""`capabilities.probe()` driven end to end without a socket.

INV-7: this is the whole reason `nod_core` talks to `SttSession` rather than to a
concrete adapter. The fake below behaves like a model whose `max_turn_silence`
genuinely works — it ends the turn that many milliseconds into the gap — so the
probe measuring it as `LIVE` is a real result rather than a tautology.

Everything is scaled down from the live plan: the shapes are identical, the
durations are not, so the suite stays fast.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest

from nod_bench.feeder import FRAME_MS, PacedFeeder
from nod_core.capabilities import (
    NO_BOUNDARY,
    CellObservation,
    CellPlan,
    observe,
    probe,
)
from nod_core.types import SessionBegin, Termination, Turn, Word

GAP_START_MS = 200
GAP_END_MS = 700
FRAME_BYTES = (16000 * FRAME_MS // 1000) * 2
CLIP_FRAMES = 20  # 1.0 s


def _turn(order: int, *, end: bool, confidence: float | None, end_ms: int) -> Turn:
    return Turn(
        turn_order=order,
        end_of_turn=end,
        end_of_turn_confidence=confidence,
        transcript="hello there",
        words=(
            Word(
                text="there",
                start_ms=end_ms - 120,
                end_ms=end_ms,
                confidence=0.9,
                is_final=True,
            ),
        ),
    )


class FakeSttSession:
    """An `SttSession` whose `max_turn_silence` actually works.

    Ends the turn `max_turn_silence` ms into the test gap, honouring both the
    connect-time value and any mid-stream update that lands before the gap opens.
    """

    def __init__(
        self, feeder: PacedFeeder, *, connect_max: int, honour_updates: bool = True
    ):
        self.id = "fake-session"
        self._feeder = feeder
        self._max = connect_max
        self._honour = honour_updates
        self.updates: list[Mapping[str, float]] = []
        self.forced_at: int | None = None
        self.closed = False
        self._forced = asyncio.Event()

    def _now(self) -> int:
        return self._feeder.now_ms()

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def update_configuration(self, patch: Mapping[str, float]) -> None:
        self.updates.append(dict(patch))
        if self._honour and "max_turn_silence" in patch:
            self._max = int(patch["max_turn_silence"])

    async def force_endpoint(self) -> None:
        self.forced_at = self._now()
        self._forced.set()

    async def aclose(self) -> None:
        self.closed = True

    async def events(self) -> AsyncIterator[SessionBegin | Turn | Termination]:
        yield SessionBegin(session_id=self.id, expires_at_ms=999_000)
        yield _turn(1, end=False, confidence=0.31, end_ms=100)
        yield _turn(1, end=False, confidence=0.55, end_ms=180)

        await self._feeder.wait_until_ms(GAP_START_MS)

        # The boundary the probe is here to measure. A forced endpoint short-
        # circuits the wait, the same way it would upstream.
        deadline = GAP_START_MS + self._max
        waiter = asyncio.ensure_future(self._feeder.wait_until_ms(deadline))
        forced = asyncio.ensure_future(self._forced.wait())
        _, pending = await asyncio.wait(
            (waiter, forced), return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if self._now() <= GAP_END_MS:
            yield _turn(1, end=True, confidence=0.88, end_ms=180)

        await self._feeder.wait_until_ms(CLIP_FRAMES * FRAME_MS)
        yield Termination(session_id=self.id, reason="terminated")


def _plan(
    cell: str, value: float, *, midstream: bool, force_at: int | None = None
) -> CellPlan:
    return CellPlan(
        cell=cell,
        field="max_turn_silence",
        arm_value=value,
        delta=300.0,
        direction=1,
        midstream=midstream,
        expected_shift_ms=600,
        update_at_ms=100,
        gap_start_ms=GAP_START_MS,
        gap_end_ms=GAP_END_MS,
        force_endpoint_at_ms=force_at,
    )


async def _run(
    plan: CellPlan, session: FakeSttSession, feeder: PacedFeeder
) -> CellObservation:
    frames = [b"\x00" * FRAME_BYTES] * CLIP_FRAMES

    async def feed() -> None:
        await feeder.feed(frames, session.send_audio)

    return await probe(
        session,
        model="fake-model",
        plan=plan,
        feed=feed,
        clock=feeder.now_ms,
        wait_until=feeder.wait_until_ms,
    )


@pytest.mark.asyncio
async def test_connect_time_low_arm_measures_an_early_boundary() -> None:
    feeder = PacedFeeder(max_lag_ms=10_000.0)
    session = FakeSttSession(feeder, connect_max=100)
    obs = await _run(_plan("connect_low", 100.0, midstream=False), session, feeder)

    assert obs.cell == "connect_low"
    assert obs.boundary_ms != NO_BOUNDARY
    assert 60 <= obs.boundary_ms <= 220
    assert session.updates == []
    assert session.closed is True


@pytest.mark.asyncio
async def test_connect_time_high_arm_measures_a_later_boundary() -> None:
    feeder = PacedFeeder(max_lag_ms=10_000.0)
    session = FakeSttSession(feeder, connect_max=400)
    obs = await _run(_plan("connect_high", 400.0, midstream=False), session, feeder)

    assert obs.boundary_ms != NO_BOUNDARY
    assert 340 <= obs.boundary_ms <= 520


@pytest.mark.asyncio
async def test_midstream_update_is_sent_once_with_exactly_one_field() -> None:
    """One field per frame, never a batch: a rejection must name one parameter."""
    feeder = PacedFeeder(max_lag_ms=10_000.0)
    session = FakeSttSession(feeder, connect_max=100)
    await _run(_plan("mid_high", 400.0, midstream=True), session, feeder)

    assert len(session.updates) == 1
    assert session.updates[0] == {"max_turn_silence": 400.0}


@pytest.mark.asyncio
async def test_midstream_update_moves_the_measured_boundary() -> None:
    """The load-bearing behaviour: an honoured update shows up in the measurement."""
    feeder = PacedFeeder(max_lag_ms=10_000.0)
    session = FakeSttSession(feeder, connect_max=100)
    obs = await _run(_plan("mid_high", 400.0, midstream=True), session, feeder)

    assert obs.boundary_ms != NO_BOUNDARY
    assert 340 <= obs.boundary_ms <= 520


@pytest.mark.asyncio
async def test_a_silently_ignored_update_is_not_mistaken_for_success() -> None:
    """The failure the whole probe exists to detect.

    The session accepts the update, returns no error, and changes nothing. The
    measured boundary must stay where the connect-time value put it.
    """
    feeder = PacedFeeder(max_lag_ms=10_000.0)
    session = FakeSttSession(feeder, connect_max=100, honour_updates=False)
    obs = await _run(_plan("mid_high", 400.0, midstream=True), session, feeder)

    assert session.updates == [{"max_turn_silence": 400.0}]  # accepted...
    assert obs.boundary_ms <= 220  # ...and ignored.


@pytest.mark.asyncio
async def test_force_endpoint_is_sent_at_its_scheduled_stream_time() -> None:
    feeder = PacedFeeder(max_lag_ms=10_000.0)
    session = FakeSttSession(feeder, connect_max=5000)
    plan = _plan("force_test", 0.0, midstream=False, force_at=GAP_START_MS + 100)
    obs = await _run(plan, session, feeder)

    assert session.forced_at is not None
    assert GAP_START_MS + 80 <= session.forced_at <= GAP_START_MS + 260
    assert obs.boundary_ms != NO_BOUNDARY


@pytest.mark.asyncio
async def test_no_boundary_when_the_turn_never_ends_inside_the_gap() -> None:
    feeder = PacedFeeder(max_lag_ms=10_000.0)
    session = FakeSttSession(feeder, connect_max=5000)
    obs = await _run(_plan("connect_high", 5000.0, midstream=False), session, feeder)
    assert obs.boundary_ms == NO_BOUNDARY


@pytest.mark.asyncio
async def test_confidence_samples_record_partials_separately() -> None:
    feeder = PacedFeeder(max_lag_ms=10_000.0)
    session = FakeSttSession(feeder, connect_max=100)
    obs = await _run(_plan("control", 100.0, midstream=False), session, feeder)

    assert obs.confidence_on_partials is True
    assert 0.31 in obs.confidence_samples
    assert len(set(obs.confidence_samples)) >= 2


@pytest.mark.asyncio
async def test_observe_ignores_turns_that_end_before_the_gap_opens() -> None:
    """A preamble turn is not the measurement; it belongs to the warm-up."""

    async def events() -> AsyncIterator[SessionBegin | Turn | Termination]:
        yield _turn(1, end=True, confidence=0.9, end_ms=50)

    plan = _plan("connect_low", 100.0, midstream=False)
    obs = await observe(events(), plan=plan, clock=lambda: 50)
    assert obs.boundary_ms == NO_BOUNDARY


@pytest.mark.asyncio
async def test_observe_ignores_turns_that_end_after_the_gap_closes() -> None:
    """A turn ended by the following speech was not ended by the knob."""

    async def events() -> AsyncIterator[SessionBegin | Turn | Termination]:
        yield _turn(1, end=True, confidence=0.9, end_ms=50)

    plan = _plan("connect_low", 100.0, midstream=False)
    obs = await observe(events(), plan=plan, clock=lambda: GAP_END_MS + 500)
    assert obs.boundary_ms == NO_BOUNDARY
