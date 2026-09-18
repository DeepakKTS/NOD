"""Render the results table, the Pareto chart and the report card.

INV-9: no number in any document is hand-written. Every figure in the README,
docs/RESULTS.md or a slide is generated here from committed traces.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import numpy as np
from pydantic import BaseModel, ConfigDict

from nod_bench.metrics import (
    LIVE_TAG,
    QUANTILE_METHOD,
    SIMULATED_TAG,
    ClipObservation,
    RunManifest,
    artifact_name,
    frag,
    pcr,
    ttl,
)
from nod_bench.replay import Arm, RunResult

BOOTSTRAP_REPLICATES: Final = 10_000
"""`B` for the clip bootstrap (ADR-019)."""

BOOTSTRAP_SEED: Final = 20260917
"""Fixed, so the chart is reproducible from the same observations."""

CI_PERCENTILES: Final = (2.5, 97.5)
"""A 95 % percentile interval."""

BAR_LABEL: Final = (
    "bars: 95% bootstrap CI over clips (B=10,000) - NOT over repeats, "
    "which are zero-width on a deterministic simulator"
)
"""Rendered into the chart itself, not into a caption.

A caption does not survive a screenshot and a zero-width bar reads as precision
(ADR-019). Whoever sees the image sees what the bars mean.
"""

README_TABLE_START: Final = "<!-- BENCH_TABLE_START -->"
README_TABLE_END: Final = "<!-- BENCH_TABLE_END -->"
"""The machine-owned region of the README. INV-9's enforcement point."""


