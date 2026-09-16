"""Capability probe: what this model's turn-detection knobs actually do.

Phase 0 of docs/ROADMAP.md, docs/PROMPTS.md P1. Observes and probes only — no
controller logic, no config decisions, no arbiter.

The hard part is not sending `UpdateConfiguration`; it is proving one landed. The
upstream API answers a successful update with silence, so acceptance is evidence
of nothing. Every knob is therefore driven in four cells — set at connect time
low and high, and updated mid-stream low and high — against a clip whose test gap
straddles the two arms. A verdict is `LIVE` only when the turn boundary actually
moved, in the right direction, in both.

The connect-time pair is what makes a null mid-stream result readable. Without
it, a knob that does nothing on this model and a broken `UpdateConfiguration`
produce identical evidence.

Lives in `nod_bench` rather than `nod_core` because it needs the paced feeder and
the concrete adapter, both of which `nod_core` is forbidden to import
(ARCHITECTURE.md §2, enforced by tests/unit/test_boundaries.py). The pure
classifier it calls stays in `nod_core.capabilities` (ADR-007).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, TextIO

from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed

from nod_adapters.assemblyai.session import AssemblyAISession, UpstreamError
from nod_bench.feeder import PacedFeeder, frames_of
from nod_bench.probe_clip import (
    LEAD_MS,
    PREAMBLE_MS,
    SAMPLE_RATE,
    TONE_LOW,
    TRAIL_MS,
    UPDATE_AT_MS,
    ZEROS,
    ClipLayout,
    SeedError,
    build_clip,
    clip_sha256,
    load_seed,
)
from nod_bench.seed import (
    MANIFEST_SUFFIX,
    RATE_WPM,
    SayUnavailableError,
    SynthesisError,
    manifest_for,
    synthesize_seed,
)
from nod_core.capabilities import (
    NO_BOUNDARY,
    CellObservation,
    CellPlan,
    classify,
    degrade,
    probe,
    verdict_for,
)
from nod_core.config import get_settings
from nod_core.trace import TraceSink, redact_url
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    KnobVerdict,
    SessionBegin,
    Termination,
    Turn,
)

PRIMARY_MODEL: Final = "universal-streaming-english"
"""Documented as confidence-based. The model the controller is meant to ship on."""

SECONDARY_MODEL: Final = "universal-3-5-pro"
"""Documented as punctuation-based, and the arm that prices ever switching.

Two AssemblyAI pages disagree about whether `min_turn_silence` and
`max_turn_silence` apply to Universal-Streaming models at all. This arm settles
the question against a model the documentation agrees about.
"""

FULL_REPEATS: Final = 3
"""`N` per cell. Below three there is no spread to compare a difference against."""

SECONDARY_REPEATS: Final = 2

CONNECT_CELLS: Final = ("connect_low", "connect_high")
MID_CELLS: Final = ("mid_low", "mid_high")

STREAMING_USD_PER_HOUR: Final = 0.15
"""Published Universal-Streaming rate, for the pre-flight cost estimate."""


class ProbeSession(Protocol):
    """The upstream surface the probe drives.

    `AssemblyAISession` and `FakeProbeSession` both satisfy it, so `--fake` and a
    live run exercise the same orchestration rather than two parallel paths.
    """

    id: str

    @property
    def url(self) -> str:
        """Connect URL, recorded in the trace's `meta` line."""
        ...

    async def __aenter__(self) -> ProbeSession:
        """Open the session."""
        ...

    async def __aexit__(self, *exc: object) -> None:
        """Close the session."""
        ...

    async def send_audio(self, frame: bytes) -> None:
        """Forward one PCM16 frame verbatim."""
        ...

    async def update_configuration(self, patch: Mapping[str, float]) -> None:
        """Apply a mid-stream config change. Answered with silence on success."""
        ...

    async def force_endpoint(self) -> None:
        """End the current turn now."""
        ...

    async def terminate(self) -> None:
        """Ask the upstream to end the session gracefully."""
        ...

    def events(self) -> AsyncIterator[SessionBegin | Turn | Termination]:
        """Yield upstream frames, partials included."""
        ...

    async def aclose(self) -> None:
        """Release the session."""
        ...


type SessionFactory = Callable[..., ProbeSession]
"""Builds one upstream session. `AssemblyAISession` is the live one."""


