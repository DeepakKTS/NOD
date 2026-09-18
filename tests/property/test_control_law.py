"""The control-law properties that outlive any particular implementation.

All ten of CONTROL_SPEC.md §9, written before the law exists so that the law is
shaped by them rather than measured against them afterwards.

- **Properties 1, 2, 4, 5, 6, 7 and 8** are `hypothesis` properties over the
  domains in `strategies.py`, as §9's opening line specifies.
- **Property 3** is written and conditionally skipped. It is vacuous while
  `ENDPOINT_OVERHEAD_MS` is 0 — CLAUDE.md §5 names it as the worked example of an
  invariant that cannot be violated under the constants in force. The skip is
  conditional on the constant, so the property activates by itself once `make
  bench` measures an overhead.
- **Properties 9 and 10** replace §9's original "both axes must move", which went
  vacuous when ADR-011 left only one axis.

`decide()` and `control_law()` are P5 stubs, so every property here is
`xfail(strict=True, raises=NotImplementedError)`. When P5 lands they turn green on
their own; if P5 lands without satisfying them, the strict marker turns the
unexpected pass into a failure rather than letting it slip by.

Two §5 guard conflicts were found by writing these rather than by reading §5, and
both are now settled by ADR — the properties came first and the decisions followed
them, which is the order this gate exists to produce:

- `NARROW_STEP` = 12 % cannot clear `HYST` = 15 %, so narrowing was unreachable.
  **ADR-020**: hysteresis gates the law's target, decay bounds the emitted step.
  No constant moved. See `test_a_narrowing_is_reachable_at_all`.
- the latency ceiling could undo the invariant repair below `ceiling_ms` 1100.
  **ADR-021**: that ceiling is rejected at configuration; §4's ordering stays.
  See `strategies.CEILING_FLOOR_MS`.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import get_args
from unittest import mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nod_core import arbiter
from nod_core.capabilities import UPDATABLE_FIELDS
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    ExpectedAnswer,
    KnobVerdict,
    SpeakerFeatures,
    TurnConfig,
    WindowHint,
)
from tests.property import strategies

STUB_XFAIL = pytest.mark.xfail(
    reason="decide() and control_law() are P5 stubs",
    raises=NotImplementedError,
    strict=True,
)
"""One marker for every §9 property, and `strict=True` is the whole mechanism.

These properties are written before the control law exists precisely so the law is
shaped by them, which only works if an implementation that does not satisfy them
cannot land quietly. A non-strict xfail would let the first passing implementation
flip to XPASS and say nothing; a strict one turns the unexpected pass into a
failure that has to be read. `raises=NotImplementedError` narrows it further: once
the law exists, a property failing with an `AssertionError` is a real failure
rather than a still-pending stub, and this marker will not absorb it.
"""

PROPERTY_SETTINGS = settings(deadline=None)
"""No per-example deadline.

