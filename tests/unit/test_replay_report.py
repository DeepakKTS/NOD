"""The offline bench path: driver, bootstrap, chart, and provenance labelling.

Gate E. What is tested hardest is the thing a chart cannot say for itself — that
the bars are a clip bootstrap and not a repeat spread (ADR-019), and that every
artifact declares it is simulated in its own filename (ADR-016, ADR-017).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from nod_bench.corpus import BuiltCorpus, CorpusManifest, SourceClip, build
from nod_bench.metrics import (
    LIVE_TAG,
    SIMULATED_TAG,
    ArmConfig,
    ProxyDivergence,
    RunManifest,
)
from nod_bench.replay import (
    SIMULATED_REPEATS,
    STATIC_ARMS,
    Arm,
    frames_of,
    run_clip,
    run_matrix,
)
from nod_bench.report import (
    BAR_LABEL,
    BOOTSTRAP_REPLICATES,
    bootstrap_points,
    pareto_svg,
    render_all,
    render_results_md,
)

SR = 16_000
ARMS: list[Arm] = ["aggressive", "balanced", "conservative"]


def _corpus(tmp_path: Path) -> BuiltCorpus:
    t = np.arange(int(2.0 * SR), dtype=np.float32) / SR
    tone = (0.25 * np.cos(2 * np.pi * 220.0 * t)).astype(np.float32)
    tone[int(0.8 * SR) : int(1.1 * SR)] = 0.0
    path = tmp_path / "src.wav"
    sf.write(path, tone, SR, subtype="PCM_16")
    manifest = CorpusManifest(
        version=1,
        track="trackA",
        clips=[SourceClip(corpus="t", license="t", file=path, sha256="0" * 64)],
    )
    return build(manifest, seed=7, out=tmp_path / "corpus")


def test_the_arms_are_the_published_presets() -> None:
    """BENCH_SPEC §3, transcribed. A strawman baseline is the one thing forbidden."""
    assert STATIC_ARMS["aggressive"].min_turn_silence == 160
    assert STATIC_ARMS["balanced"].min_turn_silence == 400
    assert STATIC_ARMS["conservative"].min_turn_silence == 800
    assert STATIC_ARMS["balanced"].max_turn_silence == 1280


def test_frames_are_whole_frames_only(tmp_path: Path) -> None:
    """A trailing partial frame would be counted as a full one downstream."""
    sf.write(tmp_path / "odd.wav", np.zeros(SR + 37, dtype=np.float32), SR)
    frames = frames_of(tmp_path / "odd.wav")
    assert frames
    assert len({len(f) for f in frames}) == 1


def test_a_wider_arm_never_cuts_more_than_a_narrower_one(tmp_path: Path) -> None:
    """The ordering the tradeoff curve depends on, asserted per clip."""
    corpus = _corpus(tmp_path)
    by_arm = run_matrix(corpus, ARMS)
    for index in range(len(corpus.clips)):
        counts = [len(by_arm[arm][index].emitted_end_ms) for arm in ARMS]
        assert counts == sorted(counts, reverse=True), corpus.clips[index].clip_id


def test_each_arm_is_configured_differently(tmp_path: Path) -> None:
    """The ordering test tolerates ties, so it cannot see arms collapsing.

    If every arm were handed the same settings, per-clip counts would be equal
    and still trivially "sorted". This asserts the arms actually diverge, which
    is the thing a tradeoff curve depends on.
    """
    corpus = _corpus(tmp_path)
    by_arm = run_matrix(corpus, ARMS)
    totals = {arm: sum(len(o.emitted_end_ms) for o in by_arm[arm]) for arm in ARMS}
    assert len(set(totals.values())) == len(ARMS), totals
    assert totals["aggressive"] > totals["balanced"] > totals["conservative"]


def test_the_driver_passes_the_sidecar_regimes_to_the_simulator(
    tmp_path: Path,
) -> None:
    """ADR-017: the regime comes from ground truth, never from the audio.

    A driver that dropped `gaps` would silently fall back to `complete` for
    every silence, so `min_turn_silence` would govern mid-utterance pauses and
    the fragment regime would never be exercised at all.
    """
    corpus = _corpus(tmp_path)
    clip = next(c for c in corpus.clips if c.truth.perturbation["type"] == "pause")
    assert any(g.preceding == "fragment" for g in clip.truth.gaps)

    with_truth = run_clip(clip, "aggressive")
    stripped = clip.model_copy(
        update={"truth": clip.truth.model_copy(update={"gaps": ()})}
    )
    without_truth = run_clip(stripped, "aggressive")
    assert with_truth.emitted_end_ms != without_truth.emitted_end_ms, (
        "dropping the sidecar changed nothing, so the driver is not using it"
    )


def test_the_driver_is_deterministic(tmp_path: Path) -> None:
    """Which is exactly why repeats are 1 and the bars are not over them."""
    corpus = _corpus(tmp_path)
    clip = corpus.clips[0]
    assert run_clip(clip, "balanced") == run_clip(clip, "balanced")
    assert SIMULATED_REPEATS == 1


def test_the_bootstrap_interval_has_width(tmp_path: Path) -> None:
    """ADR-019's whole point: a repeat interval would be zero-width.

    A zero-width bar reads as precision. If this ever collapses, the chart is
    lying about how well the corpus pins the number.
    """
    corpus = _corpus(tmp_path)
    points = bootstrap_points(run_matrix(corpus, ARMS), replicates=400)
    widest = max(p.pcr_ci[1] - p.pcr_ci[0] for p in points)
    assert widest > 0.0, "a clip bootstrap over a real corpus cannot be a point"
    for point in points:
        assert point.pcr_ci[0] <= point.pcr <= point.pcr_ci[1]
        assert point.n_clips == len(corpus.clips)


def test_the_bootstrap_is_reproducible(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    by_arm = run_matrix(corpus, ARMS)
    assert bootstrap_points(by_arm, replicates=200) == bootstrap_points(
        by_arm, replicates=200
    )


def test_the_bootstrap_refuses_unpaired_arms(tmp_path: Path) -> None:
    """Clips are resampled jointly, so arms must cover the same clips."""
    corpus = _corpus(tmp_path)
    by_arm = run_matrix(corpus, ARMS)
    by_arm["balanced"] = by_arm["balanced"][:-1]
    with pytest.raises(ValueError, match="same clips"):
        bootstrap_points(by_arm, replicates=10)


def test_the_chart_says_what_its_bars_mean(tmp_path: Path) -> None:
    """In the image, not in a caption. A screenshot loses the caption."""
    corpus = _corpus(tmp_path)
    svg = pareto_svg(bootstrap_points(run_matrix(corpus, ARMS), replicates=200))
    assert BAR_LABEL in svg
    assert "NOT over repeats" in svg
    assert "SIMULATED" in svg
    assert "lower bound on uncertainty" in svg
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")


def test_the_results_table_carries_the_same_caveats(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    table = render_results_md(
        bootstrap_points(run_matrix(corpus, ARMS), replicates=200)
    )
    assert "95% CI" in table
    assert BAR_LABEL in table
    assert "nearest-rank" in table


def _manifest(clips: int) -> RunManifest:
    return RunManifest(
        seed=7,
        generator_version="0.1.0",
        corpus_id="trackA",
        corpus_sha256="a" * 64,
        corpus_clips=clips,
        repeats_per_arm=SIMULATED_REPEATS,
        arms=(
            ArmConfig(
                name="balanced",
                end_of_turn_confidence_threshold=0.4,
                min_turn_silence=400,
                max_turn_silence=1280,
                source="BENCH_SPEC §3",
            ),
        ),
        simulated=True,
        endpoint_overhead_ms=0.0,
        quantile_method="nearest-rank, inclusive",
        proxy_divergence=ProxyDivergence(
            pcr_all=0.3,
            pcr_certain_only=0.0,
            frag_all=1.3,
            frag_certain_only=1.0,
            utterances_all=120,
            utterances_certain_only=15,
        ),
    )


def test_every_artifact_is_labelled_simulated(tmp_path: Path) -> None:
    """ADR-016 and ADR-017: structural, not editorial."""
    corpus = _corpus(tmp_path)
    written = render_all(
        run_matrix(corpus, ARMS),
        out=tmp_path / "out",
        simulated=True,
        manifest=_manifest(len(corpus.clips)),
    )
    assert written
    for path in written:
        assert SIMULATED_TAG in path.name, path.name
        assert LIVE_TAG not in path.name
        assert path.exists()


def test_the_manifest_written_beside_the_chart_agrees_with_it(
    tmp_path: Path,
) -> None:
    """A chart labelled simulated beside a manifest that says live is worse
    than either alone."""
    corpus = _corpus(tmp_path)
    written = render_all(
        run_matrix(corpus, ARMS),
        out=tmp_path / "out",
        simulated=True,
        manifest=_manifest(len(corpus.clips)),
    )
    manifest_path = next(p for p in written if p.suffix == ".json")
    assert json.loads(manifest_path.read_text())["simulated"] is True
    assert SIMULATED_TAG in manifest_path.name


def test_the_default_replicate_count_is_the_documented_one() -> None:
    assert BOOTSTRAP_REPLICATES == 10_000
