"""PCR, TTL, FRAG and the run manifest, against hand-computed fixtures.

BENCH_SPEC §5. Every published number descends from these four, and a metric
computed wrongly still prints a plausible number — so each expected value below
is derived by hand in a comment and written in as a literal. If the arithmetic
in the comment and the assertion disagree, the test is wrong, not the reader.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from pydantic import ValidationError

from nod_bench.metrics import (
    LIVE_TAG,
    QUANTILE_METHOD,
    SIMULATED_TAG,
    ArmConfig,
    ClipObservation,
    MissingSilenceStartsError,
    ProxyDivergence,
    Quantiles,
    RunManifest,
    ScoredUtterance,
    artifact_name,
    certain_utterances,
    frag,
    pcr,
    quantile,
    total_utterances,
    ttl,
)
from nod_bench.perturb import Gap
from nod_bench.replay import RunResult

CERTAIN = Gap(
    start_ms=0,
    end_ms=1,
    origin="repeat",
    preceding="fragment",
    certainty="certain",
    basis="test fixture",
)
AMBIGUOUS = Gap(
    start_ms=0,
    end_ms=1,
    origin="pause",
    preceding="fragment",
    certainty="ambiguous",
    basis="test fixture",
)


def _clip(
    clip_id: str,
    utterances: tuple[ScoredUtterance, ...],
    emitted: tuple[float, ...],
    silence_starts: tuple[float, ...] = (),
) -> ClipObservation:
    return ClipObservation(
        clip_id=clip_id,
        arm="balanced",
        utterances=utterances,
        emitted_end_ms=emitted,
        emitted_silence_start_ms=silence_starts,
    )


def _gap(start: int, end: int, certainty: str) -> Gap:
    """A gap spanning a real interval, so a silence start can land inside it."""
    return Gap(
        start_ms=start,
        end_ms=end,
        origin="pause" if certainty == "ambiguous" else "repeat",
        preceding="fragment",
        certainty=certainty,  # type: ignore[arg-type]
        basis="test fixture",
    )


# ---------------------------------------------------------------------------
# Percentile method — stated, not inherited (BENCH_SPEC §5)
# ---------------------------------------------------------------------------


def test_the_quantile_method_is_nearest_rank_not_interpolation() -> None:
    """Nine samples, chosen because the two methods visibly disagree.

    samples sorted: 10 20 30 40 50 60 70 80 90   (n = 9)

    nearest-rank, inclusive:  rank = ceil(q * n), 1-indexed
        p50 -> ceil(0.50 * 9) = ceil(4.5)  = 5 -> sorted[4] = 50
        p90 -> ceil(0.90 * 9) = ceil(8.1)  = 9 -> sorted[8] = 90
        p99 -> ceil(0.99 * 9) = ceil(8.91) = 9 -> sorted[8] = 90

    linear interpolation (numpy default, Hyndman-Fan type 7):
        p90 -> index 0.90 * (9 - 1) = 7.2 -> 80 + 0.2 * (90 - 80) = 82
    """
    samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0]

    assert quantile(samples, 0.50) == 50.0
    assert quantile(samples, 0.90) == 90.0
    assert quantile(samples, 0.99) == 90.0

    # The choice is load-bearing: the library default would report 82.
    assert float(np.percentile(samples, 90)) == pytest.approx(82.0)
    assert quantile(samples, 0.90) != float(np.percentile(samples, 90))
    assert QUANTILE_METHOD == "nearest-rank, inclusive"


def test_every_quantile_is_an_observed_sample() -> None:
    """The property that motivates the choice: no invented latency."""
    samples = [3.0, 11.0, 12.0, 100.0, 250.0, 251.0, 900.0]
    for q in (0.0, 0.25, 0.5, 0.9, 0.99, 1.0):
        assert quantile(samples, q) in samples


def test_quantiles_of_nothing_are_refused() -> None:
    """Zero is a plausible answer and a wrong one."""
    with pytest.raises(ValueError, match="no samples"):
        quantile([], 0.5)


# ---------------------------------------------------------------------------
# PCR
# ---------------------------------------------------------------------------


def test_pcr_is_the_fraction_of_utterances_cut_early() -> None:
    """Four utterances, two cut before their final word ended.

    u0  final word ends 1000, turn fired at  900  ->  900 < 1000  premature
    u1  final word ends 1000, turn fired at 1200  -> 1200 > 1000  fine
    u2  final word ends 1000, turn fired at  999  ->  999 < 1000  premature
    u3  final word ends 1000, turn fired at 1000  -> not <        fine

    PCR = 2 / 4 = 0.5
    """
    clips = [
        _clip("a", (ScoredUtterance(start_ms=0, final_word_end_ms=1000),), (900.0,)),
        _clip("b", (ScoredUtterance(start_ms=0, final_word_end_ms=1000),), (1200.0,)),
        _clip("c", (ScoredUtterance(start_ms=0, final_word_end_ms=1000),), (999.0,)),
        _clip("d", (ScoredUtterance(start_ms=0, final_word_end_ms=1000),), (1000.0,)),
    ]
    assert pcr(clips) == 0.5


def test_pcr_counts_an_utterance_once_however_often_it_was_cut() -> None:
    """PCR is a rate over utterances, not over boundaries.

    one utterance, final word ends 1000, three turns fired at 200/400/600
    all three are premature, but the utterance is one utterance

    PCR = 1 / 1 = 1.0
    """
    clips = [
        _clip(
            "a",
            (ScoredUtterance(start_ms=0, final_word_end_ms=1000),),
            (200.0, 400.0, 600.0),
        )
    ]
    assert pcr(clips) == 1.0


def test_pcr_of_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="no utterances"):
        pcr([])


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def test_ttl_measures_from_true_utterance_end_and_keeps_negatives() -> None:
    """Five utterances, each ending at 1000, one turn each.

    fired 1300 -> 1300 - 1000 =  300
    fired 1100 -> 1100 - 1000 =  100
    fired  950 ->  950 - 1000 =  -50   negative, never clipped
    fired 1200 -> 1200 - 1000 =  200
    fired 2000 -> 2000 - 1000 = 1000

    sorted: -50 100 200 300 1000   (n = 5)
        p50 -> ceil(0.50 * 5) = 3 -> sorted[2] =  200
        p90 -> ceil(0.90 * 5) = 5 -> sorted[4] = 1000
        p99 -> ceil(0.99 * 5) = 5 -> sorted[4] = 1000
    """
    fired = (1300.0, 1100.0, 950.0, 1200.0, 2000.0)
    clips = [
        _clip(str(i), (ScoredUtterance(start_ms=0, final_word_end_ms=1000),), (f,))
        for i, f in enumerate(fired)
    ]
    assert ttl(clips) == Quantiles(p50=200.0, p90=1000.0, p99=1000.0)


def test_ttl_never_clips_a_negative_latency() -> None:
    """BENCH_SPEC §5: negatives are recorded, never clipped to zero.

    Chosen so clipping visibly moves a *reported* quantile. A fixture whose only
    negative is the minimum would not: p50/p90/p99 never touch it, and the
    clipping mutation survives.

    five utterances each ending at 1000
    fired  700 ->  -300      fired  800 ->  -200      fired  900 -> -100
    fired 1050 ->    50      fired 1100 ->   100

    sorted: -300 -200 -100 50 100   (n = 5)
        p50 -> ceil(0.50 * 5) = 3 -> sorted[2] = -100

    clipped to zero it would be 0 0 0 50 100, and p50 would read 0 — which is
    the cutoff being measured, reported as if it had not happened.
    """
    fired = (700.0, 800.0, 900.0, 1050.0, 1100.0)
    clips = [
        _clip(str(i), (ScoredUtterance(start_ms=0, final_word_end_ms=1000),), (f,))
        for i, f in enumerate(fired)
    ]
    assert ttl(clips).p50 == -100.0
    assert ttl(clips).p90 == 100.0


def test_ttl_uses_the_turn_that_actually_ended_the_utterance() -> None:
    """A fragmented utterance is not finished until its last turn.

    one utterance ending at 1000, turns at 600, 800 and 1400
    the caller had the floor back at 1400, so TTL = 1400 - 1000 = 400
    """
    clips = [
        _clip(
            "a",
            (ScoredUtterance(start_ms=0, final_word_end_ms=1000),),
            (600.0, 800.0, 1400.0),
        )
    ]
    assert ttl(clips).p50 == 400.0


def test_ttl_is_undefined_when_no_turn_ever_fired() -> None:
    clips = [_clip("a", (ScoredUtterance(start_ms=0, final_word_end_ms=1000),), ())]
    with pytest.raises(ValueError, match="undefined, not zero"):
        ttl(clips)


# ---------------------------------------------------------------------------
# FRAG
# ---------------------------------------------------------------------------


def test_frag_is_mean_emitted_turns_per_utterance() -> None:
    """Two utterances in one clip, attributed by which had started.

    u0 starts    0, final word 1000
    u1 starts 2000, final word 3000
    turns fired at 500, 900, 2500  ->  500 and 900 belong to u0 (u1 not started),
                                       2500 belongs to u1

    FRAG = (2 + 1) / 2 = 1.5
    """
    clips = [
        _clip(
            "a",
            (
                ScoredUtterance(start_ms=0, final_word_end_ms=1000),
                ScoredUtterance(start_ms=2000, final_word_end_ms=3000),
            ),
            (500.0, 900.0, 2500.0),
        )
    ]
    assert frag(clips) == 1.5


def test_frag_of_one_is_perfect_and_of_zero_is_a_dropped_turn() -> None:
    """One turn per utterance is 1.0; an utterance never ended contributes 0.

    u0 one turn, u1 no turn  ->  FRAG = (1 + 0) / 2 = 0.5
    """
    clips = [
        _clip(
            "a",
            (
                ScoredUtterance(start_ms=0, final_word_end_ms=1000),
                ScoredUtterance(start_ms=2000, final_word_end_ms=3000),
            ),
            (1100.0,),
        )
    ]
    assert frag(clips) == 0.5


def test_frag_of_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="no utterances"):
        frag([])


# ---------------------------------------------------------------------------
# The construction proxy: scoring all gaps vs certain gaps only
# ---------------------------------------------------------------------------


def test_certain_only_scoring_excludes_ambiguous_regime_utterances() -> None:
    """Three utterances; only one has an unambiguously labelled gap.

    u0 gap ambiguous, final word 1000, fired  900  -> premature
    u1 gap certain,   final word 1000, fired 1100  -> fine
    u2 gap ambiguous, final word 1000, fired  900  -> premature

    all gaps      PCR = 2 / 3 = 0.666...   FRAG = 3 / 3 = 1.0
    certain only  PCR = 0 / 1 = 0.0        FRAG = 1 / 1 = 1.0

    The divergence is the point: two thirds of the headline PCR here rests on
    gaps whose regime is a construction proxy, not a semantic fact.
    """
    clips = [
        _clip(
            "a",
            (
                ScoredUtterance(
                    start_ms=0,
                    final_word_end_ms=1000,
                    gaps=(_gap(800, 1000, "ambiguous"),),
                ),
            ),
            (900.0,),
            (850.0,),
        ),
        _clip(
            "b",
            (
                ScoredUtterance(
                    start_ms=0,
                    final_word_end_ms=1000,
                    gaps=(_gap(1000, 1200, "certain"),),
                ),
            ),
            (1100.0,),
            (1050.0,),
        ),
        _clip(
            "c",
            (
                ScoredUtterance(
                    start_ms=0,
                    final_word_end_ms=1000,
                    gaps=(_gap(800, 1000, "ambiguous"),),
                ),
            ),
            (900.0,),
            (850.0,),
        ),
    ]
    assert pcr(clips) == pytest.approx(2 / 3)
    assert pcr(clips, certain_only=True) == 0.0
    assert frag(clips) == 1.0
    assert frag(clips, certain_only=True) == 1.0
    assert certain_utterances(clips) == 1


def test_an_ambiguous_gap_that_governed_no_boundary_does_not_exclude() -> None:
    """The whole of ADR-036, in one fixture.

    An utterance with a long ambiguous pause early on and a certain gap at the
    end, where the only boundary fired in the certain one. The ambiguous label
    was never consulted for this verdict, so the utterance is in scope.

    **This is the assertion that goes red under the rule ADR-036 replaced.**
    Utterance-level certainty required *every* gap to be certain, so the
    ambiguous gap at 300-800 ms excluded this utterance and the certain-only
    scope was empty. Verified red by reverting `_selected` to
    `if not certain_only or all(g.certainty == "certain" for g in utterance.gaps)`.
    """
    clips = [
        _clip(
            "a",
            (
                ScoredUtterance(
                    start_ms=0,
                    final_word_end_ms=1000,
                    gaps=(
                        _gap(300, 800, "ambiguous"),
                        _gap(1000, 1400, "certain"),
                    ),
                ),
            ),
            (1200.0,),
            (1050.0,),
        )
    ]
    assert certain_utterances(clips) == 1
    assert pcr(clips, certain_only=True) == 0.0


def test_an_utterance_whose_boundary_no_gap_covers_is_not_certain() -> None:
    """An uncovered boundary took its regime from a fallback, not from a label.

    `regime_at` returns `complete` where no gap covers the silence. That is a
    rule the simulator applies, not a fact the sidecar recorded, so a verdict
    resting on it is not `certain`. Measured on Track A: 45 boundaries per arm
    land here, all within 14 ms of `final_word_end_ms` (ADR-034).
    """
    clips = [
        _clip(
            "a",
            (
                ScoredUtterance(
                    start_ms=0,
                    final_word_end_ms=1000,
                    gaps=(_gap(1000, 1400, "certain"),),
                ),
            ),
            (1100.0,),
            (960.0,),
        )
    ]
    assert certain_utterances(clips) == 0
    with pytest.raises(ValueError, match="no utterances"):
        pcr(clips, certain_only=True)


def test_an_utterance_that_emitted_nothing_is_certain() -> None:
    """No boundary means no label was relied upon, so nothing is in doubt."""
    clips = [
        _clip(
            "a",
            (
                ScoredUtterance(
                    start_ms=0,
                    final_word_end_ms=1000,
                    gaps=(_gap(300, 800, "ambiguous"),),
                ),
            ),
            (),
            (),
        )
    ]
    assert certain_utterances(clips) == 1
    assert pcr(clips, certain_only=True) == 0.0


def test_certainty_is_scoped_by_attribution_not_pooled_across_utterances() -> None:
    """A multi-utterance clip: each utterance is judged on *its own* boundaries.

    Two utterances in one clip. The first ends inside an ambiguous gap; the
    second ends inside a certain one. Correct attribution puts exactly the
    second in scope. Pooling every boundary into every utterance would exclude
    both, because each would see the other's ambiguous gap.

    This is the Track C shape — 10-12 utterances per clip (ADR-034) — and no
    single-utterance fixture can reach it, which is why `make mutate` reported
    "attribute every boundary to every utterance" as a survivor until this
    landed.
    """
    clips = [
        _clip(
            "two-turn",
            (
                ScoredUtterance(
                    start_ms=0,
                    final_word_end_ms=1000,
                    gaps=(_gap(1000, 1400, "ambiguous"),),
                ),
                ScoredUtterance(
                    start_ms=2000,
                    final_word_end_ms=3000,
                    gaps=(_gap(3000, 3400, "certain"),),
                ),
            ),
            (1200.0, 3200.0),
            (1050.0, 3050.0),
        )
    ]
    assert certain_utterances(clips) == 1
    assert pcr(clips, certain_only=True) == 0.0
    assert frag(clips, certain_only=True) == 1.0


def test_the_denominator_counts_utterances_not_observations() -> None:
    """`pcr` is a rate over utterances, so its denominator must be one too.

    One observation holding two utterances. `len(runs)` would say 1 and make
    `ProxyDivergence` report a rate over 2 against a denominator of 1 — the
    defect ADR-036 corrects. No single-utterance fixture can see this, which is
    why it is written against a two-utterance one.
    """
    clips = [
        _clip(
            "two-turn",
            (
                ScoredUtterance(start_ms=0, final_word_end_ms=1000),
                ScoredUtterance(start_ms=2000, final_word_end_ms=3000),
            ),
            (900.0, 3100.0),
            (850.0, 3050.0),
        )
    ]
    assert total_utterances(clips) == 2
    assert len(clips) == 1
    # The rate and its denominator now agree: one of the two fired early.
    assert pcr(clips) == pytest.approx(1 / 2)


def test_certain_only_with_nothing_certain_is_refused_not_zero() -> None:
    """An empty scope must not silently report a perfect score."""
    clips = [
        _clip(
            "a",
            (
                ScoredUtterance(
                    start_ms=0,
                    final_word_end_ms=1000,
                    gaps=(_gap(800, 1000, "ambiguous"),),
                ),
            ),
            (900.0,),
            (850.0,),
        )
    ]
    with pytest.raises(ValueError, match="no utterances"):
        pcr(clips, certain_only=True)


def test_certain_only_without_silence_starts_is_refused_not_guessed() -> None:
    """A missing input must not fall back to the definition ADR-036 replaced."""
    clips = [
        _clip(
            "a",
            (
                ScoredUtterance(
                    start_ms=0,
                    final_word_end_ms=1000,
                    gaps=(_gap(800, 1000, "ambiguous"),),
                ),
            ),
            (900.0,),
        )
    ]
    with pytest.raises(MissingSilenceStartsError, match="ADR-036"):
        pcr(clips, certain_only=True)


def test_silence_starts_must_pair_one_to_one_with_boundaries() -> None:
    """A half-filled parallel array would mis-attribute every gap after the gap."""
    with pytest.raises(ValidationError, match="silence starts"):
        ClipObservation(
            clip_id="a",
            arm="balanced",
            utterances=(ScoredUtterance(start_ms=0, final_word_end_ms=1000),),
            emitted_end_ms=(900.0, 1100.0),
            emitted_silence_start_ms=(850.0,),
        )


# ---------------------------------------------------------------------------
# Run manifest
# ---------------------------------------------------------------------------

ARMS = (
    ArmConfig(
        name="balanced",
        end_of_turn_confidence_threshold=0.4,
        min_turn_silence=400,
        max_turn_silence=1280,
        source="AssemblyAI turn-detection docs, accessed 2026-09-17",
    ),
)

DIVERGENCE = ProxyDivergence(
    pcr_all=0.5,
    pcr_certain_only=0.4,
    frag_all=1.2,
    frag_certain_only=1.1,
    utterances_all=100,
    utterances_certain_only=40,
)

MANIFEST_FIELDS: dict[str, object] = {
    "seed": 7,
    "generator_version": "0.1.0",
    "corpus_id": "trackA",
    "corpus_sha256": "abc123",
    "corpus_clips": 120,
    "repeats_per_arm": 5,
    "arms": ARMS,
    "simulated": True,
    "endpoint_overhead_ms": 0.0,
    "quantile_method": QUANTILE_METHOD,
    "proxy_divergence": DIVERGENCE,
}


def test_a_complete_manifest_is_accepted() -> None:
    manifest = RunManifest.model_validate(MANIFEST_FIELDS)
    assert manifest.simulated is True
    assert manifest.endpoint_overhead_ms == 0.0


@pytest.mark.parametrize("missing", sorted(MANIFEST_FIELDS))
def test_a_manifest_missing_any_field_fails_rather_than_defaulting(
    missing: str,
) -> None:
    """No field defaults. A missing record must not become a confident wrong one.

    `simulated` and `endpoint_overhead_ms` are the ones that matter most: a
    defaulted `simulated=False` would label a simulated chart as measured, and a
    defaulted overhead of 0 would silently assert it had been measured.
    """
    fields = {k: v for k, v in MANIFEST_FIELDS.items() if k != missing}
    with pytest.raises(ValidationError):
        RunManifest.model_validate(fields)


def test_a_manifest_rejects_fields_it_does_not_declare() -> None:
    with pytest.raises(ValidationError):
        RunManifest.model_validate({**MANIFEST_FIELDS, "undeclared": "smuggled"})


def test_an_arm_config_must_cite_its_source() -> None:
    """BENCH_SPEC §3 forbids a strawman, which an uncited config cannot disprove."""
    with pytest.raises(ValidationError):
        ArmConfig.model_validate(
            {
                "name": "balanced",
                "end_of_turn_confidence_threshold": 0.4,
                "min_turn_silence": 400,
                "max_turn_silence": 1280,
            }
        )


def test_the_manifest_records_the_quantile_method() -> None:
    """The choice moves every latency number, so it travels with the numbers."""
    manifest = RunManifest.model_validate(MANIFEST_FIELDS)
    assert manifest.quantile_method == "nearest-rank, inclusive"
    assert math.isclose(quantile([1.0, 2.0, 3.0], 0.9), 3.0)


# ---------------------------------------------------------------------------
# Structural provenance labelling (ADR-016, ADR-017)
# ---------------------------------------------------------------------------


def test_a_simulated_artifact_is_labelled_in_its_filename() -> None:
    """Prose in a caption does not survive a screenshot; a filename does."""
    assert artifact_name("pareto", simulated=True, suffix="svg") == (
        "pareto.simulated.svg"
    )
    assert artifact_name("results", simulated=True, suffix="json") == (
        "results.simulated.json"
    )


def test_a_live_artifact_says_so_too() -> None:
    """Absence of the word `simulated` must not be how live is indicated."""
    assert artifact_name("pareto", simulated=False, suffix="svg") == "pareto.live.svg"


@pytest.mark.parametrize("simulated", [True, False])
def test_every_artifact_name_carries_a_provenance_tag(simulated: bool) -> None:
    name = artifact_name("chart", simulated=simulated, suffix="png")
    assert SIMULATED_TAG in name or LIVE_TAG in name


def test_the_manifest_and_the_filename_cannot_disagree() -> None:
    """The same fact in both places, so neither can be read without the other."""
    for simulated in (True, False):
        fields = {**MANIFEST_FIELDS, "simulated": simulated}
        manifest = RunManifest.model_validate(fields)
        name = artifact_name("pareto", simulated=manifest.simulated, suffix="svg")
        assert (SIMULATED_TAG in name) is manifest.simulated


# --- the five that were stubs (Gate 4a, ADR-046) ---------------------------


def _run(
    arm: str = "nod",
    *,
    patches: tuple[int, ...],
    decide_ms: tuple[float, ...],
) -> RunResult:
    return RunResult(
        run_id="r",
        clip_id="c",
        arm=arm,  # type: ignore[arg-type]
        repeats=len(patches),
        trace_paths=(),
        patches=patches,
        decide_ms=decide_ms,
    )


def test_tct_refuses_rather_than_returning_the_clip_duration() -> None:
    """ADR-046: BENCH_SPEC §5 includes repeats caused by cuts; audio cannot repeat.

    The impossible-value question applied before publishing: what could TCT
    report here? Only the clip's duration plus overhead, identical on every arm.
    A number that is equal across arms by construction, printed in a column that
    invites comparison, is worse than no column.
    """
    from nod_bench.metrics import UnmeasurableOnReplayError, tct

    with pytest.raises(UnmeasurableOnReplayError, match="repeats caused by cuts"):
        tct([_run(patches=(0,), decide_ms=())])


def test_res_refuses_rather_than_reporting_a_structural_zero() -> None:
    """ADR-046: a recorded caller never recovers, so 0.0 is not a measurement.

    And 0.0 is the *flattering* value — it reads as the controller never forcing
    anyone to repeat themselves.
    """
    from nod_bench.metrics import UnmeasurableOnReplayError, res

    with pytest.raises(UnmeasurableOnReplayError, match="cannot react"):
        res([_run(patches=(0,), decide_ms=())])


def test_patch_count_is_the_mean_over_sessions_not_over_pairs() -> None:
    """ADR-027 wants per-session counts; a (clip, arm) pair holds N sessions."""
    from nod_bench.metrics import patch_count

    runs = [
        _run(patches=(2, 4, 6), decide_ms=()),
        _run(patches=(0, 0, 0), decide_ms=()),
    ]
    assert patch_count(runs) == pytest.approx(2.0)


def test_patch_max_reports_the_busiest_single_session() -> None:
    """ADR-027: the cap's reachability turns on the maximum, not the mean."""
    from nod_bench.metrics import patch_max

    assert patch_max([_run(patches=(2, 17, 3), decide_ms=())]) == 17


