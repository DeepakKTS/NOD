"""The simulator reproduces what the real service did, or it is an assertion.

ADR-017 makes this load-bearing. `FakeAssemblyAI` is not a replayer; it computes
boundaries. Nothing stops a computed boundary from being confidently wrong, so
it is checked against five cells the real service actually produced
(`data/traces-p0-final`, recorded in ADR-017).

Five points, not one. A single point cannot distinguish *correct* from
*consistently early*: any simulator can be made to match one number by choosing
one constant. The mean-signed-error bound is what catches a uniform bias, and
`test_a_uniform_bias_is_caught_by_the_signed_bound_alone` demonstrates the case
the single-point version is blind to.
"""

from __future__ import annotations

import struct

import pytest

from nod_bench.fake_assemblyai import FRAME_MS, Endpointer
from nod_bench.perturb import Gap

SR = 16_000
SPEECH_MS = 2000.0
GAP_MS = 4000.0

PER_POINT_TOLERANCE_MS = 40.0
"""ADR-017. Justified from the service's own repeat spread (24 ms worst on the
calibrated cell, 39 ms on the max cell) plus headroom, and well inside ADR-014's
100 ms actionability floor."""

MEAN_SIGNED_TOLERANCE_MS = 15.0
"""ADR-017. The bound the absolute one is blind to: five points bounded only in
magnitude cannot tell correct from consistently early."""

CALIBRATION_OVERHEAD_MS = 195.0
"""Approximately the overhead the P1 matrix implies, used **only here**.

Not the production default, which stays 0 because the real value comes from
`make bench` (INV-9, ADR-017). Stated as a constant rather than fitted from the
points below at runtime: fitting it to the mean would force the mean signed
error to zero by construction, making that assertion unfalsifiable — the same
defect as a vacuous invariant (CLAUDE.md §5).
"""

# (label, regime, min_turn_silence, max_turn_silence, boundary the service gave)
POINTS: list[tuple[str, str, float, float, float]] = [
    ("min connect low", "complete", 100.0, 3000.0, 306.0),
    ("min connect high", "complete", 2000.0, 3000.0, 2175.0),
    ("min mid low", "complete", 100.0, 3000.0, 304.0),
    ("min mid high", "complete", 2000.0, 3000.0, 2172.0),
    ("max fragment", "fragment", 400.0, 600.0, 817.0),
]


def _frames() -> list[bytes]:
    """Speech, then a gap long enough for any gate under test. `O(n)`."""
    n = int(SR * FRAME_MS / 1000)
    loud = struct.pack(f"<{n}h", *([8000] * n))
    quiet = struct.pack(f"<{n}h", *([0] * n))
    return [loud] * int(SPEECH_MS / FRAME_MS) + [quiet] * int(GAP_MS / FRAME_MS)


def _boundary(regime: str, mn: float, mx: float, overhead: float) -> float:
    """Where the simulator ends the turn, relative to the gap opening."""
    gaps = (
        Gap(
            start_ms=int(SPEECH_MS),
            end_ms=int(SPEECH_MS + GAP_MS),
            origin="calibration",
            preceding="complete" if regime == "complete" else "fragment",
            certainty="certain",
            basis="calibration fixture",
        ),
    )
    endpointer = Endpointer(
        gaps=gaps,
        min_turn_silence=mn,
        max_turn_silence=mx,
        endpoint_overhead_ms=overhead,
    )
    for frame in _frames():
        fired = endpointer.feed(frame)
        if fired is not None:
            return fired.fired_at_ms - SPEECH_MS
    msg = f"the simulator never ended the turn for {regime} {mn}/{mx}"
    raise AssertionError(msg)


def _errors(bias: float = 0.0) -> list[float]:
    """Signed error per calibration point, simulator minus service."""
    return [
        _boundary(regime, mn, mx, CALIBRATION_OVERHEAD_MS + bias) - measured
        for _, regime, mn, mx, measured in POINTS
    ]


def test_every_calibration_point_is_within_tolerance() -> None:
    """Per-point agreement with the service, across both regimes.

    Measured errors at a 195 ms overhead:
        min connect low   -11.0    min connect high  +20.0
        min mid low        -9.0    min mid high      +23.0
        max fragment      -22.0
    """
    errors = _errors()
    worst = max(abs(e) for e in errors)
    assert worst <= PER_POINT_TOLERANCE_MS, dict(
        zip([p[0] for p in POINTS], errors, strict=True)
    )


def test_the_simulator_is_not_consistently_early_or_late() -> None:
    """The bound a per-point check cannot supply. Measured mean: +0.2 ms."""
    errors = _errors()
    mean_signed = sum(errors) / len(errors)
    assert abs(mean_signed) <= MEAN_SIGNED_TOLERANCE_MS, mean_signed


