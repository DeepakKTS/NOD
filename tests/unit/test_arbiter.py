"""`arbiter.py`: §4's two gates and every §5 guard, with the polarity reversed.

`tests/property/test_control_law.py` holds CONTROL_SPEC §9 — the properties that
must hold over arbitrary feature vectors. This file holds the other half, and the
Gate 1 strategies docstring is explicit about why it has to exist:
`strategies.arbiter_inputs` pins the capability gate open, the host override empty
and `patches_sent` below the cap, because otherwise an unrelated guard suppresses
the patch and every §9 assertion goes vacuous. Each of those guards therefore
needs a test that turns it *on*, and those tests are here.

Written because the Gate 3 mutation run said so: 21 of `make mutate`'s arbiter
mutations survived the §9 properties alone. Every test below kills at least one.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

import pytest

from nod_core import arbiter
from nod_core.arbiter import Arbiter, ArbiterInput, control_law
from nod_core.capabilities import UPDATABLE_FIELDS
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    ControllerState,
    ExpectedAnswer,
    KnobVerdict,
    SpeakerFeatures,
    TurnConfig,
    WindowHint,
)

NEUTRAL: Final = WindowHint(min_mult=1.0, max_mult=1.0)

ALL_LIVE: Final = Capabilities(
    knobs=tuple((field, KnobVerdict.LIVE) for field in UPDATABLE_FIELDS),
    confidence_field=ConfidenceField.VARYING,
    force_endpoint=KnobVerdict.LIVE,
    has_word_timings=True,
)


def features(
    *,
    g_p50: float = 400.0,
    g_p90: float = 600.0,
    disfluency: float = 0.0,
    recent_cuts: float = 0.0,
    jitter: float = 0.0,
    cold: bool = False,
) -> SpeakerFeatures:
    """A warm profile sitting clear of every §4 clamp.

    The defaults matter: `g_p50 = 400` gives `min_ms = 360` inside `[160, 900]`
    and `g_p90 = 600` gives `max_ms = 1210`, which leaves room for a 2.4x context
    multiplier *and* a full disfluency term under the 2600 ms default ceiling.
    A fixture pressed against a clamp cannot see a coefficient change, which is
    how a law test goes quietly vacuous — and the first draft of this file was
    pressed against the **ceiling** at `g_p90 = 1000`, so two of these tests
    compared 2600 with 2600 and would have passed under a dropped coefficient.

    The 2600 ms figure is the *effective* ceiling, `EFFECTIVE_LAW_CEILING_MS`. It is
    reached by passing `LAW_CEILING_MS` — see there for why the two differ.
    """
    return SpeakerFeatures(
        n_gaps=0 if cold else 64,
        g_p50_ms=g_p50,
        g_p90_ms=g_p90,
        speech_rate=2.4,
        disfluency=disfluency,
        jitter=jitter,
        recent_cuts=recent_cuts,
        cold=cold,
    )


def config(minimum: int = 400, maximum: int = 1280) -> TurnConfig:
    """A configuration plausibly already in force."""
    return TurnConfig(
        min_turn_silence_ms=minimum,
        max_turn_silence_ms=maximum,
        end_of_turn_confidence_threshold=0.4,
        vad_threshold=None,
    )


def state(
    *,
    speaker: SpeakerFeatures | None = None,
    hint: WindowHint = NEUTRAL,
    current: TurnConfig | None = None,
    expected: ExpectedAnswer | None = "free",
    turn: int = 5,
    patches: int = 0,
    host: frozenset[str] = frozenset(),
    caps: Capabilities = ALL_LIVE,
    ceiling: int = arbiter.DEFAULT_CEILING_MS,
) -> ArbiterInput:
    """Build one decision input."""
    return ArbiterInput(
        features=speaker if speaker is not None else features(),
        hint=hint,
        expected_answer=expected,
        current=current if current is not None else config(),
        capabilities=caps,
        ceiling_ms=ceiling,
        turn_order=turn,
        t_ms=turn * 1200,
        host_override_fields=host,
        patches_sent=patches,
    )


def engine(caps: Capabilities = ALL_LIVE, ceiling: int | None = None) -> Arbiter:
    """An arbiter for one session."""
    return Arbiter(
        capabilities=caps,
        ceiling_ms=arbiter.DEFAULT_CEILING_MS if ceiling is None else ceiling,
    )


EFFECTIVE_LAW_CEILING_MS: Final = arbiter.DEFAULT_CEILING_MS
"""What `max_ms` is actually clamped to under `LAW_CEILING_MS`. Milliseconds.

