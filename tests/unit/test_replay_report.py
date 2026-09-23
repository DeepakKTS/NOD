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
    ClipObservation,
    ProxyDivergence,
    RunManifest,
    ScoredUtterance,
    artifact_name,
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
    CLUSTER_BAR_LABEL,
    INCONCLUSIVE,
    LIVE_BAR_LABEL,
    README_TABLE_END,
    README_TABLE_START,
    ArmPoint,
    bootstrap_points,
    cluster_bootstrap_points,
    pareto_svg,
    render_all,
    render_results_md,
    repeat_points,
    update_readme_table,
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


# --- the repeat axis, EC-38, EC-41 and INV-9 (Gate 4a) ---------------------


def _point(arm: Arm, *, pcr_v: float, ttl: float, lo: float, hi: float) -> ArmPoint:
    """One synthetic point, so the warnings can be tested without a live run."""
    return ArmPoint(
        arm=arm,
        pcr=pcr_v,
        ttl_p90_ms=ttl,
        pcr_ci=(pcr_v - 0.01, pcr_v + 0.01),
        ttl_p90_ci_ms=(lo, hi),
        interval_kind="iqr-over-repeats",
        n_clips=10,
        n_repeats=5,
        frag=1.0,
    )


def test_the_repeat_axis_needs_more_than_one_repeat(tmp_path: Path) -> None:
    """ADR-019: an IQR over one pass is zero, and zero reads as precision."""
    corpus = _corpus(tmp_path)
    by_arm = run_matrix(corpus, ["balanced"])
    with pytest.raises(ValueError, match="at least 2 repeats"):
        repeat_points(by_arm, repeats=1)


def _observation(clip_id: str, *, premature: bool) -> ClipObservation:
    """One clip's observation, with PCR either 1.0 or 0.0.

    PCR has to **differ between clips** for the pass-recovery test below to have
    any power: with identical clips, slicing the repeat axis and slicing the clip
    axis produce the same passes and the mutation "slice the clip axis instead"
    survives a full suite. That survivor is how this fixture came to exist
    (CLAUDE.md §5 — fixtures need values that differ along the axis under test).
    """
    utterance = ScoredUtterance(start_ms=0, final_word_end_ms=1000, gaps=())
    fired = 900.0 if premature else 1100.0
    return ClipObservation(
        clip_id=clip_id,
        arm="balanced",
        utterances=(utterance,),
        emitted_end_ms=(fired,),
        emitted_silence_start_ms=(fired,),
    )


def test_repeat_points_recovers_the_passes() -> None:
    """Repeat `r` of every clip is one pass: the whole corpus measured once.

    The observations arrive in clip-major order — `c0 c0 c0 c1 c1 c1 ...` — which
    is what `_live_sweep` produces, so `[r::repeats]` must recover one pass
    holding *every* clip once. The clips differ in PCR, so the wrong slicing
    lands a single clip in each pass and the median and the IQR both move.
    """
    repeats = 3
    clips = [
        _observation("c0", premature=True),
        _observation("c1", premature=False),
        _observation("c2", premature=False),
    ]
    by_arm: dict[Arm, list[ClipObservation]] = {
        "balanced": [obs for obs in clips for _ in range(repeats)]
    }
    (point,) = repeat_points(by_arm, repeats=repeats)

    assert point.n_repeats == repeats
    assert point.n_clips == len(clips)
    assert point.interval_kind == "iqr-over-repeats"
    # Every pass is the whole corpus: 1 premature of 3, on every pass.
    assert point.pcr == pytest.approx(1 / 3), (
        "a pass must hold every clip once; this is the corpus PCR, and a per-clip "
        "pass would give 1.0 or 0.0 instead"
    )
    assert point.pcr_ci[0] == point.pcr_ci[1] == pytest.approx(1 / 3), (
        "identical passes must give a zero-width IQR; width here means the slice "
        "is reading the clip axis"
    )


def test_repeat_points_refuses_a_count_that_does_not_divide(tmp_path: Path) -> None:
    """Otherwise the passes cannot be recovered and the slicing is silent."""
    corpus = _corpus(tmp_path)
    by_arm = run_matrix(corpus, ["balanced"])
    with pytest.raises(ValueError, match="do not divide"):
        repeat_points(by_arm, repeats=7)


