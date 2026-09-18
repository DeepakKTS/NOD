"""The capability classifier, which is where "supported" is actually decided.

ADR-001. The upstream API answers a successful `UpdateConfiguration` with
silence, so every verdict here has to come from measured turn-boundary movement.
Each verdict in the lattice gets a fixture, including `STATIC_ONLY` — the one
that would break the project's central claim, and therefore the one that must be
exercised long before it is ever needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from nod_core.capabilities import (
    NO_BOUNDARY,
    CellObservation,
    classify,
    confidence_field,
    degrade,
    verdict_for,
)
from nod_core.types import Capabilities, ConfidenceField, KnobVerdict

FIELD = "max_turn_silence"
SHIFT_MS = 2400.0  # the boundary should move 600ms -> 3000ms


def cell(
    name: str,
    boundaries: Sequence[float],
    *,
    error_code: int | None = None,
    confidences: Sequence[float] = (),
    on_partials: bool = False,
) -> list[CellObservation]:
    """Build one cell's repeats."""
    return [
        CellObservation(
            cell=name,
            field=FIELD,
            arm_value=600.0 if name.endswith("low") else 3000.0,
            boundary_ms=b,
            last_word_end_ms=4300,
            error_code=error_code,
            confidence_samples=tuple(confidences),
            confidence_on_partials=on_partials,
        )
        for b in boundaries
    ]


def four_cells(
    connect_low: Sequence[float],
    connect_high: Sequence[float],
    mid_low: Sequence[float],
    mid_high: Sequence[float],
) -> list[CellObservation]:
    return [
        *cell("connect_low", connect_low),
        *cell("connect_high", connect_high),
        *cell("mid_low", mid_low),
        *cell("mid_high", mid_high),
    ]


NEVER = (NO_BOUNDARY, NO_BOUNDARY, NO_BOUNDARY)
EARLY = (610.0, 598.0, 605.0)


def test_live_when_both_connect_and_midstream_move_the_boundary() -> None:
    obs = four_cells(EARLY, NEVER, (612.0, 601.0, 607.0), NEVER)
    assert verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1) is KnobVerdict.LIVE


def test_static_only_when_connect_works_but_midstream_does_not() -> None:
    """The verdict that breaks the thesis: the knob exists, the loop cannot use it."""
    obs = four_cells(EARLY, NEVER, NEVER, NEVER)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
        is KnobVerdict.STATIC_ONLY
    )


def test_inert_when_connect_time_changes_nothing() -> None:
    """Accepted at both ends and behaviourally dead. Rules out CONTROL_SPEC §0."""
    obs = four_cells(EARLY, EARLY, EARLY, EARLY)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1) is KnobVerdict.INERT
    )


def test_rejected_when_the_server_returned_an_error_frame() -> None:
    obs = [*cell("connect_low", EARLY), *cell("connect_high", (0.0,), error_code=4003)]
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
        is KnobVerdict.REJECTED
    )


def test_unproven_when_a_cell_was_never_measured() -> None:
    obs = cell("connect_low", EARLY)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
        is KnobVerdict.UNPROVEN
    )


def test_unproven_separation_within_noise_is_not_supported() -> None:
    """BENCH_SPEC §4: spread wider than the difference is inconclusive.

    UNPROVEN, not INERT (ADR-014). The medians move 100 ms in the predicted
    direction against a spread of 800, so this is a measurement that failed to
    resolve, not evidence the model ignores the knob. The name of this test said
    "unproven" while it asserted INERT.
    """
    noisy_low = (600.0, 1400.0, 900.0)
    noisy_high = (700.0, 1500.0, 1000.0)
    obs = four_cells(noisy_low, noisy_high, noisy_low, noisy_high)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
        is KnobVerdict.UNPROVEN
    )


def test_movement_in_the_wrong_direction_is_not_support() -> None:
    """A knob that moves the boundary backwards is not a working knob."""
    obs = four_cells(NEVER, EARLY, NEVER, EARLY)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1) is KnobVerdict.INERT
    )


def test_inverted_direction_is_honoured_for_vad_threshold() -> None:
    """A low `vad_threshold` calls the room tone speech, so the boundary moves later."""
    obs = four_cells(NEVER, EARLY, NEVER, EARLY)
    assert verdict_for(obs, expected_shift_ms=1500.0, direction=-1) is KnobVerdict.LIVE


