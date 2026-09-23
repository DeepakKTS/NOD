"""The regime ladder's geometry, its predictions and its refusals.

The ladder produces the project's headline finding, so the tests here are
written against the *requirement* in each case and never against the constant
that implements it (CLAUDE.md §5). Two of them exist specifically because the
quantity they guard is one the eye cannot check: an acoustic anchor that is
32 ms from the file-length answer, and a hold a human read as "two seconds".
"""

from __future__ import annotations

import numpy as np
import pytest

from nod_bench.corpus import INTRINSIC_FLOOR_DBFS
from nod_bench.ladder import (
    MEASURED_OVERHEAD_MS,
    PREDICTION_TOLERANCE_MS,
    SILENCE_FLOOR_DBFS,
    TAIL_SILENCE_MS,
    Hypothesis,
    LadderGeometry,
    LadderRow,
    ObservedBoundary,
    Prediction,
    TakeSegmentationError,
    build_from_halves,
    envelope_of,
    hold_invariance_ms,
    predict,
    scores,
    segment_take,
    speech_runs,
    timeline_svg,
)
from nod_bench.replay import STATIC_ARMS

RATE = 16_000


def tone(ms: int, *, amplitude: float = 0.3) -> np.ndarray:
    """`ms` of audible signal, well above the floor."""
    n = round(ms * RATE / 1000.0)
    t = np.arange(n) / RATE
    return amplitude * np.sin(2 * np.pi * 220.0 * t)


def hush(ms: int, *, amplitude: float = 0.0) -> np.ndarray:
    """`ms` of silence, or of sub-threshold noise when given an amplitude."""
    n = round(ms * RATE / 1000.0)
    if amplitude == 0.0:
        return np.zeros(n)
    rng = np.random.default_rng(7)
    return amplitude * rng.standard_normal(n)


def test_ladder_floor_matches_the_corpus_floor() -> None:
    """The two silence floors must agree, and agree *in code*.

    `corpus.INTRINSIC_FLOOR_DBFS` and `ladder.SILENCE_FLOOR_DBFS` both mean
    "the level below which this project calls audio silent", and CLAUDE.md §5
    records what happens when two such constants agree only in prose: the
    agreement is a coincidence the codebase is enjoying, not a fact it knows.
    Asserted rather than derived, deliberately — deriving would make them agree
    forever, and a future change to one of them should stop and make someone
    decide rather than silently re-calibrate the other.
    """
    assert SILENCE_FLOOR_DBFS == INTRINSIC_FLOOR_DBFS


def test_ladder_tail_outlasts_the_widest_arm_gate() -> None:
    """The tail must outlast `conservative`, measured against the arm.

    Asserting `TAIL_SILENCE_MS == 4030` would pass for any value of
    `TAIL_SILENCE_MS`, which is the defect §5 names. The requirement is that
    the widest arm's end-of-clip boundary lands *inside* the file.
    """
    widest = STATIC_ARMS["conservative"].max_turn_silence
    assert widest + MEASURED_OVERHEAD_MS < TAIL_SILENCE_MS


def test_speech_runs_ignores_a_sub_threshold_tail() -> None:
    """Audio below the floor is silence, however long it runs."""
    samples = np.concatenate([tone(300), hush(200, amplitude=0.0005), tone(300)])
    runs = speech_runs(samples, RATE)
    assert len(runs) == 2
    assert runs[0].end_ms == 300
    assert runs[1].start_ms == 500


def test_build_from_halves_anchors_the_hold_to_the_acoustic_end() -> None:
    """The hold starts where the audio goes quiet, not where the file ends.

    The prefix here carries 200 ms of sub-threshold tail after 300 ms of
    speech. Anchoring to the file length would report the hold starting at
    500 ms and running 1000 ms; the service's VAD started counting at 300 ms
    and heard 1200 ms. Every prediction in the run is offset by that
    difference, in the direction that makes the service look slower than it is.

    The fixture's two numbers differ on purpose: an equal-length tail and hold
    would let a file-length anchor produce the right answer by accident.
    """
    prefix = np.concatenate([tone(300), hush(200, amplitude=0.0005)])
    audio, geo = build_from_halves(prefix, tone(400), RATE, hold_ms=1000)

    assert geo.prefix_end_ms == 300
    assert geo.hold_measured_ms == 1200
    assert geo.continuation_start_ms == 1500
    assert geo.continuation_end_ms == 1900
    assert geo.total_ms == 1900 + TAIL_SILENCE_MS
    assert len(audio) == round(geo.total_ms * RATE / 1000.0)


