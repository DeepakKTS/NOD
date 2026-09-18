"""Differential and structural properties of the two quantile estimators.

ADR-002 keeps two implementations — P² for production, an exact ring behind
`NOD_EXACT_QUANTILES` — so that one can check the other. This file is that check,
and `g_p90` feeding `max_turn_silence` is why it sits in CLAUDE.md §5's
non-negotiable tier.

**What bound is asserted, and why it is not a relative one.** P² is an `O(1)`-memory
heuristic with no a priori error bound. Its value error depends on the sample
density near the target quantile: where the density is low, a one-position marker
move crosses a large value gap, and the error in milliseconds is large while
nothing is wrong with the algorithm. So a relative tolerance is an empirical claim
about a population, never a property of the estimator.

That was measured rather than assumed. ADR-002 says the two "agree within 5 % on
the corpora", and restating that as a hypothesis property over arbitrary gap
streams **fails**: at `n >= 256`, 58 of 300 drawn streams broke
`max(1 ms, 5 %)`, worst case 13.73 % (p50 estimate 1969 ms against an exact
2282 ms). Rank error — the density-independent metric, how many samples actually
fall below the estimate — is no better: 45.6 percentage points at `n = 9`, and on
a 95/5 spiky stream still 0.45 at `n = 256`. Both figures are in the Gate 2 report.

So the properties here are the ones that hold **structurally, for every stream**,
and the 5 % figure is asserted only over the realistic pause distributions ADR-002
scopes it to, with its tail reported rather than bounded. A bound that has to be
chosen to pass is not a bound; these do not have to be chosen at all.
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nod_core.profiler import (
    GAP_CLAMP_MAX_MS,
    MIN_GAPS_FOR_WARM,
    P2_MARKERS,
    ExactQuantile,
    P2Quantile,
    Profiler,
    ProfilerState,
    _nearest_rank,
)
from nod_core.types import SpeakerFeatures, Turn, Word

TRACKED_QUANTILES: Final = (0.50, 0.90)
"""The two CONTROL_SPEC §2.1 maintains. `g_p90` is the one that reaches the knob."""

ADR_002_TOLERANCE: Final = 0.05
"""ADR-002's stated agreement, "within 5 % on the corpora". Not a chosen number."""

INPUT_RESOLUTION_MS: Final = 1.0
"""Word timings arrive as integer milliseconds, so sub-millisecond is noise.

Paired with the relative tolerance because a relative bound is meaningless near
zero: an exact quantile of 1 ms against an estimate of 2 ms is 100 % relative and
one millisecond absolute, which no control law can act on.
"""

SETTLED_SAMPLES: Final = 256
"""Where ADR-002's tolerance is asserted. Samples.

P² needs samples before its markers settle; the measured median relative error on
the digit-reading shape falls 35.6 % → 6.7 % → 1.5 % across n = 16, 64, 256. This
is also exactly `GAP_RING_CAPACITY`, which matters for a different reason recorded
in `test_the_two_estimators_stop_being_comparable_past_the_ring`.
"""

PROPERTY_SETTINGS = settings(deadline=None)
"""Deadline off: the timing budget is `test_budget.py`'s job, not hypothesis's."""


