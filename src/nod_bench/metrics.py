"""The metric suite.

BENCH_SPEC.md §5, defined there once. Definitions borrow from published
turn-taking work — Full-Duplex-Bench v3, EVA-Bench, IHBench and tau-Voice — and
the borrowings are cited in the report.

Negative latencies are physically possible when a turn ends early. They are
recorded as negative and never clipped to zero, because clipping hides exactly
the failure being measured.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Final

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

from nod_bench.replay import RunResult

DECIDE_BUDGET_MS: Final = 5.0
"""DEC must stay under this. Guards INV-2. Milliseconds."""

PERFECT_FRAG: Final = 1.0
"""FRAG of 1.0 is one emitted turn per ground-truth utterance."""

type Samples = NDArray[np.float64]


class Quantiles(BaseModel):
    """A p50 / p90 / p99 triple."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    p50: float
    p90: float
    p99: float


def pcr(runs: Sequence[RunResult]) -> float:
    """Premature cutoff rate. Lower is better.

    The fraction of ground-truth utterances where `end_of_turn` fired before the
    utterance's final word ended.

    Args:
        runs: The results to aggregate.

    Returns:
        The rate in [0, 1]. Never report it without its denominator.
    """
    raise NotImplementedError


def ttl(runs: Sequence[RunResult]) -> Quantiles:
    """Turn latency in ms from true utterance end to `end_of_turn`. Lower better.

    Args:
        runs: The results to aggregate.

    Returns:
        p50, p90 and p99. Negative values are preserved.
    """
    raise NotImplementedError


def frag(runs: Sequence[RunResult]) -> float:
    """Fragmentation: mean emitted turns per ground-truth utterance.

    Args:
        runs: The results to aggregate.

    Returns:
        The mean. `PERFECT_FRAG` is perfect.
    """
    raise NotImplementedError


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
    """Paired Wilcoxon signed-rank test (BENCH_SPEC.md §9).

    The same clips run through every arm, so the comparison is paired and an
    unpaired test would be wrong. Report the effect size and the n alongside any
    p-value.

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
