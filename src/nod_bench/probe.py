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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, TextIO

from pydantic import ValidationError

from nod_adapters.assemblyai.session import AssemblyAISession, UpstreamError
from nod_bench.feeder import PacedFeeder, frames_of
from nod_bench.probe_clip import (
    SAMPLE_RATE,
    TONE_LOW,
    UPDATE_AT_MS,
    ZEROS,
    ClipLayout,
    SeedError,
    build_clip,
    clip_sha256,
    load_seed,
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
    expected_shift_ms: int
    """How far the boundary should move between the arms, in milliseconds.

    Stated per stimulus rather than derived from `high - low`, because for the
    two threshold knobs the arm values are dimensionless and carry no
    millisecond meaning at all. For a categorical stimulus — one arm ends the
    turn inside the gap, the other never does — the largest observable shift is
    the gap itself.
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
        expected_shift_ms=2400,
        note="low ends the turn inside the gap; high never should",
    ),
    KnobStimulus(
        field="min_turn_silence",
        pinned={"end_of_turn_confidence_threshold": 0.20, "max_turn_silence": 8000},
        gap_ms=2500,
        gap_fill=ZEROS,
        low=100,
        high=2000,
        direction=1,
        expected_shift_ms=1900,
        note="confidence pinned low so the minimum silence is the binding constraint",
    ),
    KnobStimulus(
        field="end_of_turn_confidence_threshold",
        pinned={"min_turn_silence": 100, "max_turn_silence": 8000},
        gap_ms=1500,
        gap_fill=ZEROS,
        low=0.20,
        high=0.95,
        direction=1,
        expected_shift_ms=1500,
        note="silence pinned wide so only the confidence gate can end the turn",
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
        expected_shift_ms=1500,
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
"""Nothing can end a turn inside the gap unless `ForceEndpoint` does."""

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


def plan_sessions(*, quick: bool) -> list[SessionSpec]:
    """Build the full session matrix. Pure. `O(knobs * cells * repeats)`.

    Args:
        quick: Primary model only, one repeat per cell.

    Returns:
        Every session the run will open, in execution order.
    """
    repeats = 1 if quick else FULL_REPEATS
    specs: list[SessionSpec] = []

    for stimulus in STIMULI:
        layout = ClipLayout(gap_ms=stimulus.gap_ms, gap_fill=stimulus.gap_fill)
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
                    arm_value=plan.arm_value,
                    connect=connect,
                    layout=layout,
                    plan=plan,
                    repeat=repeat,
                )
                for repeat in range(repeats)
            )

    # Control: no knob under test, just enough speech to read the confidence field.
    control_layout = ClipLayout(gap_ms=1500, gap_fill=TONE_LOW)
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
    force_layout = ClipLayout(gap_ms=FORCE_GAP_MS, gap_fill=ZEROS)
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
        layout = ClipLayout(gap_ms=stimulus.gap_ms, gap_fill=stimulus.gap_fill)
        for cell in CONNECT_CELLS:
            plan = _cell_plan(stimulus, cell, layout)
            specs.extend(
                SessionSpec(
                    model=SECONDARY_MODEL,
                    label=f"{stimulus.field}@{SECONDARY_MODEL}",
                    cell=cell,
                    field=stimulus.field,
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
    pcm = build_clip(seed_pcm, layout=spec.layout)
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
            "frame_ms": 50,
            "trace_raw": raw,
        },
        t_ms=0,
        direction="local",
    )

    drain = asyncio.ensure_future(sink.drain())
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
        observation = CellObservation(
            cell=spec.cell,
            field=spec.field,
            arm_value=spec.arm_value,
            boundary_ms=NO_BOUNDARY,
            last_word_end_ms=None,
            error_code=exc.error_code,
            confidence_samples=(),
            confidence_on_partials=False,
        )
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

    conf_live = caps.verdict("end_of_turn_confidence_threshold") is KnobVerdict.LIVE
    if caps.confidence_field is ConfidenceField.VARYING and conf_live:
        lines.append("Model class: confidence-based. The confidence axis is live.")
    else:
        lines += [
            "Model class: punctuation-based, or the confidence axis is unusable.",
            "",
            "confidence_axis: dead. CONTROL_SPEC §7 row 1 calls this 'jitter",
            "disabled, conf frozen at base', which understates it. §4's conf line",
            "is the ONLY consumer of disfluency, recent_cuts and jitter, so",
            "freezing conf strands three of the four speaker features with no path",
            "to any output, and hint.conf_delta has nowhere to land.",
            "",
            "Two ways forward, and this is a control-law decision, not a commit:",
            "  (a) pin a confidence-based model and keep the axis live;",
            "  (b) revise CONTROL_SPEC §4 to route disfluency, recent_cuts and",
            "      jitter into the silence axis. That is an ADR.",
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


async def run(
    specs: Sequence[SessionSpec],
    *,
    api_key: str,
    seed_pcm: bytes,
    trace_dir: Path,
    raw: bool,
    out: TextIO,
    session_factory: SessionFactory = AssemblyAISession,
) -> RunResult:
    """Run every session sequentially and collect the measurements.

    Sequential by design: concurrent sessions contend for rate limits and pollute
    the very timing being measured.
    """
    result = RunResult()
    for index, spec in enumerate(specs, start=1):
        out.write(f"[{index}/{len(specs)}] {spec.label} {spec.cell} r{spec.repeat}\n")
        out.flush()
        try:
            observation = await run_session(
                spec,
                api_key=api_key,
                seed_pcm=seed_pcm,
                trace_dir=trace_dir,
                raw=raw,
                session_factory=session_factory,
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
    if secret is None:
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

    specs = plan_sessions(quick=args.quick)
    sessions, minutes, usd = estimate(specs)
    out.write(
        f"{sessions} sessions, ~{minutes:.1f} min of streamed audio, "
        f"~${usd:.2f} at ${STREAMING_USD_PER_HOUR:.2f}/hr.\n"
        f"Sequential, so expect roughly "
        f"{minutes + sessions * 0.04:.0f} min wall clock.\n"
    )
    if args.dry_run:
        for spec in specs:
            out.write(
                f"  {spec.model:<28} {spec.label:<36} {spec.cell} r{spec.repeat}\n"
            )
        return 0

    factory: SessionFactory = AssemblyAISession
    api_key = ""
    if args.fake:
        from nod_bench.fake_session import FakeProbeSession

        factory = FakeProbeSession
        out.write("Fake upstream: no credential used, no credits spent.\n")
    else:
        api_key = _resolve_api_key(out)
        if not api_key:
            return 2

    if args.seed_wav is None:
        if not args.fake:
            out.write(
                "--seed-wav is required: the probe needs real speech to splice.\n"
            )
            return 2
        # The fake endpoints on level alone, so synthetic speech is enough to
        # exercise every path. A live run still requires the real recording.
        from nod_bench.probe_clip import room_tone

        seed_pcm = room_tone(20_000, dbfs=-12.0, seed=11)
    else:
        try:
            seed_pcm, note = load_seed(args.seed_wav)
        except SeedError as exc:
            out.write(f"{exc}\n")
            return 2
        out.write(f"Seed: {args.seed_wav} ({note}).\n")

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
    caps = summarise(result)
    out.write(format_report(caps, result, provisional=args.quick))
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
