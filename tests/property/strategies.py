"""Hypothesis strategies for the control law's inputs.

CONTROL_SPEC.md §9 opens with "Written with `hypothesis`, over arbitrary feature
vectors". This module is what "arbitrary" means, and every bound below is derived
from the spec line that bounds it rather than picked for convenience. The domain
is the load-bearing part of a property test: too wide and it tests states the
profiler cannot produce, too narrow and it misses the state where the clamp bug
lives.

Bounds are read from the implementation constants wherever the spec states them
as constants, so moving `GAP_CLAMP_MAX_MS` moves the domain with it. That is the
right direction of coupling — the domain should follow the clamp — and it is not
the case CLAUDE.md §5 warns about, where deriving one constant from another would
have frozen a disagreement that needed to fail loudly. Nothing here is committed
data; there is no second side to disagree with.

Three fields are pinned rather than drawn in `arbiter_inputs`, and the reason is
the same for all three: they belong to guards that are *not* the property under
test, and drawing them would let an unrelated guard suppress the patch and make
the assertion vacuous.

- `capabilities` is all-`LIVE`, so the §5 capability gate passes.
- `host_override_fields` is empty, so the §5 host override never fires.
- `patches_sent` stays below `MAX_PATCHES`, so the §5 rate cap never fires.

Each of those guards needs its own test with the polarity reversed. Those are not
§9 properties and are not written here; they belong with the arbiter (Gate 3).
"""

from __future__ import annotations

from typing import Final

from hypothesis import strategies as st

from nod_core.arbiter import CEILING_FLOOR_MS as _CEILING_FLOOR_MS
from nod_core.arbiter import (
    INVARIANT_GAP_MS,
    MAX_MS_CEIL,
    MAX_MS_FLOOR,
    MAX_PATCHES,
    MIN_MS_CEIL,
    MIN_MS_FLOOR,
    ArbiterInput,
)
from nod_core.capabilities import UPDATABLE_FIELDS
from nod_core.profiler import GAP_CLAMP_MAX_MS, MIN_GAPS_FOR_WARM
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    ExpectedAnswer,
    KnobVerdict,
    SpeakerFeatures,
    TurnConfig,
    WindowHint,
)

EXPECTED_ANSWERS: Final[tuple[ExpectedAnswer, ...]] = (
    "free",
    "boolean",
    "entity_id",
    "entity_date",
    "entity_address",
    "entity_list",
    "spelling",
    "number",
)
"""The eight classes of CONTROL_SPEC.md §1, spelled out rather than derived.

`ExpectedAnswer` is a `type` alias over a `Literal`, so reaching its members needs
`get_args(ExpectedAnswer.__value__)` — a private attribute, and one mypy has no
reason to keep stable. Written out instead, with
`test_the_expected_answer_domain_is_complete` asserting this tuple equals the
`Literal`'s arguments. That is CLAUDE.md §5's rule applied deliberately: two
things that must agree, agreeing by an assertion that can fail, rather than by a
derivation that makes the question unaskable. A ninth class added to the type
with no strategy coverage has to go red.
"""

ALL_LIVE_CAPABILITIES: Final = Capabilities(
    knobs=tuple((field, KnobVerdict.LIVE) for field in UPDATABLE_FIELDS),
    confidence_field=ConfidenceField.VARYING,
    force_endpoint=KnobVerdict.LIVE,
    has_word_timings=True,
)
"""Every knob `LIVE`, so the §5 capability gate is never the thing suppressing."""

SPEECH_RATE_MAX: Final = 12.0
"""Words per second. Beyond any human rate; §2.2 states no bound of its own."""

POLICY_MULT_MAX: Final = 10.0
"""Upper bound on a drawn `WindowHint` multiplier.

CONTROL_SPEC.md §3's own table spans 0.7 to 2.4, but `WindowHintModel` puts no
bound on the field, so an operator's policy file can carry anything. The §9
clamp properties have to hold for the policy someone actually writes, not for the
one in the example, so the domain is deliberately wider than §3's table. Zero is
included: a `0.0` multiplier is the cleanest probe of the lower clamp.
"""

