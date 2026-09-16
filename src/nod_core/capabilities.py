"""Probe which knobs and fields this model actually exposes.

ARCHITECTURE.md §2: this module never assumes. Universal-3 Pro Streaming uses
punctuation-based turn detection rather than a confidence score, so the presence
of every field below has to be established rather than taken on trust
(DECISIONS.md ADR-001).

The upstream API answers a successful `UpdateConfiguration` with silence, so
acceptance proves nothing on its own: a silently dropped field looks exactly like
an applied one. Every verdict here is therefore behavioural. Each knob is driven
in four cells — set at connect time low and high, and updated mid-stream low and
high — and a verdict is only `LIVE` when the turn boundary actually moved in both.

This module owns the analysis, not the driving. It consumes an event stream and a
stream-relative clock, both injected, so it holds no socket and reads no audio
(INV-7 falls out of that rather than being asserted).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from nod_adapters.protocols import SttSession
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    KnobVerdict,
    SessionBegin,
    Termination,
    Turn,
)

UPDATABLE_FIELDS: Final = (
    "end_of_turn_confidence_threshold",
    "min_turn_silence",
    "max_turn_silence",
    "vad_threshold",
)
"""The four knobs `UpdateConfiguration` is documented to cover (CONTROL_SPEC.md §0)."""

PROBE_CACHE_SIZE: Final = 16
"""Capability probes retained, keyed on (model, api_version) (ARCHITECTURE.md §5)."""

PROBE_CACHE_TTL_S: Final = 3600
"""Probe cache time to live. Seconds (ARCHITECTURE.md §5)."""

MIN_SEPARATION_FRACTION: Final = 0.6
"""Two arms must differ by this fraction of the expected shift to count."""

IQR_MULTIPLE: Final = 2.0
"""Arm medians must differ by this multiple of the wider arm's IQR.

BENCH_SPEC.md §4: a result whose spread exceeds the arm-to-arm difference is
inconclusive. Applied here to the probe rather than to the benchmark.
"""

AGREEMENT_FRACTION: Final = 0.4
"""A mid-stream arm must land within this fraction of the expected shift of its twin.

Deliberately below `0.5`: at half the expected shift a mid-stream arm could
"agree" with the *opposite* connect-time arm, and the check would pass on a knob
that moved the boundary to exactly the wrong place.

Both this and `MIN_SEPARATION_FRACTION` scale an *expected shift in
milliseconds*, never the knob's own delta. A confidence threshold's delta is
dimensionless (0.95 - 0.20 = 0.75) and a silence knob's is milliseconds; scaling
a millisecond tolerance by the former yields 0.3 ms, which no real measurement
can satisfy, and a working knob is reported `STATIC_ONLY`.
"""

NO_BOUNDARY: Final = math.inf
"""No turn boundary fired inside the test gap.

Positive infinity rather than a `None` special case: "the turn never ended" is
the limit of "the turn ended late", so the ordering comparisons below stay total
and the direction check needs no extra branch.
"""


@dataclass(frozen=True, slots=True)
class CellPlan:
    """One probe session: one knob, one value, one way of setting it.

    `expected_shift_ms` is how far the boundary should move between the two arms,
    in milliseconds — the unit of the observable, not of the knob. It is a
    property of the stimulus design, not arithmetic on the arm values.

    `direction` is `+1` when a lower arm value is expected to produce an earlier
    boundary and `-1` when the relationship inverts. `vad_threshold` is the
    inverted one: a low threshold classifies the gap's room tone as speech, so
    silence never accumulates and the boundary moves later, not sooner.
    """

    cell: str
    field: str
    arm_value: float
    delta: float
    direction: int
    midstream: bool
    expected_shift_ms: int
    update_at_ms: int
    gap_start_ms: int
    gap_end_ms: int
    force_endpoint_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class CellObservation:
    """What one probe session measured.

    `boundary_ms` is milliseconds from the start of the test gap to the arrival of
    the `end_of_turn` turn, or `NO_BOUNDARY` if none arrived inside the gap.
    """

    cell: str
    field: str
    arm_value: float
    boundary_ms: float
    last_word_end_ms: int | None
    error_code: int | None
    confidence_samples: tuple[float, ...]
    confidence_on_partials: bool


def _median(values: Sequence[float]) -> float:
    """Return the median. `O(n log n)`, `n` is the repeat count."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _iqr(values: Sequence[float]) -> float:
    """Return the interquartile range, `0.0` for fewer than four samples."""
    if len(values) < 4:
        ordered = sorted(values)
        return ordered[-1] - ordered[0] if ordered else 0.0
    ordered = sorted(values)
    half = len(ordered) // 2
    return _median(ordered[-half:]) - _median(ordered[:half])