The "is the ceiling masking this term?" guards below must compare against *this*,
not against `ceiling_ms`. Comparing against the raw ceiling is what let
`test_the_context_axis_multiplies_max_turn_silence` assert `2383 < 2600` and call the
multiplier unmasked while the clamp was in fact holding it at 2383 — a guard that
cannot detect the thing it is named for (CLAUDE.md §5).
"""

LAW_CEILING_MS: Final = arbiter.DEFAULT_CEILING_MS + arbiter.ENDPOINT_OVERHEAD_MS
"""The ceiling these fixtures pass, so the *effective* one is `DEFAULT_CEILING_MS`.

§4 clamps `max_ms` to `ceiling_ms - ENDPOINT_OVERHEAD_MS`, so the quantity a fixture
has to stay clear of is the ceiling **less the overhead**, not the ceiling. While the
overhead was 0 the two were equal and this file passed the raw default. ADR-040 made
them differ by 217 ms and the 2.4x headroom the `features` docstring promises stopped
existing — `test_the_context_axis_multiplies_max_turn_silence` went red against a
ceiling it had been told was not binding.

Derived from both constants so the headroom survives either moving. The alternative
was to restate every arithmetic literal below against the smaller effective ceiling,
which would have left the fixtures pressed against a clamp — precisely the vacuity
the `features` docstring exists to warn about.
"""


def law(speaker: SpeakerFeatures, hint: WindowHint = NEUTRAL) -> TurnConfig:
    """`control_law` on a warm profile, away from the guards."""
    return control_law(speaker, hint, cold=speaker.cold, ceiling_ms=LAW_CEILING_MS)


# --- §4: the max_turn_silence gate, the primary surface (ADR-011) -----------


def test_the_p90_gap_drives_max_turn_silence() -> None:
    """§4: `max_ms = 1.6 * g_p90 + 250`, asserted on the arithmetic.

    Against the stated coefficients rather than against a second copy of them, so
    a change to `MAX_MS_FROM_P90_GAIN` has to be a deliberate edit to CONTROL_SPEC
    and this line together.
    """
    assert law(features(g_p90=1000.0)).max_turn_silence_ms == 1850
    assert law(features(g_p90=500.0)).max_turn_silence_ms == 1050


def test_disfluency_widens_max_turn_silence() -> None:
    """§4: disfluency routes to `max_ms`, the incomplete-utterance regime.

    ADR-011 put it there rather than on `min_ms` because a mid-sentence pause *is*
    an incomplete utterance. A stricter claim than §9 property 4's monotonicity:
    this asserts the coefficient, so dropping the term goes red here where the
    non-strict property stays green.
    """
    calm = law(features(disfluency=0.0)).max_turn_silence_ms
    twitchy = law(features(disfluency=1.0)).max_turn_silence_ms
    assert calm == 1210
    assert twitchy == int(1210 * (1.0 + arbiter.MAX_MS_DISFLUENCY_GAIN))
    assert twitchy > calm
    assert twitchy < EFFECTIVE_LAW_CEILING_MS, "the ceiling is masking the term"


def test_recent_cuts_widen_max_turn_silence() -> None:
    """§4: `recent_cuts` routes to `max_ms` for the same reason as disfluency.

    A caller the controller has already cut off is a caller it should be giving
    more room, which is the whole closed loop in one coefficient.
    """
    none = law(features(recent_cuts=0.0)).max_turn_silence_ms
    cut = law(features(recent_cuts=1.0)).max_turn_silence_ms
    assert cut == int(1210 * (1.0 + arbiter.MAX_MS_RECENT_CUTS_GAIN))
    assert cut > none
    assert cut < EFFECTIVE_LAW_CEILING_MS, "the ceiling is masking the term"


def test_jitter_is_read_nowhere_in_the_law() -> None:
    """ADR-011 weighted it 0, and §9 property 10 pins the patch-level consequence.

    Asserted on `control_law` too, because a `+ JITTER_GAIN * features.jitter`
    term would satisfy the property while leaving the feature one edit from
    carrying authority again.
    """
    assert law(features(jitter=0.0)) == law(features(jitter=1.0))
    assert arbiter.JITTER_GAIN == 0.0


def test_the_context_axis_multiplies_max_turn_silence() -> None:
    """§4: `max_ms *= hint.max_mult`, for the wide-answer classes of §3."""
    wide = law(features(), WindowHint(min_mult=1.0, max_mult=2.0))
    assert wide.max_turn_silence_ms == 2420
    assert wide.max_turn_silence_ms < EFFECTIVE_LAW_CEILING_MS, (
        "the ceiling is masking the multiplier"
    )
    narrow = law(features(), WindowHint(min_mult=1.0, max_mult=0.5))
    assert narrow.max_turn_silence_ms == 605


# --- §4: the min_turn_silence gate ------------------------------------------


def test_the_p50_gap_drives_min_turn_silence() -> None:
    """§4: `min_ms = 0.6 * g_p50 + 120`. The complete-utterance regime."""
    assert law(features(g_p50=400.0)).min_turn_silence_ms == 360
    assert law(features(g_p50=800.0)).min_turn_silence_ms == 600


def test_min_turn_silence_reads_the_median_and_not_the_tail() -> None:
    """The two gates read different quantiles, and swapping them is silent.

    `min_ms` is responsiveness after a *complete* utterance, so it is a claim
    about the caller's ordinary rhythm — the median. Reading `g_p90` there would
    make the agent as slow to answer a finished sentence as it is patient with an
    unfinished one, which erases the distinction ADR-011 is built on.
    """
    lopsided = features(g_p50=200.0, g_p90=2000.0)
    assert law(lopsided).min_turn_silence_ms == int(0.6 * 200.0 + 120)


def test_the_context_axis_multiplies_min_turn_silence() -> None:
    """§4: `min_ms *= hint.min_mult`. §3 says the context axis lands here."""
    assert (
        law(features(), WindowHint(min_mult=2.0, max_mult=1.0)).min_turn_silence_ms
        == 720
    )
    assert (
        law(features(), WindowHint(min_mult=0.5, max_mult=1.0)).min_turn_silence_ms
        == 180
    )


# --- §4: the cold branch and the clamps -------------------------------------


def test_a_cold_profile_skips_the_speaker_axis_entirely() -> None:
    """§4: when cold, only the context axis applies to the base values.

    Asserted with gap quantiles that would give a very different answer if the
    speaker axis leaked through — that is what makes it a test of the branch and
    not of the base constants.
    """
    cold = control_law(
        features(g_p50=5000.0, g_p90=6000.0, cold=True),
        NEUTRAL,
        cold=True,
        ceiling_ms=arbiter.DEFAULT_CEILING_MS,
    )
    assert cold.min_turn_silence_ms == arbiter.BASE_MIN_MS
    assert cold.max_turn_silence_ms == arbiter.BASE_MAX_MS


def test_a_cold_profile_still_takes_the_context_axis() -> None:
    """§4 again: the hint applies to the base values while cold."""
    cold = control_law(
        features(cold=True),
        WindowHint(min_mult=0.7, max_mult=0.7),
        cold=True,
        ceiling_ms=arbiter.DEFAULT_CEILING_MS,
    )
    assert cold.min_turn_silence_ms == int(arbiter.BASE_MIN_MS * 0.7)
    assert cold.max_turn_silence_ms == int(arbiter.BASE_MAX_MS * 0.7)


def test_the_clamps_bind_at_both_ends() -> None:
    """§4's hard clamps, exercised rather than merely bounded by §9 property 1."""
    fast = law(features(g_p50=0.0, g_p90=0.0))
    assert fast.min_turn_silence_ms == arbiter.MIN_MS_FLOOR
    slow = law(features(g_p50=6000.0, g_p90=6000.0))
    assert slow.min_turn_silence_ms == arbiter.MIN_MS_CEIL


