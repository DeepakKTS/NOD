"""Run a (corpus, arm) matrix against a real or fake upstream.

BENCH_SPEC.md §4. Audio must be fed in real time: dumping a wav into the socket
at once destroys every silence in it and makes the measurement meaningless.

The paced feeder itself and the run-matrix driver land in Phase 1 part two as
`feeder.py` and `run.py` (docs/PROMPTS.md P3).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Final, Literal, override

import numpy as np
import soundfile as sf
from pydantic import BaseModel, ConfigDict

from nod_adapters.assemblyai.session import AssemblyAISession, UpstreamError
from nod_bench.corpus import BuiltCorpus, GeneratedClip
from nod_bench.fake_assemblyai import Endpointer
from nod_bench.feeder import FeedReport, PacedFeeder
from nod_bench.metrics import (
    ArmConfig,
    ClipObservation,
    RunManifest,
    ScoredUtterance,
)
from nod_bench.probe import (
    PRIMARY_MODEL,
    STREAMING_USD_PER_HOUR,
    ProbeSession,
    SessionFactory,
)
from nod_bench.probe_clip import read_wav
from nod_core.arbiter import (
    BASE_MAX_MS,
    BASE_MIN_MS,
    DEFAULT_CEILING_MS,
    ENDPOINT_OVERHEAD_MS,
    Arbiter,
    ArbiterInput,
)
from nod_core.capabilities import MEASURED_CAPABILITIES
from nod_core.config import MAX_CONCURRENT_SESSIONS, UPSTREAM_CONCURRENCY_LIMIT
from nod_core.policy import CompiledPolicy, load_policy
from nod_core.profiler import Profiler
from nod_core.proxy import SessionProxy
from nod_core.trace import TraceSink

if TYPE_CHECKING:  # pragma: no cover - annotations only
    import argparse
    from typing import TextIO
from nod_core.types import (
    ConfigPatch,
    ExpectedAnswer,
    NodMode,
    SpeakerFeatures,
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

LIVE_ENDPOINT_LABEL: Final = "assemblyai-universal-streaming"
"""Recorded in `RunResult.run_id` for provenance, never a URL with a key in it."""

LIVE_CONCURRENCY: Final = MAX_CONCURRENT_SESSIONS
"""Default concurrent live sessions. Derived from the measured account limit.

`MAX_CONCURRENT_SESSIONS` is `UPSTREAM_CONCURRENCY_LIMIT // 2` because a *server*
session can hold two upstream sockets during rotation (ADR-042). A bench session
never rotates — every clip is under thirteen seconds and rotation fires near
`expires_at` — so the halving is not required here and this is the conservative
default rather than the derived one. `--concurrency` raises it up to the limit.
"""

CONCURRENCY_REFUSED_CODE: Final = 1008
"""AssemblyAI's `Error` code for "Too many concurrent sessions". Measured.

Recorded as a constant because the number is the *only* thing distinguishing an
over-subscribed sweep from a healthy one: the refusal arrives **after** a
successful WebSocket upgrade (ADR-042), so the socket, the handshake and the
session object all look fine.
"""


class LiveRunAbortedError(RuntimeError):
    """The upstream errored mid-clip, so this clip has no score. `O(1)`.

    Raised rather than returning the boundaries collected so far, which is the
    whole point. A clip that lost its socket half way through produces *fewer*
    boundaries, and fewer boundaries on a PCR denominator that comes from the
    sidecar reads as **an arm that cut nobody off** — the flattering direction,
    arrived at by the run failing rather than by the arm behaving.
    """


class ConcurrencyRefusedError(LiveRunAbortedError):
    """Error 1008: the account refused a session because too many were open.

    Fatal to the sweep, not just to the clip. An over-subscribed sweep does not
    recover on its own — every later clip is competing for the same slots — and
    each refusal costs a clip that scores as silence. So this aborts everything
    and exits non-zero rather than leaving a partially-scored corpus that looks
    complete.
    """


SLOT_RELEASE_LAG_S: Final = 30.0
"""How long the account keeps counting a session after the client closes it.

**Measured at Gate 4b, and it is the number that governs the whole run.** The
ramp in `scripts/probe_concurrency.py` established that 5 sessions can be *held
open simultaneously*. That is not the quantity a sweep is limited by: a sweep
churns, and closing a socket does not free the slot. Holding 5, closing one and
probing once after a fixed wait found the slot **still held at 1, 3, 8 and 20
seconds**; an earlier poll-until-free attempt saw it free at ~38 s, and that
figure is an upper bound because each poll opened its own socket and competed
for the slot it was measuring.

So: somewhere between 20 s and 40 s, and 30 is the working figure. It is stated
as a bound to plan against, not as a precise measurement, and the run is sized
so that being wrong by 50 % costs throughput rather than correctness.
"""

MIN_SESSION_INTERVAL_S: Final = SLOT_RELEASE_LAG_S / UPSTREAM_CONCURRENCY_LIMIT
"""Minimum wall-clock gap between two session *starts*. Seconds. 6.0.

**The constraint is a rate, not a count, and modelling it as a count is why
concurrency 4 and then 3 both failed.** If a slot is occupied for
`SLOT_RELEASE_LAG_S` after its session ends, then a workload starting sessions
faster than `limit / lag` accumulates counted-but-closed sessions until it
exceeds the limit, *whatever* the concurrency setting. At concurrency 3 with
~10 s clips the sweep started a session every ~3.7 s against a sustainable 6 s
and was refused within nine sessions.

A semaphore cannot express this: it bounds how many things run at once, and the
resource here is consumed after the thing has stopped running. So the sweep
holds a start-rate gate as well, and `--concurrency` becomes the *second*
binding constraint rather than the only one.
"""

TERMINATION_TIMEOUT_S: Final = 10.0
"""How long to wait for the upstream to finish after `Terminate`. Seconds.

