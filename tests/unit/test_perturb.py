"""Track A perturbations: exact ground truth, and byte-identical at a fixed seed.

BENCH_SPEC §2. Every published number descends from these clips, so the two
things tested hardest are that the audio is reproducible and that the sidecar
says something true about it — in particular `Gap.preceding`, which is what
`FakeAssemblyAI` reads to choose an endpointing gate (ADR-017).
"""

from __future__ import annotations

import numpy as np
import pytest

from nod_bench.perturb import (
    BURST_GAP_MS_RANGE,
    CORRECT_TEMPLATES,
    PROLONG_SEGMENT_MS,
    REPEAT_GAP_MS,
    REPEAT_WORD_MS,
    Audio,
    PerturbationKind,
    TruthSpan,
    apply,
    burst,
    correct,
    pause,
    prolong,
    repeat,
)

SR = 16_000


def _tone(ms: int, *, freq: float = 220.0, sr: int = SR) -> Audio:
    """A deterministic source clip with **no sample exactly zero**. `O(n)`.

    Cosine, not sine: `sin(0)` is exactly 0.0, which `_silent_run_ms` would read
    as a one-sample silence at the head of every clip and count as a gap. The
    source has to be distinguishable from inserted silence for these assertions
    to mean anything.
    """
    n = round(ms * sr / 1000)
    t = np.arange(n, dtype=np.float32) / sr
    tone = (0.25 * np.cos(2 * np.pi * freq * t)).astype(np.float32)
    assert not np.any(tone == 0.0), "the source must never look like silence"
    return tone


def _ms(audio: Audio, sr: int = SR) -> int:
    return round(len(audio) * 1000 / sr)


def _silent_run_ms(audio: Audio, sr: int = SR) -> list[int]:
    """Lengths of every run of digital silence, in ms. `O(n)`."""
    silent = audio == 0.0
    runs: list[int] = []
    count = 0
    for flag in silent:
        if flag:
            count += 1
        elif count:
            runs.append(round(count * 1000 / sr))
            count = 0
    if count:
        runs.append(round(count * 1000 / sr))
    return runs


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_the_same_seed_produces_byte_identical_audio() -> None:
    """The determinism claim in BENCH_SPEC §2, asserted on bytes not on shape."""
    source = _tone(4000)
    first, truth_a = burst(source, SR, bursts=4, rng=np.random.default_rng(7))
    second, truth_b = burst(source, SR, bursts=4, rng=np.random.default_rng(7))

    assert first.tobytes() == second.tobytes()
    assert truth_a == truth_b


def test_a_different_seed_produces_different_audio() -> None:
    """Guards the test above from passing because the rng is ignored entirely."""
    source = _tone(4000)
    first, _ = burst(source, SR, bursts=4, rng=np.random.default_rng(7))
    second, _ = burst(source, SR, bursts=4, rng=np.random.default_rng(8))
    assert first.tobytes() != second.tobytes()


def test_determinism_holds_through_the_dispatcher() -> None:
    """`apply` must thread the generator, not quietly make its own."""
    source = _tone(4000)
    a, _ = apply(source, SR, "burst", rng=np.random.default_rng(11), bursts=3)
    b, _ = apply(source, SR, "burst", rng=np.random.default_rng(11), bursts=3)
    assert a.tobytes() == b.tobytes()


# --------------------------------------------------------------------------
# One test per perturbation
# --------------------------------------------------------------------------


def test_pause_inserts_exactly_the_requested_silence() -> None:
    source = _tone(2000)
    out, truth = pause(source, SR, at_ms=900, len_ms=1400)

    assert _ms(out) == 2000 + 1400
    assert _silent_run_ms(out) == [1400]
    assert len(truth.gaps) == 1
    assert (truth.gaps[0].start_ms, truth.gaps[0].end_ms) == (900, 2300)


def test_repeat_duplicates_the_word_once_per_repetition() -> None:
    source = _tone(2000)
    out, truth = repeat(source, SR, at_ms=800, times=3)

    added = 3 * (REPEAT_WORD_MS + REPEAT_GAP_MS)
    assert _ms(out) == 2000 + added
    assert _silent_run_ms(out) == [REPEAT_GAP_MS] * 3
    assert len(truth.gaps) == 3


def test_prolong_lengthens_without_inserting_silence() -> None:
    source = _tone(2000)
    out, truth = prolong(source, SR, at_ms=800, factor=2.5)

    assert _ms(out) > 2000
    assert truth.gaps == (), "a stretch creates no silence, so it creates no gap"
    # librosa's phase vocoder warns from inside numba about an intermediate
    # cast, and that warning is filtered in pyproject. Assert the output is
    # actually finite so the filter cannot hide a real NaN.
    assert np.all(np.isfinite(out)), "the stretch produced non-finite samples"
    assert float(np.max(np.abs(out))) <= 1.0
    stretched = _ms(out) - 2000
    assert stretched == pytest.approx(PROLONG_SEGMENT_MS * 1.5, abs=60)