def test_the_latency_ceiling_binds_before_the_max_clamp() -> None:
    """§4: `max_ms = min(max_ms, ceiling_ms - ENDPOINT_OVERHEAD_MS)`.

    A profile wanting 9850 ms is held at the ceiling, not at `MAX_MS_CEIL`, which
    is the only thing that makes the ceiling a separate guard from the clamp.
    """
    held = control_law(
        features(g_p50=100.0, g_p90=6000.0), NEUTRAL, cold=False, ceiling_ms=1500
    )
    assert held.max_turn_silence_ms == 1500 - arbiter.ENDPOINT_OVERHEAD_MS


# --- ADR-020 step 4: the repair has to run again after decay ----------------


def test_the_invariant_is_repaired_after_decay_not_only_in_the_law() -> None:
    """ADR-020 step 4, on the case that makes it necessary.

    A reference sitting at exactly `max = min + INVARIANT_GAP_MS` narrowed by
    `NARROW_STEP` on both fields leaves a gap of `0.88 * 200 = 176 ms` — the
    invariant broken by a guard, from inputs that satisfied it. Constructed
    directly rather than hoped for from a random draw.
    """
    tight = config(minimum=900, maximum=1100)
    controller = engine()
    patch = controller.decide(
        state(speaker=features(g_p50=0.0, g_p90=0.0), current=tight)
    )
    assert patch is not None
    assert (
        patch.config.max_turn_silence_ms
        >= patch.config.min_turn_silence_ms + arbiter.INVARIANT_GAP_MS
    )