Bounded rather than open-ended: the last turn's boundary can only arrive after
the widest arm's silence gate has elapsed, `conservative`'s 3600 ms, and a socket
that has said nothing for three times that has failed rather than stalled. An
unbounded wait here would hang a 3600-session sweep on one bad socket.
"""


class StartRateGate:
    """Serialises session *starts* to at most one per `interval_s`. `O(1)`.

    Not a semaphore. A semaphore bounds how many sessions are open; this bounds
    how fast new ones begin, which is the quantity `MIN_SESSION_INTERVAL_S`
    explains the account actually limits. Both are needed: the gate stops the
    sweep from accumulating counted-but-closed sessions, and the semaphore stops
    it from holding too many genuinely open at once.

    Single-lock rather than a token bucket: bursting is exactly what must not
    happen here, so there is no allowance to accumulate.
    """

    __slots__ = ("_interval_s", "_last", "_lock")

    def __init__(self, interval_s: float = MIN_SESSION_INTERVAL_S) -> None:
        """Start ungated: the first session waits for nothing."""
        self._interval_s = interval_s
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        """Block until another session may start."""
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self._interval_s - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


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
    """One (clip, arm) result across `N` repeats.

    Live repeats are **kept separate**, one `ClipObservation` per repeat, rather
    than pooled. ADR-045: on the simulated path repeats are byte-identical so
    only the clip axis carries spread; live they are not, and BENCH_SPEC §4 asks
    for a median and an interquartile range over exactly this axis.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    clip_id: str
    arm: Arm
    repeats: int
    trace_paths: Sequence[Path]

    observations: tuple[ClipObservation, ...] = ()
    """One per repeat, in order. Empty on a result that was never scored."""

    patches: tuple[int, ...] = ()
    """`UpdateConfiguration` frames that reached the socket, per repeat.

    ADR-027 and ADR-032 both defer to this: the session cap has never been
    observed to bind, and the decision to keep or retire it waits on a live count.
    Counted at the socket rather than at the decision, because a patch computed
    and never applied is the exact failure CLAUDE.md §5 names for `proxy.py`.
    """

    decide_ms: tuple[float, ...] = ()
    """Every `Arbiter.decide` sample across every repeat. Guards INV-2."""

    wall_s: tuple[float, ...] = ()
    """Wall-clock seconds per repeat. Provenance for the run, not a metric.

    Explicitly **not** TCT: see `metrics.tct`, which refuses. Kept because a
    repeat that ran materially longer than the clip is a drifted feeder, and
    EC-37 voids such a run.
    """


def frames_of(clip: Path, *, frame_ms: int = FRAME_MS) -> list[bytes]:
    """Read a clip as PCM16 frames. `O(n)`.

    Whole frames only: a trailing partial frame would be fed as a short block and
    counted as a full one by the endpointer, shifting every later timestamp.
    """
    audio, sample_rate = sf.read(clip, dtype="float32")
    step = int(sample_rate * frame_ms / 1000) * 2
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return [pcm[at : at + step] for at in range(0, len(pcm) - step + 1, step)]


def scored_utterances(clip: GeneratedClip) -> tuple[ScoredUtterance, ...]:
    """The clip's ground-truth utterances, one or many. Pure. `O(u·g)`.

    Track A clips hold one; Track C clips hold ten to twelve (ADR-034). A gap
    belongs to the utterance whose span contains its start, so PCR and TTL are
    scored per *turn* rather than per call.

    Args:
        clip: The clip and its sidecar.

    Returns:
        One `ScoredUtterance` per caller turn.
    """
    if not clip.utterances:
        return (
            ScoredUtterance(
                start_ms=0,
                final_word_end_ms=clip.final_word_end_ms,
                gaps=clip.truth.gaps,
            ),
        )
    bounds = [u.start_ms for u in clip.utterances] + [clip.total_ms]
    return tuple(
        ScoredUtterance(
            start_ms=span.start_ms,
            final_word_end_ms=span.final_word_end_ms,
            gaps=tuple(
                g for g in clip.truth.gaps if bounds[i] <= g.start_ms < bounds[i + 1]
            ),
        )
        for i, span in enumerate(clip.utterances)
    )


def expected_at(clip: GeneratedClip, t_ms: float) -> ExpectedAnswer | None:
    """The declared class of the turn covering `t_ms`. Pure. `O(u)`.

    Per turn, not per run (ADR-034). `hint_for(None)` is the policy default, so
    a run that passed one class for a whole call would give the context axis
    one input for eleven turns.

    Args:
        clip: The clip and its per-turn spans.
        t_ms: A stream-relative time inside the turn.

    Returns:
        The class declared for that turn's prompt, or `None`.
    """
    covering = [u for u in clip.utterances if u.start_ms <= t_ms]
    return covering[-1].expected_answer if covering else None


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
        utterances=scored_utterances(clip),
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

NOD_CAPABILITIES: Final = MEASURED_CAPABILITIES
"""ADR-001's measurement, imported rather than restated (see its docstring)."""


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

    def hint_and_class(at_ms: float) -> tuple[WindowHint, ExpectedAnswer | None]:
        """The context input for the turn covering `at_ms`. `O(u)`.

        Per turn (ADR-034). A clip with per-turn spans takes its class from the
        span; a Track A clip has none and falls back to the run-level
        `expected_answer`, which is the only class it has.
        """
        declared = expected_at(clip, at_ms) if clip.utterances else expected_answer
        if not axes.context:
            return neutral, None
        hint = policy.hint_for(declared) if policy is not None else neutral
        return hint, declared

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
        hint, declared = hint_and_class(turn.words[0].start_ms)
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
                expected_answer=declared,
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
            utterances=scored_utterances(clip),
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


def _arm_config(arm: Arm) -> ArmConfig:
    """One arm's manifest entry. Pure. `O(1)`.

    **Extracted at Gate 4a, and the reason is the point.** The live sweep needs
    the same entries as the simulated one, and CLAUDE.md §5 puts "the arm
    configurations" and "the provenance labelling" on the published-number path.
    Two copies of this would be two answers to "what was this arm configured
    with", drifting silently, on a label that appears in the manifest beside
    every figure. One function with two callers is the whole change.

    Args:
        arm: Any of `STATIC_ARMS` or `NOD_ARMS`.

    Returns:
        The manifest entry, with its provenance string.
    """
    static = STATIC_ARMS.get(arm)
    if static is not None:
        return ArmConfig(
            name=arm,
            end_of_turn_confidence_threshold=(static.end_of_turn_confidence_threshold),
            min_turn_silence=static.min_turn_silence,
            max_turn_silence=static.max_turn_silence,
            source=(
                "AssemblyAI turn-detection docs, accessed 2026-09-17, BENCH_SPEC §3"
            ),
        )
    return ArmConfig(
        name=arm,
        end_of_turn_confidence_threshold=0.0,
        min_turn_silence=BASE_MIN_MS,
        max_turn_silence=BASE_MAX_MS,
        source=(
            f"nod controller, CONTROL_SPEC §4, axes={_axes_label(NOD_ARMS[arm])}"
            f"; min/max above are the STARTING config, not a fixed one"
        ),
    )


# --- the live path (ADR-044, ADR-045) --------------------------------------
#
# Deliberately a **third** construction site for `ClipObservation`, beside
# `run_clip` and `run_nod_clip`, rather than a generalisation of either. Those
# two are on the mutation-guarded published-number path; folding a live branch
# into them would put live-path bugs inside a guarded path and make every
# simulated figure depend on code only the live path exercises. The duplication
# is the cheaper of the two costs and it is bounded — the *scoring* is shared,
# because `pcr`, `ttl` and `frag` take a `ClipObservation` and neither knows nor
# should know which driver produced it.