def _separated(
    low: Sequence[float], high: Sequence[float], expected_shift_ms: float
) -> bool:
    """Whether two arms moved the boundary apart by more than the noise.

    Categorical first: if exactly one arm never produced a boundary, the arms are
    separated by construction and no statistics apply. Otherwise both medians and
    spreads must clear `IQR_MULTIPLE` and `MIN_SEPARATION_FRACTION`.

    `O(n log n)` in the repeat count.

    Args:
        low: Boundary measurements from the low arm.
        high: Boundary measurements from the high arm.
        expected_shift_ms: How far the boundary should move, in milliseconds.

    Returns:
        Whether the separation is real.
    """
    if not low or not high:
        return False
    low_never = all(math.isinf(v) for v in low)
    high_never = all(math.isinf(v) for v in high)
    low_always = not any(math.isinf(v) for v in low)
    high_always = not any(math.isinf(v) for v in high)

    if low_never and high_never:
        # Neither arm ever ended a turn: the knob moved nothing.
        return False
    if (low_never and high_always) or (high_never and low_always):
        # One arm always ended the turn and the other never did.
        return True
    if not (low_always and high_always):
        # An arm that fires on some repeats and not others is not reproducible,
        # and must not reach the categorical branch above on the strength of the
        # *other* arm being consistent.
        return False
    gap = abs(_median(high) - _median(low))
    spread = max(_iqr(low), _iqr(high))
    return gap >= IQR_MULTIPLE * spread and gap >= MIN_SEPARATION_FRACTION * abs(
        expected_shift_ms
    )


def _directed(low: Sequence[float], high: Sequence[float], direction: int) -> bool:
    """Whether the boundary moved the way the stimulus predicts. `O(n log n)`."""
    low_median = _median(low)
    high_median = _median(high)
    if math.isinf(low_median) and math.isinf(high_median):
        return False
    if math.isinf(high_median):
        return direction > 0
    if math.isinf(low_median):
        return direction < 0
    moved = high_median - low_median
    return moved > 0 if direction > 0 else moved < 0


def _agrees(
    mid: Sequence[float], connect: Sequence[float], expected_shift_ms: float
) -> bool:
    """Whether a mid-stream arm landed where its connect-time twin landed.

    A mid-stream update that moves the boundary to the *wrong* place is not a
    working knob, and comparing the two mid-stream arms only against each other
    would not catch it. `O(n log n)`.

    Args:
        mid: Boundary measurements from the mid-stream arm.
        connect: Boundary measurements from the connect-time arm at the same value.
        expected_shift_ms: How far the boundary should move, in milliseconds.

    Returns:
        Whether the two agree.
    """
    if not mid or not connect:
        return False
    mid_median = _median(mid)
    connect_median = _median(connect)
    if math.isinf(mid_median) or math.isinf(connect_median):
        return math.isinf(mid_median) and math.isinf(connect_median)
    tolerance = max(
        IQR_MULTIPLE * max(_iqr(mid), _iqr(connect)),
        AGREEMENT_FRACTION * abs(expected_shift_ms),
    )
    return abs(mid_median - connect_median) <= tolerance


def _boundaries(
    observations: Sequence[CellObservation], cell: str
) -> tuple[float, ...]:
    """Collect one cell's boundary measurements, in order. `O(n)`."""
    return tuple(o.boundary_ms for o in observations if o.cell == cell)


