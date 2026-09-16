"""The probe's session matrix, cost estimate and report.

These are the parts that decide what gets measured and what the measurement is
allowed to claim, so they are tested offline and in full. The live run itself is
`tests/live/test_probe_live.py`, excluded from CI by INV-7.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from nod_bench.probe import (
    CONNECT_CELLS,
    FULL_REPEATS,
    MID_CELLS,
    PRIMARY_MODEL,
    SECONDARY_MODEL,
    STIMULI,
    RunResult,
    SessionSpec,
    _force_verdict,
    estimate,
    format_report,
    main,
    plan_sessions,
    summarise,
)
from nod_bench.probe_clip import UPDATE_AT_MS, ClipLayout
from nod_core.capabilities import NO_BOUNDARY, CellObservation, CellPlan
from nod_core.types import Capabilities, ConfidenceField, KnobVerdict


def test_every_knob_gets_all_four_cells() -> None:
    """Connect-time and mid-stream, low and high. Two cells would not be readable."""
    specs = plan_sessions(quick=False)
    for stimulus in STIMULI:
        cells = {s.cell for s in specs if s.label == stimulus.field}
        assert cells == {*CONNECT_CELLS, *MID_CELLS}


def test_full_run_is_the_advertised_size() -> None:
    specs = plan_sessions(quick=False)
    primary = [s for s in specs if s.model == PRIMARY_MODEL]
    secondary = [s for s in specs if s.model == SECONDARY_MODEL]

    assert len(primary) == len(STIMULI) * 4 * FULL_REPEATS + FULL_REPEATS * 3
    assert len(secondary) == 3 * len(CONNECT_CELLS) * 2
    assert len(specs) == 69


def test_quick_run_is_primary_only_and_single_repeat() -> None:
    specs = plan_sessions(quick=True)
    assert {s.model for s in specs} == {PRIMARY_MODEL}
    assert {s.repeat for s in specs} == {0}


def test_connect_cells_set_the_knob_and_midstream_cells_do_not() -> None:
    """A mid-stream cell must leave the knob unset at connect, or the update
    cannot be shown to be what changed it."""
    for spec in plan_sessions(quick=True):
        if spec.field in ("", "force_endpoint"):
            continue
        if spec.cell in CONNECT_CELLS:
            assert spec.connect[spec.field] == spec.arm_value
        else:
            assert spec.field not in spec.connect


def test_the_other_three_knobs_are_pinned_in_every_cell() -> None:
    """Isolation is not only 'change one field'; the rest must not be binding."""
    for stimulus in STIMULI:
        for spec in plan_sessions(quick=True):
            if spec.label != stimulus.field:
                continue
            for pinned, value in stimulus.pinned.items():
                assert spec.connect[pinned] == value


def test_update_lands_well_before_the_gap_opens() -> None:
    for spec in plan_sessions(quick=True):
        if spec.cell in MID_CELLS:
            assert spec.plan.update_at_ms == UPDATE_AT_MS
            assert spec.plan.gap_start_ms - spec.plan.update_at_ms >= 1300


def test_vad_threshold_is_probed_against_tone_not_silence() -> None:
    """Against digital zeros both arms agree and a live knob looks inert."""
    vad = next(s for s in STIMULI if s.field == "vad_threshold")
    assert vad.gap_fill == "tone_mid"
    assert vad.direction == -1


def test_arms_straddle_the_gap_for_the_categorical_knobs() -> None:
    """Three of the four are designed to need no statistics at all."""
    for name in ("max_turn_silence", "end_of_turn_confidence_threshold"):
        stimulus = next(s for s in STIMULI if s.field == name)
        if name == "max_turn_silence":
            assert stimulus.low < stimulus.gap_ms < stimulus.high


def test_estimate_reports_sessions_audio_and_cost() -> None:
    sessions, minutes, usd = estimate(plan_sessions(quick=False))
    assert sessions == 69
    assert 8 < minutes < 25
    assert 0.0 < usd < 0.25


def test_force_endpoint_needs_its_control_arm() -> None:
    """A test arm alone proves nothing: the gap might have ended naturally."""

    def obs(cell: str, boundary: float) -> CellObservation:
        return CellObservation(
            cell=cell,
            field="force_endpoint",
            arm_value=0.0,
            boundary_ms=boundary,
            last_word_end_ms=4300,
            error_code=None,
            confidence_samples=(),
            confidence_on_partials=False,
        )

    assert _force_verdict([obs("force_test", 420.0)]) is KnobVerdict.UNPROVEN
    assert (
        _force_verdict([obs("force_test", 420.0), obs("force_control", NO_BOUNDARY)])
        is KnobVerdict.LIVE
    )
    # Both arms ending the turn means the gap did it, not ForceEndpoint.
    assert (
        _force_verdict([obs("force_test", 420.0), obs("force_control", 430.0)])
        is KnobVerdict.UNPROVEN
    )


def _caps(
    conf: ConfidenceField, conf_knob: KnobVerdict, silence: KnobVerdict
) -> Capabilities:
    return Capabilities(
        knobs=(
            ("max_turn_silence", silence),
            ("min_turn_silence", silence),
            ("end_of_turn_confidence_threshold", conf_knob),
            ("vad_threshold", KnobVerdict.UNPROVEN),
        ),
        confidence_field=conf,
        force_endpoint=KnobVerdict.UNPROVEN,
        has_word_timings=True,
    )


def test_report_names_the_confidence_based_case() -> None:
    caps = _caps(ConfidenceField.VARYING, KnobVerdict.LIVE, KnobVerdict.LIVE)
    text = format_report(caps, RunResult(), provisional=False)
    assert "confidence-based" in text
    assert "confidence axis is live" in text


def test_report_spells_out_the_punctuation_consequence() -> None:
    """CONTROL_SPEC §7 row 1 understates this, so the report must not."""
    caps = _caps(ConfidenceField.ABSENT, KnobVerdict.INERT, KnobVerdict.LIVE)
    text = format_report(caps, RunResult(), provisional=False)
    assert "confidence_axis: dead" in text
    assert "disfluency" in text and "recent_cuts" in text and "jitter" in text
    assert "ADR" in text


def test_report_flags_a_dead_silence_axis_as_breaking_the_control_law() -> None:
    caps = _caps(ConfidenceField.VARYING, KnobVerdict.LIVE, KnobVerdict.STATIC_ONLY)
    text = format_report(caps, RunResult(), provisional=False)
    assert "CONTROL_SPEC §0 fact 2 does NOT hold" in text


def test_quick_run_reports_are_labelled_provisional() -> None:
    caps = _caps(ConfidenceField.VARYING, KnobVerdict.LIVE, KnobVerdict.LIVE)
    assert "PROVISIONAL" in format_report(caps, RunResult(), provisional=True)


def test_report_lists_errors_when_a_session_failed() -> None:
    caps = _caps(ConfidenceField.VARYING, KnobVerdict.LIVE, KnobVerdict.LIVE)
    result = RunResult(errors=["max_turn_silence/mid_low: boom"])
    assert "boom" in format_report(caps, result, provisional=False)


def test_summarise_of_an_empty_run_is_all_unproven() -> None:
    caps = summarise(RunResult())
    assert all(v is KnobVerdict.UNPROVEN for _, v in caps.knobs)
    assert caps.updatable_fields == frozenset()


def _spec_with(
    *, segments: tuple[tuple[int, int], ...], lead_segment: int
) -> SessionSpec:
    """A minimal spec for exercising span selection."""
    layout = ClipLayout(gap_ms=1500)
    return SessionSpec(
        model=PRIMARY_MODEL,
        label="x",
        cell="connect_low",
        field="max_turn_silence",
        arm_value=600.0,
        connect={},
        layout=layout,
        plan=CellPlan(
            cell="connect_low",
            field="max_turn_silence",
            arm_value=600.0,
            delta=1.0,
            direction=1,
            midstream=False,
            expected_shift_ms=100,
            update_at_ms=0,
            gap_start_ms=layout.gap_start_ms,
            gap_end_ms=layout.gap_end_ms,
        ),
        repeat=0,
        segments=segments,
        lead_segment=lead_segment,
    )


def _isolated_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A valid seed plus a credential, in a directory with no real `.env`.

    Tests must not depend on whether the developer has a `.env` in the repo
    root: once one exists, `Settings` finds the live credential and a test
    asserting its absence silently changes meaning.
    """
    import wave

    from nod_core.config import Settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "sk-test")

    seed = tmp_path / "seed.wav"
    with wave.open(str(seed), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00" * 16000 * 2 * 8)
    return seed


def test_dry_run_opens_no_socket_and_prints_the_full_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed = _isolated_seed(tmp_path, monkeypatch)
    assert main(["--dry-run", "--seed-wav", str(seed)]) == 0
    printed = capsys.readouterr().out
    assert "69 sessions" in printed
    assert PRIMARY_MODEL in printed
    assert "No session was opened" in printed


def test_live_run_refuses_without_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """INV-7 and INV-5: no key, no session.

    Isolated from the repo root on purpose: a developer's real `.env` would
    otherwise satisfy the credential and turn this into a test of the seed
    check instead, without the name changing.
    """
    from nod_core.config import Settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)

    assert main([]) == 2
    assert "ASSEMBLYAI_API_KEY" in capsys.readouterr().out


