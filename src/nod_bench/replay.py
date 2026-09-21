"""Run a (corpus, arm) matrix against a real or fake upstream.

BENCH_SPEC.md §4. Audio must be fed in real time: dumping a wav into the socket
at once destroys every silence in it and makes the measurement meaningless.

The paced feeder itself and the run-matrix driver land in Phase 1 part two as
`feeder.py` and `run.py` (docs/PROMPTS.md P3).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

import numpy as np
import soundfile as sf
from pydantic import BaseModel, ConfigDict

from nod_bench.corpus import BuiltCorpus, GeneratedClip
from nod_bench.fake_assemblyai import Endpointer
from nod_bench.metrics import ClipObservation, ScoredUtterance
from nod_core.arbiter import (
    BASE_MAX_MS,
    BASE_MIN_MS,
    DEFAULT_CEILING_MS,
    Arbiter,
    ArbiterInput,
)
from nod_core.capabilities import UPDATABLE_FIELDS
from nod_core.policy import CompiledPolicy
from nod_core.profiler import Profiler
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    ExpectedAnswer,
    KnobVerdict,
    Turn,
    TurnConfig,
    WindowHint,
    Word,
)

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
    silence_starts: list[float] = []
    for frame in frames_of(clip.audio_path):
        boundary = endpointer.feed(frame)
        if boundary is not None:
            fired.append(boundary.fired_at_ms)
            silence_starts.append(boundary.silence_started_ms)
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
        emitted_silence_start_ms=tuple(silence_starts),
    )


@dataclass(frozen=True, slots=True)
class NodAxes:
    """Which of the controller's two axes an arm is allowed to use.

    BENCH_SPEC §3's ablations are implemented by **neutralising an input**, not by
    a second code path. `nod-nospeaker` drives the same `decide` with the profile
    reported `cold`, which is exactly what CONTROL_SPEC §4 does when the profiler
    has too few gaps; `nod-nocontext` drives it with the neutral hint a policy's
    `default` supplies. An ablation that ran different code would measure the
    difference between two implementations rather than between two axes.
    """

    speaker: bool
    context: bool


NOD_ARMS: Final = {
    "nod": NodAxes(speaker=True, context=True),
    "nod-nocontext": NodAxes(speaker=True, context=False),
    "nod-nospeaker": NodAxes(speaker=False, context=True),
}
"""The controlled arms of BENCH_SPEC §3, and their two ablations."""

NOD_CAPABILITIES: Final = Capabilities(
    knobs=tuple((field, KnobVerdict.LIVE) for field in UPDATABLE_FIELDS),
    confidence_field=ConfidenceField.VARYING,
    force_endpoint=KnobVerdict.LIVE,
    has_word_timings=True,
)
"""What ADR-001 measured, so the capability gate passes both silence knobs.

