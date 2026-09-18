"""The metric suite.

BENCH_SPEC.md §5, defined there once. Definitions borrow from published
turn-taking work — Full-Duplex-Bench v3, EVA-Bench, IHBench and tau-Voice — and
the borrowings are cited in the report.

Negative latencies are physically possible when a turn ends early. They are
recorded as negative and never clipped to zero, because clipping hides exactly
the failure being measured.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from typing import Final

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

from nod_bench.perturb import Gap
from nod_bench.replay import RunResult

DECIDE_BUDGET_MS: Final = 5.0
"""DEC must stay under this. Guards INV-2. Milliseconds."""

PERFECT_FRAG: Final = 1.0
"""FRAG of 1.0 is one emitted turn per ground-truth utterance."""

QUANTILE_METHOD: Final = "nearest-rank, inclusive"
"""How p50 / p90 / p99 are computed. Stated, not inherited from a library.

`p_q = sorted[ceil(q * n) - 1]`, one-indexed rank `ceil(q * n)`.

**Not** linear interpolation, which is `numpy.percentile`'s default
(Hyndman-Fan type 7) and would be the silent choice. Two reasons, and every
published latency number in this project inherits them:

1. Interpolation reports a latency that never happened. "90 % of turns completed
   within X" is only meaningful if some turn actually took X. Nearest-rank always
   returns an observed sample; type 7 returns a weighted blend of two.
2. At the sample sizes here it flatters the tail. BENCH_SPEC §4 runs `N = 5` per
   (clip, arm); over nine samples of 10..90 ms, nearest-rank reports p90 = 90 and
   type 7 reports 82. The difference is the whole tail, and it is in the
   direction that makes results look better.

Nearest-rank is the conservative choice, which is the right bias for a number
the project publishes about itself.
"""


def quantile(samples: Sequence[float], q: float) -> float:
    """The `q`-quantile by `QUANTILE_METHOD`. Pure. `O(n log n)`.

    Args:
        samples: Observations. Negative values are meaningful and preserved.
        q: Quantile in `[0, 1]`.

    Returns:
        An observed sample, never an interpolated value.

    Raises:
        ValueError: `samples` is empty, or `q` is outside `[0, 1]`.
    """
    if not samples:
        msg = "no samples: a quantile of nothing is not 0.0"
        raise ValueError(msg)
    if not 0.0 <= q <= 1.0:
        msg = f"q={q} is outside [0, 1]"
        raise ValueError(msg)
    ordered = sorted(samples)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


type Samples = NDArray[np.float64]


class ScoredUtterance(BaseModel):
    """One ground-truth utterance, and the gaps inside it.

    `gaps` comes from the `.truth.json` sidecar (`perturb.Gap`). It is carried
    here because a gap's `certainty` decides whether this utterance can be
    scored on semantics or only on the construction proxy (ADR-017).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: int
    final_word_end_ms: int
    gaps: tuple[Gap, ...] = ()

    @property
    def certain(self) -> bool:
        """Whether every gap inside this utterance is unambiguously labelled."""
        return all(g.certainty == "certain" for g in self.gaps)


class ClipObservation(BaseModel):
    """What one arm did to one clip: ground truth, and the turns it emitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_id: str
    arm: str
    utterances: tuple[ScoredUtterance, ...]
    emitted_end_ms: tuple[float, ...]
    """Stream-relative times at which `end_of_turn` fired."""


def _attribute(utterance_index: int, obs: ClipObservation) -> tuple[float, ...]:
    """Emitted turns belonging to one utterance. Pure. `O(u + e)`.

    A turn belongs to the last utterance that had started when it fired. A turn
    before the first utterance starts is attributed to it rather than dropped,
    because a boundary that early is a failure worth counting, not noise.
    """
    starts = [u.start_ms for u in obs.utterances]
    owned: list[float] = []
    for fired in obs.emitted_end_ms:
        owner = 0
        for index, start in enumerate(starts):
            if start <= fired:
                owner = index
        if owner == utterance_index:
            owned.append(fired)
    return tuple(owned)


def _selected(
    runs: Sequence[ClipObservation], *, certain_only: bool
) -> list[tuple[ClipObservation, int, ScoredUtterance]]:
    """Utterances in scope. Pure. `O(n)`."""
    return [
        (obs, index, utterance)
        for obs in runs
        for index, utterance in enumerate(obs.utterances)
        if not certain_only or utterance.certain
    ]


class Quantiles(BaseModel):
    """A p50 / p90 / p99 triple."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    p50: float
    p90: float
    p99: float