@dataclass(frozen=True, slots=True)
class LiveBoundary:
    """One `end_of_turn` observed on a real socket.

    The simulator hands back both fields from its own `Endpointer`, which watched
    the audio. Live, only the first is observed and the second is **derived**, and
    the difference matters enough to name here rather than in a comment further
    down.
    """

    fired_at_ms: float
    """Feeder stream clock when the `end_of_turn` frame arrived.

    Stream-relative, from `PacedFeeder.now_ms`, never `time.time()` (CLAUDE.md
    §6). This is the quantity TTL is measured to, so it has to be the arrival of
    the frame rather than the word timing inside it: the caller waits for the
    frame, not for the model's opinion about when they stopped talking.
    """

    silence_started_ms: float
    """Where the silence run preceding this boundary began. **Derived.**

    Taken as the last word's `end_ms` in the turn. The service does not report
    where it started counting silence, so this is the best available reading, and
    it is admissible under exactly the line ADR-031 drew: a word timing is the
    service *measuring the audio*, not the service *judging the answer*. It
    reaches PCR's arithmetic — where a gap is looked up (ADR-036) — and it cannot
    reach what counts as premature, because the gap's label is generator-owned.

    Two consequences to carry into any live figure. It arrives on the service's
    **80 ms grid** (ADR-031), coarser than the 50 ms acoustic frame the simulator
    uses. And a turn that finalises with no words has no derivable start, so the
    boundary is recorded with `silence_started_ms` equal to `fired_at_ms`, which
    `governing_certain` will read as covered by no gap and therefore ambiguous —
    the conservative direction.
    """

    turn_order: int
    words: tuple[Word, ...]


def is_caller_turn(turn: Turn) -> bool:
    """Whether a finalised turn is something the caller actually said. `O(1)`.

    **A finalised turn carrying no words is not a caller turn.** Measured at
    Gate 4b against the real service: feeding a 9274 ms clip whose speech ends at
    5024 ms produced turn 0 at 5628 ms with 17 words and the right transcript,
    then turn 1 at 9980 ms with **zero words and an empty transcript** — 706 ms
    after the last audio frame and immediately after our own `Terminate`. It is
    the service closing an open-but-empty turn at session end.

    Counting it corrupted two metrics at once and did so on every arm, which is
    why it did not look like a defect: FRAG was exactly 2.000 for all six arms
    (two emitted turns per one ground-truth utterance), and TTL p90 measured
    flush-minus-speech-end, about 4.9 s, instead of the real 376 ms. A number
    identical across arms invites the reading that no arm differs.

    **Filtering moves both figures in the flattering direction** — FRAG toward
    1.0, TTL p90 down by an order of magnitude — and that is named here rather
    than left for a reader to notice. It is done because the flush is
    demonstrably an artifact of the harness ending the session and not a
    behaviour of any arm, not because of where it moves the number. The count of
    what was dropped travels with the run (`LiveRun.flush_turns`) so the decision
    is auditable instead of invisible.

    An empty turn is also inert for all three metrics by construction: it cannot
    be a premature cut, because the caller said nothing to cut.

    Args:
        turn: A finalised upstream turn.

    Returns:
        Whether it carries any words.
    """
    return bool(turn.words)


def score_live_clip(
    clip: GeneratedClip, arm: Arm, boundaries: Sequence[LiveBoundary]
) -> ClipObservation:
    """Ground truth plus live boundaries, as one scoreable observation. `O(b)`.

    Pure. The third `ClipObservation` construction site (see the section note).

    Args:
        clip: The clip and its sidecar. Ground truth stays generator-owned.
        arm: The arm label this ran under.
        boundaries: Every `end_of_turn` the socket produced, in order.

    Returns:
        The observation, ready for `pcr`, `ttl` and `frag`.
    """
    return ClipObservation(
        clip_id=clip.clip_id,
        arm=arm,
        utterances=scored_utterances(clip),
        emitted_end_ms=tuple(b.fired_at_ms for b in boundaries),
        emitted_silence_start_ms=tuple(b.silence_started_ms for b in boundaries),
    )


class ClipContext:
    """The context axis for one clip, as `SessionProxy` wants it (ADR-044).

    Satisfies `nod_core.proxy.ContextSource`. Answers per *turn order*, because
    that is all the proxy knows about a live turn — it has no clip and no
    sidecar. The mapping from turn order to declared class is built here, where
    the sidecar is in scope.

    `enabled=False` is the `nod-nocontext` ablation, and it neutralises the
    **input** rather than skipping the call, exactly as `NodAxes` does in
    `run_nod_clip`: an ablation that ran different code would measure the
    difference between two implementations.
    """

    __slots__ = ("_by_turn", "_enabled", "_fallback", "_policy")

    def __init__(
        self,
        clip: GeneratedClip,
        *,
        policy: CompiledPolicy | None,
        enabled: bool,
        fallback: ExpectedAnswer | None = None,
    ) -> None:
        """Build the per-turn class map for one clip.

        Args:
            clip: The clip, for its per-turn spans.
            policy: The compiled policy. `None` leaves every hint neutral.
            enabled: False for the `nod-nocontext` ablation.
            fallback: The run-level class, for a clip with no per-turn spans.
        """
        self._policy = policy
        self._enabled = enabled
        self._fallback = fallback
        self._by_turn: dict[int, ExpectedAnswer | None] = {
            order: utterance.expected_answer
            for order, utterance in enumerate(clip.utterances, start=1)
        }

    def __call__(self, turn_order: int) -> ExpectedAnswer | None:
        """The declared class for this turn, or the run-level fallback."""
        if not self._enabled:
            return None
        return self._by_turn.get(turn_order, self._fallback)

    def hint_for(self, declared: ExpectedAnswer | None) -> WindowHint:
        """The multipliers for a class, or neutral without a policy."""
        if not self._enabled or self._policy is None:
            return WindowHint(min_mult=1.0, max_mult=1.0)
        return self._policy.hint_for(declared)


def frames_of_pcm(pcm: bytes, *, sample_rate: int) -> list[bytes]:
    """Split PCM16 into whole `FRAME_MS` frames. Pure. `O(n)`.

    Whole frames only, for the reason `frames_of` gives: a trailing partial frame
    is fed as a short block and counted as a full one, shifting every later
    timestamp.
    """
    step = int(sample_rate * FRAME_MS / 1000) * 2
    return [pcm[at : at + step] for at in range(0, len(pcm) - step + 1, step)]


