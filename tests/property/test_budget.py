"""The INV-2 budget test, at the path ARCHITECTURE.md §4 names.

ARCHITECTURE.md §4 states the hard budgets and says they are enforced by this
file in CI: `decide()` p99 under 5 ms, mean under 200 us.

`decide()` is a stub today, so this asserts the stub state with `strict=True`.
The moment P5 implements `decide()`, this becomes a loud XPASS failure — which
is the point. Whoever implements the control law has to replace this with the
real measurement over 100 000 synthetic states (docs/PROMPTS.md P5) rather than
discovering the file months later.
"""

from __future__ import annotations

import pytest

from nod_core.arbiter import DEFAULT_CEILING_MS, Arbiter, ArbiterInput
from nod_core.capabilities import UPDATABLE_FIELDS
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    KnobVerdict,
    SpeakerFeatures,
    TurnConfig,
    WindowHint,
)

DECIDE_P99_BUDGET_MS = 5.0
DECIDE_MEAN_BUDGET_MS = 0.2


def _state() -> ArbiterInput:
    return ArbiterInput(
        features=SpeakerFeatures(
            n_gaps=32,
            g_p50_ms=240.0,
            g_p90_ms=900.0,
            speech_rate=2.6,
            disfluency=0.2,
            jitter=0.3,
            recent_cuts=0.33,
            cold=False,
        ),
        hint=WindowHint(min_mult=1.0, max_mult=1.0),
        expected_answer="free",
        current=TurnConfig(
            min_turn_silence_ms=400,
            max_turn_silence_ms=1280,
            end_of_turn_confidence_threshold=0.4,
            vad_threshold=None,
        ),
        capabilities=Capabilities(
            knobs=tuple((f, KnobVerdict.LIVE) for f in UPDATABLE_FIELDS),
            confidence_field=ConfidenceField.VARYING,
            force_endpoint=KnobVerdict.LIVE,
            has_word_timings=True,
        ),
        ceiling_ms=DEFAULT_CEILING_MS,
        turn_order=7,
        t_ms=12_000,
        host_override_fields=frozenset(),
        patches_sent=3,
    )


@pytest.mark.xfail(
    raises=NotImplementedError,
    strict=True,
    reason="decide() is a P5 stub. Replace this with the real p99 measurement then.",
)
def test_decide_holds_the_latency_budget() -> None:
    """INV-2: `decide()` p99 under 5 ms, mean under 200 us."""
    controller = Arbiter(capabilities=_state().capabilities)
    controller.decide(_state())