CEILING_FLOOR_MS: Final = _CEILING_FLOOR_MS
"""Lowest `ceiling_ms` drawn, re-exported from the law rather than re-derived.

The arithmetic and its justification live on `arbiter.CEILING_FLOOR_MS`, which is
the single definition; this name exists only so the strategies below read in terms
of the domain rather than reaching into the law for a bound mid-expression.

**Settled by ADR-021, and this bound does not widen.** A `ceiling_ms` below
1100 ms is not a valid configuration: `Settings` rejects it at process startup
and `SessionProxy` clamps a per-connection override up to it. §4's ordering stays
verbatim and repair is not re-applied after the ceiling, because a law that
applies a ceiling and then knowingly raises `max_ms` back above it returns a
config violating the ceiling on purpose, every turn, silently.

So the restriction below is **spec-backed rather than a convenience**. It was
written as a deliberate narrowing with both readings noted, on the grounds that a
property failing on an unreachable state reports a bug that does not exist; under
ADR-021 that state is unreachable by construction, and this domain is the whole of
the valid domain rather than a safe corner of it.

**Re-derived, and this note was right** (ADR-040). The floor was stated against the
ceiling *before* the overhead was subtracted, so landing the constant at 217 ms left
`max=883` against `min=800` at a ceiling of 1100 and took §9 property 1 red.
`CEILING_FLOOR_MS` now carries `+ ENDPOINT_OVERHEAD_MS` and this domain follows it,
because it is derived from the constant rather than written as 1100.
"""

CEILING_MAX_MS: Final = 6000
"""Highest `ceiling_ms` drawn. Above `MAX_MS_CEIL`, so the ceiling stops binding."""


def unit_intervals() -> st.SearchStrategy[float]:
    """Floats in `[0, 1]`: the range §2.3, §2.4 and §2.5 clamp their outputs to."""
    return st.floats(
        min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
    )


def gap_quantiles_ms() -> st.SearchStrategy[float]:
    """Floats in `[0, GAP_CLAMP_MAX_MS]`.

    §2.1 clamps every gap to `[0, 6000]` ms *before* ingestion, so no quantile of
    those gaps can leave that interval regardless of what the caller did.
    """
    return st.floats(
        min_value=0.0,
        max_value=float(GAP_CLAMP_MAX_MS),
        allow_nan=False,
        allow_infinity=False,
    )


@st.composite
def speaker_features(
    draw: st.DrawFn,
    *,
    warm: bool | None = None,
    disfluency: st.SearchStrategy[float] | None = None,
    gaps_ms: st.SearchStrategy[float] | None = None,
) -> SpeakerFeatures:
    """An internally consistent `SpeakerFeatures` (CONTROL_SPEC.md §2).

    Two couplings are enforced rather than drawn, because a property test that
    fails on an unreachable state reports a bug that does not exist:

    - `cold == (n_gaps < MIN_GAPS_FOR_WARM)`. `Profiler.features` derives `cold`
      from `n_gaps`; they are two views of one fact, and §4 branches on it. This
      one is genuinely unreachable otherwise.
    - `g_p90_ms >= g_p50_ms`. Two quantiles over one sample set — **and this
      justification is now known to be too strong.** Gate 2 measured the
      profiler emitting the inversion: `g_p50` and `g_p90` are two *independent*
      P² estimators (CONTROL_SPEC §2.1 says to maintain two and says nothing
      about coupling them), their approximation errors are independent, and over
      20 000 random gap streams the order inverted 28 times, 0.14 %, worst
      inversion 146 ms. See
      `test_profiler_quantiles.test_two_independent_estimators_can_report_p90_below_p50`.

      The restriction is kept for now rather than widened, because widening it is
      a decision about what the control law must tolerate and CONTROL_SPEC §0
      makes that an ADR. The consequence of keeping it is recorded here so it is
      not mistaken for a guarantee: **the §9 properties below have never been
      evaluated on an inverted state**, and an inverted state can reach
      `decide()` in production. The §4 invariant repair stops it producing
      `max < min`, so the visible damage is bounded; what is untested is
      everything else. Gate 3 owns the call.

    Args:
        draw: Hypothesis draw function.
        warm: `True` forces `n_gaps >= MIN_GAPS_FOR_WARM`, `False` forces below it,
            `None` draws across the boundary.
        disfluency: Override strategy for the disfluency feature.
        gaps_ms: Override strategy for both gap quantiles. Narrow this to aim the
            law at a particular window.

    Returns:
        The drawn features.
    """
    gaps = gap_quantiles_ms() if gaps_ms is None else gaps_ms
    g_p50 = draw(gaps)
    g_p90 = draw(gaps.filter(lambda value: value >= g_p50))
    if warm is None:
        n_gaps = draw(st.integers(min_value=0, max_value=4096))
    elif warm:
        n_gaps = draw(st.integers(min_value=MIN_GAPS_FOR_WARM, max_value=4096))
    else:
        n_gaps = draw(st.integers(min_value=0, max_value=MIN_GAPS_FOR_WARM - 1))
    return SpeakerFeatures(
        n_gaps=n_gaps,
        g_p50_ms=g_p50,
        g_p90_ms=g_p90,
        speech_rate=draw(
            st.floats(
                min_value=0.0,
                max_value=SPEECH_RATE_MAX,
                allow_nan=False,
                allow_infinity=False,
            )
        ),
        disfluency=draw(unit_intervals() if disfluency is None else disfluency),
        jitter=draw(unit_intervals()),
        recent_cuts=draw(unit_intervals()),
        cold=n_gaps < MIN_GAPS_FOR_WARM,
    )