@dataclass(frozen=True, slots=True)
class LiveRun:
    """What one live pass over one clip produced."""

    observation: ClipObservation
    patches: int
    turns: int
    decide_ms: tuple[float, ...]
    wall_s: float
    feed_report: FeedReport | None
    flush_turns: int = 0
    """Wordless finalised turns dropped by `is_caller_turn`. See there.

    Carried rather than discarded because dropping a boundary changes FRAG and
    TTL, and a silent filter on the input to a published number is the thing
    CLAUDE.md §5 is about. One per session is the expected shape — the
    `Terminate` flush — and a run reporting materially more has something else
    happening.
    """


async def run_live_clip(
    clip: GeneratedClip,
    arm: Arm,
    *,
    session_factory: SessionFactory,
    api_key: str,
    model: str = PRIMARY_MODEL,
    policy: CompiledPolicy | None = None,
    expected_answer: ExpectedAnswer | None = None,
    trace_dir: Path | None = None,
    ceiling_ms: int = DEFAULT_CEILING_MS,
) -> LiveRun:
    """Feed one clip to one upstream session, in paced real time. `O(frames)`.

    **Paced, unlike `run_clip`.** The simulator consumes frames on a stream clock
    and would produce identical output at any wall-clock rate; a real socket
    measures arrival, so dumping the wav destroys every silence in it and makes
    the measurement meaningless (BENCH_SPEC §4).

    Controlled arms run through **`SessionProxy`**, not through a loop written
    here. That is the point: the arm has to be the production controller on a
    production socket, or the published number is about the harness. `probe.py`'s
    `run_session` drives `AssemblyAISession` directly and does not model this.

    Args:
        clip: The clip and its sidecar.
        arm: Any of `STATIC_ARMS` or `NOD_ARMS`.
        session_factory: Builds the upstream. `FakeProbeSession` for offline.
        api_key: Upstream credential. Ignored by the fake.
        model: Speech model.
        policy: The compiled context policy, for the context axis.
        expected_answer: Run-level class, for a clip with no per-turn spans.
        trace_dir: Where the session trace lands. A temp sink when omitted.
        ceiling_ms: Latency ceiling for the controller.

    Returns:
        The observation and what the controller did.

    Raises:
        FeederDriftError: The feeder sustained lag past EC-37's threshold, which
            voids the run rather than reporting a latency measured off a drifted
            clock.
    """
    pcm, sample_rate = read_wav(clip.audio_path)
    frames = frames_of_pcm(pcm, sample_rate=sample_rate)
    feeder = PacedFeeder(frame_ms=FRAME_MS)
    started = time.monotonic()

    if arm in NOD_ARMS:
        run = await _run_live_controlled(
            clip,
            arm,
            session_factory=session_factory,
            api_key=api_key,
            model=model,
            policy=policy,
            expected_answer=expected_answer,
            trace_dir=trace_dir,
            ceiling_ms=ceiling_ms,
            feeder=feeder,
            frames=frames,
        )
    else:
        run = await _run_live_static(
            clip,
            arm,
            session_factory=session_factory,
            api_key=api_key,
            model=model,
            feeder=feeder,
            frames=frames,
        )
    return replace(run, wall_s=time.monotonic() - started)


async def _run_live_static(
    clip: GeneratedClip,
    arm: Arm,
    *,
    session_factory: SessionFactory,
    api_key: str,
    model: str,
    feeder: PacedFeeder,
    frames: Sequence[bytes],
) -> LiveRun:
    """One static arm: configure at connect, feed, collect boundaries."""
    settings = STATIC_ARMS[arm]
    boundaries: list[LiveBoundary] = []
    dropped: list[Turn] = []
    session = session_factory(
        api_key=api_key,
        model=model,
        config={
            "min_turn_silence": float(settings.min_turn_silence),
            "max_turn_silence": float(settings.max_turn_silence),
            "end_of_turn_confidence_threshold": (
                settings.end_of_turn_confidence_threshold
            ),
        },
    )
    async with session:
        report = await _feed_and_drain(session, feeder, frames, boundaries, dropped)
    return LiveRun(
        observation=score_live_clip(clip, arm, boundaries),
        patches=0,
        turns=len(boundaries),
        decide_ms=(),
        wall_s=0.0,
        feed_report=report,
        flush_turns=len(dropped),
    )


async def _run_live_controlled(
    clip: GeneratedClip,
    arm: Arm,
    *,
    session_factory: SessionFactory,
    api_key: str,
    model: str,
    policy: CompiledPolicy | None,
    expected_answer: ExpectedAnswer | None,
    trace_dir: Path | None,
    ceiling_ms: int,
    feeder: PacedFeeder,
    frames: Sequence[bytes],
) -> LiveRun:
    """One controlled arm, through the production `SessionProxy`.

    `nod-nospeaker` neutralises the profiler's warmth rather than branching, the
    same way `run_nod_clip` does: a `Profiler` that reports `cold` is what §4
    already sees below `MIN_GAPS_FOR_WARM`, so the law skips the speaker axis
    itself instead of the harness skipping the law.
    """
    axes = NOD_ARMS[arm]
    boundaries: list[LiveBoundary] = []
    dropped: list[Turn] = []

    upstream = session_factory(
        api_key=api_key,
        model=model,
        config={
            "min_turn_silence": float(BASE_MIN_MS),
            "max_turn_silence": float(BASE_MAX_MS),
        },
    )
    directory = (
        trace_dir if trace_dir is not None else Path(mkdtemp(prefix="nod-live-"))
    )
    sink = TraceSink(f"{clip.clip_id}-{arm}", directory=directory)
    profiler = ColdProfiler() if not axes.speaker else Profiler()
    controller = TimedArbiter(capabilities=NOD_CAPABILITIES, ceiling_ms=ceiling_ms)

    async with upstream:
        proxy = SessionProxy(
            upstream=upstream,
            profiler=profiler,
            arbiter=controller,
            trace=sink,
            mode=NodMode.ADAPT,
            ceiling_ms=ceiling_ms,
            context=ClipContext(
                clip,
                policy=policy,
                enabled=axes.context,
                fallback=expected_answer,
            ),
        )
        driver = asyncio.create_task(proxy.run())
        collector = asyncio.create_task(
            _collect_from_proxy(proxy, feeder, boundaries, dropped)
        )
        try:
            report = await feeder.feed(frames, _sync_feed(proxy))
            await upstream.terminate()
            await asyncio.wait_for(collector, timeout=TERMINATION_TIMEOUT_S)
        except TimeoutError:
            collector.cancel()
            report = None
        # **No broad catch here, unlike `_feed_and_drain`, and the asymmetry is
        # real.** That function's feeder calls `session.send_audio`, which
        # touches the socket and so dies when the socket does. This one calls
        # `proxy.feed_audio`, which is synchronous and non-throwing by
        # construction (INV-1: the audio path never awaits controller work), so
        # a socket failure cannot reach the feeder at all — it lands in
        # `proxy.run()` and the `finally` below harvests it on every path.
        #
        # A broad clause here was therefore dead for its stated purpose and
        # actively wrong for another: it swallowed `FeederDriftError` into
        # `report = None`, and EC-37 says sustained lag **voids the run** rather
        # than producing a quietly unreported one. Found by `make mutate` —
        # narrowing it back to `OSError` changed no test, which is what an
        # unreachable clause looks like.
        finally:
            collector.cancel()
            driver.cancel()
            # `proxy.run()` awaits `pump_events_down` inside a TaskGroup, and
            # `AssemblyAISession.events` raises `UpstreamError` on an `Error`
            # frame — so an upstream refusal lands *here*, in a task nothing
            # was inspecting. `suppress(BaseException)` then discarded it, the
            # collector waited out its ten seconds, and the clip scored on
            # however many boundaries had arrived before the socket died. That
            # is the silent-clip failure ADR-042 warned about, one layer in.
            driver_error = await _harvest(driver)
            await proxy.aclose()
    if driver_error is not None:
        raise _abort_for(driver_error) from driver_error

    return LiveRun(
        observation=score_live_clip(clip, arm, boundaries),
        patches=proxy.patches_sent,
        turns=len(boundaries),
        decide_ms=tuple(controller.samples),
        wall_s=0.0,
        feed_report=report,
        flush_turns=len(dropped),
    )


