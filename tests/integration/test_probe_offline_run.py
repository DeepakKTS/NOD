"""The whole probe pipeline, offline, on scaled-down clips.

`run()` -> `summarise()` -> `format_report()` against `FakeProbeSession`, which
endpoints on the level of the audio it is actually given rather than replaying a
script. So `LIVE` here is earned from the stimulus, not asserted into existence.

Durations are scaled down; shapes are identical to the live plan. The full-size
`--quick` run against the same fake is a manual check, since 19 real-time-paced
sessions take minutes by design.
"""

from __future__ import annotations

import functools
import io
from pathlib import Path

import pytest

from nod_bench.fake_session import FakeProbeSession
from nod_bench.probe import (
    PRIMARY_MODEL,
    RunResult,
    SessionSpec,
    format_report,
    run,
    summarise,
)
from nod_bench.probe_clip import ZEROS, ClipLayout, room_tone
from nod_core.capabilities import CellPlan
from nod_core.types import KnobVerdict

FIELD = "max_turn_silence"
LOW, HIGH = 200.0, 1500.0
LAYOUT = ClipLayout(
    gap_ms=600,
    gap_fill=ZEROS,
    preamble_ms=200,
    warm_gap_ms=100,
    lead_ms=200,
    trail_ms=200,
    tail_ms=200,
)
PINNED = {"end_of_turn_confidence_threshold": 0.95, "min_turn_silence": 400}
SEED = room_tone(2000, dbfs=-12.0, seed=11)


def _spec(cell: str) -> SessionSpec:
    value = LOW if cell.endswith("low") else HIGH
    midstream = cell.startswith("mid")
    return SessionSpec(
        model=PRIMARY_MODEL,
        label=FIELD,
        cell=cell,
        field=FIELD,
        arm_value=value,
        connect=dict(PINNED) if midstream else {**PINNED, FIELD: value},
        layout=LAYOUT,
        plan=CellPlan(
            cell=cell,
            field=FIELD,
            arm_value=value,
            delta=HIGH - LOW,
            direction=1,
            midstream=midstream,
            expected_shift_ms=600,
            update_at_ms=400,
            gap_start_ms=LAYOUT.gap_start_ms,
            gap_end_ms=LAYOUT.gap_end_ms,
        ),
        repeat=0,
    )


ALL_CELLS = ["connect_low", "connect_high", "mid_low", "mid_high"]


async def _run_all(tmp_path: Path, *, honour: bool) -> RunResult:
    return await run(
        [_spec(c) for c in ALL_CELLS],
        api_key="",
        seed_pcm=SEED,
        trace_dir=tmp_path,
        raw=False,
        out=io.StringIO(),
        session_factory=functools.partial(FakeProbeSession, honour_updates=honour),
    )


@pytest.mark.asyncio
async def test_a_working_knob_is_measured_as_live(tmp_path: Path) -> None:
    result = await _run_all(tmp_path, honour=True)
    assert not result.errors

    boundaries = {o.cell: o.boundary_ms for o in result.observations[FIELD]}
    # Low arm ends the turn inside the gap; high arm outlasts it.
    assert boundaries["connect_low"] != float("inf")
    assert boundaries["connect_high"] == float("inf")
    assert boundaries["mid_low"] != float("inf")
    assert boundaries["mid_high"] == float("inf")


@pytest.mark.asyncio
async def test_a_silently_ignored_update_lands_as_static_only(tmp_path: Path) -> None:
    """The failure mode the whole probe exists to detect, end to end.

    The fake accepts every update, returns no error, and changes nothing. The
    connect-time cells still separate, so the knob is real — but the mid-stream
    pair cannot move the boundary, and that must not read as support.
    """
    result = await _run_all(tmp_path, honour=False)
    observations = result.observations[FIELD]

    from nod_core.capabilities import verdict_for

    verdict = verdict_for(observations, expected_shift_ms=600, direction=1)
    assert verdict is KnobVerdict.STATIC_ONLY


@pytest.mark.asyncio
async def test_every_session_writes_a_redacted_trace(tmp_path: Path) -> None:
    await _run_all(tmp_path, honour=True)
    traces = sorted(tmp_path.glob("*.jsonl"))
    assert len(traces) == len(ALL_CELLS)

    body = traces[0].read_text()
    assert '"kind":"meta"' in body
    assert '"kind":"frame"' in body
    assert '"v":1' in body


@pytest.mark.asyncio
async def test_the_report_renders_from_a_real_run(tmp_path: Path) -> None:
    result = await _run_all(tmp_path, honour=True)
    text = format_report(summarise(result), result, provisional=True)
    assert "ADR-001" in text
    assert "PROVISIONAL" in text
    assert FIELD in text