def window_hints() -> st.SearchStrategy[WindowHint]:
    """A compiled context-axis row (CONTROL_SPEC.md §3), over `POLICY_MULT_MAX`."""
    multiplier = st.floats(
        min_value=0.0,
        max_value=POLICY_MULT_MAX,
        allow_nan=False,
        allow_infinity=False,
    )
    return st.builds(WindowHint, min_mult=multiplier, max_mult=multiplier)


def ceilings_ms() -> st.SearchStrategy[int]:
    """A latency ceiling. Lower bound justified at `CEILING_FLOOR_MS`."""
    return st.integers(min_value=CEILING_FLOOR_MS, max_value=CEILING_MAX_MS)


@st.composite
def turn_configs(
    draw: st.DrawFn,
    *,
    min_ms: st.SearchStrategy[int] | None = None,
    max_ms: st.SearchStrategy[int] | None = None,
) -> TurnConfig:
    """A `TurnConfig` that could plausibly already be in force on the socket.

    Drawn inside the §4 clamps and satisfying the §4 invariant, because that is
    what the arbiter's own previous decision would have left there. A `current`
    outside the clamps is a state the controller never produced, so a decay or
    hysteresis property failing on one says nothing.

    Args:
        draw: Hypothesis draw function.
        min_ms: Override strategy for `min_turn_silence_ms`.
        max_ms: Override strategy for `max_turn_silence_ms`. Constrained after the
            fact to satisfy the invariant against the drawn minimum.

    Returns:
        The drawn configuration.
    """
    minimum = draw(
        st.integers(min_value=MIN_MS_FLOOR, max_value=MIN_MS_CEIL)
        if min_ms is None
        else min_ms
    )
    floor = max(MAX_MS_FLOOR, minimum + INVARIANT_GAP_MS)
    candidate = (
        st.integers(min_value=floor, max_value=MAX_MS_CEIL)
        if max_ms is None
        else max_ms.map(lambda value: max(value, floor))
    )
    return TurnConfig(
        min_turn_silence_ms=minimum,
        max_turn_silence_ms=draw(candidate),
        # ADR-001 measured this INERT and §4 never emits it, but it is on the
        # dataclass and a decision may not read it. Drawn so that it cannot be
        # read without the property noticing.
        end_of_turn_confidence_threshold=draw(unit_intervals()),
        vad_threshold=draw(st.none() | unit_intervals()),
    )


@st.composite
def arbiter_inputs(
    draw: st.DrawFn,
    *,
    features: st.SearchStrategy[SpeakerFeatures] | None = None,
    hint: st.SearchStrategy[WindowHint] | None = None,
    expected_answer: st.SearchStrategy[ExpectedAnswer | None] | None = None,
    current: st.SearchStrategy[TurnConfig] | None = None,
    ceiling_ms: st.SearchStrategy[int] | None = None,
) -> ArbiterInput:
    """A full `ArbiterInput`, with the three suppressing guards pinned off.

    Overrides are strategies rather than values so that `st.none()` means "always
    no host hint" and cannot be confused with "draw one" — `expected_answer` is
    legitimately `None` when the host declares nothing, so a `None` default
    sentinel would be ambiguous exactly where it matters.

    Args:
        draw: Hypothesis draw function.
        features: Override strategy for the speaker axis.
        hint: Override strategy for the context axis.
        expected_answer: Override strategy for the declared dialogue state.
        current: Override strategy for the configuration already in force.
        ceiling_ms: Override strategy for the latency ceiling.

    Returns:
        The drawn decision input.
    """
    return ArbiterInput(
        features=draw(speaker_features() if features is None else features),
        hint=draw(window_hints() if hint is None else hint),
        expected_answer=draw(
            st.none() | st.sampled_from(EXPECTED_ANSWERS)
            if expected_answer is None
            else expected_answer
        ),
        current=draw(turn_configs() if current is None else current),
        capabilities=ALL_LIVE_CAPABILITIES,
        ceiling_ms=draw(ceilings_ms() if ceiling_ms is None else ceiling_ms),
        turn_order=draw(st.integers(min_value=1, max_value=500)),
        # Stream-relative milliseconds (CLAUDE.md §6), up to a four-hour call —
        # the duration INV-3 names as the memory bound.
        t_ms=draw(st.integers(min_value=0, max_value=4 * 60 * 60 * 1000)),
        host_override_fields=frozenset(),
        patches_sent=draw(st.integers(min_value=0, max_value=MAX_PATCHES - 1)),
    )