def test_live_run_refuses_without_a_seed_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nod_core.config import Settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "not-a-real-key")

    assert main([]) == 2
    printed = capsys.readouterr().out
    assert "--seed-wav is required" in printed
    assert "--make-seed" in printed


def test_report_is_writable_to_a_stream() -> None:
    caps = _caps(ConfidenceField.VARYING, KnobVerdict.LIVE, KnobVerdict.LIVE)
    buffer = io.StringIO()
    buffer.write(format_report(caps, RunResult(), provisional=False))
    assert "ADR-001" in buffer.getvalue()


def test_dry_run_validates_every_precondition_the_real_run_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: --dry-run printed the matrix without checking the seed, then
    the real run refused on exactly that. A pre-flight check that skips the
    checks the flight makes is not a pre-flight check."""
    from nod_core.config import Settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "sk-test")

    assert main(["--quick", "--dry-run", "--seed-wav", str(tmp_path / "gone.wav")]) == 2
    printed = capsys.readouterr().out
    assert "No seed recording at" in printed
    # And crucially, it did not print the matrix as if everything were ready.
    assert "connect_low" not in printed


def test_dry_run_without_a_credential_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nod_core.config import Settings

    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["--quick", "--dry-run"]) == 2
    assert "connect_low" not in capsys.readouterr().out


def test_dry_run_with_everything_present_prints_the_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed = _isolated_seed(tmp_path, monkeypatch)
    assert main(["--quick", "--dry-run", "--seed-wav", str(seed)]) == 0
    printed = capsys.readouterr().out
    assert "19 sessions" in printed
    assert "connect_low" in printed
    assert "All preconditions satisfied" in printed
    assert "No session was opened" in printed


def test_the_lead_span_is_the_requested_segment_not_the_second() -> None:
    """Regression: the full matrix ran three knobs in the wrong regime.

    `build_clip` was handed all four spans and took `segments[:3]`, so the lead
    was always segment 1 — a complete sentence — no matter what `lead_segment`
    said. `max_turn_silence`, `vad_threshold` and `ForceEndpoint` all need the
    fragment regime, and all three reported inert against a stimulus that never
    gave them a chance to bind (EC-50).
    """
    from nod_bench.probe import lead_spans

    spans = ((0, 100), (200, 300), (400, 500), (600, 700))
    spec = _spec_with(segments=spans, lead_segment=3)
    assert lead_spans(spec) == ((0, 100), (600, 700), (400, 500))

    spec_default = _spec_with(segments=spans, lead_segment=1)
    assert lead_spans(spec_default) == ((0, 100), (200, 300), (400, 500))


def test_lead_spans_falls_back_when_the_seed_has_no_fragment() -> None:
    from nod_bench.probe import lead_spans

    three = ((0, 100), (200, 300), (400, 500))
    assert lead_spans(_spec_with(segments=three, lead_segment=3)) is None
    assert lead_spans(_spec_with(segments=(), lead_segment=1)) is None


def test_every_fragment_regime_stimulus_asks_for_a_fragment_segment() -> None:
    """The knobs that only bind after an incomplete utterance must say so."""
    from nod_bench.probe import STIMULI

    by_field = {k.field: k for k in STIMULI}
    assert by_field["max_turn_silence"].lead_segment == 3
    assert by_field["vad_threshold"].lead_segment == 3
    assert by_field["min_turn_silence"].lead_segment == 1
    assert by_field["end_of_turn_confidence_threshold"].lead_segment == 1


def test_the_planned_specs_carry_the_lead_segment_through() -> None:
    """A stimulus asking for the fragment must reach the session that runs it."""
    specs = [
        s
        for s in plan_sessions(quick=True, segment_ms=(100, 100, 100, 100))
        if s.field == "max_turn_silence"
    ]
    assert specs
    assert all(s.lead_segment == 3 for s in specs)
