"""Render the results table, the Pareto chart and the report card.

INV-9: no number in any document is hand-written. Every figure in the README,
docs/RESULTS.md or a slide is generated here from committed traces.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from nod_bench.replay import Arm, RunResult

README_TABLE_START: Final = "<!-- BENCH_TABLE_START -->"
README_TABLE_END: Final = "<!-- BENCH_TABLE_END -->"
"""The machine-owned region of the README. INV-9's enforcement point."""


class ArmPoint(BaseModel):
    """One arm plotted on the headline chart (BENCH_SPEC.md §6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: Arm
    pcr: float
    ttl_p90_ms: float
    pcr_iqr: tuple[float, float]
    ttl_p90_iqr_ms: tuple[float, float]


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
    raise NotImplementedError


def render_results_md(runs: Sequence[RunResult]) -> str:
    """Render docs/RESULTS.md: the arm table with IQRs and the Pareto chart.

    A result with IQR larger than the arm-to-arm difference is reported as
    inconclusive, in those words (EC-38). If Track A and Track C disagree in
    direction, both are reported prominently (EC-41).

    Args:
        runs: Every run in the sweep.

    Returns:
        The markdown document.
    """
    raise NotImplementedError


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