class ColdProfiler(Profiler):
    """A `Profiler` that never reports warm. The `nod-nospeaker` ablation.

    Subclassed rather than branched for `run_nod_clip`'s stated reason: the law
    must skip the speaker axis by its own `cold` rule, so what the ablation
    changes is the *input*. Everything else — gap accumulation, the quantile
    estimators, the trace — runs exactly as it does on `nod`.
    """

    @override
    def features(self) -> SpeakerFeatures:
        """The real features, forced cold. `O(1)`."""
        return replace(super().features(), cold=True)


class TimedArbiter(Arbiter):
    """An `Arbiter` that records its own `decide` latency. Guards INV-2.

    A subclass rather than a wrapper assigned over the bound method, because
    `SessionProxy` reads `arbiter.capabilities` and `arbiter.state` as well as
    calling `decide`, so what it needs is an `Arbiter` and not a callable.

    `perf_counter`, not the stream clock: this is telemetry about the process,
    which CLAUDE.md §6 exempts from the stream-relative rule, and a stream clock
    could not measure a CPU cost anyway.

    The measurement includes this wrapper's own two `perf_counter` calls, which
    biases `dec_p99` **upward** by tens of nanoseconds against a 5 ms budget. That
    is the harmless direction and it is stated rather than corrected: a
    correction would be a number subtracted from a published latency.
    """

    __slots__ = ("samples",)

    def __init__(self, *args: object, **kwargs: object) -> None:
        """As `Arbiter`, plus an empty sample list."""
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.samples: list[float] = []

    @override
    def decide(self, state: ArbiterInput) -> ConfigPatch | None:
        """`Arbiter.decide`, timed. `O(1)` over the parent's cost."""
        at = time.perf_counter()
        try:
            return super().decide(state)
        finally:
            self.samples.append((time.perf_counter() - at) * 1000.0)


def _sync_feed(proxy: SessionProxy) -> Callable[[bytes], Awaitable[None]]:
    """Adapt `feed_audio` to the feeder's async `send`, without awaiting it.

    INV-1: `feed_audio` is synchronous and must not be awaited, so this calls it
    and returns an already-resolved coroutine. The feeder's contract wants an
    awaitable; the audio path stays free of controller work.
    """

    async def send(frame: bytes) -> None:
        proxy.feed_audio(frame)

    return send


async def _feed_and_drain(
    session: ProbeSession,
    feeder: PacedFeeder,
    frames: Sequence[bytes],
    into: list[LiveBoundary],
    dropped: list[Turn],
) -> FeedReport | None:
    """Feed a static session and collect its caller turns.

    Args:
        session: The upstream.
        feeder: The paced feeder, also the stream clock boundaries are timed on.
        frames: PCM16 frames.
        into: Collected boundaries, appended as they arrive.
        dropped: Wordless finalised turns, for the audit count.

    Returns:
        The feed report, or `None` if the upstream never finished.
    """

    async def drain() -> None:
        async for event in session.events():
            if isinstance(event, Turn) and event.end_of_turn:
                if not is_caller_turn(event):
                    dropped.append(event)
                    continue
                # `append`, not the `extend`-with-comprehension PERF401 asks for.
                # This task is cancelled on `TERMINATION_TIMEOUT_S`, and `extend`
                # only mutates the list once the comprehension completes — so a
                # cancelled collector would discard every boundary it had already
                # seen and the clip would score as though the socket said nothing.
                into.append(_boundary(event, feeder))

    reader = asyncio.create_task(drain())
    try:
        report = await feeder.feed(frames, session.send_audio)
        await session.terminate()
        await asyncio.wait_for(reader, timeout=TERMINATION_TIMEOUT_S)
    except UpstreamError as exc:
        reader.cancel()
        raise _abort_for(exc) from exc
    except TimeoutError:
        reader.cancel()
        return None
    except Exception as exc:
        # The socket died under the feeder. Whatever closed it said *why* on the
        # reader's side an instant earlier, so the reader's exception is the
        # real diagnosis and this one is the symptom. Observed at Gate 4b: a
        # rejected config produced `ConnectionClosedError: received 3006` here
        # and `UpstreamError: Invalid 'min_turn_silence'` there, and only the
        # second says what to fix. Without this the useful one surfaced as an
        # unretrieved-task warning, by luck.
        #
        # Broad on purpose, and it has to be. `websockets.ConnectionClosedError`
        # is **not** an `OSError` — it descends from `Exception` via
        # `WebSocketException` — so the narrower clause this replaces caught
        # nothing and a 1008 escaped the sweep as a raw traceback instead of the
        # handled abort. Narrowing to the websockets type would re-couple this
        # module to a transport the `ProbeSession` protocol exists to hide.
        reader.cancel()
        raise _abort_for(await _harvest(reader) or exc) from exc
    return report


async def _collect_from_proxy(
    proxy: SessionProxy,
    feeder: PacedFeeder,
    into: list[LiveBoundary],
    dropped: list[Turn],
) -> None:
    """Collect boundaries from the proxy's client-facing event stream.

    Read from the *client* side rather than from the upstream socket, because
    that is what a caller actually receives and therefore what TTL is about.
    """
    async for event in proxy.client_events():
        if isinstance(event, Turn) and event.end_of_turn:
            if not is_caller_turn(event):
                dropped.append(event)
                continue
            # See `_feed_and_drain`: `append` survives cancellation, `extend` does
            # not, and this collector is cancelled on timeout.
            into.append(_boundary(event, feeder))