def test_midstream_landing_in_the_wrong_place_is_not_live() -> None:
    """Separation alone is not enough: the arms must match their connect twins.

    Both mid-stream arms move apart here, but neither lands where the same value
    landed at connect time. Comparing the two mid arms only against each other
    would call this LIVE.
    """
    obs = four_cells(EARLY, NEVER, (3500.0, 3480.0, 3510.0), NEVER)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
        is KnobVerdict.STATIC_ONLY
    )


def test_an_arm_that_fires_inconsistently_is_not_proof() -> None:
    """UNPROVEN, not INERT (ADR-014).

    An arm that ends the turn on some repeats and not others is not
    reproducible. That is absence of usable data, which is the UNPROVEN case;
    INERT is a positive claim that the model ignores the field.
    """
    mixed = (605.0, NO_BOUNDARY, 611.0)
    obs = four_cells(mixed, NEVER, mixed, NEVER)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
        is KnobVerdict.UNPROVEN
    )


@pytest.mark.parametrize(
    ("confidences", "on_partials", "expected"),
    [
        ((), False, ConfidenceField.ABSENT),
        ((0.5, 0.5, 0.5), True, ConfidenceField.CONSTANT),
        ((0.4, 0.8), False, ConfidenceField.FINALS_ONLY),
        ((0.4, 0.8), True, ConfidenceField.VARYING),
    ],
)
def test_confidence_field_classification(
    confidences: Sequence[float], on_partials: bool, expected: ConfidenceField
) -> None:
    """Presence is the weakest question; a constant field is present and useless."""
    obs = cell("control", (600.0,), confidences=confidences, on_partials=on_partials)
    assert confidence_field(obs) is expected


def test_classify_builds_the_record_and_fails_closed() -> None:
    live = four_cells(EARLY, NEVER, (612.0,), NEVER)
    static = four_cells(EARLY, NEVER, NEVER, NEVER)
    caps = classify(
        [
            ("max_turn_silence", live, SHIFT_MS, 1, None),
            ("min_turn_silence", static, SHIFT_MS, 1, None),
        ],
        control=cell("control", (600.0,), confidences=(0.4, 0.8), on_partials=True),
        force_endpoint=KnobVerdict.UNPROVEN,
        has_word_timings=True,
    )
    assert caps.verdict("max_turn_silence") is KnobVerdict.LIVE
    assert caps.verdict("min_turn_silence") is KnobVerdict.STATIC_ONLY

    # STATIC_ONLY and an unmeasured knob both fail closed.
    assert caps.updatable_fields == frozenset({"max_turn_silence"})
    assert caps.verdict("vad_threshold") is KnobVerdict.UNPROVEN
    assert caps.supports_force_endpoint is False
    assert caps.has_end_of_turn_confidence is True


def test_constant_confidence_does_not_count_as_having_the_field() -> None:
    caps = Capabilities(
        knobs=(),
        confidence_field=ConfidenceField.CONSTANT,
        force_endpoint=KnobVerdict.LIVE,
        has_word_timings=True,
    )
    assert caps.has_end_of_turn_confidence is False


def test_degrade_disables_the_confidence_axis_on_a_punctuation_model() -> None:
    """CONTROL_SPEC §7 row 1, and the reason ADR-001 exists."""
    caps = Capabilities(
        knobs=(
            ("max_turn_silence", KnobVerdict.LIVE),
            ("min_turn_silence", KnobVerdict.LIVE),
            ("end_of_turn_confidence_threshold", KnobVerdict.INERT),
        ),
        confidence_field=ConfidenceField.ABSENT,
        force_endpoint=KnobVerdict.INERT,
        has_word_timings=True,
    )
    disabled = degrade(caps)
    # jitter is unavailable, but it was already weight 0, so no output changes.
    assert "jitter" in disabled
    assert "early_endpoint" in disabled
    # The silence knobs are live, so the law still has its primary surface.
    assert "max_axis" not in disabled
    assert "observe_only" not in disabled