Hypothesis fails an example taking over 200 ms by default, which on a shared CI
runner is a flake rather than a signal. It is also the wrong instrument for INV-2:
a property test's job is the invariant, and the latency budget is measured in
`test_budget.py` over 100 000 states with a stated clock and a stated warmup. Two
mechanisms guarding one number would mean the weaker one reports first.
`max_examples` stays at hypothesis's default of 100; that is the knob to raise if
a clamp bug ever slips through this file.
"""

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


@STUB_XFAIL
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


@STUB_XFAIL
def test_a_longer_pause_profile_widens_max_more_than_a_shorter_one() -> None:
    """Monotonic in the direction that matters, not merely non-zero."""
    assert (
        _decide(_features(g_p90_ms=1800.0)).max_turn_silence_ms
        > _decide(_features(g_p90_ms=700.0)).max_turn_silence_ms
    )


@STUB_XFAIL
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


# --- CONTROL_SPEC §9, properties 1 to 8 -------------------------------------
#
# §9's opening line is "Written with `hypothesis`, over arbitrary feature
# vectors". The domain each property draws from lives in `strategies.py`, where
# every bound is justified against the spec line that sets it.
NON_BOOLEAN_ANSWERS = st.sampled_from(
    tuple(answer for answer in strategies.EXPECTED_ANSWERS if answer != "boolean")
)
"""Everything except `boolean`, for properties the §5 boolean floor would confound."""


def _assert_within_clamps(config: TurnConfig) -> None:
    assert arbiter.MIN_MS_FLOOR <= config.min_turn_silence_ms <= arbiter.MIN_MS_CEIL, (
        f"min_turn_silence_ms={config.min_turn_silence_ms} is outside the §4 "
        f"clamp [{arbiter.MIN_MS_FLOOR}, {arbiter.MIN_MS_CEIL}]"
    )
    assert arbiter.MAX_MS_FLOOR <= config.max_turn_silence_ms <= arbiter.MAX_MS_CEIL, (
        f"max_turn_silence_ms={config.max_turn_silence_ms} is outside the §4 "
        f"clamp [{arbiter.MAX_MS_FLOOR}, {arbiter.MAX_MS_CEIL}]"
    )


def _engine(state: arbiter.ArbiterInput) -> arbiter.Arbiter:
    return arbiter.Arbiter(capabilities=state.capabilities, ceiling_ms=state.ceiling_ms)


def test_the_expected_answer_domain_is_complete() -> None:
    """The strategy covers every `ExpectedAnswer`, asserted rather than derived.

    CLAUDE.md §5: two things that must agree should agree by a check that can
    fail. `strategies.EXPECTED_ANSWERS` is written out by hand because reaching
    the `Literal`'s members needs a private `__value__`; this is what stops the
    hand-written copy drifting. A ninth answer class added to the type with no
    strategy coverage goes red here rather than silently narrowing every property
    below.
    """
    declared = get_args(ExpectedAnswer.__value__)
    assert set(strategies.EXPECTED_ANSWERS) == set(declared)
    assert len(strategies.EXPECTED_ANSWERS) == len(declared) == 8


@STUB_XFAIL
@PROPERTY_SETTINGS
@given(state=strategies.arbiter_inputs())
def test_output_is_always_within_the_hard_clamps(state: arbiter.ArbiterInput) -> None:
    """§9 property 1. `min_ms ∈ [160, 900]` and `max_ms ∈ [400, 4000]`, always.

    Asserted on both surfaces, because they can disagree. `control_law` applies
    the clamps; `decide` then applies the §5 guards on top, and asymmetric decay
    interpolates towards the law's output from a `current` that the clamps do not
    constrain. A decay step computed from an out-of-range `current` could leave
    the emitted value outside the clamp even though the law's own output was
    inside it, so checking the law alone would not cover what reaches the socket.
    """
    _assert_within_clamps(
        arbiter.control_law(
            state.features,
            state.hint,
            cold=state.features.cold,
            ceiling_ms=state.ceiling_ms,
        )
    )
    patch = _engine(state).decide(state)
    if patch is not None:
        _assert_within_clamps(patch.config)


@STUB_XFAIL
@PROPERTY_SETTINGS
@given(state=strategies.arbiter_inputs())
def test_max_ms_always_clears_min_ms_by_the_invariant_gap(
    state: arbiter.ArbiterInput,
) -> None:
    """§9 property 2. `max_ms >= min_ms + 200`, always.

    The gap is what makes the two knobs describe a window rather than a point. If
    `max_ms` can land at or below `min_ms`, the incomplete-utterance regime has no
    room left in it at all and the primary control surface of ADR-011 stops
    existing.

    The domain excludes `ceiling_ms` below `CEILING_FLOOR_MS`, because §4 applies
    the ceiling *after* the invariant repair and a low enough ceiling therefore
    breaks this property by the spec's own ordering. ADR-021 settles that by making
    such a ceiling an invalid configuration rather than by re-ordering §4, so the
    exclusion is the boundary of the valid domain and not a corner of it. The
    arithmetic is at `strategies.CEILING_FLOOR_MS`.

    Asserted on `decide()`'s output as well as the law's, which is load-bearing
    under ADR-020: asymmetric decay applies per field, so a reference config
    sitting at `max = min + 200` narrowed 12 % on both fields leaves a gap of
    176 ms. ADR-020 step 4 re-applies repair after decay for exactly this reason,
    and this is the property that catches it if it is skipped.
    """
    law = arbiter.control_law(
        state.features,
        state.hint,
        cold=state.features.cold,
        ceiling_ms=state.ceiling_ms,
    )
    patch = _engine(state).decide(state)
    reached = [law] if patch is None else [law, patch.config]
    for config in reached:
        assert (
            config.max_turn_silence_ms
            >= config.min_turn_silence_ms + arbiter.INVARIANT_GAP_MS
        ), (
            f"max={config.max_turn_silence_ms} does not clear "
            f"min={config.min_turn_silence_ms} by {arbiter.INVARIANT_GAP_MS} ms"
        )


@pytest.mark.skipif(
    arbiter.ENDPOINT_OVERHEAD_MS == 0,
    reason=(
        "CONTROL_SPEC §9 property 3 is vacuous at ENDPOINT_OVERHEAD_MS = 0: it "
        "reduces to `max_ms <= ceiling_ms`, which the §4 clamps already give, so "
        "it would pass under any control law. CLAUDE.md §5 names this exact "
        "property as an invariant that cannot be violated under the constants "
        "actually in force, and therefore as a test that cannot go red. It is "
        "written and skipped rather than omitted so that it activates on its own "
        "the moment `make bench` supplies a measured overhead (INV-9, §5 Ceiling, "
        "ADR-017) — a conditional skip cannot be forgotten the way a TODO can. "
        "Note ADR-017 measured the overhead as a value *and a spread* (206, 175, "
        "204, 172, 217 ms), so whoever lands the constant has to decide which end "
        "of that spread the ceiling subtracts before this assertion means anything."
    ),
)
@STUB_XFAIL
@PROPERTY_SETTINGS
@given(state=strategies.arbiter_inputs())
def test_max_ms_stays_under_the_ceiling_less_the_measured_overhead(
    state: arbiter.ArbiterInput,
) -> None:
    """§9 property 3. Skipped while the overhead is 0; see the marker above."""
    config = arbiter.control_law(
        state.features,
        state.hint,
        cold=state.features.cold,
        ceiling_ms=state.ceiling_ms,
    )
    assert config.max_turn_silence_ms <= state.ceiling_ms - arbiter.ENDPOINT_OVERHEAD_MS


@STUB_XFAIL
@PROPERTY_SETTINGS
@given(
    features=strategies.speaker_features(),
    hint=strategies.window_hints(),
    ceiling_ms=strategies.ceilings_ms(),
    pair=st.tuples(strategies.unit_intervals(), strategies.unit_intervals()),
)
def test_raising_disfluency_never_lowers_max_turn_silence(
    features: SpeakerFeatures,
    hint: WindowHint,
    ceiling_ms: int,
    pair: tuple[float, float],
) -> None:
    """§9 property 4. Monotonic in `disfluency`, everything else held fixed.

    The inequality is deliberately non-strict. Two regimes make equality the
    correct answer rather than a missed effect: a `cold` profile skips the speaker
    axis entirely (§4), and a warm one whose `max_ms` has already reached
    `MAX_MS_CEIL` cannot rise further. Requiring a strict increase would fail on
    both, so the property asserts direction and never magnitude.

    `cold` is drawn across the boundary rather than forced warm, which makes the
    cold branch part of the property instead of a case it avoids.
    """
    lower, higher = sorted(pair)
    calmer = replace(features, disfluency=lower)
    twitchier = replace(features, disfluency=higher)
    assert (
        arbiter.control_law(
            twitchier, hint, cold=features.cold, ceiling_ms=ceiling_ms
        ).max_turn_silence_ms
        >= arbiter.control_law(
            calmer, hint, cold=features.cold, ceiling_ms=ceiling_ms
        ).max_turn_silence_ms
    ), (
        f"raising disfluency from {lower} to {higher} lowered max_turn_silence; "
        "a more disfluent speaker must never be given less room to pause"
    )


@STUB_XFAIL
@PROPERTY_SETTINGS
@given(state=strategies.arbiter_inputs())
def test_deciding_twice_on_the_same_state_is_idempotent(
    state: arbiter.ArbiterInput,
) -> None:
    """§9 property 5. Same state twice: an equal patch, then nothing.

    §9's sentence carries two claims and both are asserted, because either alone
    is satisfiable by a broken arbiter:

    1. **Determinism.** Two fresh arbiters on one state produce equal patches.
       Equality is over the whole `ConfigPatch`, including the `ConfigDecision`,
       so the INV-4 explanation is pinned as tightly as the numbers. A decision
       record that varies run to run is a reason line the console cannot reproduce.
    2. **Suppression.** The second call on the *same* arbiter emits nothing.

    Claim 2 forces a design choice, and it is now **ADR-020**: the caller passes
    the identical `ArbiterInput` both times, so `state.current` has not moved. If
    hysteresis compared the law's target against `state.current`, the second call
    would clear the threshold exactly as the first did and emit again. It cannot,
    so the arbiter remembers the configuration it last emitted and gates against
    that, falling back to `state.current` only before it has emitted anything.
    This property is what that ADR was written from.
    """
    engine = _engine(state)
    first = engine.decide(state)
    second = engine.decide(state)
    assert _engine(state).decide(state) == first, (
        "two fresh arbiters on one state disagreed; decide() must be a pure "
        "function of its input and the arbiter's own history"
    )
    assert second is None, (
        "the second decision on an unchanged state emitted a patch; hysteresis "
        "must gate against the last emitted config, not against state.current"
    )


@STUB_XFAIL
@PROPERTY_SETTINGS
@given(
    state=strategies.arbiter_inputs(
        # A wide config already in force, and a speaker who no longer needs it:
        # warm, fluent, with gap quantiles near zero, so the law's target sits at
        # the bottom clamps and a large narrowing is what it asks for.
        current=strategies.turn_configs(
            min_ms=st.integers(min_value=500, max_value=900),
            max_ms=st.integers(min_value=2000, max_value=4000),
        ),
        features=strategies.speaker_features(
            warm=True,
            disfluency=st.just(0.0),
            gaps_ms=st.floats(
                min_value=0.0, max_value=50.0, allow_nan=False, allow_infinity=False
            ),
        ),
        hint=st.builds(
            WindowHint,
            min_mult=st.floats(
                min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
            ),
            max_mult=st.floats(
                min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
            ),
        ),
        # `boolean` is excluded: the §5 floor guard caps `min_ms` at 400 on a
        # boolean turn, which is itself a narrowing and would confound this
        # property with property 8.
        expected_answer=st.none() | NON_BOOLEAN_ANSWERS,
    )
)
def test_narrowing_never_exceeds_one_step_per_turn(
    state: arbiter.ArbiterInput,
) -> None:
    """§9 property 6. A single turn may not narrow a field by more than 12 %.

    §5's reason for the guard: "one stumble must not make the agent permanently
    slow, and one crisp answer must not immediately re-expose the caller to
    cutting". The second half is this property. A speaker who answers two
    questions fluently has not stopped being someone who pauses mid-sentence, and
    collapsing the window back in one turn puts them straight back under the
    static config's failure mode.

    Widening is skipped rather than bounded: §5 makes it immediate and unbounded
    on purpose, so the asymmetry is the guard and not an oversight.

    The 1 ms slack absorbs integer rounding only. `NARROW_STEP` is a fraction of
    an integer millisecond count, so the exact bound depends on whether the
    implementation floors, rounds or ceils, and none of those choices is the
    behaviour under test.
    """
    patch = _engine(state).decide(state)
    if patch is None:
        return
    for field in ("min_turn_silence_ms", "max_turn_silence_ms"):
        old = getattr(state.current, field)
        new = getattr(patch.config, field)
        if new >= old:
            continue
        assert old - new <= math.ceil(old * arbiter.NARROW_STEP) + 1, (
            f"{field} narrowed {old} → {new}, a drop of {old - new} ms, which "
            f"exceeds NARROW_STEP={arbiter.NARROW_STEP} of {old} ms"
        )


@STUB_XFAIL
@PROPERTY_SETTINGS
@given(
    state=strategies.arbiter_inputs(
        current=strategies.turn_configs(
            min_ms=st.integers(min_value=500, max_value=900),
            max_ms=st.integers(min_value=2000, max_value=4000),
        ),
        features=strategies.speaker_features(
            warm=True,
            disfluency=st.just(0.0),
            gaps_ms=st.floats(
                min_value=0.0, max_value=50.0, allow_nan=False, allow_infinity=False
            ),
        ),
        expected_answer=st.none() | NON_BOOLEAN_ANSWERS,
    )
)
def test_a_narrowing_is_reachable_at_all(state: arbiter.ArbiterInput) -> None:
    """Companion to property 6, and the reason it is not vacuous.

    **This test found a contradiction in §5 and is now the guard on its
    resolution.** Hysteresis emits "only if any field moves more than
    `HYST = 15 %` of its current value". Asymmetric decay says "narrowing applies
    at most `NARROW_STEP = 12 %` per turn". Twelve is less than fifteen, so if both
    guards applied in series to the emitted value, *every* narrowing step would be
    smaller than the threshold that lets it out: narrowing unreachable, and the
    controller a one-way ratchet drifting to `MAX_MS_CEIL` over a long call. That
    would serve a caller who becomes fluent mid-call **worse than the static
    `balanced` arm**, which is the project's premise inverted.

    **ADR-020 resolves it**: hysteresis gates the law's *target*, asymmetric decay
    bounds the *step* emitted, and no constant moves. This test is what makes that
    ordering checkable — an implementation that reverts to guards-in-series goes
    red here and nowhere else.

    Property 6 alone cannot carry it. Its assertion is guarded on a patch that
    narrows, so a law that never narrows satisfies it by never reaching the
    assertion — a test that cannot go red, the defect CLAUDE.md §5 catalogues.
    This test supplies the missing half: over a domain that demands a large
    narrowing, at least one decision must actually emit one. §9 property 6 being a
    live constraint only under ADR-020's reading is also the second argument for
    that reading, since §9 was written against a law in which §9.6 does work.
    """
    patch = _engine(state).decide(state)
    assert patch is not None, (
        "no decision emitted over a domain built to demand a large narrowing"
    )
    narrowed = (
        patch.config.min_turn_silence_ms < state.current.min_turn_silence_ms
        or patch.config.max_turn_silence_ms < state.current.max_turn_silence_ms
    )
    assert narrowed, (
        "a warm, fluent speaker on a wide config produced no narrowing at all. "
        "If this is HYST=15 % suppressing a NARROW_STEP=12 % step, the two §5 "
        "constants are in conflict and one of them moves by ADR — see this "
        "test's docstring"
    )


@contextmanager
def _no_io() -> Iterator[None]:
    """Make the two I/O primitives of §9 property 7 raise for the duration.

    `builtins.open` and `socket` are patched, exactly as §9 names them. Neither is
    imported by `arbiter.py`, which is what makes this worth running rather than
    redundant: the AST checks in `tests/unit/test_boundaries.py` can only see the
    module's own imports, so they are blind to I/O reached *transitively* — a
    structlog call configured to a file sink, a lazily imported helper that reads
    a policy off disk, a metrics client that opens a socket. Those are how INV-2
    actually gets broken, and only a runtime patch catches them.

    Patching `builtins.open` globally is safe here because the scope is one call.
    The context manager restores on the way out, including when the stub raises
    `NotImplementedError` through it, so hypothesis's own bookkeeping never runs
    inside the patched window.
    """
    breach = AssertionError(
        "decide() attempted I/O. INV-2: no I/O, no logging to disk, no network, "
        "no LLM call. The audio path cannot afford it and nothing in the "
        "signature needs it."
    )
    with (
        mock.patch("builtins.open", side_effect=breach),
        mock.patch("socket.socket", side_effect=breach),
        mock.patch("socket.create_connection", side_effect=breach),
    ):
        yield


@STUB_XFAIL
@PROPERTY_SETTINGS
@given(state=strategies.arbiter_inputs())
def test_decide_performs_no_io(state: arbiter.ArbiterInput) -> None:
    """§9 property 7. `decide()` does no I/O, checked at runtime.

    The arbiter is constructed *outside* the patched window on purpose. §9 scopes
    this property to `decide`, and `__init__` reading something once at session
    start is a different question with a different answer. Narrowing the window to
    the call keeps the test about the hot path.
    """
    engine = _engine(state)
    with _no_io():
        engine.decide(state)


@STUB_XFAIL
@PROPERTY_SETTINGS
@given(
    state=strategies.arbiter_inputs(
        expected_answer=st.just("boolean"),
        # `current.min` is drawn at or below the cap, so no narrowing is required
        # to satisfy the guard and the §5 decay limit cannot be what enforces it.
        # Starting above the cap would need more than one turn under ADR-020's
        # decay bound — 900 ms reaches the 400 ms cap in about seven turns, not
        # one — so drawing there would test decay's rate rather than the cap, and
        # `test_narrowing_never_exceeds_one_step_per_turn` already owns that.
        current=strategies.turn_configs(
            min_ms=st.integers(
                min_value=arbiter.MIN_MS_FLOOR, max_value=arbiter.BOOLEAN_MIN_MS_CAP
            )
        ),
    )
)
def test_a_boolean_turn_never_leaves_min_above_the_floor_cap(
    state: arbiter.ArbiterInput,
) -> None:
    """§9 property 8. On `boolean`, the config in force has `min_ms <= 400`.

    Stated over the *effective* config rather than over the patch, which makes it
    total. §5's guard is "on `boolean`, `min_ms` never exceeds 400" — a claim about
    the value the caller is left running, not about whether a patch was sent. An
    assertion guarded on `patch is not None` would be satisfied by an arbiter that
    computes 900 ms for a yes/no question and then suppresses the patch, leaving
    exactly the config the guard exists to prevent.

    Note this property is unreachable through `control_law`, whose signature takes
    no `ExpectedAnswer` — it sees only the compiled multipliers, and `boolean`'s
    `min_mult` of 0.7 is a widening of a small number, not a cap on a large one.
    The guard therefore has to live in `Arbiter.decide`.
    """
    patch = _engine(state).decide(state)
    effective = state.current if patch is None else patch.config
    assert effective.min_turn_silence_ms <= arbiter.BOOLEAN_MIN_MS_CAP, (
        f"a boolean turn is left with min_turn_silence_ms="
        f"{effective.min_turn_silence_ms}, above the §5 cap of "
        f"{arbiter.BOOLEAN_MIN_MS_CAP}; yes/no has to stay snappy"
    )