def verdict_for(
    observations: Sequence[CellObservation],
    *,
    expected_shift_ms: float,
    direction: int,
) -> KnobVerdict:
    """Reduce one knob's four cells to a verdict. Pure. `O(n log n)`.

    The lattice is the point of the four-cell design. Connect-time cells
    establish whether the parameter has any behavioural force on this model at
    all; without them a null mid-stream result cannot be attributed, because a
    broken `UpdateConfiguration` and an inert knob look identical.

    Args:
        observations: Every cell observation for one knob, all repeats.
        expected_shift_ms: How far the boundary should move between the arms, in
            milliseconds. Not the knob's own delta: a confidence threshold's
            delta is dimensionless and would make the tolerances meaningless.
        direction: `+1` if a lower arm value should yield an earlier boundary.

    Returns:
        The verdict. Anything short of proof is `UNPROVEN`, never `LIVE`.
    """
    if any(o.error_code is not None for o in observations):
        return KnobVerdict.REJECTED

    connect_low = _boundaries(observations, "connect_low")
    connect_high = _boundaries(observations, "connect_high")
    mid_low = _boundaries(observations, "mid_low")
    mid_high = _boundaries(observations, "mid_high")

    if not (connect_low and connect_high):
        return KnobVerdict.UNPROVEN

    connect_moved = _separated(
        connect_low, connect_high, expected_shift_ms
    ) and _directed(connect_low, connect_high, direction)
    if not connect_moved:
        # The parameter does nothing on this model, so mid-stream is moot.
        return KnobVerdict.INERT

    mid_moved = (
        _separated(mid_low, mid_high, expected_shift_ms)
        and _directed(mid_low, mid_high, direction)
        and _agrees(mid_low, connect_low, expected_shift_ms)
        and _agrees(mid_high, connect_high, expected_shift_ms)
    )
    return KnobVerdict.LIVE if mid_moved else KnobVerdict.STATIC_ONLY


def confidence_field(observations: Sequence[CellObservation]) -> ConfidenceField:
    """Classify how usable `end_of_turn_confidence` is. Pure. `O(n)`.

    Presence is the weakest of the three questions and the only one a naive probe
    asks. A field that is present but pinned at one value satisfies a key check
    and still cannot feed CONTROL_SPEC.md §2.4, which is defined over a
    trajectory.

    Args:
        observations: Observations from the control sessions.

    Returns:
        `ABSENT`, `CONSTANT`, `FINALS_ONLY` or `VARYING`.
    """
    samples = tuple(s for o in observations for s in o.confidence_samples)
    if not samples:
        return ConfidenceField.ABSENT
    if len(set(samples)) < 2:
        return ConfidenceField.CONSTANT
    if not any(o.confidence_on_partials for o in observations):
        return ConfidenceField.FINALS_ONLY
    return ConfidenceField.VARYING


def classify(
    by_field: Sequence[tuple[str, Sequence[CellObservation], float, int]],
    *,
    control: Sequence[CellObservation],
    force_endpoint: KnobVerdict,
    has_word_timings: bool,
) -> Capabilities:
    """Reduce every measurement to the capability record. Pure. `O(n log n)`.

    Args:
        by_field: One entry per knob: name, its observations, the expected
            boundary shift in milliseconds, and the expected direction.
        control: Observations from the knob-free control sessions.
        force_endpoint: The separately measured `ForceEndpoint` verdict.
        has_word_timings: Whether `words[].start`/`.end` arrived at all.

    Returns:
        What this model exposes, with every unproven knob failing closed.
    """
    knobs = tuple(
        (
            field,
            verdict_for(observations, expected_shift_ms=shift_ms, direction=direction),
        )
        for field, observations, shift_ms, direction in by_field
    )
    return Capabilities(
        knobs=knobs,
        confidence_field=confidence_field(control),
        force_endpoint=force_endpoint,
        has_word_timings=has_word_timings,
    )


