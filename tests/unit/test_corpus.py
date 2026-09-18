"""Track A generation: deterministic, identified, and honest about its gaps.

BENCH_SPEC §2. The corpus is what every published number is computed over, so
the two things tested hardest are that a seed reproduces it exactly and that the
sidecar describes every silence a gate could bind on — including the ones
already present in the source speech, which the generator did not create and
which an earlier version of this file silently mislabelled.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from nod_bench.corpus import (
    MIN_INTRINSIC_GAP_MS,
    TAIL_SILENCE_MS,
    BuiltCorpus,
    CorpusManifest,
    SourceClip,
    build,
    sweep,
)
from nod_bench.perturb import (
    BURST_SWEEP,
    CORRECT_TEMPLATES,
    NOISE_SNR_DB_SWEEP,
    PAUSE_SWEEP_MS,
    PROLONG_SWEEP,
    REPEAT_SWEEP,
)

SR = 16_000


def _source(tmp_path: Path, name: str = "src") -> CorpusManifest:
    """Speech with a real silence inside it, so intrinsic gaps have something
    to find."""
    t = np.arange(int(2.0 * SR), dtype=np.float32) / SR
    tone = (0.25 * np.cos(2 * np.pi * 220.0 * t)).astype(np.float32)
    tone[int(0.8 * SR) : int(1.1 * SR)] = 0.0  # a 300 ms pause mid-utterance
    path = tmp_path / f"{name}.wav"
    sf.write(path, tone, SR, subtype="PCM_16")
    return CorpusManifest(
        version=1,
        track="trackA",
        clips=[
            SourceClip(
                corpus="test",
                license="test",
                file=path,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        ],
    )


def test_the_sweep_is_every_declared_configuration() -> None:
    kinds = [kind for kind, _ in sweep()]
    assert kinds.count("pause") == len(PAUSE_SWEEP_MS)
    assert kinds.count("repeat") == len(REPEAT_SWEEP)
    assert kinds.count("prolong") == len(PROLONG_SWEEP)
    assert kinds.count("correct") == len(CORRECT_TEMPLATES)
    assert kinds.count("burst") == len(BURST_SWEEP)
    assert kinds.count("noise") == len(NOISE_SNR_DB_SWEEP)


def test_a_corpus_is_byte_identical_at_the_same_seed(tmp_path: Path) -> None:
    """BENCH_SPEC §2's determinism claim, asserted on the corpus hash."""
    manifest = _source(tmp_path)
    first = build(manifest, seed=7, out=tmp_path / "a")
    second = build(manifest, seed=7, out=tmp_path / "b")
    assert first.corpus_sha256 == second.corpus_sha256
    assert [c.sha256 for c in first.clips] == [c.sha256 for c in second.clips]


def test_a_different_seed_produces_a_different_corpus(tmp_path: Path) -> None:
    """Guards the test above from passing on a build that ignores the seed."""
    manifest = _source(tmp_path)
    first = build(manifest, seed=7, out=tmp_path / "a")
    second = build(manifest, seed=8, out=tmp_path / "b")
    assert first.corpus_sha256 != second.corpus_sha256


def test_the_corpus_identifies_itself_for_a_manifest_to_cite(tmp_path: Path) -> None:
    built = build(_source(tmp_path), seed=7, out=tmp_path / "out")
    assert built.corpus_id == "trackA"
    assert len(built.corpus_sha256) == 64
    assert built.seed == 7
    assert built.generator_version
    assert (tmp_path / "out" / "corpus.json").exists()
    reloaded = BuiltCorpus.model_validate_json(
        (tmp_path / "out" / "corpus.json").read_text()
    )
    assert reloaded.corpus_sha256 == built.corpus_sha256


WIDEST_GATE_MS = 3600
"""`conservative`'s `max_turn_silence` (BENCH_SPEC §3), the longest any arm waits."""


def test_every_clip_ships_a_sidecar_and_a_tail(tmp_path: Path) -> None:
    """The tail must outlast the widest gate or `conservative` never ends.

    Compared against `WIDEST_GATE_MS`, not against `TAIL_SILENCE_MS`. Asserting
    a constant against itself passes for any value of that constant, which is
    the vacuous-invariant defect in CLAUDE.md §5 — a mutation shrinking the tail
    to 1000 ms survived the earlier version of this test.
    """
    built = build(_source(tmp_path), seed=7, out=tmp_path / "out")
    assert built.clips
    assert TAIL_SILENCE_MS > WIDEST_GATE_MS
    for clip in built.clips:
        assert clip.audio_path.exists()
        assert clip.truth_path.exists()
        tail_ms = clip.total_ms - clip.final_word_end_ms
        assert tail_ms > WIDEST_GATE_MS, (
            f"{clip.clip_id}: a {tail_ms} ms tail cannot outlast a "
            f"{WIDEST_GATE_MS} ms gate, so conservative would never end"
        )
        assert clip.truth.gaps, "a clip with no described gap cannot be scored"