def test_the_clamps_are_reapplied_after_decay() -> None:
    """A decayed value must not be allowed to sit outside §4's clamps."""
    controller = engine()
    patch = controller.decide(
        state(
            speaker=features(g_p50=6000.0, g_p90=6000.0),
            current=config(minimum=arbiter.MIN_MS_CEIL, maximum=arbiter.MAX_MS_CEIL),
        )
    )
    if patch is not None:
        assert patch.config.min_turn_silence_ms <= arbiter.MIN_MS_CEIL
        assert patch.config.max_turn_silence_ms <= arbiter.MAX_MS_CEIL


# --- §5 hysteresis, measured on the target against the last emitted ---------


def test_a_small_move_is_suppressed() -> None:
    """§5: emit only if a field moves more than `HYST_FRACTION` of its value.

    The reversed polarity of §9 property 5: a law target within the band produces
    no patch at all, which is what stops the socket chattering every turn.
    """
    # A target of 1210 against a current of 1200 is a 0.8 % move on max and
    # 360 against 355 is 1.4 % on min — both well inside the 15 % band.
    assert engine().decide(state(current=config(minimum=355, maximum=1200))) is None


def test_the_hysteresis_boundary_is_strict() -> None:
    """§5 says "more than", so a move of exactly the band does not emit.

    A boundary worth pinning rather than leaving to a `>=`: at the threshold the
    two readings differ by one patch per turn on a caller who sits right there,
    which is the socket chatter the guard exists to prevent.
    """
    # A target of 1210 against a current of 1210/1.15 = 1052.2 is a move of
    # exactly 15 %; 1053 puts it just inside the band, so nothing may emit.
    exact = config(minimum=360, maximum=1053)
    assert engine().decide(state(current=exact)) is None


def test_hysteresis_gates_against_the_last_emitted_config(caplog: object) -> None:
    """ADR-020: the reference is what this arbiter last sent, not `current`.

    Drives two turns with a `current` that never changes — which is what the proxy
    does, since `current` tracks the socket and the socket is only updated by the
    patch. If hysteresis measured against `current`, turn two would re-emit the
    same move forever and the rate cap would be the only thing stopping it.
    """
    del caplog
    controller = engine()
    # A speaker whose target is far from `current`, so the first turn certainly
    # emits and there is a second step left to take.
    wide = features(g_p50=6000.0, g_p90=6000.0)
    fixed = config(minimum=400, maximum=1280)
    first = controller.decide(state(speaker=wide, current=fixed, turn=5))
    assert first is not None
    second = controller.decide(state(speaker=wide, current=fixed, turn=6))
    # Turn 6 may continue stepping toward the target, but it must not repeat
    # turn 5's patch: the reference has moved even though `current` has not.
    if second is not None:
        assert second.decision.old == first.config, (
            "turn 6 measured against state.current rather than the last emitted "
            "config, so it re-decided turn 5's move"
        )


# --- §5 asymmetric decay (ADR-020 ordering, ADR-022 caps) -------------------


def test_widening_is_capped_at_widen_step() -> None:
    """ADR-022: widening is no longer immediate. The bound, not just reachability.

    `test_a_widening_is_reachable_at_all` asserts a widening happens; this asserts
    it happens *by at most one step*. Without both, `WIDEN_STEP` could be either
    ignored or set so small it suppresses widening entirely — the two failure
    modes of a step cap, and neither is visible from the other side.
    """
    controller = engine()
    patch = controller.decide(
        state(speaker=features(g_p50=6000.0, g_p90=6000.0), current=config(400, 1280))
    )
    assert patch is not None
    assert patch.config.max_turn_silence_ms == int(1280 * (1.0 + arbiter.WIDEN_STEP))


def test_narrowing_is_capped_at_narrow_step() -> None:
    """§5: narrowing at most `NARROW_STEP` per turn. The exact step."""
    controller = engine()
    patch = controller.decide(
        state(speaker=features(g_p50=0.0, g_p90=0.0), current=config(800, 3000))
    )
    assert patch is not None
    assert patch.config.max_turn_silence_ms == int(3000 * (1.0 - arbiter.NARROW_STEP))


def test_the_two_caps_are_asymmetric_in_the_documented_direction() -> None:
    """§5's asymmetry is the guard, so the inequality is worth asserting.

    Symmetric caps would take 7 turns to serve a genuinely hesitant caller
    (ADR-022's arithmetic), which is 7 turns of cutting off the person the project
    exists for.
    """
    assert arbiter.WIDEN_STEP > arbiter.NARROW_STEP


# --- §5 rate cap, both halves -----------------------------------------------