@dataclass(frozen=True, slots=True)
class KnobStimulus:
    """How one knob is made measurable.

    `pinned` fixes the *other* knobs so the one under test is the only thing that
    can end the turn inside the gap. Changing one field while leaving the rest at
    undocumented defaults produces null results that cannot be attributed.
    """

    field: str
    pinned: Mapping[str, float]
    gap_ms: int
    gap_fill: str
    low: float
    high: float
    direction: int
    pin_rationale: str
    """Why the pinned values are what they are, recorded in the trace meta.

    The first live run pinned `max_turn_silence` at 8000 ms for two knobs on the
    reasoning that it "could not be the binding constraint". On this model it is
    the *only* mechanism that ends a turn, so nothing fired in any cell and the
    arms measured nothing at all.
    """

    lead_segment: int
    """Which seed segment sits immediately before the test gap.

    `1` is a complete sentence, which lets the semantic gate fire; `3` is a
    fragment, which keeps it waiting so the acoustic fallback is the only thing
    that can end the turn. The knob under test dictates which regime is needed.
    """

    expected_shift_ms: int
    """How far the boundary should move between the arms, in milliseconds.

    Stated per stimulus rather than derived from `high - low`, because for the
    two threshold knobs the arm values are dimensionless and carry no
    millisecond meaning at all.

    The bound is the mechanism's own scale, not the gap length. `vad_threshold`
    shifts only *when silence starts accumulating*, so its scale is the
    accumulation window (`max_turn_silence`), not the gap it sits in; scaling by
    the gap demanded a 900 ms shift from a knob that can only move the boundary
    by a few hundred.
    """

    note: str

    @property
    def delta(self) -> float:
        """Configured distance between the arms. `O(1)`."""
        return abs(self.high - self.low)


STIMULI: Final = (
    KnobStimulus(
        field="max_turn_silence",
        pinned={"end_of_turn_confidence_threshold": 0.95, "min_turn_silence": 400},
        gap_ms=1500,
        gap_fill=ZEROS,
        low=600,
        high=3000,
        direction=1,
        lead_segment=3,
        pin_rationale=(
            "lead segment is a FRAGMENT, so the semantic gate keeps waiting and "
            "the acoustic fallback is the only thing that can end the turn. With "
            "a complete sentence the gate fires at min_turn_silence and max is "
            "never reached, which reads as inert; "
            "the low arm (600) ends the turn inside the 1500 ms gap and the high "
            "arm (3000) outlasts it, making the split categorical"
        ),
        expected_shift_ms=2400,
        note="low ends the turn inside the gap; high never should",
    ),
    KnobStimulus(
        field="min_turn_silence",
        pinned={"end_of_turn_confidence_threshold": 0.20, "max_turn_silence": 3000},
        gap_ms=4000,
        gap_fill=ZEROS,
        low=100,
        high=2000,
        direction=1,
        lead_segment=1,
        pin_rationale=(
            "max pinned at 3000 ms, above the 2000 ms high arm so it cannot bind "
            "before the knob acts, but inside the 4000 ms gap so the turn still "
            "terminates; pinning it beyond the gap removes the only mechanism "
            "that ends a turn on this model and every cell measures nothing"
        ),
        expected_shift_ms=1900,
        note="confidence pinned low so the minimum silence is the binding constraint",
    ),
    KnobStimulus(
        field="end_of_turn_confidence_threshold",
        pinned={"min_turn_silence": 200, "max_turn_silence": 3000},
        gap_ms=4000,
        gap_fill=ZEROS,
        low=0.0,
        high=1.0,
        direction=1,
        lead_segment=1,
        pin_rationale=(
            "arms at the documented endpoints rather than mid-range: threshold 0 "
            "is specified to force end-of-turn as soon as silence is detected, "
            "per min_turn_silence; threshold 1 is specified to fall back to "
            "acoustic-only detection on max_turn_silence. min=200 and max=3000 "
            "are both reachable inside the 4000 ms gap and 2800 ms apart, so the "
            "two documented behaviours must land in visibly different places. "
            "Mid-range arms (0.20 vs 0.95) cannot distinguish an inert knob from "
            "one whose gate the stimulus happens to satisfy either way"
        ),
        expected_shift_ms=2800,
        note="0.0 should end at min_turn_silence, 1.0 at max_turn_silence; "
        "identical boundaries mean the knob is inert beyond argument",
    ),
    KnobStimulus(
        field="vad_threshold",
        pinned={
            "end_of_turn_confidence_threshold": 0.95,
            "min_turn_silence": 400,
            "max_turn_silence": 800,
        },
        gap_ms=1500,
        gap_fill="tone_mid",
        low=0.05,
        high=0.90,
        direction=-1,
        lead_segment=3,
        pin_rationale=(
            "lead segment is a FRAGMENT so max_turn_silence is the binding gate; "
            "max pinned at 800 ms so a gap classified as silence terminates well "
            "inside the 1500 ms gap; the fill is -35 dBFS room tone so the "
            "threshold decides whether that silence accumulates at all"
        ),
        expected_shift_ms=800,
        note="gap is -35 dBFS room tone; a low threshold calls it speech, so the "
        "boundary moves later, not sooner",
    ),
)

CONTROL_PINNED: Final = {
    "end_of_turn_confidence_threshold": 0.40,
    "min_turn_silence": 400,
    "max_turn_silence": 1280,
}
"""CONTROL_SPEC.md §4 base values, for the confidence-field control sessions."""

FORCE_PINNED: Final = {
    "end_of_turn_confidence_threshold": 0.95,
    "min_turn_silence": 400,
    "max_turn_silence": 8000,
}
"""Nothing can end a turn inside the gap unless `ForceEndpoint` does.

Paired with `FORCE_LEAD_SEGMENT`: with a complete sentence the semantic gate
fires at `min_turn_silence` and the control arm ends on its own at ~590 ms,
which makes a forced boundary indistinguishable from an ordinary one.
"""