async def observe(
    events: AsyncIterator[SessionBegin | Turn | Termination],
    *,
    plan: CellPlan,
    clock: Callable[[], int],
) -> CellObservation:
    """Measure one session's turn boundary from its event stream.

    `clock` returns stream-relative milliseconds — frames sent times the frame
    size — never wall clock (CLAUDE.md §6). It is injected rather than read so
    this function holds no feeder and no socket.

    The boundary is the arrival of the first `end_of_turn` turn inside the test
    gap. A turn that ends before the gap opens belongs to the preamble and is
    ignored; a turn that ends after the gap closes was ended by the following
    speech, not by the knob.

    `O(ΔW)` per turn, `O(1)` space beyond the confidence samples.

    Args:
        events: The upstream stream, partials included.
        plan: What this session is testing.
        clock: Current stream-relative milliseconds.

    Returns:
        The measurement.
    """
    boundary = NO_BOUNDARY
    last_word_end: int | None = None
    confidence_samples: list[float] = []
    on_partials = False

    async for event in events:
        if not isinstance(event, Turn):
            continue
        if event.end_of_turn_confidence is not None:
            confidence_samples.append(event.end_of_turn_confidence)
            if not event.end_of_turn:
                on_partials = True
        if event.words:
            last_word_end = event.words[-1].end_ms
        if not event.end_of_turn:
            continue
        now = clock()
        if plan.gap_start_ms <= now <= plan.gap_end_ms and math.isinf(boundary):
            boundary = float(now - plan.gap_start_ms)

    return CellObservation(
        cell=plan.cell,
        field=plan.field,
        arm_value=plan.arm_value,
        boundary_ms=boundary,
        last_word_end_ms=last_word_end,
        error_code=None,
        confidence_samples=tuple(confidence_samples),
        confidence_on_partials=on_partials,
    )


async def probe(
    session: SttSession,
    *,
    model: str,
    plan: CellPlan,
    feed: Callable[[], Awaitable[None]],
    clock: Callable[[], int],
    wait_until: Callable[[int], Awaitable[None]],
) -> CellObservation:
    """Drive one probe session and return what it measured.

    Attempts a mid-stream `UpdateConfiguration` for a single field — never a
    batch, so a rejection names exactly one parameter and costs exactly one
    session (docs/PROMPTS.md P1).

    `feed` pumps the clip into `session` and is injected, because the paced feeder
    is bench machinery and `nod_core` imports only the protocols
    (ARCHITECTURE.md §2). `model` is recorded for the cache key of
    ARCHITECTURE.md §5, `PROBE_CACHE_SIZE` entries with TTL `PROBE_CACHE_TTL_S`.

    Args:
        session: A live upstream session, already connected with this cell's
            connect-time configuration.
        model: The model name, part of the cache key.
        plan: What this session is testing.
        feed: Pumps audio into the session and returns when the clip is done.
        clock: Current stream-relative milliseconds.
        wait_until: Resolves when the feeder reaches a stream-relative deadline,
            or immediately once the clip has been fully sent.

    Returns:
        The measurement.
    """
    observed = asyncio.ensure_future(observe(session.events(), plan=plan, clock=clock))
    feeder = asyncio.ensure_future(feed())

    async def _control() -> None:
        """Send this cell's one control frame at its scheduled stream time."""
        if plan.midstream:
            await wait_until(plan.update_at_ms)
            await session.update_configuration({plan.field: plan.arm_value})
        if plan.force_endpoint_at_ms is not None:
            await wait_until(plan.force_endpoint_at_ms)
            await session.force_endpoint()

    try:
        await _control()
        await feeder
    finally:
        await session.aclose()
    return await observed


def degrade(caps: Capabilities) -> frozenset[str]:
    """Map capabilities onto the degradation matrix of CONTROL_SPEC.md §7. `O(1)`.

    Args:
        caps: What the probe found.

    Returns:
        The names of the axes and features that must be disabled. An empty set
        means the full law applies.
    """
    disabled: set[str] = set()

    if not caps.has_end_of_turn_confidence:
        disabled |= {"jitter", "conf_axis"}
    if "end_of_turn_confidence_threshold" not in caps.updatable_fields:
        disabled |= {"jitter", "conf_axis"}
    if not caps.has_word_timings:
        disabled |= {"profiler", "speaker_axis"}
    if not caps.supports_force_endpoint:
        disabled.add("early_endpoint")
    if not {"min_turn_silence", "max_turn_silence"} & caps.updatable_fields:
        # CONTROL_SPEC.md §0 fact 2: silence is the axis that actually endpoints.
        # With neither knob live there is nothing left to control.
        disabled |= {"silence_axis", "observe_only"}

    return frozenset(disabled)