def test_the_session_rate_cap_suppresses_everything_past_max_patches() -> None:
    """§5: at most `MAX_PATCHES` per session, counted by the caller."""
    controller = engine()
    wide = features(g_p50=6000.0, g_p90=6000.0)
    assert controller.decide(state(speaker=wide, patches=arbiter.MAX_PATCHES)) is None
    assert (
        controller.decide(state(speaker=wide, patches=arbiter.MAX_PATCHES + 5)) is None
    )


def test_the_per_turn_rate_cap_allows_one_patch_per_turn() -> None:
    """§5: at most 1 patch per turn, counted by the arbiter.

    The caller's `patches_sent` cannot carry this half — the case it exists for is
    the same turn asking twice, where `patches_sent` has not moved. It is also
    what makes §9 property 5 true under the step caps: decay means a large move
    takes several turns, so "the second emits nothing" has to mean one patch per
    turn rather than the journey finishing in one.
    """
    controller = engine()
    wide = features(g_p50=6000.0, g_p90=6000.0)
    assert controller.decide(state(speaker=wide, turn=5)) is not None
    assert controller.decide(state(speaker=wide, turn=5)) is None
    assert controller.decide(state(speaker=wide, turn=6)) is not None


# --- §5 capability gate and host override -----------------------------------


def test_a_knob_the_probe_did_not_prove_live_is_never_sent() -> None:
    """§5 capability gate: `LIVE` only, every other verdict fails closed.

    ADR-001's four states exist because "no error came back" is equally consistent
    with applied and silently dropped. `STATIC_ONLY` means mid-stream updates do
    not work and `UNPROVEN` means we could not show that they do; neither is a
    licence to send, and a patch naming one would be a config change the session
    believes in and the service ignored.
    """
    degraded = Capabilities(
        knobs=(
            ("min_turn_silence", KnobVerdict.LIVE),
            ("max_turn_silence", KnobVerdict.UNPROVEN),
            ("end_of_turn_confidence_threshold", KnobVerdict.INERT),
            ("vad_threshold", KnobVerdict.STATIC_ONLY),
        ),
        confidence_field=ConfidenceField.VARYING,
        force_endpoint=KnobVerdict.LIVE,
        has_word_timings=True,
    )
    patch = engine(caps=degraded).decide(
        state(speaker=features(g_p50=6000.0, g_p90=6000.0), caps=degraded)
    )
    assert patch is not None
    assert "max_turn_silence" not in patch.changed
    assert patch.config.max_turn_silence_ms == config().max_turn_silence_ms


def test_a_field_the_host_set_recently_is_left_alone() -> None:
    """§5 host override, EC-33: the host owns its own decisions."""
    patch = engine().decide(
        state(
            speaker=features(g_p50=6000.0, g_p90=6000.0),
            host=frozenset({"max_turn_silence"}),
        )
    )
    assert patch is not None
    assert "max_turn_silence" not in patch.changed
    assert patch.config.max_turn_silence_ms == config().max_turn_silence_ms


def test_a_host_holding_both_knobs_leaves_nothing_to_send() -> None:
    """Both fields overridden means no patch at all, rather than an empty one."""
    assert (
        engine().decide(
            state(
                speaker=features(g_p50=6000.0, g_p90=6000.0),
                host=frozenset({"min_turn_silence", "max_turn_silence"}),
            )
        )
        is None
    )


# --- §6 the state machine ---------------------------------------------------


def test_a_controller_error_enters_safe_and_emits_nothing_that_turn() -> None:
    """§6 and INV-8: last known good, no patches, error counted, call continues."""
    controller = engine()
    controller.note_error(RuntimeError("boom"))
    assert controller.state is ControllerState.SAFE
    assert controller.errors == 1
    assert (
        controller.decide(state(speaker=features(g_p50=6000.0, g_p90=6000.0))) is None
    )


def test_safe_returns_to_cold_on_the_next_turn() -> None:
    """§6: `SAFE --(next turn)--> COLD`, so an error costs one turn and not a call."""
    controller = engine()
    controller.note_error(RuntimeError("boom"))
    controller.decide(state(turn=5))
    assert controller.state is ControllerState.COLD
    assert (
        controller.decide(state(speaker=features(g_p50=6000.0, g_p90=6000.0), turn=6))
        is not None
    )


def test_the_state_machine_warms_when_the_profiler_does() -> None:
    """§6: `COLD --(n_gaps >= MIN_GAPS_FOR_WARM)--> WARM`."""
    controller = engine()
    controller.decide(state(speaker=features(cold=True), turn=1))
    while_cold = controller.state
    controller.decide(state(speaker=features(cold=False), turn=2))
    once_warm = controller.state
    assert while_cold is ControllerState.COLD
    assert once_warm is ControllerState.WARM