SIMULATED_TAG: Final = "simulated"
LIVE_TAG: Final = "live"
"""Artifact provenance tags (ADR-016, ADR-017).

Structural, not editorial. A chart leaves the repository as a file and is read
as an image; prose in a caption does not survive a screenshot, and a filename
does. Every artifact carries one of these, and `RunManifest.simulated` carries
the same fact in the data.
"""


def artifact_name(stem: str, *, simulated: bool, suffix: str) -> str:
    """Name one run artifact so it declares its own provenance. Pure. `O(1)`.

    Args:
        stem: Base name, for example `pareto`.
        simulated: Whether a simulator produced it.
        suffix: Extension without the dot, for example `svg`.

    Returns:
        `"<stem>.<simulated|live>.<suffix>"`.
    """
    return f"{stem}.{SIMULATED_TAG if simulated else LIVE_TAG}.{suffix}"


class ArmConfig(BaseModel):
    """One static arm's turn-detection settings, with where they came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    end_of_turn_confidence_threshold: float
    min_turn_silence: int
    max_turn_silence: int
    source: str
    """Citation for these values. BENCH_SPEC §3 forbids a strawman baseline, and
    a config with no provenance cannot be checked against that."""


class ProxyDivergence(BaseModel):
    """How much a metric moves when ambiguous-regime utterances are excluded.

    Recorded in the manifest rather than only in the report, because it says how
    much of the result rests on the construction proxy instead of on semantics
    (ADR-017). A reader who only sees the headline cannot recover it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pcr_all: float
    pcr_certain_only: float
    frag_all: float
    frag_certain_only: float
    utterances_all: int
    utterances_certain_only: int


class RunManifest(BaseModel):
    """What a run has to declare about itself to be reproducible.

    Every field is required. There are no defaults, deliberately: a manifest
    that silently filled in `simulated=False` or `endpoint_overhead_ms=0` would
    turn a missing record into a confident and wrong one, which is the failure
    mode a manifest exists to prevent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int
    generator_version: str
    corpus_id: str
    corpus_sha256: str
    corpus_clips: int
    repeats_per_arm: int
    arms: tuple[ArmConfig, ...]
    simulated: bool
    """True for `FakeAssemblyAI`, false for a live run (ADR-016, ADR-017).

    Structural, not editorial: artifacts also carry `simulated` in the filename.
    """

    endpoint_overhead_ms: float
    """ADR-017's parameter. 0 means unmeasured, and biases every TTL early."""

    quantile_method: str
    proxy_divergence: ProxyDivergence


def pcr(runs: Sequence[ClipObservation], *, certain_only: bool = False) -> float:
    """Premature cutoff rate. Lower is better.

    The fraction of ground-truth utterances where `end_of_turn` fired before the
    utterance's final word ended.

    Args:
        runs: The results to aggregate.
        certain_only: Restrict to utterances whose every gap is labelled
            `certain`. The default scores everything, including gaps whose
            regime rests on the construction proxy rather than on semantics
            (ADR-017). Report both; a material divergence is a finding about
            the corpus, not a rounding detail.

    Returns:
        The rate in [0, 1]. Never report it without its denominator.
    """
    scope = _selected(runs, certain_only=certain_only)
    if not scope:
        msg = "no utterances in scope: PCR of nothing is not 0.0"
        raise ValueError(msg)
    premature = sum(
        any(fired < utterance.final_word_end_ms for fired in _attribute(index, obs))
        for obs, index, utterance in scope
    )
    return premature / len(scope)