FORCE_LEAD_SEGMENT: Final = 3
"""A fragment, so the control arm has no way to end the turn by itself."""

FORCE_GAP_MS: Final = 2500
FORCE_AT_OFFSET_MS: Final = 400
"""How far into the gap `ForceEndpoint` is sent."""


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """One live session: one model, one cell, one repeat."""

    model: str
    label: str
    cell: str
    field: str
    arm_value: float
    connect: Mapping[str, float]
    layout: ClipLayout
    plan: CellPlan
    repeat: int
    segments: tuple[tuple[int, int], ...] = ()
    lead_segment: int = 1
    pin_rationale: str = ""

    @property
    def session_id(self) -> str:
        """Names the trace file, and reads as what it is."""
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"probe-{stamp}-{self.label}-{self.cell}-r{self.repeat}"


@dataclass
class RunResult:
    """Everything one probe run measured."""

    observations: dict[str, list[CellObservation]] = field(default_factory=dict)
    secondary: dict[str, list[CellObservation]] = field(default_factory=dict)
    """Non-primary model cells. Kept separate: they inform the model choice but
    must never be mixed into the primary model's verdicts."""

    control: list[CellObservation] = field(default_factory=list)
    force: list[CellObservation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    audio_ms: int = 0
    seed_caveat: str = ""
    """Non-empty when the seed was not a human recording (INV-9 adjacent)."""


CONCURRENCY_MARKER: Final = "concurrent"
"""Substring of the upstream's own words for the account session-concurrency cap."""

SESSION_SETTLE_S: Final = 2.0
"""Pause between sessions. Seconds.

The sessions are sequential, but closing a socket does not release the account's
concurrency slot instantly, and the upstream counts the overlap rather than the
intent.
"""

CONCURRENCY_BACKOFF_S: Final = (5.0, 15.0, 30.0)
"""Waits before each retry after a concurrency refusal. Seconds."""


class ConcurrencyLimitError(RuntimeError):
    """The account's concurrent-session cap refused this session.

    Distinct from a knob rejection: it says nothing about the field under test,
    so it must be retried rather than recorded as a verdict.
    """


def lead_spans(spec: SessionSpec) -> tuple[tuple[int, int], ...] | None:
    """Pick the three seed spans this cell's clip is built from. `O(1)`.

    The middle one is `spec.lead_segment`, not `segments[1]`. Which segment sits
    before the test gap decides which silence knob can bind at all (EC-50), so
    getting this wrong silently runs every cell in the other regime and reports
    the knob under test as inert.

    Args:
        spec: The cell to build a clip for.

    Returns:
        `(preamble, lead, trail)` spans, or `None` when the seed has no manifest
        and the caller should fall back to fixed offsets.
    """
    if len(spec.segments) <= max(2, spec.lead_segment):
        return None
    return (
        spec.segments[0],
        spec.segments[spec.lead_segment],
        spec.segments[2],
    )


def _failed(spec: SessionSpec, error_code: int | None) -> CellObservation:
    """Build the observation for a session that produced no measurement. `O(1)`."""
    return CellObservation(
        cell=spec.cell,
        field=spec.field,
        arm_value=spec.arm_value,
        boundary_ms=NO_BOUNDARY,
        last_word_end_ms=None,
        error_code=error_code,
        confidence_samples=(),
        confidence_on_partials=False,
    )


def _cell_plan(
    stimulus: KnobStimulus,
    cell: str,
    layout: ClipLayout,
    *,
    force_at: int | None = None,
) -> CellPlan:
    """Build the per-session plan for one cell. `O(1)`."""
    value = stimulus.low if cell.endswith("low") else stimulus.high
    return CellPlan(
        cell=cell,
        field=stimulus.field,
        arm_value=value,
        delta=stimulus.delta,
        direction=stimulus.direction,
        midstream=cell in MID_CELLS,
        expected_shift_ms=stimulus.expected_shift_ms,
        update_at_ms=UPDATE_AT_MS,
        gap_start_ms=layout.gap_start_ms,
        gap_end_ms=layout.gap_end_ms,
        force_endpoint_at_ms=force_at,
    )


def _neutral_for(stimulus: KnobStimulus) -> dict[str, float]:
    """Connect config for a mid-stream cell: pinned others, knob left at default.

    The knob under test is deliberately *not* set at connect time here, so the
    mid-stream frame is the only thing that could have changed it.
    """
    return dict(stimulus.pinned)


def plan_sessions(
    *, quick: bool, segment_ms: tuple[int, ...] | None = None
) -> list[SessionSpec]:
    """Build the full session matrix. Pure. `O(knobs * cells * repeats)`.

    Args:
        quick: Primary model only, one repeat per cell.
        segment_ms: Actual speaking length of the three seed segments. The gap
            position is derived from these, so a plan built without them and a
            clip built with them would disagree about where the gap starts.

    Returns:
        Every session the run will open, in execution order.
    """
    repeats = 1 if quick else FULL_REPEATS
    spans = segment_ms or (PREAMBLE_MS, LEAD_MS, TRAIL_MS)

    def _layout(gap_ms: int, gap_fill: str, lead_segment: int = 1) -> ClipLayout:
        lead_ms = spans[lead_segment] if lead_segment < len(spans) else spans[1]
        return ClipLayout(
            gap_ms=gap_ms,
            gap_fill=gap_fill,
            preamble_ms=spans[0],
            lead_ms=lead_ms,
            trail_ms=spans[2],
        )

    specs: list[SessionSpec] = []

    for stimulus in STIMULI:
        layout = _layout(stimulus.gap_ms, stimulus.gap_fill, stimulus.lead_segment)
        for cell in (*CONNECT_CELLS, *MID_CELLS):
            plan = _cell_plan(stimulus, cell, layout)
            connect = (
                {**stimulus.pinned, stimulus.field: plan.arm_value}
                if cell in CONNECT_CELLS
                else _neutral_for(stimulus)
            )
            specs.extend(
                SessionSpec(
                    model=PRIMARY_MODEL,
                    label=stimulus.field,
                    cell=cell,
                    field=stimulus.field,
                    lead_segment=stimulus.lead_segment,
                    pin_rationale=stimulus.pin_rationale,
                    arm_value=plan.arm_value,
                    connect=connect,
                    layout=layout,
                    plan=plan,
                    repeat=repeat,
                )
                for repeat in range(repeats)
            )

    # Control: no knob under test, just enough speech to read the confidence field.
    control_layout = _layout(1500, TONE_LOW)
    control_plan = CellPlan(
        cell="control",
        field="",
        arm_value=0.0,
        delta=0.0,
        direction=1,
        midstream=False,
        expected_shift_ms=control_layout.gap_ms,
        update_at_ms=0,
        gap_start_ms=control_layout.gap_start_ms,
        gap_end_ms=control_layout.gap_end_ms,
    )
    specs.extend(
        SessionSpec(
            model=PRIMARY_MODEL,
            label="control",
            cell="control",
            field="",
            arm_value=0.0,
            connect=dict(CONTROL_PINNED),
            layout=control_layout,
            plan=control_plan,
            repeat=repeat,
        )
        for repeat in range(repeats)
    )

    # ForceEndpoint: a test arm and a control arm, same clip.
    force_layout = _layout(FORCE_GAP_MS, ZEROS, FORCE_LEAD_SEGMENT)
    for cell, force_at in (
        ("force_test", force_layout.gap_start_ms + FORCE_AT_OFFSET_MS),
        ("force_control", None),
    ):
        plan = CellPlan(
            cell=cell,
            field="force_endpoint",
            arm_value=0.0,
            delta=0.0,
            direction=1,
            midstream=False,
            expected_shift_ms=force_layout.gap_ms,
            update_at_ms=0,
            gap_start_ms=force_layout.gap_start_ms,
            gap_end_ms=force_layout.gap_end_ms,
            force_endpoint_at_ms=force_at,
        )
        specs.extend(
            SessionSpec(
                model=PRIMARY_MODEL,
                label="force_endpoint",
                cell=cell,
                field="force_endpoint",
                arm_value=0.0,
                connect=dict(FORCE_PINNED),
                layout=force_layout,
                plan=plan,
                repeat=repeat,
            )
            for repeat in range(repeats)
        )

    if quick:
        return specs

    # Secondary model: connect-time only, for the knobs the documentation
    # disagrees about plus the confidence gate.
    for stimulus in STIMULI[:3]:
        layout = _layout(stimulus.gap_ms, stimulus.gap_fill, stimulus.lead_segment)
        for cell in CONNECT_CELLS:
            plan = _cell_plan(stimulus, cell, layout)
            specs.extend(
                SessionSpec(
                    model=SECONDARY_MODEL,
                    label=f"{stimulus.field}@{SECONDARY_MODEL}",
                    cell=cell,
                    field=stimulus.field,
                    lead_segment=stimulus.lead_segment,
                    pin_rationale=stimulus.pin_rationale,
                    arm_value=plan.arm_value,
                    connect={**stimulus.pinned, stimulus.field: plan.arm_value},
                    layout=layout,
                    plan=plan,
                    repeat=repeat,
                )
                for repeat in range(SECONDARY_REPEATS)
            )

    return specs


def estimate(specs: Sequence[SessionSpec]) -> tuple[int, float, float]:
    """Pre-flight cost. Pure. `O(n)`.

    Returns:
        Session count, streamed audio in minutes, and estimated USD.
    """
    audio_ms = sum(s.layout.total_ms for s in specs)
    minutes = audio_ms / 60_000
    return len(specs), minutes, minutes / 60 * STREAMING_USD_PER_HOUR


async def run_session(
    spec: SessionSpec,
    *,
    api_key: str,
    seed_pcm: bytes,
    trace_dir: Path,
    raw: bool,
    session_factory: SessionFactory = AssemblyAISession,
) -> CellObservation:
    """Open one session, feed one clip, and return what it measured.

    A rejection is attributable because exactly one field is ever changed: the
    `Error` frame closes this session and no other.

    Args:
        spec: The cell to run.
        api_key: Server-side credential (INV-5).
        seed_pcm: The owner-recorded seed speech.
        trace_dir: Where the JSONL trace lands.
        raw: Disable redaction (INV-6).
        session_factory: Builds the upstream; `FakeProbeSession` for `--fake`.

    Returns:
        The measurement, carrying `error_code` if the field was rejected.
    """
    pcm = build_clip(seed_pcm, layout=spec.layout, segments=lead_spans(spec))
    frames = frames_of(pcm, sample_rate=SAMPLE_RATE)
    sink = TraceSink(spec.session_id, directory=trace_dir, raw=raw)
    feeder = PacedFeeder(
        on_frame=lambda r: sink.emit(
            "frame",
            {
                "n": r.n,
                "bytes": r.bytes,
                "deadline_ms": r.deadline_ms,
                "actual_ms": r.actual_ms,
                "lag_ms": r.lag_ms,
                "max_lag_ms": r.max_lag_ms,
            },
            t_ms=r.n * 50,
            direction="up",
        )
    )

    session = session_factory(
        api_key=api_key,
        model=spec.model,
        sample_rate=SAMPLE_RATE,
        config=spec.connect,
        on_frame=lambda f: sink.emit(
            "event", dict(f), t_ms=feeder.now_ms(), direction="down"
        ),
        on_sent=lambda f: sink.emit(
            "sent", dict(f), t_ms=feeder.now_ms(), direction="up"
        ),
    )

    sink.emit(
        "meta",
        {
            "probe_version": 1,
            "model": spec.model,
            "cell": spec.cell,
            "field": spec.field,
            "arm_value": spec.arm_value,
            "pinned": dict(spec.connect),
            "connect_url": redact_url(session.url),
            "clip_sha256": clip_sha256(pcm),
            "clip_ms": spec.layout.total_ms,
            "gap_start_ms": spec.layout.gap_start_ms,
            "gap_end_ms": spec.layout.gap_end_ms,
            "gap_fill": spec.layout.gap_fill,
            "pin_rationale": spec.pin_rationale,
            "lead_segment": spec.lead_segment,
            "segment_spans_ms": [list(x) for x in spec.segments],
            "frame_ms": 50,
            "trace_raw": raw,
        },
        t_ms=0,
        direction="local",
    )

    drain = asyncio.ensure_future(sink.drain())
    # Bound before the try: the `finally` below records the measurement, and an
    # exception on a path that never assigned it would surface as an
    # UnboundLocalError that masks the real failure.
    observation = _failed(spec, None)
    try:
        async with session:

            async def feed() -> None:
                report = await feeder.feed(frames, session.send_audio)
                sink.emit(
                    "feed_report",
                    {
                        "frames": report.frames,
                        "audio_ms": report.audio_ms,
                        "max_lag_ms": report.max_lag_ms,
                        "sum_lag_ms": report.sum_lag_ms,
                        "p99_lag_ms": report.p99_lag_ms,
                    },
                    t_ms=report.audio_ms,
                )
                await session.terminate()

            observation = await probe(
                session,
                model=spec.model,
                plan=spec.plan,
                feed=feed,
                clock=feeder.now_ms,
                wait_until=feeder.wait_until_ms,
            )
    except UpstreamError as exc:
        if CONCURRENCY_MARKER in exc.message.lower():
            raise ConcurrencyLimitError(exc.message) from exc
        observation = _failed(spec, exc.error_code)
    except (ConnectionClosed, OSError) as exc:
        # A bare transport close with no Error frame: the server hung up without
        # saying why, which is not attributable to the field under test.
        observation = _failed(spec, None)
        if CONCURRENCY_MARKER in str(exc).lower():
            raise ConcurrencyLimitError(str(exc)) from exc
    finally:
        sink.emit(
            "verdict",
            {
                "cell": spec.cell,
                "field": spec.field,
                "arm_value": spec.arm_value,
                "boundary_ms": (
                    None
                    if math.isinf(observation.boundary_ms)
                    else observation.boundary_ms
                ),
                "last_word_end_ms": observation.last_word_end_ms,
                "error_code": observation.error_code,
                "confidence_samples": list(observation.confidence_samples),
            },
            t_ms=spec.layout.total_ms,
        )
        await sink.aclose()
        drain.cancel()

    return observation


def summarise(result: RunResult) -> Capabilities:
    """Reduce every measurement to the capability record. Pure. `O(n log n)`."""
    by_field = [
        (
            stimulus.field,
            result.observations.get(stimulus.field, []),
            float(stimulus.expected_shift_ms),
            stimulus.direction,
        )
        for stimulus in STIMULI
    ]
    return classify(
        by_field,
        control=result.control,
        force_endpoint=_force_verdict(result.force),
        has_word_timings=any(
            o.last_word_end_ms is not None
            for obs in result.observations.values()
            for o in obs
        ),
    )


def _force_verdict(observations: Sequence[CellObservation]) -> KnobVerdict:
    """Classify `ForceEndpoint` the same way as any knob. Pure. `O(n)`.

    Proven only if the test arm ended a turn inside the gap and the control arm,
    on the identical clip, did not. A test arm alone proves nothing: the gap might
    simply have been long enough to end naturally.
    """
    if any(o.error_code is not None for o in observations):
        return KnobVerdict.REJECTED
    test = [o.boundary_ms for o in observations if o.cell == "force_test"]
    control = [o.boundary_ms for o in observations if o.cell == "force_control"]
    if not test or not control:
        return KnobVerdict.UNPROVEN
    fired = all(b != NO_BOUNDARY for b in test)
    quiet = all(b == NO_BOUNDARY for b in control)
    return KnobVerdict.LIVE if fired and quiet else KnobVerdict.UNPROVEN


def format_report(caps: Capabilities, result: RunResult, *, provisional: bool) -> str:
    """Render the capability report. Pure. `O(n)`.

    Args:
        caps: What was measured.
        result: The raw observations behind it.
        provisional: True for a `--quick` run, which has no noise floor.

    Returns:
        The report, ready for stdout and for ADR-001.
    """
    lines: list[str] = ["", "Nod capability probe — ADR-001", "=" * 52]
    if provisional:
        lines += ["", "PROVISIONAL: N=1. No spread, so no separation from noise.", ""]

    lines.append("")
    lines.append(f"{'knob':<36} {'verdict':<12} note")
    lines.append("-" * 88)
    for stimulus in STIMULI:
        verdict = caps.verdict(stimulus.field)
        lines.append(f"{stimulus.field:<36} {verdict.value:<12} {stimulus.note}")

    lines += [
        "",
        f"{'ForceEndpoint':<36} {caps.force_endpoint.value}",
        f"{'end_of_turn_confidence':<36} {caps.confidence_field.value}",
        f"{'word timings':<36} {'present' if caps.has_word_timings else 'ABSENT'}",
        "",
        "Interpretation",
        "-" * 52,
    ]

    conf_knob = caps.verdict("end_of_turn_confidence_threshold")
    conf_arm_valid = conf_knob in (KnobVerdict.LIVE, KnobVerdict.INERT)

    if not conf_arm_valid:
        lines += [
            f"Model class: NOT DETERMINED (confidence arm returned {conf_knob.value}).",
            "",
            "The arm did not measure anything, so it says nothing about whether",
            "this model is confidence-based. Reporting a class from a null arm",
            "would state a property of the model on the strength of an",
            "experiment that did not run.",
        ]
    elif caps.confidence_field is ConfidenceField.VARYING and (
        conf_knob is KnobVerdict.LIVE
    ):
        lines.append("Model class: confidence-based. The confidence axis is live.")
    else:
        lines += [
            "Model class: the confidence axis is not usable on this model.",
            "",
            "confidence_axis: dead. This is the measured state on",
            "universal-streaming-english and the law already reflects it:",
            "ADR-011 routed disfluency and recent_cuts onto max_turn_silence,",
            "put the context axis on min_turn_silence, weighted jitter 0 and",
            "stopped sending the threshold. The capability gate still consults",
            "it, so a model that honours the field can be enabled by a future",
            "ADR without re-litigating the law.",
        ]

    silence_live = {"min_turn_silence", "max_turn_silence"} & caps.updatable_fields
    if not silence_live:
        lines += [
            "",
            "CONTROL_SPEC §0 fact 2 does NOT hold for this model: neither silence",
            "knob is live mid-stream, so max_turn_silence is not a hard cutoff Nod",
            "can move. The control law cannot work as written.",
        ]

    if result.secondary:
        lines += [
            "",
            f"Secondary model ({SECONDARY_MODEL}), connect-time only",
            "-" * 52,
            "Settles whether min_turn_silence / max_turn_silence apply at all on a",
            "model the documentation agrees about. Never merged into the verdicts",
            "above: a different model is a different measurement.",
        ]
        for field_name, observations in sorted(result.secondary.items()):
            match = next((k for k in STIMULI if k.field == field_name), None)
            if match is None:
                continue
            verdict = verdict_for(
                observations,
                expected_shift_ms=float(match.expected_shift_ms),
                direction=match.direction,
            )
            connect_only = (
                "connect-time effect"
                if verdict is not KnobVerdict.INERT
                else "no connect-time effect"
            )
            lines.append(f"  {field_name:<36} {connect_only}")

    disabled = degrade(caps)
    lines += [
        "",
        f"Degradation (CONTROL_SPEC §7): {', '.join(sorted(disabled)) or 'none'}",
        f"Sendable mid-stream: {', '.join(sorted(caps.updatable_fields)) or 'NONE'}",
        "",
    ]
    if result.errors:
        lines += ["Errors:", *(f"  {e}" for e in result.errors), ""]
    return "\n".join(lines)


async def _run_session_with_retry(
    spec: SessionSpec,
    *,
    api_key: str,
    seed_pcm: bytes,
    trace_dir: Path,
    raw: bool,
    session_factory: SessionFactory,
    out: TextIO,
) -> CellObservation:
    """Run one session, retrying only a concurrency refusal.

    A concurrency refusal says nothing about the field under test, so recording
    it as a verdict would be a measurement of the account rather than the model.
    Every other failure is returned as-is.

    Args:
        spec: The cell to run.
        api_key: Server-side credential.
        seed_pcm: Seed speech.
        trace_dir: Where traces land.
        raw: Disable redaction.
        session_factory: Builds the upstream.
        out: Where to report a retry.

    Returns:
        The measurement.

    Raises:
        ConcurrencyLimitError: Still refused after every backoff.
    """
    for attempt, wait_s in enumerate((*CONCURRENCY_BACKOFF_S, None)):
        try:
            return await run_session(
                spec,
                api_key=api_key,
                seed_pcm=seed_pcm,
                trace_dir=trace_dir,
                raw=raw,
                session_factory=session_factory,
            )
        except ConcurrencyLimitError:
            if wait_s is None:
                raise
            out.write(
                f"      concurrency cap hit; waiting {wait_s:.0f}s "
                f"(attempt {attempt + 2})\n"
            )
            out.flush()
            await asyncio.sleep(wait_s)
    raise AssertionError  # pragma: no cover - the loop always returns or raises


async def run(
    specs: Sequence[SessionSpec],
    *,
    api_key: str,
    seed_pcm: bytes,
    trace_dir: Path,
    raw: bool,
    out: TextIO,
    session_factory: SessionFactory = AssemblyAISession,
    settle_s: float = SESSION_SETTLE_S,
) -> RunResult:
    """Run every session sequentially and collect the measurements.

    Sequential by design: concurrent sessions contend for rate limits and pollute
    the very timing being measured.
    """
    result = RunResult()
    for index, spec in enumerate(specs, start=1):
        out.write(f"[{index}/{len(specs)}] {spec.label} {spec.cell} r{spec.repeat}\n")
        out.flush()
        if index > 1 and settle_s > 0:
            await asyncio.sleep(settle_s)
        try:
            observation = await _run_session_with_retry(
                spec,
                api_key=api_key,
                seed_pcm=seed_pcm,
                trace_dir=trace_dir,
                raw=raw,
                session_factory=session_factory,
                out=out,
            )
        except (OSError, RuntimeError) as exc:
            result.errors.append(f"{spec.label}/{spec.cell}: {exc}")
            continue

        result.audio_ms += spec.layout.total_ms
        if spec.cell == "control":
            result.control.append(observation)
        elif spec.field == "force_endpoint":
            result.force.append(observation)
        elif spec.model == PRIMARY_MODEL:
            result.observations.setdefault(spec.field, []).append(observation)
        else:
            result.secondary.setdefault(spec.field, []).append(observation)
    return result


def _make_seed(path: Path, out: TextIO, *, rate_wpm: int) -> int:
    """Synthesize a seed recording and report exactly what it is.

    Args:
        path: Where to write the wav.
        out: Where to report.
        rate_wpm: Speaking rate.

    Returns:
        Process exit code.
    """
    try:
        manifest = synthesize_seed(path, rate_wpm=rate_wpm)
    except (SayUnavailableError, SynthesisError) as exc:
        out.write(f"{exc}\n")
        return 2

    out.write(
        f"Wrote {path}\n"
        f"  {manifest.duration_ms} ms, {manifest.channels} channel, "
        f"{manifest.sample_rate} Hz, 16-bit PCM\n"
        f"  engine={manifest.engine} voice={manifest.voice} "
        f"rate={manifest.rate_wpm}wpm\n"
        f"  sha256={manifest.sha256}\n"
        f"  manifest: {path.name}{MANIFEST_SUFFIX}\n"
        f"\n!! {manifest.caveat}\n"
    )
    return 0


def _resolve_api_key(out: TextIO) -> str:
    """Find the credential the way DEPLOYMENT.md §2 says it is configured.

    Through `Settings` rather than `os.environ` directly, so a `.env` file — the
    documented first-run path, `cp .env.example .env` — actually works. An
    exported variable still wins, because that is pydantic-settings' own
    precedence, which keeps a one-off inline key overriding a committed `.env`.

    Args:
        out: Where to explain a failure.

    Returns:
        The key, or an empty string if none was found or settings would not load.
    """
    try:
        secret = get_settings().assemblyai_api_key
    except ValidationError as exc:
        out.write(
            f"Settings would not load, so the credential could not be read:\n{exc}\n"
            f"A `.env` entry that is not in DEPLOYMENT.md §2 will do this; "
            f"`Settings` forbids unknown keys.\n"
        )
        return ""
    if secret is None or not secret.get_secret_value().strip():
        out.write(
            "No ASSEMBLYAI_API_KEY found. Either export it for one run:\n"
            "  ASSEMBLYAI_API_KEY=... python -m nod_bench.probe --quick "
            "--seed-wav data/seed.wav\n"
            "or put it in a .env file:  cp .env.example .env\n"
            "Run with --fake to exercise the probe without a credential.\n"
        )
        return ""
    return secret.get_secret_value()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the capability probe CLI.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="python -m nod_bench.probe",
        description="Probe which turn-detection knobs this model actually honours.",
    )
    parser.add_argument("--quick", action="store_true", help="N=1, primary model only")
    parser.add_argument(
        "--seed-wav", type=Path, help="owner-recorded seed, mono 16 kHz"
    )
    parser.add_argument("--out", type=Path, default=Path("data/traces"))
    parser.add_argument("--raw", action="store_true", help="disable redaction (INV-6)")
    parser.add_argument(
        "--make-seed",
        type=Path,
        metavar="PATH",
        help="synthesize a seed recording with macOS `say`, then exit",
    )
    parser.add_argument(
        "--seed-rate",
        type=int,
        default=RATE_WPM,
        help=f"words per minute for --make-seed (default {RATE_WPM})",
    )
    parser.add_argument(
        "--only",
        metavar="FIELD",
        help="run only this knob's cells, for an isolated re-test",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="run against the in-memory upstream; no key, no credits (INV-7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the session matrix and cost estimate, open no sockets",
    )
    args = parser.parse_args(argv)
    out = sys.stdout

    if args.make_seed is not None:
        return _make_seed(args.make_seed, out, rate_wpm=args.seed_rate)

    # Preconditions first, and identically for --dry-run. A pre-flight check that
    # skips the checks the real run makes is not a pre-flight check: it reports
    # ready and the run then refuses on something the dry run could have caught.
    factory: SessionFactory = AssemblyAISession
    api_key = ""
    if args.fake:
        from nod_bench.fake_session import FakeProbeSession

        factory = FakeProbeSession
    else:
        api_key = _resolve_api_key(out)
        if not api_key:
            return 2

    seed_caveat = ""
    spans: tuple[tuple[int, int], ...] = ()
    segment_ms: tuple[int, ...] | None = None
    if args.seed_wav is None:
        if not args.fake:
            out.write(
                "--seed-wav is required: the probe needs real speech to splice.\n"
                "Generate one with --make-seed data/seed.wav, or record your own.\n"
            )
            return 2
        from nod_bench.probe_clip import room_tone

        seed_pcm = room_tone(20_000, dbfs=-12.0, seed=11)
        seed_note = "generated tone, --fake only"
    else:
        try:
            seed_pcm, seed_note = load_seed(args.seed_wav)
        except SeedError as exc:
            out.write(f"{exc}\n")
            return 2
        manifest = manifest_for(args.seed_wav)
        if manifest is not None:
            if manifest.segments:
                spans = tuple((x.start_ms, x.end_ms) for x in manifest.segments)
                segment_ms = tuple(x.duration_ms for x in manifest.segments)
            if manifest.synthesized:
                seed_caveat = manifest.caveat
                seed_note = (
                    f"{seed_note}; {manifest.engine} voice={manifest.voice} "
                    f"rate={manifest.rate_wpm}wpm sha256={manifest.sha256[:12]}"
                )

    specs = plan_sessions(
        quick=args.quick,
        segment_ms=segment_ms,
    )
    if spans:
        specs = [replace(spec, segments=spans) for spec in specs]
    if args.only:
        specs = [spec for spec in specs if spec.field == args.only]
        if not specs:
            known = sorted({k.field for k in STIMULI})
            out.write(f"--only {args.only!r} matched no cells; known: {known}\n")
            return 2

    sessions, minutes, usd = estimate(specs)
    out.write(
        f"{sessions} sessions, ~{minutes:.1f} min of streamed audio, "
        f"~${usd:.2f} at ${STREAMING_USD_PER_HOUR:.2f}/hr.\n"
        f"Sequential, so expect roughly "
        f"{minutes + sessions * 0.04:.0f} min wall clock.\n"
    )
    if args.fake:
        out.write("Fake upstream: no credential used, no credits spent.\n")
    out.write(f"Seed: {args.seed_wav or '(generated)'} ({seed_note}).\n")
    if seed_caveat:
        out.write(f"\n!! {seed_caveat}\n\n")

    if args.dry_run:
        for spec in specs:
            out.write(
                f"  {spec.model:<28} {spec.label:<36} {spec.cell} r{spec.repeat}\n"
            )
        out.write("\nAll preconditions satisfied. No session was opened.\n")
        return 0

    result = asyncio.run(
        run(
            specs,
            api_key=api_key,
            seed_pcm=seed_pcm,
            trace_dir=args.out,
            raw=args.raw,
            out=out,
            session_factory=factory,
        )
    )
    result.seed_caveat = seed_caveat
    caps = summarise(result)
    out.write(format_report(caps, result, provisional=args.quick))
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
