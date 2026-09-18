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

import numpy as np
import soundfile as sf
from pydantic import BaseModel, ConfigDict

from nod_bench.corpus import BuiltCorpus, GeneratedClip
from nod_bench.fake_assemblyai import Endpointer
from nod_bench.metrics import ClipObservation, ScoredUtterance

FRAME_MS: Final = 50
"""Feeder frame size, PCM16 (BENCH_SPEC.md §4). Milliseconds."""

MAX_DRIFT_MS: Final = 25
"""Abort above this cumulative drift; a drifted run is void (EC-37). Milliseconds."""

DEFAULT_REPEATS: Final = 5
"""`N` per (clip, arm) pair on a **live** upstream, which is non-deterministic.

Against the simulator this is 1, and forcing it higher would be worse than
useless: `FakeAssemblyAI` is deterministic, so repeats are byte-identical and
their spread is exactly zero. Reporting that spread as an interval would present
determinism as precision (ADR-017, ADR-019).
"""

SIMULATED_REPEATS: Final = 1
"""Repeats against the simulator. See `DEFAULT_REPEATS`."""


class ArmSettings(BaseModel):
    """One arm's turn-detection configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    end_of_turn_confidence_threshold: float
    min_turn_silence: int
    max_turn_silence: int


STATIC_ARMS: Final = {
    "aggressive": ArmSettings(
        end_of_turn_confidence_threshold=0.4,
        min_turn_silence=160,
        max_turn_silence=400,
    ),
    "balanced": ArmSettings(
        end_of_turn_confidence_threshold=0.4,
        min_turn_silence=400,
        max_turn_silence=1280,
    ),
    "conservative": ArmSettings(
        end_of_turn_confidence_threshold=0.7,
        min_turn_silence=800,
        max_turn_silence=3600,
    ),
}
"""AssemblyAI's published quick-start presets, transcribed (BENCH_SPEC §3).