def test_patch_count_refuses_an_empty_scope() -> None:
    """The mean of no sessions is not 0.0, which reads as "never patched"."""
    from nod_bench.metrics import patch_count

    with pytest.raises(ValueError, match=r"not 0\.0"):
        patch_count([_run(patches=(), decide_ms=())])


def test_dec_p99_uses_the_module_quantile_definition() -> None:
    """Not numpy's interpolation: every percentile here is `QUANTILE_METHOD`."""
    from nod_bench.metrics import dec_p99, quantile

    samples = tuple(float(n) for n in range(1, 101))
    runs = [_run(patches=(0,), decide_ms=samples)]
    assert dec_p99(runs) == quantile(samples, 0.99)


def test_dec_p99_refuses_an_empty_scope() -> None:
    """INV-2 is not satisfied by an absent measurement reported as 0.0."""
    from nod_bench.metrics import dec_p99

    with pytest.raises(ValueError, match=r"not 0\.0"):
        dec_p99([_run(patches=(0,), decide_ms=())])


def test_wilcoxon_refuses_unpaired_samples() -> None:
    """BENCH_SPEC §9 pairs on the clip; a length mismatch means it did not."""
    from nod_bench.metrics import wilcoxon

    with pytest.raises(ValueError, match="paired samples"):
        wilcoxon(np.array([1.0, 2.0]), np.array([1.0]))


def test_wilcoxon_refuses_identical_samples() -> None:
    """ADR-019: against the simulator every pair is identical and the test is
    undefined — which is also the signal it should not have been run."""
    from nod_bench.metrics import wilcoxon

    same = np.array([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="all identical"):
        wilcoxon(same, same.copy())


def test_wilcoxon_reports_a_statistic_and_a_p_value() -> None:
    """The happy path, on samples with a real difference."""
    from nod_bench.metrics import wilcoxon

    a = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    b = a + 3.0
    statistic, p_value = wilcoxon(a, b)
    assert statistic >= 0.0
    assert 0.0 <= p_value <= 1.0