def test_oscillation_freezes_the_speaker_axis_and_the_context_axis_survives() -> None:
    """§5 and §6: `FREEZE_REVERSALS` reversals in the window park the speaker axis.

    While frozen the law is driven as if cold, so the base values times the hint —
    which is §6's "speaker axis held, context axis still applies", and the reason
    a frozen session is still responsive to a `boolean` prompt rather than stuck.
    """
    controller = engine()
    wide = features(g_p50=6000.0, g_p90=6000.0)
    narrow = features(g_p50=0.0, g_p90=0.0)
    current = config()
    for turn, speaker in enumerate((wide, narrow, wide, narrow, wide), start=1):
        patch = controller.decide(state(speaker=speaker, current=current, turn=turn))
        if patch is not None:
            current = patch.config
    assert controller.state is ControllerState.FROZEN


# --- ADR-021: the ceiling floor ---------------------------------------------


def test_a_ceiling_below_the_floor_is_refused_at_construction() -> None:
    """ADR-021: rejected at a configuration boundary, not repaired per turn."""
    with pytest.raises(ValueError, match="CEILING_FLOOR_MS"):
        Arbiter(capabilities=ALL_LIVE, ceiling_ms=arbiter.CEILING_FLOOR_MS - 1)
    # The floor itself is valid, so the boundary is inclusive and the rejection
    # is of values *below* it rather than of the floor.
    at_floor = Arbiter(capabilities=ALL_LIVE, ceiling_ms=arbiter.CEILING_FLOOR_MS)
    assert at_floor.state is ControllerState.COLD


def test_a_per_turn_ceiling_below_the_floor_is_clamped_not_refused() -> None:
    """ADR-021's other side, and INV-8's: fail soft inside a call.

    A mid-session voice switch feeds `Voice.pacing_hint_ms` into the ceiling, so a
    bad value can arrive per turn. Raising there would drop a live call to enforce
    a latency preference, which INV-8 forbids outright — and dropping the floor
    silently would let §4's ordering return `max_ms < min_ms + 200`.
    """
    controller = engine()
    patch = controller.decide(
        state(speaker=features(g_p50=6000.0, g_p90=6000.0), ceiling=200)
    )
    assert patch is not None
    assert (
        patch.config.max_turn_silence_ms
        >= patch.config.min_turn_silence_ms + arbiter.INVARIANT_GAP_MS
    )


# --- §4's confident early endpoint ------------------------------------------


def test_the_early_endpoint_needs_a_warm_confident_speaker() -> None:
    """§4: fires above `max(max_ms, g_p90 * 1.8)`, once per turn, never disfluent."""
    controller = engine()
    warm = state(speaker=features(g_p50=400.0, g_p90=1000.0))
    controller.decide(warm)
    assert controller.should_force_endpoint(warm, 5000) is True
    # Rate-limited to once per turn.
    assert controller.should_force_endpoint(warm, 5000) is False


def test_a_disfluent_speakers_long_gap_is_not_a_finished_turn() -> None:
    """§4 disables the early endpoint above `EARLY_ENDPOINT_MAX_DISFLUENCY`.

    A disfluent speaker is exactly the person whose long gap is not a finished
    turn, which is the whole premise of the project in one guard.
    """
    controller = engine()
    disfluent = state(speaker=features(disfluency=0.9))
    controller.decide(disfluent)
    assert controller.should_force_endpoint(disfluent, 9000) is False


def test_the_early_endpoint_is_off_without_the_capability() -> None:
    """§7's degradation matrix: `ForceEndpoint` unsupported disables the path."""
    without = replace(ALL_LIVE, force_endpoint=KnobVerdict.UNPROVEN)
    controller = engine(caps=without)
    live = state(caps=without)
    controller.decide(live)
    assert controller.should_force_endpoint(live, 9000) is False


# --- gaps found by the Gate 3 mutation run ----------------------------------


def test_the_post_decay_clamps_bind_when_the_reference_is_outside_them() -> None:
    """ADR-020 step 4's clamps, on the only case that reaches them.

    Added because a mutation dropping the post-decay `min` clamp survived every
    other test here. Decay interpolates between a reference and a target, so when
    both are inside §4's clamps the result is too and the clamp is unreachable.
    It becomes reachable when the *reference* is outside them — which happens for
    real: `ArbiterInput.current` describes what is on the socket, and EC-33 lets
    the host put a value there that our clamps would never have chosen.

    Without the re-clamp the arbiter would emit 1056 ms for `min_turn_silence`,
    156 ms above its own hard ceiling, having arrived there by decaying politely
    from the host's 1200.
    """
    host_set = TurnConfig(
        min_turn_silence_ms=1200,
        max_turn_silence_ms=3000,
        end_of_turn_confidence_threshold=0.4,
        vad_threshold=None,
    )
    patch = engine().decide(
        state(speaker=features(g_p50=0.0, g_p90=0.0), current=host_set)
    )
    assert patch is not None
    assert patch.config.min_turn_silence_ms <= arbiter.MIN_MS_CEIL, (
        f"emitted min_turn_silence_ms={patch.config.min_turn_silence_ms}, above "
        f"the §4 clamp of {arbiter.MIN_MS_CEIL}, by decaying from a host value"
    )


