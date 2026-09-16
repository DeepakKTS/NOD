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
    """BENCH_SPEC §4: spread wider than the difference is inconclusive."""
    noisy_low = (600.0, 1400.0, 900.0)
    noisy_high = (700.0, 1500.0, 1000.0)
    obs = four_cells(noisy_low, noisy_high, noisy_low, noisy_high)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1) is KnobVerdict.INERT
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
    mixed = (605.0, NO_BOUNDARY, 611.0)
    obs = four_cells(mixed, NEVER, mixed, NEVER)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1) is KnobVerdict.INERT
    )


def test_neither_arm_ever_ending_a_turn_is_inert() -> None:
    obs = four_cells(NEVER, NEVER, NEVER, NEVER)
    assert (
        verdict_for(obs, expected_shift_ms=SHIFT_MS, direction=1) is KnobVerdict.INERT
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
            ("max_turn_silence", live, SHIFT_MS, 1),
            ("min_turn_silence", static, SHIFT_MS, 1),
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
    assert "conf_axis" in disabled
    assert "jitter" in disabled
    assert "early_endpoint" in disabled
    assert "silence_axis" not in disabled


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


def test_degrade_falls_to_observe_when_no_silence_knob_is_live() -> None:
    """CONTROL_SPEC §0 fact 2: silence is the axis that actually endpoints."""
    caps = Capabilities(
        knobs=(
            ("max_turn_silence", KnobVerdict.STATIC_ONLY),
            ("min_turn_silence", KnobVerdict.INERT),
        ),
        confidence_field=ConfidenceField.VARYING,
        force_endpoint=KnobVerdict.LIVE,
        has_word_timings=True,
    )
    disabled = degrade(caps)
    assert "silence_axis" in disabled
    assert "observe_only" in disabled


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

    assert _separated([], [600.0], 100.0) is False
    assert _separated([600.0], [], 100.0) is False
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