def test_the_fragment_regime_is_calibrated_too() -> None:
    """Four of five points share the complete-utterance regime.

    A simulator modelling only `min_turn_silence` would pass those four while
    being wrong about the regime Nod exists for, so the `max` point carries its
    own assertion rather than hiding inside an aggregate.
    """
    label, regime, mn, mx, measured = POINTS[-1]
    assert (label, regime) == ("max fragment", "fragment")
    error = _boundary(regime, mn, mx, CALIBRATION_OVERHEAD_MS) - measured
    assert abs(error) <= PER_POINT_TOLERANCE_MS, error


def test_a_uniform_bias_is_caught_by_the_signed_bound_alone() -> None:
    """The case a single-point calibration is blind to, demonstrated.

    A 16 ms uniform bias leaves every point inside the 40 ms per-point bound —
    worst becomes 39.0 ms — while the mean signed error moves to +16.2 ms and
    trips the 15 ms bound. Without the signed check this simulator would be
    reported as calibrated while biasing every TTL in one direction.
    """
    biased = _errors(bias=16.0)
    assert max(abs(e) for e in biased) <= PER_POINT_TOLERANCE_MS
    assert abs(sum(biased) / len(biased)) > MEAN_SIGNED_TOLERANCE_MS


def test_the_regime_selects_the_gate() -> None:
    """`Gap.preceding` chooses the gate; the audio is identical either way."""
    complete = _boundary("complete", 400.0, 3000.0, 0.0)
    fragment = _boundary("fragment", 400.0, 3000.0, 0.0)
    assert complete == pytest.approx(400.0)
    assert fragment == pytest.approx(3000.0)


def test_the_confidence_threshold_changes_nothing() -> None:
    """ADR-001 measured it inert, so the simulator must not honour it."""
    gaps = (
        Gap(
            start_ms=int(SPEECH_MS),
            end_ms=int(SPEECH_MS + GAP_MS),
            origin="c",
            preceding="complete",
            certainty="certain",
            basis="fixture",
        ),
    )
    fired: list[float] = []
    for threshold in (0.0, 0.4, 1.0):
        endpointer = Endpointer(
            gaps=gaps,
            min_turn_silence=400.0,
            end_of_turn_confidence_threshold=threshold,
        )
        for frame in _frames():
            boundary = endpointer.feed(frame)
            if boundary is not None:
                fired.append(boundary.fired_at_ms)
                break
    assert len(set(fired)) == 1, f"the threshold moved the boundary: {fired}"


def test_a_silence_the_sidecar_does_not_describe_uses_the_complete_gate() -> None:
    """The documented fallback in `regime_at`, exercised rather than assumed.

    A silence no gap covers is the end of the clip, where the speaker has
    finished and the semantic gate would fire. Falling back to `fragment` would
    make every clip end on `max_turn_silence` and quietly inflate TTL — which is
    exactly what an unexercised fallback lets through.
    """
    from nod_bench.fake_assemblyai import regime_at

    assert regime_at((), 1234.0) == "complete"

    endpointer = Endpointer(gaps=(), min_turn_silence=400.0, max_turn_silence=3000.0)
    for frame in _frames():
        fired = endpointer.feed(frame)
        if fired is not None:
            assert fired.gate == "min_turn_silence"
            assert fired.regime == "complete"
            assert fired.fired_at_ms - SPEECH_MS == pytest.approx(400.0)
            break
    else:
        pytest.fail("no boundary fired")


def test_a_gap_outside_the_silence_does_not_claim_it() -> None:
    """Lookup is by containment, not by "any gap in the clip"."""
    from nod_bench.fake_assemblyai import regime_at

    elsewhere = (
        Gap(
            start_ms=0,
            end_ms=100,
            origin="pause",
            preceding="fragment",
            certainty="certain",
            basis="fixture",
        ),
    )
    assert regime_at(elsewhere, 50.0) == "fragment"
    assert regime_at(elsewhere, 5000.0) == "complete"


def test_the_vad_default_is_the_documented_one() -> None:
    """0.4, not 0.5. A fake carrying a default the service does not have
    disagrees with it for free, before any configuration is applied.

    Accessed 2026-09-17, BENCH_SPEC §3.
    """
    from nod_bench import fake_assemblyai, fake_session

    assert fake_assemblyai.DEFAULT_VAD == 0.4
    assert fake_session.DEFAULT_VAD == 0.4
    assert Endpointer().vad_threshold == 0.4


def test_the_documented_defaults_are_the_balanced_preset() -> None:
    """BENCH_SPEC §3: `balanced` is also the global default."""
    endpointer = Endpointer()
    assert endpointer.min_turn_silence == 400.0
    assert endpointer.max_turn_silence == 1280.0
    assert endpointer.end_of_turn_confidence_threshold == 0.4
    assert endpointer.endpoint_overhead_ms == 0.0, "0 means unmeasured"
