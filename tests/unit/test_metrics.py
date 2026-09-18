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
    ProxyDivergence,
    Quantiles,
    RunManifest,
    ScoredUtterance,
    artifact_name,
    frag,
    pcr,
    quantile,
    ttl,
)
from nod_bench.perturb import Gap

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
) -> ClipObservation:
    return ClipObservation(
        clip_id=clip_id, arm="balanced", utterances=utterances, emitted_end_ms=emitted
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
            (ScoredUtterance(start_ms=0, final_word_end_ms=1000, gaps=(AMBIGUOUS,)),),
            (900.0,),
        ),
        _clip(
            "b",
            (ScoredUtterance(start_ms=0, final_word_end_ms=1000, gaps=(CERTAIN,)),),
            (1100.0,),
        ),
        _clip(
            "c",
            (ScoredUtterance(start_ms=0, final_word_end_ms=1000, gaps=(AMBIGUOUS,)),),
            (900.0,),
        ),
    ]
    assert pcr(clips) == pytest.approx(2 / 3)
    assert pcr(clips, certain_only=True) == 0.0
    assert frag(clips) == 1.0
    assert frag(clips, certain_only=True) == 1.0


def test_an_utterance_with_no_gaps_counts_as_certain() -> None:
    """An unperturbed utterance rests on no proxy, so it is always in scope."""
    clips = [
        _clip("a", (ScoredUtterance(start_ms=0, final_word_end_ms=1000),), (1100.0,))
    ]
    assert pcr(clips, certain_only=True) == 0.0


def test_certain_only_with_nothing_certain_is_refused_not_zero() -> None:
    """An empty scope must not silently report a perfect score."""
    clips = [
        _clip(
            "a",
            (ScoredUtterance(start_ms=0, final_word_end_ms=1000, gaps=(AMBIGUOUS,)),),
            (900.0,),
        )
    ]
    with pytest.raises(ValueError, match="no utterances"):
        pcr(clips, certain_only=True)


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