def test_render_all_refuses_repeats_against_the_simulator(tmp_path: Path) -> None:
    """ADR-019, mechanised: the simulated path has no repeat axis to report."""
    corpus = _corpus(tmp_path)
    by_arm = run_matrix(corpus, ["balanced"])
    with pytest.raises(ValueError, match="byte-identical"):
        render_all(
            by_arm,
            out=tmp_path / "out",
            simulated=True,
            manifest=_manifest(len(corpus.clips)),
            repeats=5,
        )


def test_a_live_table_is_not_printed_under_a_simulated_legend() -> None:
    """The column heading and the bar label follow `interval_kind`.

    A live table under `95% bootstrap CI over clips` would misdescribe its own
    numbers, and ADR-019's whole point is that the legend travels with the image.
    """
    table = render_results_md([_point("balanced", pcr_v=0.2, ttl=500, lo=490, hi=510)])
    assert "IQR" in table
    assert LIVE_BAR_LABEL in table
    assert BAR_LABEL not in table


def test_an_interval_wider_than_the_difference_is_called_inconclusive() -> None:
    """EC-38, in those words. Documented since Phase 1, implemented at Gate 4a."""
    points = [
        _point("balanced", pcr_v=0.20, ttl=500.0, lo=495.0, hi=505.0),
        # 10 ms apart, with a 200 ms interval: noise dominates the difference.
        _point("nod", pcr_v=0.10, ttl=510.0, lo=410.0, hi=610.0),
    ]
    table = render_results_md(points)
    assert INCONCLUSIVE in table
    assert "`nod` vs `balanced`" in table


def test_a_difference_clear_of_the_noise_is_not_flagged() -> None:
    """The companion direction: a rule that flags everything is not a rule."""
    points = [
        _point("balanced", pcr_v=0.20, ttl=500.0, lo=495.0, hi=505.0),
        _point("nod", pcr_v=0.10, ttl=900.0, lo=890.0, hi=910.0),
    ]
    assert INCONCLUSIVE not in render_results_md(points)


def test_a_direction_flip_between_tracks_is_reported_prominently() -> None:
    """EC-41. The sign is then a property of the corpus, not of the arm."""
    track_a = [
        _point("balanced", pcr_v=0.2, ttl=500.0, lo=495.0, hi=505.0),
        _point("nod", pcr_v=0.1, ttl=400.0, lo=395.0, hi=405.0),
    ]
    track_c = [
        _point("balanced", pcr_v=0.2, ttl=500.0, lo=495.0, hi=505.0),
        _point("nod", pcr_v=0.1, ttl=700.0, lo=695.0, hi=705.0),
    ]
    table = render_results_md(track_a, other_track=track_c)
    assert "disagrees in direction" in table
    assert "EC-41" in table


def test_agreeing_tracks_produce_no_disagreement_section() -> None:
    """Otherwise the EC-41 warning would appear on every run and mean nothing."""
    track_a = [
        _point("balanced", pcr_v=0.2, ttl=500.0, lo=495.0, hi=505.0),
        _point("nod", pcr_v=0.1, ttl=400.0, lo=395.0, hi=405.0),
    ]
    track_c = [
        _point("balanced", pcr_v=0.2, ttl=600.0, lo=595.0, hi=605.0),
        _point("nod", pcr_v=0.1, ttl=450.0, lo=445.0, hi=455.0),
    ]
    assert "EC-41" not in render_results_md(track_a, other_track=track_c)


# --- INV-9's enforcement arm ------------------------------------------------


def test_update_readme_table_replaces_only_the_owned_region(tmp_path: Path) -> None:
    """INV-9: the region belongs to `make bench`, not to whoever edits prose."""
    readme = tmp_path / "README.md"
    readme.write_text(
        f"# Nod\n\nprose above\n\n{README_TABLE_START}\nold\n{README_TABLE_END}\n\n"
        "prose below\n",
        encoding="utf-8",
    )
    update_readme_table(readme, "| arm | PCR |\n|---|---|\n| `nod` | 0.100 |")
    text = readme.read_text(encoding="utf-8")
    assert "prose above" in text
    assert "prose below" in text
    assert "old" not in text
    assert "`nod` | 0.100" in text


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("no markers at all\n", id="absent"),
        pytest.param(f"{README_TABLE_START}\nx\n", id="end-missing"),
        pytest.param(f"{README_TABLE_END}\nx\n{README_TABLE_START}\n", id="inverted"),
        pytest.param(
            f"{README_TABLE_START}\na\n{README_TABLE_END}\n"
            f"{README_TABLE_START}\nb\n{README_TABLE_END}\n",
            id="duplicated",
        ),
    ],
)
def test_update_readme_table_refuses_an_ambiguous_region(
    tmp_path: Path, body: str
) -> None:
    """A best-effort write would leave a hand-written number and report success."""
    readme = tmp_path / "README.md"
    readme.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        update_readme_table(readme, "| arm |\n|---|\n")