def test_a_move_of_exactly_the_hysteresis_band_does_not_emit() -> None:
    """§5 says "more than `HYST = 15 %`", so the boundary is strict.

    Added because a mutation turning `>` into `>=` survived: the first boundary
    fixture picked a reference where neither reading emits, so it could not tell
    them apart. Fixtures need values that differ along the axis under test, which
    is a lesson CLAUDE.md §5 already records for `segment_ms`.

    Constructed exactly rather than approximately. `g_p90 = 562.5` gives a target
    `max_ms` of `1.6 x 562.5 + 250 = 1150`; against a reference of 1000 that is a
    move of 150, and `0.15 x 1000` is exactly 150. `min` moves 40 against a band
    of 60, so it cannot emit on its own and the test is about the boundary and
    nothing else.
    """
    target_move = 1150 - 1000
    assert target_move == int(arbiter.HYST_FRACTION * 1000), "fixture drifted"
    assert (
        engine().decide(
            state(
                speaker=features(g_p50=400.0, g_p90=562.5),
                current=config(minimum=400, maximum=1000),
            )
        )
        is None
    )


def test_a_frozen_arbiter_holds_the_speaker_axis() -> None:
    """§6: `FROZEN` holds the speaker axis; the context axis still applies.

    Added because a mutation letting the speaker axis through while frozen
    survived — the oscillation test asserted the *state* reached `FROZEN` and said
    nothing about what that state does, which is a test of a label rather than of
    behaviour.

    Asserted by difference: once frozen, two speaker profiles that would give very
    different windows must give the *same* decision, because neither is being
    read. That is a stronger statement than any single expected value, and it
    cannot be satisfied by an arbiter that merely damps the speaker axis.
    """

    def frozen_arbiter() -> Arbiter:
        controller = engine()
        wide = features(g_p50=6000.0, g_p90=6000.0)
        narrow = features(g_p50=0.0, g_p90=0.0)
        current = config()
        for turn, speaker in enumerate((wide, narrow, wide, narrow, wide), start=1):
            patch = controller.decide(
                state(speaker=speaker, current=current, turn=turn)
            )
            if patch is not None:
                current = patch.config
        assert controller.state is ControllerState.FROZEN
        return controller

    # Turn 8, not 20: the freeze lasts FREEZE_DURATION_TURNS from the turn it
    # fired on, so a probe past that window is a probe of a thawed arbiter. The
    # first draft used 20 and tested nothing.
    probe_turn = 8
    assert probe_turn < 5 + arbiter.FREEZE_DURATION_TURNS, "probe is past the thaw"
    hesitant = frozen_arbiter().decide(
        state(speaker=features(g_p50=6000.0, g_p90=6000.0), turn=probe_turn)
    )
    fluent = frozen_arbiter().decide(
        state(speaker=features(g_p50=0.0, g_p90=0.0), turn=probe_turn)
    )
    assert (hesitant is None) == (fluent is None)
    if hesitant is not None and fluent is not None:
        assert hesitant.config == fluent.config, (
            "a frozen arbiter gave two different windows for two different "
            "speakers, so the speaker axis is not held"
        )


def test_a_sub_floor_per_turn_ceiling_behaves_exactly_like_the_floor() -> None:
    """ADR-021's soft side, asserted by equivalence rather than by invariant.

    Added because a mutation removing the per-turn clamp survived. The first
    attempt asserted that §4's invariant still held, and it did — `_repair` runs
    after decay and restores the gap, so the missing clamp was masked by a later
    guard rather than being harmless. That is the same shape as §4's own repair
    hiding an incoherent `g_p90`: a guard downstream can make an upstream defect
    invisible without making it absent.

    So this asserts the thing the clamp actually claims: a ceiling below the floor
    is *treated as* the floor. A reference of 1000 is the case where it shows —
    clamped, the target is held at 1100 and the arbiter widens; unclamped it is
    held at 200 and the arbiter narrows instead, moving the window the wrong way
    for a caller who needs more room.
    """
    wide = features(g_p50=6000.0, g_p90=6000.0)
    below = engine(ceiling=arbiter.CEILING_FLOOR_MS).decide(
        state(speaker=wide, current=config(400, 1000), ceiling=200)
    )
    at_floor = engine(ceiling=arbiter.CEILING_FLOOR_MS).decide(
        state(speaker=wide, current=config(400, 1000), ceiling=arbiter.CEILING_FLOOR_MS)
    )
    assert below is not None
    assert at_floor is not None
    assert below.config == at_floor.config, (
        "a per-turn ceiling of 200 ms was not treated as the 1100 ms floor, so "
        "§4's ordering drove max_turn_silence the wrong way (ADR-021)"
    )
    assert below.config.max_turn_silence_ms > 1000, (
        "a hesitant caller was narrowed rather than widened"
    )