def test_segment_take_measures_the_hold_it_found_not_the_one_asked_for() -> None:
    """A human reading "two seconds" does not deliver 2000 ms.

    The take below is read against a 2000 ms label and actually holds 1700 ms.
    Scoring the firing time against 2000 would compare it to a pause that never
    happened, so the measured value is what travels and the label is only a
    name. The two differ here by more than the prediction tolerance, so a
    regression to using the label cannot pass this inside the noise.
    """
    take = np.concatenate([tone(400), hush(1700), tone(500)])
    (built,) = segment_take(take, RATE, (2000,))
    _, geo = built

    assert geo.hold_label_ms == 2000
    assert geo.hold_measured_ms == 1700
    assert abs(geo.hold_measured_ms - geo.hold_label_ms) > PREDICTION_TOLERANCE_MS


def test_segment_take_refuses_a_take_with_the_wrong_run_count() -> None:
    """A take that is not four clean pairs is an error, not a guess.

    Salvaging it means deciding which silence was the hold, and the hold is the
    one quantity the ladder measures. INV-8's fail-loud direction.
    """
    take = np.concatenate([tone(400), hush(1000), tone(400), hush(1000), tone(400)])
    with pytest.raises(TakeSegmentationError, match="expected 4 speech runs"):
        segment_take(take, RATE, (500, 1000))


@pytest.mark.parametrize("hypothesis", ["min_gate", "max_gate", "confidence"])
def test_every_hypothesis_names_a_time_on_every_row(hypothesis: Hypothesis) -> None:
    """No hypothesis is allowed to decline to predict.

    A hypothesis that says "no boundary in the hold" and stops cannot be
    scored against a row where a boundary *did* land late; it has to say where
    the boundary goes instead, or it is unfalsifiable on that row.
    """
    geo = LadderGeometry(
        hold_label_ms=500,
        hold_measured_ms=500,
        prefix_end_ms=2240,
        continuation_start_ms=2740,
        continuation_end_ms=4000,
        total_ms=4000 + TAIL_SILENCE_MS,
    )
    p = predict(
        geo,
        "balanced",
        400,
        1280,
        hypothesis,
        confidence_ms=590.0,
    )
    assert p.fired_at_ms > geo.prefix_end_ms


def test_aggressive_is_the_arm_that_separates_min_gate_from_confidence() -> None:
    """The two readings must be distinguishable by the run, on some arm.

    `conservative`'s 800 ms min gate is longer than the 590 ms confidence time,
    so both hypotheses predict the same firing there and that arm can never
    tell them apart. `aggressive`'s 160 ms gate is shorter, so the two separate
    by more than the tolerance — which is what makes the ladder able to decide
    between them at all. If this ever stops holding, the run has lost its
    discriminating row and the result means less than it appears to.
    """
    geo = LadderGeometry(
        hold_label_ms=2000,
        hold_measured_ms=2000,
        prefix_end_ms=2240,
        continuation_start_ms=4240,
        continuation_end_ms=5500,
        total_ms=5500 + TAIL_SILENCE_MS,
    )
    agg = STATIC_ARMS["aggressive"]
    con = STATIC_ARMS["conservative"]

    a_min = predict(
        geo,
        "aggressive",
        agg.min_turn_silence,
        agg.max_turn_silence,
        "min_gate",
        confidence_ms=590.0,
    )
    a_conf = predict(
        geo,
        "aggressive",
        agg.min_turn_silence,
        agg.max_turn_silence,
        "confidence",
        confidence_ms=590.0,
    )
    c_min = predict(
        geo,
        "conservative",
        con.min_turn_silence,
        con.max_turn_silence,
        "min_gate",
        confidence_ms=590.0,
    )
    c_conf = predict(
        geo,
        "conservative",
        con.min_turn_silence,
        con.max_turn_silence,
        "confidence",
        confidence_ms=590.0,
    )

    assert abs(a_min.fired_at_ms - a_conf.fired_at_ms) > 2 * PREDICTION_TOLERANCE_MS
    assert c_min.fired_at_ms == c_conf.fired_at_ms