class ArmPoint(BaseModel):
    """One arm plotted on the headline chart (BENCH_SPEC.md §6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: Arm
    pcr: float
    ttl_p90_ms: float
    pcr_ci: tuple[float, float]
    ttl_p90_ci_ms: tuple[float, float]
    """Bootstrap intervals over clips, not interquartile ranges over repeats.

    Named `_ci` rather than `_iqr` because the two are not interchangeable and
    the earlier name would have described the wrong statistic (ADR-019).
    """

    n_clips: int
    frag: float


def bootstrap_points(
    by_arm: dict[Arm, list[ClipObservation]],
    *,
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> list[ArmPoint]:
    """Point estimates with clip-bootstrap intervals. `O(B * arms * clips)`.

    Clips are resampled **jointly across arms** inside each replicate, so the
    paired structure BENCH_SPEC §9 relies on survives and an arm-to-arm
    difference could be bootstrapped the same way.

    Args:
        by_arm: Observations per arm, all over the same clips in the same order.
        replicates: `B`.
        seed: Fixed so the chart is reproducible.

    Returns:
        One point per arm.

    Raises:
        ValueError: The arms do not cover the same clips.
    """
    arms = list(by_arm)
    if not arms:
        msg = "no arms to plot"
        raise ValueError(msg)
    n_clips = len(by_arm[arms[0]])
    for arm in arms:
        if [o.clip_id for o in by_arm[arm]] != [o.clip_id for o in by_arm[arms[0]]]:
            msg = f"arm {arm!r} was not run over the same clips, so pairing is lost"
            raise ValueError(msg)

    rng = np.random.default_rng(seed)
    draws = rng.integers(0, n_clips, size=(replicates, n_clips))
    samples: dict[Arm, tuple[list[float], list[float]]] = {
        arm: ([], []) for arm in arms
    }
    for replicate in draws:
        for arm in arms:
            picked = [by_arm[arm][int(i)] for i in replicate]
            samples[arm][0].append(pcr(picked))
            samples[arm][1].append(ttl(picked).p90)

    points: list[ArmPoint] = []
    for arm in arms:
        pcr_draws, ttl_draws = samples[arm]
        lo, hi = CI_PERCENTILES
        points.append(
            ArmPoint(
                arm=arm,
                pcr=pcr(by_arm[arm]),
                ttl_p90_ms=ttl(by_arm[arm]).p90,
                pcr_ci=(
                    float(np.percentile(pcr_draws, lo)),
                    float(np.percentile(pcr_draws, hi)),
                ),
                ttl_p90_ci_ms=(
                    float(np.percentile(ttl_draws, lo)),
                    float(np.percentile(ttl_draws, hi)),
                ),
                n_clips=n_clips,
                frag=frag(by_arm[arm]),
            )
        )
    return points


def pareto_svg(points: Sequence[ArmPoint]) -> str:
    """Render the headline chart as hand-rolled SVG.

    PCR on the y axis, TTL p90 on the x axis. The three static arms trace the
    tradeoff curve; Nod is a point with error bars. Hand-rolled rather than
    charted because Recharts styling fights the glass system (CLAUDE.md §3).

    Args:
        points: One point per arm.

    Returns:
        The SVG document.
    """
    width, height = 720, 460
    left, right, top, bottom = 82, 28, 56, 96
    xs = [p.ttl_p90_ci_ms[1] for p in points] + [p.ttl_p90_ms for p in points]
    x_max = max(xs) * 1.08 if xs else 1.0
    y_max = 1.0

    def sx(value: float) -> float:
        return left + (value / x_max) * (width - left - right)

    def sy(value: float) -> float:
        return top + (1.0 - value / y_max) * (height - top - bottom)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family="ui-sans-serif, system-ui, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#0b0f14"/>',
        f'<text x="{left}" y="26" fill="#e6edf3" font-size="15" '
        f'font-weight="600">Premature cutoff rate vs turn latency</text>',
        f'<text x="{left}" y="44" fill="#f0b429" font-size="11">'
        f"SIMULATED - not a measurement of AssemblyAI</text>",
    ]
    parts.append(
        f'<line x1="{left}" y1="{sy(0)}" x2="{width - right}" y2="{sy(0)}" '
        f'stroke="#30363d"/>'
    )
    parts.append(
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{sy(0)}" stroke="#30363d"/>'
    )
    parts.extend(
        f'<text x="{left - 10}" y="{sy(tick) + 4}" fill="#8b949e" '
        f'font-size="10" text-anchor="end">{tick:.2f}</text>'
        for tick in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    for point in points:
        x, y = sx(point.ttl_p90_ms), sy(point.pcr)
        parts.append(
            f'<line x1="{sx(point.ttl_p90_ci_ms[0])}" y1="{y}" '
            f'x2="{sx(point.ttl_p90_ci_ms[1])}" y2="{y}" stroke="#58a6ff" '
            f'stroke-width="2" opacity="0.75"/>'
        )
        parts.append(
            f'<line x1="{x}" y1="{sy(point.pcr_ci[0])}" x2="{x}" '
            f'y2="{sy(point.pcr_ci[1])}" stroke="#58a6ff" stroke-width="2" '
            f'opacity="0.75"/>'
        )
        parts.append(f'<circle cx="{x}" cy="{y}" r="5" fill="#58a6ff"/>')
        parts.append(
            f'<text x="{x + 10}" y="{y - 8}" fill="#e6edf3" font-size="11">'
            f"{point.arm}</text>"
        )
    parts.append(
        f'<text x="{(left + width - right) / 2}" y="{height - 58}" '
        f'fill="#8b949e" font-size="11" text-anchor="middle">'
        f"TTL p90 (ms), {QUANTILE_METHOD}</text>"
    )
    parts.append(
        f'<text x="18" y="{(top + sy(0)) / 2}" fill="#8b949e" font-size="11" '
        f'text-anchor="middle" transform="rotate(-90 18 {(top + sy(0)) / 2})">'
        f"PCR</text>"
    )
    parts.append(
        f'<text x="{left}" y="{height - 34}" fill="#8b949e" font-size="10">'
        f"{BAR_LABEL}</text>"
    )
    n = points[0].n_clips if points else 0
    parts.append(
        f'<text x="{left}" y="{height - 18}" fill="#8b949e" font-size="10">'
        f"n={n} clips, one synthetic voice: the interval is a lower bound on "
        f"uncertainty (ADR-018, ADR-019)</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts)


def render_results_md(points: Sequence[ArmPoint]) -> str:
    """Render the arm table with its bootstrap intervals.

    Intervals are over clips, not repeats (ADR-019). An interval wider than the
    arm-to-arm difference is reported as inconclusive, in those words (EC-38).
    If Track A and Track C disagree in direction, both are reported prominently
    (EC-41).

    Args:
        points: One point per arm, with bootstrap intervals.

    Returns:
        The markdown document.
    """
    lines = [
        "| arm | PCR | 95% CI | TTL p90 (ms) | 95% CI | FRAG |",
        "|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| `{point.arm}` | {point.pcr:.3f} | "
        f"[{point.pcr_ci[0]:.3f}, {point.pcr_ci[1]:.3f}] | "
        f"{point.ttl_p90_ms:.0f} | "
        f"[{point.ttl_p90_ci_ms[0]:.0f}, {point.ttl_p90_ci_ms[1]:.0f}] | "
        f"{point.frag:.3f} |"
        for point in points
    )
    lines.append("")
    lines.append(f"{BAR_LABEL}.")
    lines.append(
        f"Percentiles are {QUANTILE_METHOD}. n={points[0].n_clips if points else 0} "
        f"clips from one synthetic voice; the interval is a lower bound on "
        f"uncertainty (ADR-018, ADR-019)."
    )
    return "\n".join(lines)


def render_all(
    by_arm: dict[Arm, list[ClipObservation]],
    *,
    out: Path,
    simulated: bool,
    manifest: RunManifest,
) -> list[Path]:
    """Write every artifact for one run, each labelled with its provenance.

    ADR-016 and ADR-017: `simulated` appears in the filename as well as in the
    manifest, because a chart leaves the repository as an image and a caption
    does not survive a screenshot.

    Args:
        by_arm: Observations per arm over the same clips.
        out: Output directory.
        simulated: Whether a simulator produced these.
        manifest: The run manifest, written alongside.

    Returns:
        The paths written.
    """
    out.mkdir(parents=True, exist_ok=True)
    points = bootstrap_points(by_arm)
    written: list[Path] = []
    for stem, suffix, payload in (
        ("pareto", "svg", pareto_svg(points)),
        ("results", "md", render_results_md(points)),
        ("manifest", "json", manifest.model_dump_json(indent=1)),
    ):
        path = out / artifact_name(stem, simulated=simulated, suffix=suffix)
        path.write_text(payload)
        written.append(path)
    tag = SIMULATED_TAG if simulated else LIVE_TAG
    unlabelled = [p.name for p in written if tag not in p.name]
    if unlabelled:
        msg = f"artifacts escaped without a provenance tag: {unlabelled}"
        raise AssertionError(msg)
    return written


def render_report_card(runs: Sequence[RunResult]) -> str:
    """Render the one-page shareable report card.

    A single self-contained HTML file: the Pareto chart, the arm table with IQRs,
    ablation deltas, the corpus manifest and the honest-scope section. No
    external assets, no network on open (BENCH_SPEC.md §8).

    Args:
        runs: Every run in the sweep.

    Returns:
        The HTML document.
    """
    raise NotImplementedError


def update_readme_table(readme: Path, table: str) -> None:
    """Replace the machine-owned region of the README with a generated table.

    Writes between `README_TABLE_START` and `README_TABLE_END` and touches
    nothing else. This is how INV-9 is kept: the table region is owned by
    `make bench`, not by whoever is editing prose.

    Args:
        readme: Path to the README.
        table: The generated markdown table.
    """
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    """Run the report CLI.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit code.
    """
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