def test_degrade_is_empty_when_everything_is_live() -> None:
    caps = Capabilities(
        knobs=(
            ("max_turn_silence", KnobVerdict.LIVE),
            ("min_turn_silence", KnobVerdict.LIVE),
            ("end_of_turn_confidence_threshold", KnobVerdict.LIVE),
            ("vad_threshold", KnobVerdict.LIVE),
        ),
        confidence_field=ConfidenceField.VARYING,
        force_endpoint=KnobVerdict.LIVE,
        has_word_timings=True,
    )
    assert degrade(caps) == frozenset()


def test_degrade_falls_to_observe_without_the_primary_surface() -> None:
    """ADR-011: `max_turn_silence` governs the incomplete-utterance regime.

    Losing it means the mid-sentence pause — the case Nod exists for — cannot be
    controlled at all, so the session drops to observe rather than pretending the
    remaining knob covers it.
    """
    caps = Capabilities(
        knobs=(
            ("max_turn_silence", KnobVerdict.STATIC_ONLY),
            ("min_turn_silence", KnobVerdict.LIVE),
        ),
        confidence_field=ConfidenceField.VARYING,
        force_endpoint=KnobVerdict.LIVE,
        has_word_timings=True,
    )
    disabled = degrade(caps)
    assert "max_axis" in disabled
    assert "observe_only" in disabled


def test_losing_min_turn_silence_costs_only_the_context_axis() -> None:
    caps = Capabilities(
        knobs=(
            ("max_turn_silence", KnobVerdict.LIVE),
            ("min_turn_silence", KnobVerdict.INERT),
        ),
        confidence_field=ConfidenceField.VARYING,
        force_endpoint=KnobVerdict.LIVE,
        has_word_timings=True,
    )
    disabled = degrade(caps)
    assert "context_axis" in disabled
    assert "observe_only" not in disabled


def test_degrade_disables_the_profiler_without_word_timings() -> None:
    caps = Capabilities(
        knobs=(("max_turn_silence", KnobVerdict.LIVE),),
        confidence_field=ConfidenceField.VARYING,
        force_endpoint=KnobVerdict.LIVE,
        has_word_timings=False,
    )
    disabled = degrade(caps)
    assert "profiler" in disabled
    assert "speaker_axis" in disabled


def test_median_of_an_even_number_of_repeats() -> None:
    """Not hypothetical: the secondary-model arm runs N=2."""
    from nod_core.capabilities import _median

    assert _median([600.0, 700.0]) == 650.0
    assert _median([100.0, 200.0, 300.0, 500.0]) == 250.0


def test_iqr_uses_quartiles_once_there_are_four_samples() -> None:
    from nod_core.capabilities import _iqr

    assert _iqr([1.0, 2.0]) == 1.0  # range, below four samples
    assert _iqr([0.0, 10.0, 20.0, 30.0]) == 20.0


def test_separation_and_agreement_need_both_arms_present() -> None:
    from nod_core.capabilities import _agrees, _separated

    assert _separated([], [600.0]) is False
    assert _separated([600.0], []) is False
    assert _agrees([], [600.0], 100.0) is False
    assert _agrees([600.0], [], 100.0) is False


def test_direction_when_neither_arm_ends_a_turn() -> None:
    from nod_core.capabilities import _directed

    assert _directed([NO_BOUNDARY], [NO_BOUNDARY], 1) is False


def test_direction_with_an_inverted_knob_and_a_never_ending_low_arm() -> None:
    """The `vad_threshold` shape: low arm never ends, high arm does."""
    from nod_core.capabilities import _directed

    assert _directed([NO_BOUNDARY], [800.0], -1) is True
    assert _directed([NO_BOUNDARY], [800.0], 1) is False


def test_agreement_requires_both_or_neither_to_have_ended() -> None:
    from nod_core.capabilities import _agrees

    assert _agrees([NO_BOUNDARY], [NO_BOUNDARY], 100.0) is True
    assert _agrees([NO_BOUNDARY], [600.0], 100.0) is False
    assert _agrees([600.0], [NO_BOUNDARY], 100.0) is False


def test_midstream_cells_missing_entirely_is_not_live() -> None:
    """Connect-time worked but mid-stream was never run: not proof of a loop."""
    obs = [*cell("connect_low", EARLY), *cell("connect_high", NEVER)]
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
        is KnobVerdict.STATIC_ONLY
    )