`end_of_turn_confidence_threshold` is marked `LIVE` here and still never sent:
§4 does not emit it (ADR-011). Marking it live rather than `INERT` keeps the
gate from being the reason it is absent, so a law that started emitting it would
show up on the chart instead of being silently filtered.
"""


def _axes_label(axes: NodAxes) -> str:
    """Name the axes an arm uses, for the run manifest. `O(1)`."""
    names = [
        name
        for name, on in (("speaker", axes.speaker), ("context", axes.context))
        if on
    ]
    return "+".join(names) or "none"


@dataclass(frozen=True, slots=True)
class NodRun:
    """One clip through one controlled arm, plus what the controller did."""

    observation: ClipObservation
    patches: int
    turns: int
    final_min_ms: int
    final_max_ms: int


class MissingWordsError(RuntimeError):
    """A clip has no transcribed words, so a controlled arm cannot be run."""


def _turn_from_words(
    clip: GeneratedClip, consumed: int, until_ms: float, turn_order: int
) -> tuple[Turn | None, int]:
    """Replay the `Turn` the upstream sent for one boundary. `O(ΔW)`.

    **This replays; it does not reconstruct.** Until ADR-031 the sidecar carried
    only gaps, so this function inverted a gap list into a word sequence — one
    synthetic word per gap, text `w0, w1, …` — which was faithful on the pause
    axis and silent on every other. §2.3's adjacent repeats, filler set and
    duration outliers scored 0 on every clip of every run because no placeholder
    token is ever equal to its predecessor or a member of `FILLER_TOKENS`, and
    that was a floor on what the controller could demonstrate (ADR-028).

    The sidecar now carries the service's own `words[].start`, `words[].end` and
    text, so the words are handed to the profiler as they arrived. Every
    inter-word gap reaches the estimator at its true duration, including the ones
    no energy threshold could see, and the tokens are real.

    Args:
        clip: The clip and its ground truth.
        consumed: Words already delivered by earlier turns.
        until_ms: The boundary this turn ends at.
        turn_order: 1-based turn index.

    Returns:
        The turn and the new `consumed` index, or `(None, consumed)` when this
        boundary added no new words.

    Raises:
        MissingWordsError: the clip has no transcribed words at all.
    """
    if not clip.truth.words:
        msg = (
            f"{clip.clip_id}: the sidecar carries no transcribed words. Run "
            f"`python -m nod_bench.transcribe --corpus <dir>` first (ADR-031). "
            f"Reconstructing them from gaps is what that ADR removed."
        )
        raise MissingWordsError(msg)

    taken = [w for w in clip.truth.words[consumed:] if w.end_ms <= until_ms]
    if not taken:
        return None, consumed
    words = tuple(
        Word(
            text=word.text,
            start_ms=word.start_ms,
            end_ms=word.end_ms,
            confidence=0.9,
            is_final=True,
        )
        for word in taken
    )
    return (
        Turn(
            turn_order=turn_order,
            end_of_turn=True,
            end_of_turn_confidence=0.4,
            transcript=" ".join(word.text for word in words),
            words=words,
        ),
        consumed + len(taken),
    )


def run_nod_clip(
    clip: GeneratedClip,
    arm: Arm,
    *,
    endpoint_overhead_ms: float = 0.0,
    policy: CompiledPolicy | None = None,
    expected_answer: ExpectedAnswer | None = None,
) -> NodRun:
    """Run one clip through a controlled arm, reconfiguring mid-clip. `O(frames)`.

    This is the closed loop, and the mid-clip reconfiguration is the whole point:
    the arm starts at `BASE_MIN_MS` / `BASE_MAX_MS` and every emitted
    `ConfigPatch` is written onto the live endpointer, exactly as the proxy writes
    it onto a live socket. An arm that configured once at the start would be a
    fourth static arm wearing the controller's name.

    Args:
        clip: The generated clip and its ground truth.
        arm: One of `NOD_ARMS`.
        endpoint_overhead_ms: ADR-017's parameter; 0 fires early.
        policy: The compiled context policy. Required for the context axis.
        expected_answer: The host's declared dialogue state for this clip.

    Returns:
        The observation and what the controller did.
    """
    axes = NOD_ARMS[arm]
    endpointer = Endpointer(
        gaps=clip.truth.gaps,
        min_turn_silence=BASE_MIN_MS,
        max_turn_silence=BASE_MAX_MS,
        endpoint_overhead_ms=endpoint_overhead_ms,
    )
    profiler = Profiler()
    controller = Arbiter(capabilities=NOD_CAPABILITIES, ceiling_ms=DEFAULT_CEILING_MS)
    current = TurnConfig(
        min_turn_silence_ms=BASE_MIN_MS,
        max_turn_silence_ms=BASE_MAX_MS,
        end_of_turn_confidence_threshold=0.0,
        vad_threshold=None,
    )
    neutral = WindowHint(min_mult=1.0, max_mult=1.0)
    hint = neutral
    if axes.context and policy is not None:
        hint = policy.hint_for(expected_answer)

    fired: list[float] = []
    silence_starts: list[float] = []
    consumed = 0
    patches = 0
    turns = 0
    for frame in frames_of(clip.audio_path):
        boundary = endpointer.feed(frame)
        if boundary is None:
            continue
        fired.append(boundary.fired_at_ms)
        silence_starts.append(boundary.silence_started_ms)
        turns += 1
        turn, consumed = _turn_from_words(clip, consumed, boundary.fired_at_ms, turns)
        if turn is None:
            continue
        profiler.observe_turn(turn)
        features = profiler.features()
        if not axes.speaker:
            # The ablation: report the profile cold, which is what §4 already does
            # below MIN_GAPS_FOR_WARM, so the speaker axis is skipped by the law
            # rather than by a branch in the harness.
            features = replace(features, cold=True)
        patch = controller.decide(
            ArbiterInput(
                features=features,
                hint=hint,
                expected_answer=expected_answer if axes.context else None,
                current=current,
                capabilities=NOD_CAPABILITIES,
                ceiling_ms=DEFAULT_CEILING_MS,
                turn_order=turns,
                t_ms=int(boundary.fired_at_ms),
                host_override_fields=frozenset(),
                patches_sent=patches,
            )
        )
        if patch is None:
            continue
        patches += 1
        current = patch.config
        endpointer.min_turn_silence = float(current.min_turn_silence_ms)
        endpointer.max_turn_silence = float(current.max_turn_silence_ms)

    return NodRun(
        observation=ClipObservation(
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
            emitted_silence_start_ms=tuple(silence_starts),
        ),
        patches=patches,
        turns=turns,
        final_min_ms=current.min_turn_silence_ms,
        final_max_ms=current.max_turn_silence_ms,
    )


def run_matrix(
    corpus: BuiltCorpus,
    arms: Sequence[Arm],
    *,
    endpoint_overhead_ms: float = 0.0,
    policy: CompiledPolicy | None = None,
    expected_answer: ExpectedAnswer | None = None,
) -> dict[Arm, list[ClipObservation]]:
    """Run every clip through every arm. `O(arms * frames)`.

    The same clips through every arm, so downstream comparisons are paired
    (BENCH_SPEC §9).
    """
    out: dict[Arm, list[ClipObservation]] = {}
    for arm in arms:
        if arm in NOD_ARMS:
            out[arm] = [
                run_nod_clip(
                    clip,
                    arm,
                    endpoint_overhead_ms=endpoint_overhead_ms,
                    policy=policy,
                    expected_answer=expected_answer,
                ).observation
                for clip in corpus.clips
            ]
        else:
            out[arm] = [
                run_clip(clip, arm, endpoint_overhead_ms=endpoint_overhead_ms)
                for clip in corpus.clips
            ]
    return out


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
        certain_utterances,
        frag,
        pcr,
    )
    from nod_bench.report import render_all
    from nod_core.arbiter import MAX_PATCHES

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
    arms: list[Arm] = [
        "aggressive",
        "balanced",
        "conservative",
        "nod",
        "nod-nocontext",
        "nod-nospeaker",
    ]
    out.write(
        f"{len(corpus.clips)} clips x {len(arms)} arms, simulated, "
        f"{SIMULATED_REPEATS} repeat.\n"
    )
    by_arm = run_matrix(corpus, arms, endpoint_overhead_ms=args.overhead_ms)

    # The patch census. §5 caps a session at MAX_PATCHES and Gate 4 observed that
    # the cap may be unreachable once hysteresis has converged, so the question is
    # answered with a count rather than an argument.
    census = {
        arm: [
            run_nod_clip(clip, arm, endpoint_overhead_ms=args.overhead_ms)
            for clip in corpus.clips
        ]
        for arm in arms
        if arm in NOD_ARMS
    }
    for arm, runs in census.items():
        counts = sorted(run.patches for run in runs)
        turns = sorted(run.turns for run in runs)
        out.write(
            f"  {arm}: patches/clip min={counts[0]} median="
            f"{counts[len(counts) // 2]} max={counts[-1]} "
            f"(cap {MAX_PATCHES}); turns/clip median={turns[len(turns) // 2]}\n"
        )
    (args.out / "patch_census.simulated.json").write_text(
        json.dumps(
            {
                arm: {
                    "patches": [run.patches for run in runs],
                    "turns": [run.turns for run in runs],
                    "final_min_ms": [run.final_min_ms for run in runs],
                    "final_max_ms": [run.final_max_ms for run in runs],
                    "cap": MAX_PATCHES,
                }
                for arm, runs in census.items()
            },
            indent=1,
        )
    )

    everything = [o for obs in by_arm.values() for o in obs]
    # ADR-036: an empty certain scope is the absence of a measurement, recorded
    # as `None` rather than as a float. `pcr` and `frag` still raise on it —
    # "PCR of nothing is not 0.0" is their job and is not softened here — so the
    # guard lives in the caller, which is the only place that knows a missing
    # column is reportable rather than fatal.
    certain_n = certain_utterances(by_arm[arms[0]])
    try:
        certain_pcr: float | None = pcr(everything, certain_only=True)
        certain_frag: float | None = frag(everything, certain_only=True)
    except ValueError:
        certain_pcr, certain_frag = None, None

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
                    if arm in STATIC_ARMS
                    else 0.0
                ),
                min_turn_silence=(
                    STATIC_ARMS[arm].min_turn_silence
                    if arm in STATIC_ARMS
                    else BASE_MIN_MS
                ),
                max_turn_silence=(
                    STATIC_ARMS[arm].max_turn_silence
                    if arm in STATIC_ARMS
                    else BASE_MAX_MS
                ),
                source=(
                    "AssemblyAI turn-detection docs, accessed 2026-09-17, BENCH_SPEC §3"
                    if arm in STATIC_ARMS
                    else (
                        f"nod controller, CONTROL_SPEC §4, axes="
                        f"{_axes_label(NOD_ARMS[arm])}"
                        f"; min/max above are the STARTING config, not a fixed one"
                    )
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