def _row(arm: str, hold: int, fire_at: float | None) -> LadderRow:
    """One row with a single in-hold boundary at `fire_at`, or none."""
    bounds = (
        ()
        if fire_at is None
        else (
            ObservedBoundary(
                fired_at_ms=fire_at,
                silence_started_ms=fire_at - 300.0,
                turn_order=0,
                word_count=8,
                text="",
            ),
        )
    )
    return LadderRow(
        arm=arm,
        hold_label_ms=hold,
        hold_measured_ms=hold,
        prefix_end_ms=2240,
        boundaries=bounds,
        flush_turns=0,
    )


def test_hold_invariance_is_small_when_the_min_gate_governs() -> None:
    """Firing at a fixed silence gives a spread inside the tolerance."""
    rows = (
        _row("balanced", 1000, 3008.0),
        _row("balanced", 2000, 2980.0),
        _row("balanced", 3500, 3010.0),
    )
    spread = hold_invariance_ms(rows)
    assert spread is not None
    assert spread <= PREDICTION_TOLERANCE_MS


def test_hold_invariance_is_large_when_firing_tracks_the_hold() -> None:
    """The PASS signature: the service waits longer when the pause is longer.

    Constructed as a service that waits out most of each hold. The statistic
    has to separate this from the case above, or ADR-054's decisive observation
    is not decided by anything.
    """
    rows = (
        _row("balanced", 1000, 2240.0 + 900.0),
        _row("balanced", 2000, 2240.0 + 1900.0),
        _row("balanced", 3500, 2240.0 + 3400.0),
    )
    spread = hold_invariance_ms(rows)
    assert spread is not None
    assert spread > PREDICTION_TOLERANCE_MS


def test_hold_invariance_declines_to_answer_from_one_row() -> None:
    """One in-hold boundary is not a spread, and must not report as zero.

    A zero spread reads as "invariant, the max gate did not bind", which is a
    conclusion. From a single row it would be an absent instrument reporting a
    result — the ADR-050 shape.
    """
    assert hold_invariance_ms((_row("balanced", 1000, 3008.0),)) is None
    assert hold_invariance_ms((_row("balanced", 1000, None),)) is None


def test_a_boundary_outside_the_hold_is_not_an_in_hold_boundary() -> None:
    """The end-of-clip boundary must not be read as a mid-pause one.

    Every row has a boundary after the continuation, on every arm. Counting it
    would make `fires_in_hold` true everywhere and destroy the only field that
    distinguishes the three hypotheses.
    """
    row = LadderRow(
        arm="conservative",
        hold_label_ms=500,
        hold_measured_ms=500,
        prefix_end_ms=2240,
        boundaries=(
            ObservedBoundary(
                fired_at_ms=5010.0,
                silence_started_ms=4700.0,
                turn_order=0,
                word_count=12,
                text="",
            ),
        ),
        flush_turns=0,
    )
    assert row.in_hold is None
    assert row.silence_at_fire_ms is None


def _prediction(*, in_hold: bool, at: float) -> Prediction:
    """One prediction, for scoring."""
    return Prediction(
        arm="balanced",
        hold_label_ms=2000,
        hypothesis="confidence",
        fires_in_hold=in_hold,
        fired_at_ms=at,
    )


