"""Nod controller: profiler, policy, arbiter and the session proxy.

The controller is the product (CLAUDE.md §3), so this package is the one held to
strict typing with no `Any`, and the one the 85 % coverage gate measures.

Read docs/CONTROL_SPEC.md before changing anything here.
"""

from __future__ import annotations

__all__ = [
    "arbiter",
    "capabilities",
    "config",
    "policy",
    "profiler",
    "proxy",
    "trace",
    "types",
]
