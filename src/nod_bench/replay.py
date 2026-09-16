"""Run a (corpus, arm) matrix against a real or fake upstream.

BENCH_SPEC.md §4. Audio must be fed in real time: dumping a wav into the socket
at once destroys every silence in it and makes the measurement meaningless.

The paced feeder itself and the run-matrix driver land in Phase 1 part two as
`feeder.py` and `run.py` (docs/PROMPTS.md P3).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

FRAME_MS: Final = 50
"""Feeder frame size, PCM16 (BENCH_SPEC.md §4). Milliseconds."""

MAX_DRIFT_MS: Final = 25
"""Abort above this cumulative drift; a drifted run is void (EC-37). Milliseconds."""

DEFAULT_REPEATS: Final = 5
"""`N` per (clip, arm) pair. Report median and IQR, not a single value."""

type Arm = Literal[
    "aggressive",
    "balanced",
    "conservative",
    "nod",
    "nod-nocontext",
    "nod-nospeaker",
    "oracle",
]
"""The seven arms of BENCH_SPEC.md §3. `balanced` is the headline baseline."""


class RunResult(BaseModel):
    """One (clip, arm) result across `N` repeats."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    clip_id: str
    arm: Arm
    repeats: int
    trace_paths: Sequence[Path]


async def replay(
    clip: Path,
    arm: Arm,
    *,
    endpoint: str,
    repeats: int = DEFAULT_REPEATS,
) -> RunResult:
    """Replay one clip through one arm, in paced real time.

    Frames are emitted on a monotonic deadline schedule, never `sleep(0.05)` in a
    loop, so jitter does not accumulate. Actual send timestamps are recorded, and
    the run aborts above `MAX_DRIFT_MS` cumulative drift (EC-37).

    Results are cached on `sha256(audio, config, code_version)` under `.nodcache/`
    with atomic writes only, so changing one arm re-runs only that arm (EC-44).

    Args:
        clip: The audio clip.
        arm: Which configuration to run.
        endpoint: Upstream URL; `FakeAssemblyAI` for offline runs.
        repeats: Repeats per pair.

    Returns:
        The run result.
    """
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    """Run the replay CLI.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit code.
    """
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