def test_scores_rejects_a_right_time_with_the_wrong_in_hold_call() -> None:
    """Getting the time right by accident is not getting the row right.

    The prediction below names 5010 ms and says it lands inside the hold; the
    observation has a boundary at 5010 ms that lands *after* the continuation.
    The number agrees to the millisecond and the claim is still false, because
    the in-hold question is the one that separates the hypotheses. A scorer
    that compared times first would award this, and `max_gate` — the reading
    that would overturn ADR-054 — is the hypothesis that benefits.
    """
    row = LadderRow(
        arm="balanced",
        hold_label_ms=2000,
        hold_measured_ms=500,
        prefix_end_ms=2240,
        boundaries=(
            ObservedBoundary(
                fired_at_ms=5010.0,
                silence_started_ms=4700.0,
                turn_order=0,
                word_count=12,
                text="",
            ),
        ),
        flush_turns=0,
    )
    assert scores(row, _prediction(in_hold=True, at=5010.0)) is False
    assert scores(row, _prediction(in_hold=False, at=5010.0)) is True


def test_scores_needs_the_time_when_both_agree_a_boundary_landed() -> None:
    """Agreeing that something fired is not agreeing on when."""
    row = _row("balanced", 2000, 2990.0)
    assert scores(row, _prediction(in_hold=True, at=2990.0)) is True
    assert scores(row, _prediction(in_hold=True, at=2560.0)) is False


def _whole(arm: str) -> LadderRow:
    """`balanced` on the 500 ms row: one turn, ending only at end of stream."""
    return LadderRow(
        arm=arm,
        hold_label_ms=500,
        hold_measured_ms=532,
        prefix_end_ms=2240,
        boundaries=(
            ObservedBoundary(
                fired_at_ms=5406.0,
                silence_started_ms=3840.0,
                turn_order=0,
                word_count=10,
                text="",
            ),
        ),
        flush_turns=0,
    )


def test_the_timeline_draws_its_provenance_and_its_turn_counts_inside_the_image() -> (
    None
):
    """A chart leaves the repository as an image; a caption does not (ADR-016).

    This SVG is the demo video's cold open, so it is the most likely artifact
    in the project to be screenshotted away from its markdown. Two things have
    to survive that: which run produced it, and the turn count per row — "two
    turns versus one turn" is the entire claim the picture makes, and a viewer
    should not have to count tick marks to check it.
    """
    rows = (_whole("aggressive"), _whole("balanced"))
    svg = timeline_svg(
        rows, (0.5, 1.0, 0.2), 8079.0, caption="ladder_say_500.wav - run X"
    )
    assert "ladder_say_500.wav - run X" in svg
    assert "aggressive" in svg and "balanced" in svg
    assert svg.count("1 turn<") == 2, "each row carries its own count"
    assert "1 turns" not in svg, "the singular must not read '1 turns'"


def test_the_timeline_counts_turns_per_row_and_does_not_share_one_count() -> None:
    """Two arms with different turn counts must render different numbers.

    The failure this guards is a loop that computes the count once outside it
    and draws it on every row — which would put `aggressive`'s split on
    `balanced` and destroy the comparison the video is built on. The fixture
    uses 2 against 1 so a shared count cannot be right for both.
    """
    split = LadderRow(
        arm="aggressive",
        hold_label_ms=500,
        hold_measured_ms=532,
        prefix_end_ms=2240,
        boundaries=(
            ObservedBoundary(
                fired_at_ms=2855.0,
                silence_started_ms=1920.0,
                turn_order=0,
                word_count=6,
                text="",
            ),
            ObservedBoundary(
                fired_at_ms=4587.0,
                silence_started_ms=3840.0,
                turn_order=1,
                word_count=4,
                text="",
            ),
        ),
        flush_turns=0,
    )
    whole = _whole("balanced")
    svg = timeline_svg((split, whole), (1.0,), 8079.0, caption="c")
    assert "2 turns" in svg
    assert "1 turn<" in svg


def test_the_envelope_normalises_to_its_own_peak() -> None:
    """A quiet clip and a loud one must draw the same height.

    Drawing absolute amplitude would make the waveform a picture of the
    recording level rather than of where the speech is, and the hold is read
    off exactly that contrast.
    """
    loud = envelope_of(np.concatenate([tone(200), hush(200), tone(200)]))
    quiet = envelope_of(
        np.concatenate(
            [tone(200, amplitude=0.02), hush(200), tone(200, amplitude=0.02)]
        )
    )
    assert max(loud) == pytest.approx(1.0)
    assert max(quiet) == pytest.approx(1.0)