def test_the_report_cli_refuses_to_publish_simulated_figures(tmp_path: Path) -> None:
    """ADR-016 and INV-9: a published number comes from a live run only.

    The most valuable refusal in this file. `make bench` runs constantly and
    produces a complete, plausible table; publishing it would put simulated
    figures in the README under no label at all, because the README's table
    region carries no provenance tag of its own.
    """
    from nod_bench.report import main

    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / artifact_name("results", simulated=True, suffix="md")).write_text(
        "| arm | PCR |\n|---|---|\n| `nod` | 0.100 |\n"
    )
    readme = tmp_path / "README.md"
    readme.write_text(f"{README_TABLE_START}\nplaceholder\n{README_TABLE_END}\n")

    code = main(
        [
            "--runs",
            str(runs),
            "--results",
            str(tmp_path / "RESULTS.md"),
            "--readme",
            str(readme),
            "--publish",
        ]
    )
    assert code == 2
    assert "placeholder" in readme.read_text()
    assert not (tmp_path / "RESULTS.md").exists()


def test_the_report_cli_publishes_a_live_table(tmp_path: Path) -> None:
    """The companion: with a live table it writes both destinations."""
    from nod_bench.report import main

    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / artifact_name("results", simulated=False, suffix="md")).write_text(
        "| arm | PCR |\n|---|---|\n| `nod` | 0.100 |\n"
    )
    readme = tmp_path / "README.md"
    readme.write_text(f"{README_TABLE_START}\nplaceholder\n{README_TABLE_END}\n")
    results = tmp_path / "RESULTS.md"

    code = main(
        [
            "--runs",
            str(runs),
            "--results",
            str(results),
            "--readme",
            str(readme),
            "--publish",
        ]
    )
    assert code == 0
    assert "`nod` | 0.100" in readme.read_text()
    assert "placeholder" not in readme.read_text()
    assert "Do not edit by hand" in results.read_text()


def test_the_report_card_is_self_contained(tmp_path: Path) -> None:
    """BENCH_SPEC §8: no external assets, no network on open."""
    from nod_bench.replay import RunResult
    from nod_bench.report import render_report_card

    corpus = _corpus(tmp_path)
    # Static arms only: `_corpus` has no transcribed words, and a controlled arm
    # raises `MissingWordsError` on such a sidecar rather than reconstructing
    # them (ADR-031). The card's rendering does not depend on which arms it got.
    by_arm = run_matrix(corpus, ["balanced", "aggressive"])
    runs = [
        RunResult(
            run_id=f"{obs.clip_id}:{arm}",
            clip_id=obs.clip_id,
            arm=arm,
            repeats=1,
            trace_paths=(),
            observations=(obs,),
        )
        for arm, observations in by_arm.items()
        for obs in observations
    ]
    html_doc = render_report_card(runs)
    assert "<svg" in html_doc
    assert "<table>" in html_doc
    assert "honest scope" in html_doc.lower()

    # "No network on open" is about *fetches*, not about every occurrence of a
    # URL. The SVG namespace declaration is `xmlns="http://www.w3.org/2000/svg"`,
    # which is an inert identifier no renderer resolves — the first version of
    # this assertion banned the substring and failed on it, which was the
    # assertion being wrong rather than the card.
    for attribute in ("src=", "href=", "@import", "<script", "url("):
        assert attribute not in html_doc, f"card reaches out via {attribute}"
    assert html_doc.count("http") == html_doc.count('xmlns="http'), (
        "every URL in the card must be the SVG namespace declaration"
    )


def test_the_report_card_refuses_an_empty_sweep() -> None:
    """A card over nothing still renders a chart and a table about nothing."""
    from nod_bench.report import render_report_card

    with pytest.raises(ValueError, match="no observations"):
        render_report_card([])


# --- the live interval at n=12 (ADR-049) -----------------------------------