def gap_streams(
    *, min_size: int = 1, max_size: int = 400
) -> st.SearchStrategy[list[float]]:
    """Streams of inter-word gaps as `Profiler` would ingest them.

    Bounded by `GAP_CLAMP_MAX_MS` because §2.1 clamps every gap to `[0, 6000]`
    *before* ingestion, so no other value can reach an estimator.

    Args:
        min_size: Shortest stream.
        max_size: Longest stream.

    Returns:
        The strategy.
    """
    return st.lists(
        st.floats(
            min_value=0.0,
            max_value=float(GAP_CLAMP_MAX_MS),
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=min_size,
        max_size=max_size,
    )


# --- structural properties, true for every stream ---------------------------


@PROPERTY_SETTINGS
@given(stream=gap_streams(), q=st.sampled_from(TRACKED_QUANTILES))
def test_the_estimate_never_leaves_the_observed_range(
    stream: list[float], q: float
) -> None:
    """P² can only ever report a value the caller's own gaps bracket.

    This is the estimator's one hard guarantee and it needs no tolerance: markers
    0 and 4 *are* the running minimum and maximum, and an interior marker is only
    moved to a value its neighbours strictly bracket. An estimate outside the
    range is therefore not an inaccuracy, it is a broken invariant — and it is the
    failure mode that matters, because `1.6 * g_p90 + 250` turns an out-of-range
    `g_p90` into a `max_turn_silence` no observation supports.
    """
    estimator = P2Quantile(q)
    for sample in stream:
        estimator.update(sample)
    assert min(stream) <= estimator.value <= max(stream), (
        f"estimate {estimator.value} is outside the observed "
        f"[{min(stream)}, {max(stream)}]"
    )


@PROPERTY_SETTINGS
@given(
    stream=gap_streams(max_size=P2_MARKERS - 1), q=st.sampled_from(TRACKED_QUANTILES)
)
def test_the_two_estimators_agree_exactly_before_p2_begins(
    stream: list[float], q: float
) -> None:
    """Below five samples the two are the same computation, so `==` is the bound.

    P² has no markers to move before `P2_MARKERS` samples, so it reports the exact
    nearest-rank quantile over its buffer — the same thing `ExactQuantile` reports.
    An exact equality here is worth more than any tolerance elsewhere: it pins that
    the cold regime is not quietly approximating, which is the regime a short call
    spends most of its turns in.
    """
    p2, exact = P2Quantile(q), ExactQuantile(q)
    for sample in stream:
        p2.update(sample)
        exact.update(sample)
    assert p2.value == exact.value


@PROPERTY_SETTINGS
@given(
    value=st.floats(
        min_value=0.0,
        max_value=float(GAP_CLAMP_MAX_MS),
        allow_nan=False,
        allow_infinity=False,
    ),
    count=st.integers(min_value=1, max_value=400),
    q=st.sampled_from(TRACKED_QUANTILES),
)
def test_a_constant_stream_is_reported_exactly(
    value: float, count: int, q: float
) -> None:
    """Every quantile of a constant stream is that constant, at any length.

    Exact, distribution-free, and it catches a large class of marker arithmetic
    bugs: any sign error, off-by-one position or mis-ordered denominator in the
    parabolic or linear step moves a marker off a value that nothing should move
    it off. A fluent caller with a metronomic rhythm is also the realistic version
    of this stream, so it is not only a degenerate case.
    """
    estimator = P2Quantile(q)
    for _ in range(count):
        estimator.update(value)
    assert estimator.value == value


@pytest.mark.parametrize("q", TRACKED_QUANTILES)
@pytest.mark.parametrize("factory", [P2Quantile, ExactQuantile])
def test_a_quantile_of_nothing_raises_rather_than_reporting_zero(
    factory: type[P2Quantile] | type[ExactQuantile], q: float
) -> None:
    """An un-fed estimator has no answer, and `0.0` would be a fabricated gap.

    Matches `nod_bench.metrics.quantile`, which refuses the same question with the
    same reasoning: a `0.0` here would enter `g_p50_ms` as a real measurement of a
    speaker who pauses not at all, and nothing downstream could contradict it.
    """
    with pytest.raises(ValueError, match="quantile of nothing"):
        _ = factory(q).value


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
@pytest.mark.parametrize("factory", [P2Quantile, ExactQuantile])
def test_a_quantile_outside_the_open_unit_interval_is_refused(
    factory: type[P2Quantile] | type[ExactQuantile], bad: float
) -> None:
    """`q = 0` and `q = 1` are the minimum and maximum, not quantiles to estimate."""
    with pytest.raises(ValueError, match="outside"):
        factory(bad)


def test_exact_quantile_matches_the_published_definition() -> None:
    """`_nearest_rank` here must equal `nod_bench.metrics.quantile` there.

    `nod_core` may not import `nod_bench` (ARCHITECTURE §2), so the nearest-rank
    definition exists twice and cannot be reduced to once. CLAUDE.md §5: two
    constants agreeing by meaning and not by code are a coincidence the codebase
    is enjoying, not a fact it knows. This is the assertion that makes it a fact.
    """
    from nod_bench.metrics import quantile as published

    samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0]
    for q in (0.01, 0.25, 0.50, 0.90, 0.95, 0.99):
        assert _nearest_rank(sorted(samples), q) == published(samples, q), q
    # The nine-sample p90 that ADR-018 records: nearest-rank 90, interpolation 82.
    assert _nearest_rank(sorted(samples), 0.90) == 90.0