def ttl(runs: Sequence[ClipObservation], *, certain_only: bool = False) -> Quantiles:
    """Turn latency in ms from true utterance end to `end_of_turn`. Lower better.

    Args:
        runs: The results to aggregate.
        certain_only: Restrict to utterances whose every gap is labelled
            `certain`. The default scores everything, including gaps whose
            regime rests on the construction proxy rather than on semantics
            (ADR-017). Report both; a material divergence is a finding about
            the corpus, not a rounding detail.

    Returns:
        p50, p90 and p99. Negative values are preserved.
    """
    latencies: list[float] = []
    for obs, index, utterance in _selected(runs, certain_only=certain_only):
        owned = _attribute(index, obs)
        if owned:
            latencies.append(owned[-1] - utterance.final_word_end_ms)
    if not latencies:
        msg = "no utterance was ever ended: TTL is undefined, not zero"
        raise ValueError(msg)
    return Quantiles(
        p50=quantile(latencies, 0.50),
        p90=quantile(latencies, 0.90),
        p99=quantile(latencies, 0.99),
    )


def frag(runs: Sequence[ClipObservation], *, certain_only: bool = False) -> float:
    """Fragmentation: mean emitted turns per ground-truth utterance.

    Args:
        runs: The results to aggregate.
        certain_only: Restrict to utterances whose every gap is labelled
            `certain`. The default scores everything, including gaps whose
            regime rests on the construction proxy rather than on semantics
            (ADR-017). Report both; a material divergence is a finding about
            the corpus, not a rounding detail.

    Returns:
        The mean. `PERFECT_FRAG` is perfect.
    """
    scope = _selected(runs, certain_only=certain_only)
    if not scope:
        msg = "no utterances in scope: FRAG of nothing is not 1.0"
        raise ValueError(msg)
    emitted = sum(len(_attribute(index, obs)) for obs, index, _ in scope)
    return emitted / len(scope)


def tct(runs: Sequence[RunResult]) -> float:
    """Task completion time in wall-clock seconds. Lower is better.

    Includes repeats caused by cuts.

    Args:
        runs: The results to aggregate.

    Returns:
        Seconds to complete the scripted intake.
    """
    raise NotImplementedError


def res(runs: Sequence[RunResult]) -> float:
    """Resume rate: the fraction of cuts the caller recovered from by repeating.

    Args:
        runs: The results to aggregate.

    Returns:
        The rate in [0, 1]. Lower is better.
    """
    raise NotImplementedError


def patch_count(runs: Sequence[RunResult]) -> float:
    """Patches per session. A cost measure, reported as context.

    Args:
        runs: The results to aggregate.

    Returns:
        Mean patches per session.
    """
    raise NotImplementedError


def dec_p99(runs: Sequence[RunResult]) -> float:
    """p99 of `Arbiter.decide`, in milliseconds. Guards INV-2.

    Args:
        runs: The results to aggregate.

    Returns:
        The p99. Must stay under `DECIDE_BUDGET_MS`.
    """
    raise NotImplementedError


def wilcoxon(a: Samples, b: Samples) -> tuple[float, float]:
    """Paired Wilcoxon signed-rank test, via `scipy.stats.wilcoxon` (ADR-005).

    The same clips run through every arm, so the comparison is paired and an
    unpaired test would be wrong (BENCH_SPEC.md §9). Report the effect size and
    the n alongside any p-value.

    Args:
        a: One arm's per-clip values.
        b: The paired arm's per-clip values.

    Returns:
        The statistic and the two-sided p-value.
    """
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    """Recompute every metric from committed traces, with zero API spend.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit code.
    """
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
