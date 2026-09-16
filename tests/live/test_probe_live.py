"""One real session against the AssemblyAI streaming API.

INV-7: excluded from CI and from a bare `pytest`. Run deliberately:

    ASSEMBLYAI_API_KEY=... NOD_PROBE_SEED_WAV=data/seed.wav pytest -m live

This is a harness check, not the probe itself — it establishes that a session
opens, frames flow, a trace lands and no credential reaches the file. The actual
capability run is `python -m nod_bench.probe`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nod_bench.probe import PRIMARY_MODEL, run_session
from nod_bench.probe_clip import SAMPLE_RATE, ClipLayout, read_wav
from nod_bench.probe_clip import ZEROS as FILL_ZEROS
from nod_core.capabilities import CellPlan

pytestmark = pytest.mark.live


def _seed() -> bytes:
    path = os.environ.get("NOD_PROBE_SEED_WAV")
    if not path:
        pytest.skip("NOD_PROBE_SEED_WAV is not set")
    pcm, rate = read_wav(Path(path))
    if rate != SAMPLE_RATE:
        pytest.skip(f"seed wav is {rate} Hz, need {SAMPLE_RATE} Hz")
    return pcm


@pytest.mark.asyncio
async def test_one_session_opens_streams_and_leaves_a_clean_trace(
    tmp_path: Path,
) -> None:
    api_key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not api_key:
        pytest.skip("ASSEMBLYAI_API_KEY is not set")

    from nod_bench.probe import SessionSpec

    layout = ClipLayout(gap_ms=1500, gap_fill=FILL_ZEROS)
    spec = SessionSpec(
        model=PRIMARY_MODEL,
        label="smoke",
        cell="connect_low",
        field="max_turn_silence",
        arm_value=600.0,
        connect={
            "end_of_turn_confidence_threshold": 0.95,
            "min_turn_silence": 400,
            "max_turn_silence": 600,
        },
        layout=layout,
        plan=CellPlan(
            cell="connect_low",
            field="max_turn_silence",
            arm_value=600.0,
            delta=2400.0,
            direction=1,
            midstream=False,
            expected_shift_ms=1500,
            update_at_ms=3000,
            gap_start_ms=layout.gap_start_ms,
            gap_end_ms=layout.gap_end_ms,
        ),
        repeat=0,
    )

    observation = await run_session(
        spec, api_key=api_key, seed_pcm=_seed(), trace_dir=tmp_path, raw=False
    )
    assert observation.field == "max_turn_silence"

    traces = list(tmp_path.glob("*.jsonl"))
    assert traces, "the session left no trace"
    records = [json.loads(line) for line in traces[0].read_text().splitlines()]

    kinds = {r["kind"] for r in records}
    assert "meta" in kinds
    assert "frame" in kinds, "no frame timing was recorded"

    # INV-5: the credential never reaches the file, in any form.
    body = traces[0].read_text()
    assert api_key not in body

    # EC-37: the feeder held its schedule, or the run is void.
    frames = [r for r in records if r["kind"] == "frame"]
    assert max(f["payload"]["max_lag_ms"] for f in frames) < 25.0

    # The server said something we can read.
    assert any(r["kind"] == "event" for r in records)