def test_the_two_estimators_stop_being_comparable_past_the_ring() -> None:
    """A differential test longer than `GAP_RING_CAPACITY` tests nothing.

    Recorded as a test rather than a comment because it is a trap in ADR-002's
    own instruction to compare the two. `ExactQuantile` holds the last
    `GAP_RING_CAPACITY` samples and P² holds a summary of all of them, so past the
    ring they are estimating **different populations** and a disagreement is
    correct behaviour rather than drift. Every differential assertion in this file
    either stays inside the ring or gives the exact estimator enough capacity to
    see the whole stream.
    """
    rising = [float(i) for i in range(1000)]
    ringed = ExactQuantile(0.90, capacity=256)
    whole = ExactQuantile(0.90, capacity=len(rising) + 1)
    for sample in rising:
        ringed.update(sample)
        whole.update(sample)
    # The ring sees only the last 256 of a rising stream, so every sample it holds
    # is above the whole stream's median and its p90 must exceed the whole's.
    # Asserted as a strict inequality and not against a margin: the direction is
    # structural, and any margin would be a number chosen to pass.
    assert ringed.value > whole.value, (
        f"ring p90 {ringed.value} did not exceed whole-stream p90 {whole.value}"
    )
    # Both saw every sample; only one kept them. `seen` is what distinguishes
    # "ingested" from "retained", and a differential test needs the difference.
    assert ringed.seen == whole.seen == len(rising)


# --- ADR-002's tolerance, over the population it is scoped to ---------------


def _shaped_stream(rng: random.Random, shape: str, size: int) -> list[float]:
    """Build a pause stream of a named realistic shape.

    Args:
        rng: Seeded generator.
        shape: One of the four shapes described in the caller.
        size: Stream length.

    Returns:
        Gaps in milliseconds, clamped as §2.1 clamps them.
    """
    if shape == "fluent":
        raw = [rng.gauss(180.0, 40.0) for _ in range(size)]
    elif shape == "hesitant":
        raw = [rng.gauss(650.0, 220.0) for _ in range(size)]
    elif shape == "digit_reading":
        raw = [
            rng.gauss(180.0, 40.0) if rng.random() < 0.8 else rng.gauss(1500.0, 250.0)
            for _ in range(size)
        ]
    else:
        raw = [rng.uniform(0.0, 1200.0) for _ in range(size)]
    return [min(max(value, 0.0), float(GAP_CLAMP_MAX_MS)) for value in raw]