Not chosen by us, and cited there with the access date. `balanced` is also the
documented global default, which is why it is the headline baseline.
"""

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


def frames_of(clip: Path, *, frame_ms: int = FRAME_MS) -> list[bytes]:
    """Read a clip as PCM16 frames. `O(n)`.

    Whole frames only: a trailing partial frame would be fed as a short block and
    counted as a full one by the endpointer, shifting every later timestamp.
    """
    audio, sample_rate = sf.read(clip, dtype="float32")
    step = int(sample_rate * frame_ms / 1000) * 2
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return [pcm[at : at + step] for at in range(0, len(pcm) - step + 1, step)]


def run_clip(
    clip: GeneratedClip, arm: Arm, *, endpoint_overhead_ms: float = 0.0
) -> ClipObservation:
    """Run one clip through one arm against the simulator. `O(frames)`.

    Not paced. BENCH_SPEC §4's real-time feeding exists because a live socket
    measures wall-clock arrival; the simulator consumes frames on a stream clock
    and would produce identical output at any wall-clock rate. Feeding it in real
    time would add 20 minutes to `make bench` and change no number. The paced
    feeder is still what the live path uses.

    Args:
        clip: The generated clip and its ground truth.
        arm: Which static arm to configure.
        endpoint_overhead_ms: ADR-017's parameter; 0 fires early.

    Returns:
        What the arm did, ready for scoring.
    """
    settings = STATIC_ARMS[arm]
    endpointer = Endpointer(
        gaps=clip.truth.gaps,
        min_turn_silence=settings.min_turn_silence,
        max_turn_silence=settings.max_turn_silence,
        end_of_turn_confidence_threshold=(settings.end_of_turn_confidence_threshold),
        endpoint_overhead_ms=endpoint_overhead_ms,
    )
    fired: list[float] = []
    for frame in frames_of(clip.audio_path):
        boundary = endpointer.feed(frame)
        if boundary is not None:
            fired.append(boundary.fired_at_ms)
    return ClipObservation(
        clip_id=clip.clip_id,
        arm=arm,
        utterances=(
            ScoredUtterance(
                start_ms=0,
                final_word_end_ms=clip.final_word_end_ms,
                gaps=clip.truth.gaps,
            ),
        ),
        emitted_end_ms=tuple(fired),
    )


def run_matrix(
    corpus: BuiltCorpus,
    arms: Sequence[Arm],
    *,
    endpoint_overhead_ms: float = 0.0,
) -> dict[Arm, list[ClipObservation]]:
    """Run every clip through every arm. `O(arms * frames)`.

    The same clips through every arm, so downstream comparisons are paired
    (BENCH_SPEC §9).
    """
    return {
        arm: [
            run_clip(clip, arm, endpoint_overhead_ms=endpoint_overhead_ms)
            for clip in corpus.clips
        ]
        for arm in arms
    }


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
    import argparse
    import json

    from nod_bench.metrics import (
        QUANTILE_METHOD,
        ArmConfig,
        ProxyDivergence,
        RunManifest,
        frag,
        pcr,
    )
    from nod_bench.report import render_all

    parser = argparse.ArgumentParser(prog="python -m nod_bench.replay")
    parser.add_argument(
        "--fake", action="store_true", help="run against the simulator (ADR-017)"
    )
    parser.add_argument("--live", action="store_true", help="not implemented yet")
    parser.add_argument("--corpus", type=Path, default=Path("data/corpus/trackA"))
    parser.add_argument("--out", type=Path, default=Path("bench/runs"))
    parser.add_argument(
        "--overhead-ms",
        type=float,
        default=0.0,
        help="ADR-017's endpoint overhead; 0 means unmeasured and fires early",
    )
    args = parser.parse_args(argv)
    out = sys.stdout

    if args.live:
        out.write(
            "--live is not implemented. BENCH_SPEC §4 reserves live runs for the\n"
            "published table at Phase 4; this driver runs the simulator only.\n"
        )
        return 2

    corpus_file = args.corpus / "corpus.json"
    if not corpus_file.exists():
        out.write(
            f"no corpus at {corpus_file}.\n"
            f"Build one with `python -m nod_bench.corpus build`, which needs a\n"
            f"seed recording. The corpus is not committed (it is ~35 MB of audio)\n"
            f"and `say` is macOS-only, so this step does not yet run on a clean\n"
            f"clone. That is an open Phase 1 exit item, not a transient error.\n"
        )
        return 2

    corpus = BuiltCorpus.model_validate_json(corpus_file.read_text())
    arms: list[Arm] = ["aggressive", "balanced", "conservative"]
    out.write(
        f"{len(corpus.clips)} clips x {len(arms)} arms, simulated, "
        f"{SIMULATED_REPEATS} repeat.\n"
    )
    by_arm = run_matrix(corpus, arms, endpoint_overhead_ms=args.overhead_ms)

    everything = [o for obs in by_arm.values() for o in obs]
    try:
        certain_pcr = pcr(everything, certain_only=True)
        certain_frag = frag(everything, certain_only=True)
        certain_n = sum(1 for o in by_arm[arms[0]] for u in o.utterances if u.certain)
    except ValueError:
        certain_pcr, certain_frag, certain_n = float("nan"), float("nan"), 0

    manifest = RunManifest(
        seed=corpus.seed,
        generator_version=corpus.generator_version,
        corpus_id=corpus.corpus_id,
        corpus_sha256=corpus.corpus_sha256,
        corpus_clips=len(corpus.clips),
        repeats_per_arm=SIMULATED_REPEATS,
        arms=tuple(
            ArmConfig(
                name=arm,
                end_of_turn_confidence_threshold=(
                    STATIC_ARMS[arm].end_of_turn_confidence_threshold
                ),
                min_turn_silence=STATIC_ARMS[arm].min_turn_silence,
                max_turn_silence=STATIC_ARMS[arm].max_turn_silence,
                source=(
                    "AssemblyAI turn-detection docs, accessed 2026-09-17, BENCH_SPEC §3"
                ),
            )
            for arm in arms
        ),
        simulated=True,
        endpoint_overhead_ms=args.overhead_ms,
        quantile_method=QUANTILE_METHOD,
        proxy_divergence=ProxyDivergence(
            pcr_all=pcr(everything),
            pcr_certain_only=certain_pcr,
            frag_all=frag(everything),
            frag_certain_only=certain_frag,
            utterances_all=len(by_arm[arms[0]]),
            utterances_certain_only=certain_n,
        ),
    )

    written = render_all(by_arm, out=args.out, simulated=True, manifest=manifest)
    (args.out / "observations.simulated.json").write_text(
        json.dumps(
            {
                arm: [o.model_dump(mode="json") for o in obs]
                for arm, obs in by_arm.items()
            },
            indent=1,
        )
    )
    for path in written:
        out.write(f"  wrote {path}\n")
    out.write(f"  wrote {args.out / 'observations.simulated.json'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
