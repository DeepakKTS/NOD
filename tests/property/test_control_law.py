"""The control-law properties that outlive any particular implementation.

CONTROL_SPEC.md §9 tests 9 and 10, which replace the test that used to require
both axes to move. That one is vacuous now: there is only one axis (ADR-011).

`decide()` is a P5 stub, so these are xfail-strict. When P5 lands they turn green
on their own, and if P5 lands without satisfying them the strict marker turns the
unexpected pass into a failure rather than letting it slip by.
"""

from __future__ import annotations

import pytest

from nod_core import arbiter
from nod_core.capabilities import UPDATABLE_FIELDS
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    KnobVerdict,
    SpeakerFeatures,
    TurnConfig,
    WindowHint,
)

CAPS = Capabilities(
    knobs=tuple((f, KnobVerdict.LIVE) for f in UPDATABLE_FIELDS),
    confidence_field=ConfidenceField.VARYING,
    force_endpoint=KnobVerdict.LIVE,
    has_word_timings=True,
)
BASE = TurnConfig(
    min_turn_silence_ms=arbiter.BASE_MIN_MS,
    max_turn_silence_ms=arbiter.BASE_MAX_MS,
    end_of_turn_confidence_threshold=0.4,
    vad_threshold=None,
)
HINT = WindowHint(min_mult=1.0, max_mult=1.0)


def _features(*, g_p90_ms: float, jitter: float = 0.2) -> SpeakerFeatures:
    return SpeakerFeatures(
        n_gaps=20,
        g_p50_ms=280.0,
        g_p90_ms=g_p90_ms,
        speech_rate=2.4,
        disfluency=0.3,
        jitter=jitter,
        recent_cuts=0.0,
        cold=False,
    )


def _decide(features: SpeakerFeatures) -> TurnConfig:
    engine = arbiter.Arbiter(capabilities=CAPS)
    patch = engine.decide(
        arbiter.ArbiterInput(
            features=features,
            hint=HINT,
            expected_answer="free",
            current=BASE,
            capabilities=CAPS,
            ceiling_ms=arbiter.DEFAULT_CEILING_MS,
            turn_order=5,
            t_ms=5000,
            host_override_fields=frozenset(),
            patches_sent=0,
        )
    )
    assert patch is not None, "a warm profile past hysteresis must emit a patch"
    return patch.config


@pytest.mark.xfail(
    reason="decide() is a P5 stub", raises=NotImplementedError, strict=True
)
def test_an_incomplete_utterance_widens_max_turn_silence() -> None:
    """CONTROL_SPEC §9 test 9. The test the whole project turns on.

    A caller pausing mid-sentence has produced an incomplete utterance, and that
    is the regime `max_turn_silence` governs — the model's own gate keeps waiting
    and nothing else will end the turn. A profile whose `g_p90` implies a pause
    longer than the base window must therefore widen that knob.

    This replaces §9's old requirement that both axes move. Confidence is not an
    axis any more (ADR-011), so a law that moved only `min_turn_silence` would
    pass the old test while leaving every mid-sentence pause exposed.
    """
    slow = _decide(_features(g_p90_ms=1400.0))
    assert slow.max_turn_silence_ms > arbiter.BASE_MAX_MS, (
        "a caller whose p90 pause exceeds the base window must get a wider "
        "max_turn_silence; that is the only knob that tolerates a mid-sentence pause"
    )


@pytest.mark.xfail(
    reason="decide() is a P5 stub", raises=NotImplementedError, strict=True
)
def test_a_longer_pause_profile_widens_max_more_than_a_shorter_one() -> None:
    """Monotonic in the direction that matters, not merely non-zero."""
    assert (
        _decide(_features(g_p90_ms=1800.0)).max_turn_silence_ms
        > _decide(_features(g_p90_ms=700.0)).max_turn_silence_ms
    )


@pytest.mark.xfail(
    reason="decide() is a P5 stub", raises=NotImplementedError, strict=True
)
def test_jitter_has_no_effect_on_any_output() -> None:
    """CONTROL_SPEC §9 test 10, pinning ADR-011's weight-0 decision.

    §2.4 assumed the confidence field measures speaker uncertainty; P1 measured
    it near zero throughout an utterance and spiking only on the boundary frame.
    The feature is still computed so the bench can evaluate it, but restoring its
    weight must be a deliberate change with a failing test, not a silent one.
    """
    assert arbiter.JITTER_GAIN == 0.0
    assert _decide(_features(g_p90_ms=1400.0, jitter=0.0)) == _decide(
        _features(g_p90_ms=1400.0, jitter=1.0)
    )


def test_the_confidence_axis_has_no_constants_left() -> None:
    """ADR-011: removed rather than frozen, so it cannot be reintroduced quietly."""
    for gone in ("BASE_CONF", "CONF_FLOOR", "CONF_CEIL", "CONF_JITTER_GAIN"):
        assert not hasattr(arbiter, gone), f"{gone} survived the ADR-011 removal"


def test_the_ceiling_subtracts_a_measured_overhead() -> None:
    """EC-49 and INV-9: the constant exists and is not hand-written."""
    assert hasattr(arbiter, "ENDPOINT_OVERHEAD_MS")
    assert arbiter.ENDPOINT_OVERHEAD_MS == 0, (
        "must stay 0 until `make bench` measures it; a hand-written value here "
        "is an INV-9 violation in the control law"
    )