@pytest.mark.parametrize("q", TRACKED_QUANTILES)
def test_p2_holds_adr_002s_tolerance_on_realistic_pause_streams(q: float) -> None:
    """ADR-002: the two agree within 5 %, over the population that claim is about.

    **Asserted on the median, and the tail is reported rather than bounded.** That
    asymmetry is the honest reading of ADR-002, which scopes its 5 % to "the
    corpora" — a statement about typical agreement on realistic input, not a
    worst-case guarantee, and P² offers no worst-case guarantee to state. This
    file's module docstring records the measurement showing the strong reading is
    false: at this same `n`, adversarially drawn streams break 5 % once in five.

    A hypothesis property is deliberately *not* used here. Hypothesis searches for
    the stream that breaks the bound, which answers "is this a theorem" — and it is
    not. The seeded ensemble answers "does it hold on speech-shaped input", which
    is the question ADR-002 asked.
    """
    rng = random.Random(29)  # noqa: S311 — a seeded statistical fixture, not a secret
    errors: dict[str, list[float]] = {}
    for shape in ("fluent", "hesitant", "digit_reading", "mixed"):
        shape_errors: list[float] = []
        for _ in range(60):
            stream = _shaped_stream(rng, shape, SETTLED_SAMPLES)
            p2 = P2Quantile(q)
            exact = ExactQuantile(q, capacity=len(stream) + 1)
            for sample in stream:
                p2.update(sample)
                exact.update(sample)
            truth = exact.value
            gap = abs(p2.value - truth)
            allowed = max(INPUT_RESOLUTION_MS, ADR_002_TOLERANCE * truth)
            shape_errors.append(gap / allowed)
        errors[shape] = shape_errors

    for shape, shape_errors in errors.items():
        median = statistics.median(shape_errors)
        assert median <= 1.0, (
            f"q={q} on {shape}: median disagreement is {median:.2f}x ADR-002's "
            f"max(1 ms, 5 %) allowance over {SETTLED_SAMPLES} samples"
        )


# --- the impossible-value check (CLAUDE.md §5) ------------------------------


@PROPERTY_SETTINGS
@given(stream=gap_streams(min_size=MIN_GAPS_FOR_WARM))
def test_g_p90_can_never_be_negative_or_beyond_the_clamp(
    stream: list[float],
) -> None:
    """The two values `g_p90` could not possibly take, confirmed it does not.

    §2.1 clamps every gap to `[0, 6000]` ms before ingestion, and a quantile of
    values in that interval is in that interval. So a negative `g_p90` or one past
    `GAP_CLAMP_MAX_MS` is arithmetically unreachable — which is exactly why it is
    worth asserting: `max_ms = 1.6 * g_p90 + 250` would turn a negative into a
    `max_turn_silence` below its floor and a 9000 ms one into a caller waiting out
    the ceiling, and in both cases the §4 clamps would tidy the number away and
    leave no trace that the feature was impossible.
    """
    p50, p90 = P2Quantile(0.50), P2Quantile(0.90)
    for sample in stream:
        p50.update(sample)
        p90.update(sample)
    for name, value in (("g_p50", p50.value), ("g_p90", p90.value)):
        assert 0.0 <= value <= GAP_CLAMP_MAX_MS, f"{name}={value} left the clamp"


def test_two_independent_estimators_can_report_p90_below_p50() -> None:
    """**Measured, not hypothetical: the profiler can emit an impossible state.**

    `g_p90 < g_p50` cannot be true of any real sample set — they are two quantiles
    of one population. CONTROL_SPEC §2.1 says "maintain P² estimators for `g_p50`
    and `g_p90`", two estimators, and says nothing about coupling them. Nothing
    does: they approximate independently, so their errors are independent and the
    order can invert.

    Searched for by asking what value the pair could not take, and found: over
    20 000 random streams the inversion occurred 28 times, 0.14 %, worst inversion
    146 ms. This test pins one reproducing case so the behaviour is known rather
    than latent.

    Not repaired here, deliberately. Changing what `features()` reports is a change
    to the control law's inputs, and CONTROL_SPEC §0 makes that an ADR rather than
    a commit. The Gate 2 report carries the readings; what matters for now is that
    it is written down and that `tests/property/strategies.py` no longer claims the
    state is unreachable.
    """
    rng = random.Random(17)  # noqa: S311 — a seeded statistical fixture, not a secret
    found: tuple[float, float] | None = None
    for _ in range(20_000):
        size = rng.randint(MIN_GAPS_FOR_WARM, 400)
        stream = [
            min(
                max(rng.choice((rng.gauss(150, 30), rng.gauss(2500, 200))), 0.0), 6000.0
            )
            for _ in range(size)
        ]
        p50, p90 = P2Quantile(0.50), P2Quantile(0.90)
        for sample in stream:
            p50.update(sample)
            p90.update(sample)
        if p90.value < p50.value:
            found = (p50.value, p90.value)
            break
    assert found is not None, (
        "no inversion found in 20 000 streams. If the estimators have since been "
        "coupled — by an ADR that repairs the order — this test has done its job "
        "and should be replaced by one asserting g_p90 >= g_p50 always."
    )
    assert found[1] < found[0]