def test_the_trailing_silence_is_labelled_complete(tmp_path: Path) -> None:
    """The speaker has finished, so `min_turn_silence` governs it."""
    built = build(_source(tmp_path), seed=7, out=tmp_path / "out")
    for clip in built.clips:
        tail = [g for g in clip.truth.gaps if g.origin == "utterance_end"]
        assert len(tail) == 1
        assert tail[0].preceding == "complete"
        assert tail[0].certainty == "certain"


def test_silences_already_in_the_source_are_described(tmp_path: Path) -> None:
    """Regression, and the reason this function exists.

    The source fixture has a 300 ms pause at 800 ms that the generator did not
    create. Undescribed, `regime_at` falls back to `complete`, so a pause in the
    middle of an utterance is governed by `min_turn_silence` — and on the
    `aggressive` arm, whose minimum is 160 ms, it fires a boundary that is then
    scored as a premature cutoff. Measured on the real corpus this accounted for
    most of that arm's PCR.
    """
    built = build(_source(tmp_path), seed=7, out=tmp_path / "out")
    intrinsic = [
        g
        for clip in built.clips
        for g in clip.truth.gaps
        if g.origin == "source_intrinsic"
    ]
    assert intrinsic, "the source pause was not described"
    for gap in intrinsic:
        assert gap.preceding == "fragment"
        assert gap.certainty == "ambiguous"
        assert gap.end_ms - gap.start_ms >= MIN_INTRINSIC_GAP_MS


def test_an_intrinsic_gap_never_shadows_a_generated_one(tmp_path: Path) -> None:
    """A silence the generator inserted is described by the generator, once."""
    built = build(_source(tmp_path), seed=7, out=tmp_path / "out")
    for clip in built.clips:
        spans = sorted((g.start_ms, g.origin) for g in clip.truth.gaps)
        starts = [s for s, _ in spans]
        assert len(starts) == len(set(starts)), clip.clip_id


def test_the_utterance_end_is_measured_not_asserted(tmp_path: Path) -> None:
    """Ground truth is scanned back from the audio, so a broken perturbation
    cannot also write the truth that would have caught it."""
    built = build(_source(tmp_path), seed=7, out=tmp_path / "out")
    for clip in built.clips:
        audio, sr = sf.read(clip.audio_path, dtype="float32")
        after = audio[int(clip.final_word_end_ms * sr / 1000) :]
        assert float(np.max(np.abs(after))) < 0.05, clip.clip_id


def test_noise_reaches_the_requested_snr(tmp_path: Path) -> None:
    from nod_bench.perturb import noise

    t = np.arange(SR, dtype=np.float32) / SR
    src = (0.25 * np.cos(2 * np.pi * 220.0 * t)).astype(np.float32)
    for target in NOISE_SNR_DB_SWEEP:
        out, truth = noise(src, SR, snr_db=float(target), rng=np.random.default_rng(1))
        signal = float(np.mean(src.astype(np.float64) ** 2))
        bed = float(np.mean((out - src).astype(np.float64) ** 2))
        assert 10 * np.log10(signal / bed) == pytest.approx(target, abs=0.5)
        assert truth.gaps == (), "noise changes no timing, so it creates no gap"


def test_noise_is_deterministic_and_seed_sensitive() -> None:
    from nod_bench.perturb import noise

    t = np.arange(SR, dtype=np.float32) / SR
    src = (0.25 * np.cos(2 * np.pi * 220.0 * t)).astype(np.float32)
    a, _ = noise(src, SR, snr_db=20.0, rng=np.random.default_rng(3))
    b, _ = noise(src, SR, snr_db=20.0, rng=np.random.default_rng(3))
    c, _ = noise(src, SR, snr_db=20.0, rng=np.random.default_rng(4))
    assert a.tobytes() == b.tobytes()
    assert a.tobytes() != c.tobytes()


def test_noise_against_silence_is_refused() -> None:
    """An SNR needs a signal; against silence it is undefined, not infinite."""
    from nod_bench.perturb import noise

    with pytest.raises(ValueError, match="SNR against silence"):
        noise(
            np.zeros(SR, dtype=np.float32),
            SR,
            snr_db=20.0,
            rng=np.random.default_rng(1),
        )