async def _harvest(task: asyncio.Task[None]) -> BaseException | None:
    """Await a cancelled task and return the exception it was hiding. `O(1)`.

    `CancelledError` is the expected outcome and is not an error; anything else
    is something the task raised before the cancel reached it, and is exactly
    what the caller needs to see.
    """
    try:
        await task
    except asyncio.CancelledError:
        return None
    except BaseException as exc:
        return _unwrap(exc)
    return None


def _unwrap(exc: BaseException) -> BaseException:
    """The first non-group exception inside a possibly-nested group. `O(n)`.

    `asyncio.TaskGroup` re-raises as an `ExceptionGroup`, so the `UpstreamError`
    carrying error 1008 can arrive wrapped one or two deep. Matching on the group
    would miss the code and the sweep would treat a concurrency refusal as an
    ordinary abort.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _abort_for(exc: BaseException) -> LiveRunAbortedError:
    """Classify an upstream failure. Pure. `O(1)`."""
    code = getattr(exc, "error_code", None)
    if code == CONCURRENCY_REFUSED_CODE:
        return ConcurrencyRefusedError(
            f"upstream refused with {CONCURRENCY_REFUSED_CODE} "
            f"(too many concurrent sessions): the sweep is over-subscribed. "
            f"The account permits {UPSTREAM_CONCURRENCY_LIMIT} concurrent "
            f"streams and the refusal arrives after a successful upgrade, so "
            f"every affected clip would otherwise score as silence. Lower "
            f"--concurrency and re-run; partial results are not usable."
        )
    return LiveRunAbortedError(f"upstream errored mid-clip, so it has no score: {exc}")


def _boundary(turn: Turn, feeder: PacedFeeder) -> LiveBoundary:
    """One `LiveBoundary` from a finalised turn. See `LiveBoundary`. `O(1)`."""
    fired = float(feeder.now_ms())
    return LiveBoundary(
        fired_at_ms=fired,
        silence_started_ms=(float(turn.words[-1].end_ms) if turn.words else fired),
        turn_order=turn.turn_order,
        words=turn.words,
    )


async def replay(
    clip: Path,
    arm: Arm,
    *,
    endpoint: str,
    repeats: int = DEFAULT_REPEATS,
    corpus_clip: GeneratedClip | None = None,
    session_factory: SessionFactory | None = None,
    api_key: str = "",
    policy: CompiledPolicy | None = None,
    expected_answer: ExpectedAnswer | None = None,
    out: Path | None = None,
    gate: StartRateGate | None = None,
) -> RunResult:
    """Replay one clip through one arm, in paced real time, `repeats` times.

    Frames are emitted on a monotonic deadline schedule, never `sleep(0.05)` in a
    loop, so jitter does not accumulate. Actual send timestamps are recorded, and
    the run aborts above `MAX_DRIFT_MS` cumulative drift (EC-37).

    **Repeats are kept, not pooled** (ADR-045). One `ClipObservation` per repeat
    reaches `RunResult`, because a live upstream is non-deterministic and
    BENCH_SPEC §4's median-and-IQR is stated over exactly this axis.

    Args:
        clip: The audio clip.
        arm: Which configuration to run.
        endpoint: Upstream URL; recorded for provenance. `FakeAssemblyAI` for
            offline runs, in which case pass its `session_factory` too.
        repeats: Repeats per pair.
        corpus_clip: The clip's sidecar. Required, and keyword-only so a caller
            cannot accidentally score against the wrong ground truth.
        session_factory: Builds each upstream session.
        api_key: Upstream credential.
        policy: The compiled context policy.
        expected_answer: Run-level class fallback.
        out: Trace directory.
        gate: Start-rate gate, awaited before each repeat. `None` means
            ungated, which is right for a single clip and wrong for a sweep.

    Returns:
        The run result, carrying one observation per repeat.

    Raises:
        ValueError: `corpus_clip` was not supplied, so there is no ground truth.
    """
    if corpus_clip is None:
        msg = (
            f"replay({clip.name}, {arm}) needs `corpus_clip`: an observation "
            "cannot be scored without the sidecar, and guessing the sidecar from "
            "the audio path is how a run gets scored against another clip's truth"
        )
        raise ValueError(msg)
    factory: SessionFactory = (
        session_factory if session_factory is not None else AssemblyAISession
    )
    observations: list[ClipObservation] = []
    patches: list[int] = []
    decide_ms: list[float] = []
    wall_s: list[float] = []
    traces: list[Path] = []
    directory = out if out is not None else Path(mkdtemp(prefix="nod-replay-"))

    for index in range(repeats):
        if gate is not None:
            await gate.wait()
        repeat_dir = directory / f"{corpus_clip.clip_id}-{arm}-r{index}"
        repeat_dir.mkdir(parents=True, exist_ok=True)
        run = await run_live_clip(
            corpus_clip,
            arm,
            session_factory=factory,
            api_key=api_key,
            policy=policy,
            expected_answer=expected_answer,
            trace_dir=repeat_dir,
        )
        observations.append(run.observation)
        patches.append(run.patches)
        decide_ms.extend(run.decide_ms)
        wall_s.append(run.wall_s)
        traces.extend(sorted(repeat_dir.glob("*.jsonl")))

    return RunResult(
        run_id=f"{corpus_clip.clip_id}:{arm}:{endpoint}",
        clip_id=corpus_clip.clip_id,
        arm=arm,
        repeats=repeats,
        trace_paths=tuple(traces),
        observations=tuple(observations),
        patches=tuple(patches),
        decide_ms=tuple(decide_ms),
        wall_s=tuple(wall_s),
    )


def _live_main(args: argparse.Namespace, out: TextIO) -> int:
    """The `--live` sweep. Bounded by the account's concurrency limit.

    Separate from the simulated path rather than a branch inside it, for the same
    reason `score_live_clip` is a third construction site: the simulated sweep
    feeds every published-simulated figure and is mutation-guarded, and a live
    branch threaded through it would put live-path failures inside that guard.

    Returns:
        Process exit code. `2` on any refusal to run.
    """
    import os

    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not api_key:
        out.write(
            "--live needs ASSEMBLYAI_API_KEY. It is server-side only (INV-5) and "
            "is never read from a committed file other than `.env`.\n"
        )
        return 2
    if args.concurrency > UPSTREAM_CONCURRENCY_LIMIT:
        out.write(
            f"--concurrency {args.concurrency} exceeds the measured account "
            f"limit of {UPSTREAM_CONCURRENCY_LIMIT} (ADR-042). The upstream "
            f"refuses the surplus with error 1008 *after* accepting the socket, "
            f"so the sessions would look healthy and score as silent clips.\n"
        )
        return 2

    corpus_file = args.corpus / "corpus.json"
    if not corpus_file.exists():
        out.write(
            f"no corpus at {corpus_file}.\n"
            f"The Track A corpus **is** committed (120 clips, 38 MB on disk), so\n"
            f"a clean clone has one and this usually means --corpus points\n"
            f"somewhere else.\n"
        )
        return 2
    corpus = BuiltCorpus.model_validate_json(corpus_file.read_text())
    policy = load_policy(args.policy) if args.policy.exists() else None
    arms: list[Arm] = [
        "aggressive",
        "balanced",
        "conservative",
        "nod",
        "nod-nocontext",
        "nod-nospeaker",
    ]

    pairs = len(corpus.clips) * len(arms)
    sessions = pairs * args.repeats
    audio_s = sum(c.total_ms for c in corpus.clips) / 1000 * len(arms) * args.repeats
    out.write(
        f"{len(corpus.clips)} clips x {len(arms)} arms x {args.repeats} repeats "
        f"= {sessions} live sessions, {audio_s / 3600:.2f} h of audio, "
        f"~${audio_s / 3600 * STREAMING_USD_PER_HOUR:.2f} at the published rate.\n"
        # Printed, always, because a concurrency that quietly differs from the
        # one the operator planned for changes the wall clock by a factor and
        # nothing else would say so. `make bench-live` passes it explicitly for
        # the same reason; the default is the safe value, not the intended one.
        f"concurrency {args.concurrency} of {UPSTREAM_CONCURRENCY_LIMIT} "
        f"permitted (default {LIVE_CONCURRENCY}"
        f"{', OVERRIDDEN' if args.concurrency != LIVE_CONCURRENCY else ''}).\n"
        f"start-rate gate: one session per {args.min_interval:.1f} s "
        f"({UPSTREAM_CONCURRENCY_LIMIT} slots / {SLOT_RELEASE_LAG_S:.0f} s "
        f"release lag), which binds before concurrency does.\n"
        f"Expect at least {sessions * args.min_interval / 3600:.2f} h wall "
        f"clock: the start rate is the floor here, not the audio duration.\n"
    )
    return asyncio.run(_live_sweep(corpus, arms, args, api_key, policy, out))


async def _live_sweep(
    corpus: BuiltCorpus,
    arms: Sequence[Arm],
    args: argparse.Namespace,
    api_key: str,
    policy: CompiledPolicy | None,
    out: TextIO,
    session_factory: SessionFactory | None = None,
) -> int:
    """Run every (clip, arm) pair live, `args.concurrency` at a time.

    Args:
        corpus: The built corpus.
        arms: Every arm to sweep.
        args: Parsed CLI arguments.
        api_key: Upstream credential.
        policy: The compiled context policy.
        out: Where progress is written.
        session_factory: Builds each upstream. Injected so the abort path can be
            tested without a socket (INV-7); `None` is the real one.

    Returns:
        `0` on a complete sweep, `1` when an upstream failure aborted it.
    """
    from nod_bench.report import render_all

    slots = asyncio.Semaphore(args.concurrency)
    starts = StartRateGate(args.min_interval)
    results: list[RunResult] = []

    async def one(clip: GeneratedClip, arm: Arm) -> RunResult:
        async with slots:
            return await replay(
                clip.audio_path,
                arm,
                endpoint=LIVE_ENDPOINT_LABEL,
                repeats=args.repeats,
                corpus_clip=clip,
                session_factory=session_factory,
                api_key=api_key,
                policy=policy,
                out=args.out / "traces",
                gate=starts,
            )

    pending = [
        asyncio.ensure_future(one(clip, arm)) for arm in arms for clip in corpus.clips
    ]
    try:
        for done in asyncio.as_completed(pending):
            results.append(await done)
            out.write(f"\r{len(results)}/{len(pending)} pairs")
            out.flush()
    except LiveRunAbortedError as exc:
        # Nothing is salvageable from a partial sweep: `bootstrap_points` and
        # `repeat_points` both pair arms clip-by-clip, and a corpus missing the
        # clips that happened to be in flight is not the same corpus on every
        # arm. Writing artifacts from it would publish a table whose rows were
        # measured over different subsets.
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        out.write(f"\n\nABORTED after {len(results)} of {len(pending)} pairs.\n")
        out.write(f"{exc}\n")
        out.write("No artifacts written; a partially-scored corpus is not one.\n")
        return 1
    out.write("\n")

    # `as_completed` yields in finishing order, which is not the corpus order.
    # `report.repeat_points` pairs arms clip-by-clip and refuses a mismatch, so
    # leaving these unsorted would either lose the pairing BENCH_SPEC §9 rests on
    # or raise — and the failure would depend on which sockets happened to be
    # slow, which is the worst kind of intermittent.
    results.sort(key=lambda r: (r.arm, r.clip_id))

    by_arm: dict[Arm, list[ClipObservation]] = {arm: [] for arm in arms}
    for result in results:
        # One observation per repeat, all kept: ADR-045's repeat axis.
        by_arm[result.arm].extend(result.observations)

    census = {
        arm: list(counts)
        for arm, counts in (
            (arm, [n for r in results if r.arm == arm for n in r.patches])
            for arm in arms
        )
        if counts
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "patch_census.live.json").write_text(json.dumps(census, indent=1))
    # **Persisted because the live run is the one that cannot be repeated.**
    # The simulated path has written `observations.simulated.json` all along and
    # regenerates in 19 seconds anyway; the live path wrote none, so Gate 4b's
    # 1.6-hour sweep could not be re-scored or re-rendered under a different
    # interval estimator without paying for it again. That asymmetry was exactly
    # backwards.
    (args.out / "observations.live.json").write_text(
        json.dumps(
            {
                arm: [o.model_dump(mode="json") for o in obs]
                for arm, obs in by_arm.items()
            },
            indent=1,
        )
    )

    manifest = _live_manifest(corpus, arms, args, by_arm, results)
    written = render_all(
        by_arm,
        out=args.out,
        simulated=False,
        manifest=manifest,
        repeats=args.repeats,
    )
    for path in written:
        out.write(f"wrote {path}\n")
    return 0


def patch_census_from_traces(trace_root: Path) -> dict[str, list[int]]:
    """Per-session `config_applied` counts, read back from the traces. `O(lines)`.

    **Counted from the trace rather than from memory**, and that is the point.
    ADR-027 defers the `MAX_PATCHES` decision to a live per-session count and
    ADR-032 defers its quantisation question to the same run; both want the
    number of patches the socket *accepted*. `config_applied` is emitted only
    after the upstream answered (see `SessionProxy.patches_sent`), so counting
    those lines counts applications, not decisions — the distinction CLAUDE.md
    §5 names for `proxy.py`, where a patch computed and never applied produces a
    run that looks like `nod` and behaves like `balanced`.

    Reading the trace also means the census survives the process that produced
    it, which a `RunResult` held in memory does not.

    Args:
        trace_root: The sweep's trace directory, one subdirectory per repeat.

    Returns:
        Arm name to a list of per-session accepted-patch counts.
    """
    census: dict[str, list[int]] = {}
    for repeat_dir in sorted(p for p in trace_root.iterdir() if p.is_dir()):
        for trace in sorted(repeat_dir.glob("*.jsonl")):
            # `<clip_id>-<arm>.jsonl`, and an arm may contain a hyphen.
            stem = trace.stem
            arm = next(
                (a for a in (*NOD_ARMS, *STATIC_ARMS) if stem.endswith(f"-{a}")),
                None,
            )
            if arm is None:
                continue
            applied = sum(
                1
                for line in trace.read_text().splitlines()
                if line.strip() and json.loads(line).get("kind") == "config_applied"
            )
            census.setdefault(arm, []).append(applied)
    return census


def _live_manifest(
    corpus: BuiltCorpus,
    arms: Sequence[Arm],
    args: argparse.Namespace,
    by_arm: dict[Arm, list[ClipObservation]],
    results: Sequence[RunResult],
) -> RunManifest:
    """The live run's manifest. Every field required, none defaulted (ADR-016)."""
    from nod_bench.metrics import (
        QUANTILE_METHOD,
        ProxyDivergence,
        RunManifest,
        certain_utterances,
        frag,
        pcr,
        total_utterances,
    )

    pooled = [obs for arm in arms for obs in by_arm[arm]]
    certain = certain_utterances(pooled)
    return RunManifest(
        seed=corpus.seed,
        generator_version=corpus.generator_version,
        corpus_id=corpus.corpus_id,
        corpus_sha256=corpus.corpus_sha256,
        corpus_clips=len(corpus.clips),
        repeats_per_arm=args.repeats,
        arms=tuple(_arm_config(arm) for arm in arms),
        simulated=False,
        endpoint_overhead_ms=float(ENDPOINT_OVERHEAD_MS),
        quantile_method=QUANTILE_METHOD,
        proxy_divergence=ProxyDivergence(
            pcr_all=pcr(pooled),
            pcr_certain_only=pcr(pooled, certain_only=True) if certain else None,
            frag_all=frag(pooled),
            frag_certain_only=frag(pooled, certain_only=True) if certain else None,
            utterances_all=total_utterances(pooled),
            utterances_certain_only=certain,
        ),
    )


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
        ProxyDivergence,
        RunManifest,
        certain_utterances,
        frag,
        pcr,
        total_utterances,
    )
    from nod_bench.report import render_all
    from nod_core.arbiter import MAX_PATCHES

    parser = argparse.ArgumentParser(prog="python -m nod_bench.replay")
    parser.add_argument(
        "--fake", action="store_true", help="run against the simulator (ADR-017)"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="run against the real API; needs ASSEMBLYAI_API_KEY (INV-9)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=DEFAULT_REPEATS,
        help=(
            f"repeats per (clip, arm) on --live. Default {DEFAULT_REPEATS} "
            f"(BENCH_SPEC §4). Ignored by --fake, which is deterministic."
        ),
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=MIN_SESSION_INTERVAL_S,
        help=(
            f"seconds between session starts. Default "
            f"{MIN_SESSION_INTERVAL_S:.1f}; the account's slot-release lag is "
            f"the binding constraint, not concurrency (Gate 4b)"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=LIVE_CONCURRENCY,
        help=(
            f"concurrent live sessions. Default {LIVE_CONCURRENCY}; the account "
            f"permits {UPSTREAM_CONCURRENCY_LIMIT} and refuses the next with "
            f"error 1008 (ADR-042)"
        ),
    )
    parser.add_argument("--corpus", type=Path, default=Path("data/corpus/trackA"))
    parser.add_argument("--out", type=Path, default=Path("bench/runs"))
    parser.add_argument(
        "--overhead-ms",
        type=float,
        default=0.0,
        help="ADR-017's endpoint overhead; 0 means unmeasured and fires early",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("config/policy.yaml"),
        help="the context axis (CONTROL_SPEC §3); without it every hint is 1.0",
    )
    args = parser.parse_args(argv)
    out = sys.stdout

    if args.live and args.fake:
        out.write("--live and --fake are exclusive: pick one upstream.\n")
        return 2
    if args.live:
        return _live_main(args, out)

    corpus_file = args.corpus / "corpus.json"
    if not corpus_file.exists():
        out.write(
            f"no corpus at {corpus_file}.\n"
            f"The Track A corpus **is** committed (120 clips, 38 MB on disk), so\n"
            f"a clean clone has one and this usually means --corpus points\n"
            f"somewhere else. Rebuilding needs `python -m nod_bench.corpus build`\n"
            f"and a seed recording, and `say` is macOS-only — which is why the\n"
            f"audio is committed rather than generated (Phase 1 exit, closed).\n"
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
    # Without a policy every `hint_for` returns the default and the context
    # axis contributes nothing on any corpus — which is what `make bench` did
    # until Gate 8. Refused rather than defaulted to neutral: a silently
    # neutral context axis is an arm measured under the wrong label.
    if not args.policy.exists():
        out.write(f"policy not found: {args.policy}\n")
        return 2
    policy = load_policy(args.policy)

    by_arm = run_matrix(
        corpus, arms, endpoint_overhead_ms=args.overhead_ms, policy=policy
    )

    # The patch census. §5 caps a session at MAX_PATCHES and Gate 4 observed that
    # the cap may be unreachable once hysteresis has converged, so the question is
    # answered with a count rather than an argument.
    census = {
        arm: [
            run_nod_clip(
                clip, arm, endpoint_overhead_ms=args.overhead_ms, policy=policy
            )
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
    certain_n = certain_utterances(everything)
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
        arms=tuple(_arm_config(arm) for arm in arms),
        simulated=True,
        endpoint_overhead_ms=args.overhead_ms,
        quantile_method=QUANTILE_METHOD,
        proxy_divergence=ProxyDivergence(
            pcr_all=pcr(everything),
            pcr_certain_only=certain_pcr,
            frag_all=frag(everything),
            frag_certain_only=certain_frag,
            utterances_all=total_utterances(everything),
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