def test_tolerances_are_milliseconds_not_the_knobs_own_units() -> None:
    """Regression: a dimensionless knob must not get a sub-millisecond tolerance.

    `end_of_turn_confidence_threshold` moves between 0.20 and 0.95, so scaling a
    millisecond tolerance by the *arm delta* (0.75) yields 0.3 ms — tighter than
    any real measurement, and a working knob is reported STATIC_ONLY. The
    tolerance must scale the expected boundary shift in milliseconds instead.

    The mid-stream arm here lands 7 ms from its connect-time twin, which is
    ordinary run-to-run jitter and must still read as agreement.
    """
    obs = four_cells(
        connect_low=(118.0, 121.0, 119.0),
        connect_high=NEVER,
        mid_low=(125.0, 128.0, 126.0),
        mid_high=NEVER,
    )
    # The gap is 1500 ms, so that is the largest shift the stimulus can show.
    assert verdict_for(obs, expected_shift_ms=1500.0, direction=1) is KnobVerdict.LIVE
    # Scaling by the dimensionless arm delta is what produced the bug.
    assert (
        verdict_for(obs, expected_shift_ms=0.75, direction=1) is KnobVerdict.STATIC_ONLY
    )


def test_a_midstream_arm_cannot_agree_with_the_opposite_connect_arm() -> None:
    """AGREEMENT_FRACTION stays below 0.5 for this reason.

    If the tolerance reached half the expected shift, a mid-stream arm sitting
    where the *other* value landed would pass the agreement check, and a knob
    that moved the boundary to exactly the wrong place would read as LIVE.
    """
    from nod_core.capabilities import AGREEMENT_FRACTION, _agrees

    assert AGREEMENT_FRACTION < 0.5
    # connect_low at 100 ms, connect_high at 1600 ms: expected shift 1500 ms.
    assert _agrees([1600.0], [100.0], 1500.0) is False


def test_neither_arm_producing_a_boundary_is_unproven_not_inert() -> None:
    """Absence of data is not evidence of absence of effect.

    This is the shape the first live run produced for `min_turn_silence` and
    `end_of_turn_confidence_threshold`: `max_turn_silence` was pinned beyond the
    gap, which removed the only mechanism that ends a turn on that model, so no
    cell ever fired. Reporting INERT would have stated a property of the model
    on the strength of an experiment that never ran.
    """
    obs = four_cells(NEVER, NEVER, NEVER, NEVER)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
        is KnobVerdict.UNPROVEN
    )


def test_inert_still_requires_boundaries_that_failed_to_move() -> None:
    """INERT remains available, but only when there is data behind it."""
    obs = four_cells(EARLY, EARLY, EARLY, EARLY)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1) is KnobVerdict.INERT
    )


def test_one_arm_silent_and_one_firing_is_still_a_real_separation() -> None:
    """The categorical case must survive the new guard."""
    obs = four_cells(EARLY, NEVER, EARLY, NEVER)
    assert verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1) is KnobVerdict.LIVE


def test_a_continuous_separation_must_clear_both_the_noise_and_the_floor() -> None:
    """ADR-014: conjunctive. Clearing one gate is not enough.

    80 ms apart on very tight repeats clears `2 x IQR` easily and still fails the
    100 ms floor, because a shift that small is inside the unmodelled endpoint
    overhead of the system the knob is meant to steer.
    """
    from nod_core.capabilities import MIN_SEPARATION_MS

    tight_low = (600.0, 602.0, 601.0)
    tight_high = (680.0, 682.0, 681.0)
    assert MIN_SEPARATION_MS > 80.0
    obs = four_cells(tight_low, tight_high, tight_low, tight_high)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
        is KnobVerdict.UNPROVEN
    )


def test_the_vad_threshold_shape_resolves_live_on_the_continuous_path() -> None:
    """ADR-014, from `data/traces-p0-final`.

    The measured `vad_threshold` cells: 404 ms apart at connect and 415 mid,
    worst arm spread 42 ms, inverted direction, mid landing within 7 and 18 ms of
    its connect twin. The old `0.6 * expected_shift_ms` gate demanded 480 ms and
    called this INERT.
    """
    connect_low = (1371.0, 1371.0, 1349.0)
    connect_high = (967.0, 988.0, 946.0)
    mid_low = (1374.0, 1346.0, 1364.0)
    mid_high = (951.0, 929.0, 949.0)
    obs = four_cells(connect_low, connect_high, mid_low, mid_high)
    assert verdict_for(obs, expected_shift_ms=800.0, direction=-1) is KnobVerdict.LIVE