def _clustered(
    *, n_clips: int, repeats: int, premature_clips: int, jitter_ms: float
) -> dict[Arm, list[ClipObservation]]:
    """Clips that differ from each other *and* repeats that differ within a clip.

    Both axes have to vary or the comparison below is vacuous: with identical
    clips the cluster bootstrap collapses to zero width, and with identical
    repeats the IQR does.
    """
    out: list[ClipObservation] = []
    for c in range(n_clips):
        premature = c < premature_clips
        for r in range(repeats):
            fired = (900.0 if premature else 1100.0) + r * jitter_ms
            out.append(
                ClipObservation(
                    clip_id=f"c{c}",
                    arm="balanced",
                    utterances=(
                        ScoredUtterance(start_ms=0, final_word_end_ms=1000, gaps=()),
                    ),
                    emitted_end_ms=(fired,),
                    emitted_silence_start_ms=(fired,),
                )
            )
    return {"balanced": out}


def test_the_cluster_bootstrap_is_wider_than_the_repeat_iqr() -> None:
    """ADR-049's whole justification, asserted rather than argued.

    At n=12 the clip axis is the dominant uncertainty and `repeat_points`
    ignores it: its five passes each contain every clip, so between-clip
    variation cancels. The cluster bootstrap resamples clips whole and so
    carries both sources, and must therefore report the wider interval.
    """
    by_arm = _clustered(n_clips=12, repeats=5, premature_clips=4, jitter_ms=20.0)
    (cluster,) = cluster_bootstrap_points(by_arm, repeats=5)
    (iqr,) = repeat_points(by_arm, repeats=5)

    cluster_width = cluster.pcr_ci[1] - cluster.pcr_ci[0]
    iqr_width = iqr.pcr_ci[1] - iqr.pcr_ci[0]
    assert iqr_width == pytest.approx(0.0), (
        "every pass holds all 12 clips, so the repeat IQR of PCR is zero here — "
        "which is the understatement ADR-049 is about"
    )
    assert cluster_width > iqr_width, (
        f"cluster interval {cluster_width:.4f} is not wider than the repeat "
        f"IQR {iqr_width:.4f}; the clip axis is not reaching the interval"
    )
    assert cluster.pcr == pytest.approx(4 / 12)


def test_the_cluster_bootstrap_keeps_the_repeats_inside_the_replicate() -> None:
    """Drawing a clip takes all of its repeats, so run-to-run variation stays in.

    Checked on TTL, where the repeats differ by construction. A per-observation
    resample would break the clustering and report a narrower interval by
    treating 60 correlated rows as 60 independent ones.
    """
    by_arm = _clustered(n_clips=12, repeats=5, premature_clips=4, jitter_ms=60.0)
    (point,) = cluster_bootstrap_points(by_arm, repeats=5)
    assert point.n_clips == 12
    assert point.n_repeats == 5
    assert point.ttl_p90_ci_ms[1] > point.ttl_p90_ci_ms[0]


def test_the_cluster_bootstrap_refuses_uneven_clusters() -> None:
    """Unequal repeat counts would silently weight some clips more than others."""
    by_arm = _clustered(n_clips=3, repeats=5, premature_clips=1, jitter_ms=10.0)
    by_arm["balanced"].pop()
    with pytest.raises(ValueError, match="uneven"):
        cluster_bootstrap_points(by_arm, repeats=5)


def test_the_cluster_bootstrap_refuses_unpaired_arms() -> None:
    """BENCH_SPEC §9 pairs on the clip; the cluster estimator must too."""
    by_arm = _clustered(n_clips=3, repeats=2, premature_clips=1, jitter_ms=10.0)
    other = [
        obs.model_copy(update={"clip_id": f"x{i}"})
        for i, obs in enumerate(by_arm["balanced"])
    ]
    by_arm["aggressive"] = other
    with pytest.raises(ValueError, match="same clips"):
        cluster_bootstrap_points(by_arm, repeats=2)


def test_a_live_table_says_the_bars_cover_both_sources() -> None:
    """The legend travels with the image (ADR-019), including which estimator."""
    by_arm = _clustered(n_clips=12, repeats=5, premature_clips=4, jitter_ms=20.0)
    points = cluster_bootstrap_points(by_arm, repeats=5)
    table = render_results_md(points)
    assert CLUSTER_BAR_LABEL in table
    assert BAR_LABEL not in table
    assert LIVE_BAR_LABEL not in table