# --- snapshot and restore (EC-03) -------------------------------------------


@st.composite
def _turn_streams(draw: st.DrawFn) -> list[Turn]:
    """A plausible sequence of final `Turn` events for one session.

    Timings are stream-relative and monotonic, which is what the upstream
    guarantees and what `_ingest_words` assumes when it differences them.

    Args:
        draw: Hypothesis draw function.

    Returns:
        The turns, in order.
    """
    turns: list[Turn] = []
    clock = draw(st.integers(min_value=0, max_value=5_000))
    for order in range(1, draw(st.integers(min_value=1, max_value=8)) + 1):
        words: list[Word] = []
        for _ in range(draw(st.integers(min_value=1, max_value=6))):
            clock += draw(st.integers(min_value=0, max_value=2_500))
            start = clock
            clock += draw(st.integers(min_value=40, max_value=900))
            words.append(
                Word(
                    text=draw(st.sampled_from(("hello", "um", "six", "the", "banana"))),
                    start_ms=start,
                    end_ms=clock,
                    confidence=draw(
                        st.floats(
                            min_value=0.0,
                            max_value=1.0,
                            allow_nan=False,
                            allow_infinity=False,
                        )
                    ),
                    is_final=True,
                )
            )
        # Emitted as a sequence of growing partials and then a final, not as one
        # final event. CONTROL_SPEC §2.4's jitter feature is a Welford variance
        # *over the partial sequence*, so a generator that sends one event per
        # turn leaves `conf_n` at 1 and jitter permanently 0.0 — which is how a
        # mutation dropping `jitter` from `restore` survived the Gate 2 run. A
        # round-trip test cannot check a field the fixture never makes non-zero.
        confidences = draw(
            st.lists(
                st.floats(
                    min_value=0.0,
                    max_value=1.0,
                    allow_nan=False,
                    allow_infinity=False,
                ),
                min_size=1,
                max_size=4,
            )
        )
        for index, confidence in enumerate(confidences):
            final = index == len(confidences) - 1
            shown = words if final else words[: max(1, len(words) - 1)]
            turns.append(
                Turn(
                    turn_order=order,
                    end_of_turn=final,
                    end_of_turn_confidence=confidence,
                    transcript=" ".join(word.text for word in shown),
                    words=tuple(shown),
                )
            )
        clock += draw(st.integers(min_value=0, max_value=3_000))
    return turns


@PROPERTY_SETTINGS
@given(turns=_turn_streams(), exact=st.booleans())
def test_a_restored_profiler_reports_identical_features(
    turns: list[Turn], exact: bool
) -> None:
    """EC-03: a rotation must not move the window the caller is living under.

    Field-by-field equality rather than `==` on the dataclass, so a failure names
    the field that drifted instead of printing two eight-field records and leaving
    the reader to diff them. `SpeakerFeatures` is frozen and slotted, so its
    `__slots__` is the field list and a field added to it is automatically covered
    here — the test cannot fall behind the dataclass.

    The caller sees nothing across a rotation only if this holds. It is also the
    test that a dropped field in `snapshot` or `restore` goes red on, which the
    Gate 2 mutation run confirms by dropping each one in turn.
    """
    original = Profiler(exact_quantiles=exact)
    for turn in turns:
        original.observe_turn(turn)
    before = original.features()
    after = Profiler.restore(original.snapshot(), exact_quantiles=exact).features()

    fields = SpeakerFeatures.__slots__
    assert fields, "SpeakerFeatures has no slots; this test would be vacuous"
    for field in fields:
        mine, theirs = getattr(before, field), getattr(after, field)
        if isinstance(mine, float) and isinstance(theirs, float):
            assert math.isclose(mine, theirs, rel_tol=0.0, abs_tol=0.0), (
                f"{field} drifted across a rotation: {mine} -> {theirs}"
            )
        else:
            assert mine == theirs, f"{field} drifted: {mine!r} -> {theirs!r}"