def test_the_categorical_path_applies_no_floor() -> None:
    """ADR-014 path 1: one arm never ends the turn, the other always does.

    Separated by construction. The floor is a statistic about two distributions
    and there is only one here, so applying it would be meaningless — and would
    reject `max_turn_silence`, whose whole stimulus is this shape.
    """
    obs = four_cells(EARLY, NEVER, EARLY, NEVER)
    assert verdict_for(obs, expected_shift_ms=1.0, direction=1) is KnobVerdict.LIVE


def test_a_missed_floor_is_never_reported_as_inert() -> None:
    """The distinction ADR-014 exists to protect.

    INERT is a positive claim that the model ignores the field. Movement in the
    predicted direction that is merely too small to act on does not license it.
    """
    low = (600.0, 601.0, 602.0)
    high = (640.0, 641.0, 642.0)
    obs = four_cells(low, high, low, high)
    verdict = verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1)
    assert verdict is not KnobVerdict.INERT
    assert verdict is KnobVerdict.UNPROVEN


def test_boundaries_that_do_not_move_are_still_inert() -> None:
    """ADR-014 narrowed INERT; it did not remove it.

    `end_of_turn_confidence_threshold` on `universal-streaming-english`: arms at
    the documented endpoints landed 6 ms apart, the high arm *earlier* than the
    low one, against a documented 2800 ms (`data/traces-p0-final`).
    """
    connect_low = (353.0, 378.0, 353.0)
    connect_high = (347.0, 357.0, 342.0)
    obs = four_cells(connect_low, connect_high, connect_low, connect_high)
    assert verdict_for(obs, expected_shift_ms=2800.0, direction=1) is KnobVerdict.INERT


def test_a_knob_that_never_bound_is_unproven_not_inert() -> None:
    """ADR-001's `universal-3-5-pro` rows, from `data/traces-p0-final`.

    Both `min_turn_silence` arms (100 and 2000 ms) produced boundaries at 3431
    and 3364 ms with `max_turn_silence` pinned at 3000. The minimum never bound,
    so the 67 ms between the arms is noise around a boundary another gate set —
    not the knob moving backwards. Calling that INERT asserts the model ignores
    a field, on an experiment that never ran.
    """
    low = (3641.0, 3221.0)
    high = (3414.0, 3314.0)
    obs = four_cells(low, high, low, high)

    assert (
        verdict_for(obs, expected_shift_ms=1900.0, direction=1) is KnobVerdict.INERT
    ), "without the pinned gate the classifier cannot know, and says INERT"
    assert (
        verdict_for(obs, expected_shift_ms=1900.0, direction=1, other_gate_ms=3000.0)
        is KnobVerdict.UNPROVEN
    )


def test_a_knob_that_did_bind_is_still_judged_on_its_own_merits() -> None:
    """The rule must not launder a real INERT into UNPROVEN.

    `end_of_turn_confidence_threshold` on `universal-streaming-english`: arms at
    the documented endpoints landed 353 and 347 ms with `max_turn_silence`
    pinned at 3000. Nothing reached that gate, so it explains nothing, and the
    verdict stays INERT.
    """
    low = (353.0, 378.0, 353.0)
    high = (347.0, 357.0, 342.0)
    obs = four_cells(low, high, low, high)
    assert (
        verdict_for(obs, expected_shift_ms=2800.0, direction=1, other_gate_ms=3000.0)
        is KnobVerdict.INERT
    )


def test_a_live_knob_is_untouched_by_the_rule() -> None:
    """`vad_threshold` pins `max_turn_silence` at 800 and its boundaries exceed it.

    Those boundaries are past the pinned gate, but the arms are properly
    separated and correctly directed, so the direction branch is never reached
    and the rule never applies.
    """
    obs = four_cells(
        (1371.0, 1371.0, 1349.0),
        (967.0, 988.0, 946.0),
        (1374.0, 1346.0, 1364.0),
        (951.0, 929.0, 949.0),
    )
    assert (
        verdict_for(obs, expected_shift_ms=800.0, direction=-1, other_gate_ms=800.0)
        is KnobVerdict.LIVE
    )