def test_correct_restarts_from_the_clip_and_adds_no_vocabulary() -> None:
    source = _tone(3000)
    pre_gap, restart_ms, post_gap = CORRECT_TEMPLATES[1]
    out, truth = correct(source, SR, at_ms=1500, template=1)

    assert _ms(out) == 3000 + pre_gap + restart_ms + post_gap
    assert _silent_run_ms(out) == [pre_gap, post_gap]
    assert len(truth.gaps) == 2


def test_burst_splits_into_the_requested_number_of_bursts() -> None:
    source = _tone(6000)
    out, truth = burst(source, SR, bursts=4, rng=np.random.default_rng(3))

    runs = _silent_run_ms(out)
    assert len(runs) == 3, "four bursts means three gaps"
    assert len(truth.gaps) == 3
    for gap_ms in runs:
        assert BURST_GAP_MS_RANGE[0] <= gap_ms <= BURST_GAP_MS_RANGE[1]


# --------------------------------------------------------------------------
# The sidecar contract Gate C depends on
# --------------------------------------------------------------------------


def test_every_gap_declares_the_regime_that_governs_it() -> None:
    """ADR-017: `preceding` is the field the simulator reads."""
    source = _tone(6000)
    spans: list[TruthSpan] = [
        pause(source, SR, at_ms=900, len_ms=1400)[1],
        repeat(source, SR, at_ms=800, times=2)[1],
        correct(source, SR, at_ms=1500, template=0)[1],
        burst(source, SR, bursts=3, rng=np.random.default_rng(5))[1],
    ]
    for span in spans:
        for gap in span.gaps:
            assert gap.preceding in {"complete", "fragment"}
            assert gap.certainty in {"certain", "ambiguous"}
            assert gap.basis, "every label states its grounds"


def test_gap_timings_are_inside_the_clip_they_describe() -> None:
    """Ground truth that points outside the audio is worse than none."""
    source = _tone(6000)
    for out, span in (
        pause(source, SR, at_ms=900, len_ms=1400),
        repeat(source, SR, at_ms=800, times=2),
        correct(source, SR, at_ms=1500, template=2),
        burst(source, SR, bursts=4, rng=np.random.default_rng(2)),
    ):
        for gap in span.gaps:
            assert 0 <= gap.start_ms < gap.end_ms <= _ms(out) + 1, gap


def test_which_perturbations_produce_ambiguous_regimes() -> None:
    """Pins the ambiguity report so it cannot drift silently.

    `repeat` is certain: a duplicated word leaves the speaker audibly
    mid-utterance. `pause`, `correct` and `burst` all cut at an offset that is
    not aligned to clause boundaries, so the structural label can disagree with
    what the model's semantic gate would decide.
    """
    source = _tone(6000)
    certainties = {
        "pause": {
            g.certainty for g in pause(source, SR, at_ms=900, len_ms=800)[1].gaps
        },
        "repeat": {g.certainty for g in repeat(source, SR, at_ms=800, times=2)[1].gaps},
        "correct": {
            g.certainty for g in correct(source, SR, at_ms=1500, template=0)[1].gaps
        },
        "burst": {
            g.certainty
            for g in burst(source, SR, bursts=3, rng=np.random.default_rng(5))[1].gaps
        },
    }
    assert certainties["repeat"] == {"certain"}
    assert certainties["pause"] == {"ambiguous"}
    assert certainties["correct"] == {"ambiguous"}
    assert certainties["burst"] == {"ambiguous"}


def test_a_split_point_outside_the_clip_is_refused() -> None:
    source = _tone(1000)
    with pytest.raises(ValueError, match="not inside"):
        pause(source, SR, at_ms=5000, len_ms=400)


def test_all_six_perturbations_are_implemented() -> None:
    """`noise` landed in D1; this replaces the tripwire that tracked it.

    The stub test it replaces did its job: it failed the moment `noise` was
    implemented, rather than letting an obsolete claim sit in the suite.
    """
    from nod_bench.perturb import apply

    rng = np.random.default_rng(1)
    cases: list[tuple[PerturbationKind, dict[str, float]]] = [
        ("pause", {"at_ms": 500, "len_ms": 400}),
        ("repeat", {"at_ms": 500, "times": 1}),
        ("prolong", {"at_ms": 500, "factor": 1.5}),
        ("correct", {"at_ms": 500, "template": 0}),
        ("burst", {"bursts": 2}),
        ("noise", {"snr_db": 20.0}),
    ]
    for kind, params in cases:
        out, _ = apply(_tone(2000), SR, kind, rng=rng, **params)
        assert len(out) > 0, kind