def test_a_cold_speaker_never_gets_an_early_endpoint() -> None:
    """§4 requires a warm profile before the confident early endpoint.

    A cold profiler has no `g_p90` worth comparing a trailing gap against, so
    firing there would be guessing that the caller has finished from a window the
    controller has not yet earned.
    """
    controller = engine()
    cold = state(speaker=features(cold=True))
    controller.decide(cold)
    assert controller.should_force_endpoint(cold, 9000) is False


def test_a_gap_inside_the_window_is_not_an_early_endpoint() -> None:
    """§4: the trailing gap must exceed `max(max_ms, g_p90 * 1.8)`.

    The reversed polarity of the firing test, and the one that matters more: a
    gap the caller is still inside must not be cut. `g_p90 = 600` gives a
    threshold of `max(1210, 1080) = 1210`, so 1200 ms of silence is a pause and
    1300 ms is a finished turn.
    """
    controller = engine()
    warm = state(speaker=features(g_p50=400.0, g_p90=600.0))
    controller.decide(warm)
    assert controller.should_force_endpoint(warm, 1200) is False
    assert controller.should_force_endpoint(warm, 1300) is True


def test_the_boolean_floor_is_not_gated_by_hysteresis() -> None:
    """ADR-024's second exemption, on the only case that isolates it.

    Added because a mutation setting `overdue = False` survived the §9 property:
    that property draws a wide range of speaker features, so almost every example
    has `max_turn_silence` moving far enough to clear hysteresis on its own, and
    the decay exemption then applies the floor anyway. The hysteresis exemption
    only matters when **nothing else moved enough**, which a broad domain almost
    never draws. A property test over a wide domain and a unit test over one
    constructed point are not substitutes.

    Constructed so that neither field clears the band. `g_p50 = 400` gives a
    target `min` of 360 against a reference of 420 — a move of 60 against a band
    of `0.15 x 420 = 63`. `g_p90 = 600` gives a target `max` of 1210 against a
    reference of 1210, a move of zero. So `_moved_enough` is False on both fields,
    and without the exemption the caller is left at 420 ms on a yes/no question
    for the rest of the call.
    """
    controller = engine()
    reference = config(minimum=420, maximum=1210)
    # The premise of the fixture, asserted rather than assumed.
    assert abs(360 - 420) < arbiter.HYST_FRACTION * 420
    assert engine().decide(state(current=reference, expected="free")) is None

    patch = controller.decide(state(current=reference, expected="boolean"))
    assert patch is not None, (
        "hysteresis suppressed the boolean floor; ADR-024 exempts a correctness "
        "bound from a churn damper"
    )
    assert patch.config.min_turn_silence_ms <= arbiter.BOOLEAN_MIN_MS_CAP
    assert "min_turn_silence" in patch.changed, (
        "the floor moved min_turn_silence but the patch does not name it, so "
        "send_patch_upstream would not put it on the socket"
    )


def test_the_boolean_floor_does_not_touch_max_turn_silence() -> None:
    """ADR-024's exemption is narrow in the place that matters.

    `max_turn_silence` is the incomplete-utterance regime ADR-011 exists for, so
    a yes/no prompt must not collapse the room a caller has to pause mid-sentence.
    The floor governs `min` only, and `max` keeps its ordinary decay bound.
    """
    controller = engine()
    reference = config(minimum=900, maximum=3000)
    patch = controller.decide(
        state(
            speaker=features(g_p50=0.0, g_p90=0.0),
            current=reference,
            expected="boolean",
        )
    )
    assert patch is not None
    assert patch.config.min_turn_silence_ms == arbiter.BOOLEAN_MIN_MS_CAP
    assert patch.config.max_turn_silence_ms == int(
        3000 * (1.0 - arbiter.NARROW_STEP)
    ), (
        "max_turn_silence was not bounded by NARROW_STEP on a boolean turn; the "
        "ADR-024 exemption has leaked past min_turn_silence"
    )