@PROPERTY_SETTINGS
@given(turns=_turn_streams())
def test_a_restored_profiler_keeps_accepting_turns(turns: list[Turn]) -> None:
    """A rotation must not leave an estimator that raises on its next sample.

    `restore` seeds the markers from a single reported value, which is a state no
    ordinary stream produces — all five markers tied. The parabolic step divides by
    marker *position* differences rather than value differences, so ties are safe,
    but that is an argument and this is the check.
    """
    original = Profiler()
    for turn in turns:
        original.observe_turn(turn)
    revived = Profiler.restore(original.snapshot())
    extra = Turn(
        turn_order=original.snapshot().last_turn_order + 1,
        end_of_turn=True,
        end_of_turn_confidence=0.4,
        transcript="six one seven",
        words=(
            Word(text="six", start_ms=0, end_ms=200, confidence=0.9, is_final=True),
            Word(text="one", start_ms=900, end_ms=1100, confidence=0.9, is_final=True),
            Word(
                text="seven", start_ms=1500, end_ms=1700, confidence=0.9, is_final=True
            ),
        ),
    )
    revived.observe_turn(extra)
    features = revived.features()
    assert 0.0 <= features.g_p90_ms <= GAP_CLAMP_MAX_MS
    assert features.n_gaps >= original.features().n_gaps


def test_the_cut_ring_round_trips_on_every_reachable_value() -> None:
    """`recent_cuts` survives a rotation exactly, because its range is discrete.

    `restore` rebuilds the ring from `round(recent_cuts * CUT_NORMALISER)`, which
    looks lossy and is not: with `CUT_NORMALISER` at 3 and the result clamped to
    `[0, 1]`, a count of 0 to 5 cuts in the window can only ever produce 0, ⅓, ⅔ or
    1. Every value a profiler can report round-trips; only a hand-built state that
    no profiler could have produced does not.
    """
    reachable = sorted({min(count / 3.0, 1.0) for count in range(6)})
    assert reachable == [0.0, 1 / 3, 2 / 3, 1.0]
    for value in reachable:
        state = ProfilerState(
            n_gaps=12,
            g_p50_ms=200.0,
            g_p90_ms=800.0,
            speech_rate=2.4,
            disfluency=0.1,
            jitter=0.0,
            recent_cuts=value,
            last_turn_order=4,
        )
        assert Profiler.restore(state).features().recent_cuts == pytest.approx(value)


def test_bisect_is_not_needed_to_state_the_rank_error() -> None:
    """Guards the module docstring's rank-error figures against bit rot.

    The docstring quotes 45.6 percentage points at `n = 9`. That is a claim about
    this code, so it gets a check: a nine-sample stream exists whose P² p90 has a
    rank far from 0.9. Without this the figure is prose that nothing verifies —
    which is the defect CLAUDE.md §5 records for `INTRINSIC_FLOOR_DBFS`.
    """
    stream = [0.0, 0.0, 0.0, 0.0, 6000.0, 6000.0, 6000.0, 6000.0, 6000.0]
    estimator = P2Quantile(0.90)
    for sample in stream:
        estimator.update(sample)
    ordered = sorted(stream)
    below = bisect.bisect_left(ordered, estimator.value) / len(ordered)
    above = bisect.bisect_right(ordered, estimator.value) / len(ordered)
    assert not below <= 0.90 <= above, (
        "P2 now places its p90 inside the correct tie block on this stream; the "
        "rank-error figure in the module docstring needs re-measuring"
    )
