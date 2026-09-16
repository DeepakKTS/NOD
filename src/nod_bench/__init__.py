"""Deterministic harness that measures whether the controller helped.

The benchmark is not optional garnish (CLAUDE.md §1). A claim in the README that
`make bench` cannot regenerate does not go in the README.

Read docs/BENCH_SPEC.md before changing anything here.
"""

from __future__ import annotations

__all__ = ["corpus", "metrics", "perturb", "replay", "report"]
