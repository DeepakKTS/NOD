"""Merge the two axes, apply the guards, emit a `ConfigPatch`.

ARCHITECTURE.md §2: this module never does I/O. `decide` is declared `def`, not
`async def`, so the audio pump cannot await it (INV-1), and its signature carries
no logger, sink or socket, so there is nothing to do I/O with (INV-2).

Every constant below is transcribed from CONTROL_SPEC.md §4 and §5. Changing one
is an ADR with the bench delta in the commit message, not a commit
(CONTROL_SPEC.md §0 and §8).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Final

from nod_core.types import (
    Capabilities,
    ConfigDecision,
    ConfigPatch,
    ControllerState,
    ExpectedAnswer,
    SpeakerFeatures,
    TurnConfig,
    WindowHint,
)

WIRE_FIELDS: Final = (
    ("min_turn_silence", "min_turn_silence_ms"),
    ("max_turn_silence", "max_turn_silence_ms"),
)
"""The two knobs §4 emits, as (wire name, `TurnConfig` attribute).

`UpdateConfiguration` names them without the unit suffix that `TurnConfig` carries,
and the capability gate is keyed on the wire name (`capabilities.UPDATABLE_FIELDS`).
Pairing them here rather than translating at each use keeps the two spellings from
drifting, and `test_the_wire_fields_match_the_capability_names` asserts the pairing
against `UPDATABLE_FIELDS` rather than trusting this tuple.

`end_of_turn_confidence_threshold` and `vad_threshold` are absent deliberately:
ADR-001 measured the first INERT and §4 never emits either, so they are carried
through from the config already in force rather than computed (ADR-011).
"""

# --- CONTROL_SPEC.md §4: the control law ------------------------------------

BASE_MIN_MS: Final = 400
"""Balanced starting point for `min_turn_silence`. Milliseconds."""

BASE_MAX_MS: Final = 1280
"""Balanced starting point for `max_turn_silence`. Milliseconds."""

MIN_MS_FROM_P50_GAIN: Final = 0.6
MIN_MS_OFFSET: Final = 120
"""`min_ms = MIN_MS_FROM_P50_GAIN * g_p50 + MIN_MS_OFFSET`. Milliseconds."""

MAX_MS_FROM_P90_GAIN: Final = 1.6
MAX_MS_OFFSET: Final = 250
"""`max_ms = MAX_MS_FROM_P90_GAIN * g_p90 + MAX_MS_OFFSET`. Milliseconds."""

MAX_MS_DISFLUENCY_GAIN: Final = 0.45
MAX_MS_RECENT_CUTS_GAIN: Final = 0.15
"""Speaker-axis weights on `max_turn_silence` (CONTROL_SPEC.md §4, ADR-011).

They moved off the confidence threshold because P1 measured that field inert on
`universal-streaming-english`, and onto `max_turn_silence` because a mid-sentence
pause is an incomplete utterance, which is the regime that knob governs.
"""

JITTER_GAIN: Final = 0.0
"""Weight on `jitter`. Zero, deliberately (ADR-011).

CONTROL_SPEC.md §2.4 assumed `end_of_turn_confidence` measures speaker
uncertainty; P1 measured it near zero throughout an utterance and spiking only on
the boundary frame, so it reports turn completion instead. The feature is still
computed and logged so the bench can evaluate it. Raising this weight is an ADR,
and CONTROL_SPEC.md §9 test 10 fails if it is raised silently.
"""

ENDPOINT_OVERHEAD_MS: Final = 0
"""Milliseconds the boundary lands after the configured gate (EC-49).

Zero until `make bench` measures it. P1 saw 155-290 ms across eleven single-sample
configurations, which is directionally clear and not a number to hand-write into
the control law: INV-9 applies here as much as to the README.
"""

MIN_MS_FLOOR: Final = 160
MIN_MS_CEIL: Final = 900
MAX_MS_FLOOR: Final = 400
MAX_MS_CEIL: Final = 4000
"""Hard, absolute clamps. Milliseconds."""

INVARIANT_GAP_MS: Final = 200
"""Invariant repair: `max_ms >= min_ms + INVARIANT_GAP_MS`. Milliseconds."""

DEFAULT_CEILING_MS: Final = 2600
"""Default latency ceiling. Per-deployment via `NOD_CEILING_MS`. Milliseconds."""

CEILING_FLOOR_MS: Final = MIN_MS_CEIL + INVARIANT_GAP_MS
"""Lowest valid `ceiling_ms`. 1100 ms. Milliseconds (ADR-021).

§4 applies the latency ceiling *after* the invariant repair, so a ceiling below
this would undo it: `min_ms` clamps up to `MIN_MS_CEIL`, the repair lifts `max_ms`
to `min_ms + INVARIANT_GAP_MS`, and a smaller ceiling then pulls it back under.
ADR-021 resolves that by making such a ceiling an invalid *configuration* rather
than by re-ordering §4 — a law that applies a ceiling and then knowingly raises
`max_ms` back above it violates the ceiling on purpose, every turn, silently.

Derived rather than written as 1100 so it follows the clamps it comes from.
`Settings` enforces it at startup and §9 property 2 is stated over it.
"""

EARLY_ENDPOINT_GP90_MULT: Final = 1.8
"""Confident early endpoint fires above `max(max_ms, g_p90 * this)`."""

EARLY_ENDPOINT_MAX_DISFLUENCY: Final = 0.35
"""Early endpoint is disabled above this disfluency.

A disfluent speaker is exactly the person whose long gap is not a finished turn.
"""

# --- CONTROL_SPEC.md §5: the guards -----------------------------------------

HYST_FRACTION: Final = 0.15
"""Hysteresis: emit only if a field moves more than this fraction of its value."""

HYST_CONF_ABS: Final = 0.05
"""Hysteresis: or if `conf` moves more than this, absolute."""

NARROW_STEP: Final = 0.12
"""Asymmetric decay: narrowing is capped at this fraction of the reference per turn."""

WIDEN_STEP: Final = 0.25
"""Asymmetric decay: widening is capped at this fraction of the reference per turn.

**New in ADR-022; widening was immediate and unbounded before it.** Unbounded, a
single spurious early estimate landed `max_ms` at `MAX_MS_CEIL` in one turn, and
`NARROW_STEP` then needed 19 turns to undo it — ADR-020's ratchet reached by
another route, with the guard §5 built to *bound* a stumble being what made the
stumble persist.

0.25 is derived against three quantities at once: 4 turns to serve a genuinely
hesitant caller (1280 → 2650 ms), 2 turns to undo one spurious turn at
`NARROW_STEP`, and 6 consecutive wrong turns to reach the ceiling. Symmetric 0.12
was rejected at 7 turns to serve, which is 7 turns of cutting off the caller this
project exists for. It stays 2.08x `NARROW_STEP`, so §5's asymmetry survives as
an asymmetry rather than being flattened into symmetry.
"""

MAX_PATCHES: Final = 24
"""Rate cap: patches per session. At most one per turn (EC-35)."""

BOOLEAN_MIN_MS_CAP: Final = 400
"""Floor guard: on a `boolean` turn, `min_ms` never exceeds this. Milliseconds."""

FREEZE_REVERSALS: Final = 3
FREEZE_WINDOW_TURNS: Final = 5
FREEZE_DURATION_TURNS: Final = 10
"""Freeze on instability: this many reversals in this window freezes the speaker
axis for this many turns (EC-30)."""

HOST_OVERRIDE_MS: Final = 5000
"""Host override: fields the host set stay the host's for this long (EC-33)."""


@dataclass(frozen=True, slots=True)
class ArbiterInput:
    """Everything `decide` is allowed to see.

    Frozen and slotted so that a decision cannot mutate its own input, and so
    that the whole input is one cheap object (INV-2).
    """

    features: SpeakerFeatures
    hint: WindowHint
    expected_answer: ExpectedAnswer | None
    current: TurnConfig
    capabilities: Capabilities
    ceiling_ms: int
    turn_order: int
    t_ms: int
    host_override_fields: frozenset[str]
    patches_sent: int


def control_law(
    features: SpeakerFeatures,
    hint: WindowHint,
    *,
    cold: bool,
    ceiling_ms: int,
) -> TurnConfig:
    """Apply CONTROL_SPEC.md §4 verbatim. Pure, stateless, `O(1)`.

    Speaker axis, then context axis, then the hard clamps, then invariant repair,
    then the latency ceiling. When `cold`, the speaker axis is skipped entirely
    and only the context axis applies to the base values (EC-36).

    Both axes move together by construction: silence beats confidence, so raising
    the confidence threshold alone does not stop the agent interrupting a long
    pause (CONTROL_SPEC.md §0).

    Args:
        features: The speaker axis.
        hint: The context axis for this one turn.
        cold: Whether the profiler is still cold.
        ceiling_ms: The latency ceiling `max_ms` may never exceed.

    Returns:
        The resulting configuration, clamped and repaired.

        `end_of_turn_confidence_threshold` is `0.0` and `vad_threshold` is `None`
        in the returned record, and neither is a computed value: §4 produces only
        the two silence knobs, and `Arbiter.decide` carries the other two through
        from the configuration already in force. They are on `TurnConfig` because
        that dataclass describes the socket's four knobs, not because this
        function has an opinion about them.
    """
    if cold:
        min_ms = float(BASE_MIN_MS)
        max_ms = float(BASE_MAX_MS)
    else:
        min_ms = MIN_MS_FROM_P50_GAIN * features.g_p50_ms + MIN_MS_OFFSET
        max_ms = (MAX_MS_FROM_P90_GAIN * features.g_p90_ms + MAX_MS_OFFSET) * (
            1.0
            + MAX_MS_DISFLUENCY_GAIN * features.disfluency
            + MAX_MS_RECENT_CUTS_GAIN * features.recent_cuts
            # `jitter` is read nowhere. ADR-011 weighted it 0 and §9 property 10
            # asserts two states differing only in it produce an equal patch, so
            # the feature is absent from this expression rather than multiplied
            # by a zero constant — a `+ JITTER_GAIN * features.jitter` term would
            # satisfy the property today and restore the weight with one edit.
        )
    min_ms *= hint.min_mult
    max_ms *= hint.max_mult
    min_ms = min(max(min_ms, MIN_MS_FLOOR), MIN_MS_CEIL)
    max_ms = min(max(max_ms, MAX_MS_FLOOR), MAX_MS_CEIL)
    max_ms = max(max_ms, min_ms + INVARIANT_GAP_MS)
    max_ms = min(max_ms, ceiling_ms - ENDPOINT_OVERHEAD_MS)
    return TurnConfig(
        min_turn_silence_ms=int(min_ms),
        max_turn_silence_ms=int(max_ms),
        end_of_turn_confidence_threshold=0.0,
        vad_threshold=None,
    )


def _repair(config: TurnConfig) -> TurnConfig:
    """Re-apply §4's clamps and invariant gap to an already-built config. `O(1)`.

    ADR-020 step 4. Asymmetric decay moves the two knobs independently, so a
    reference sitting at `max = min + INVARIANT_GAP_MS` narrowed by
    `NARROW_STEP` on both fields leaves a gap of `0.88 * 200 = 176 ms` — the
    invariant broken by a guard, from inputs that satisfied it. §9 properties 1
    and 2 assert on `decide()`'s output for exactly this reason.

    Args:
        config: A post-decay configuration.

    Returns:
        The same configuration with the clamps and the invariant restored.
    """
    min_ms = min(max(config.min_turn_silence_ms, MIN_MS_FLOOR), MIN_MS_CEIL)
    max_ms = min(max(config.max_turn_silence_ms, MAX_MS_FLOOR), MAX_MS_CEIL)
    max_ms = max(max_ms, min_ms + INVARIANT_GAP_MS)
    return replace(
        config, min_turn_silence_ms=int(min_ms), max_turn_silence_ms=int(max_ms)
    )


class Arbiter:
    """Stateful wrapper around the control law: guards, state machine, patches.

    The pure law is `control_law`; this class adds the parts that need history —
    hysteresis, asymmetric decay, the rate cap, the freeze detector and the
    four-state machine of CONTROL_SPEC.md §6.
    """

    def __init__(
        self,
        *,
        capabilities: Capabilities,
        ceiling_ms: int = DEFAULT_CEILING_MS,
    ) -> None:
        """Initialise an arbiter for one session.

        Args:
            capabilities: What the probe found. A field marked unsupported is
                never sent (CONTROL_SPEC.md §5, capability gate).
            ceiling_ms: The latency ceiling for this deployment.

        Raises:
            ValueError: `ceiling_ms` is below `CEILING_FLOOR_MS`.

                ADR-021 rejects such a ceiling at configuration rather than
                repairing it per turn, and a constructor is a configuration
                boundary. `decide` clamps the per-turn value instead, which is
                the same ADR's other side: INV-8's "fail soft in a call".
        """
        if ceiling_ms < CEILING_FLOOR_MS:
            msg = (
                f"ceiling_ms={ceiling_ms} is below CEILING_FLOOR_MS="
                f"{CEILING_FLOOR_MS}; §4 applies the ceiling after the invariant "
                "repair, so a lower one would return max_ms < min_ms + "
                f"{INVARIANT_GAP_MS} (ADR-021)"
            )
            raise ValueError(msg)
        self._capabilities = capabilities
        self._ceiling_ms = ceiling_ms
        self._state = ControllerState.COLD
        # ADR-020: hysteresis and decay measure against the config this arbiter
        # last *emitted*, not against `ArbiterInput.current`. §9 property 5 is
        # unsatisfiable otherwise — the caller passes the same `current` twice,
        # so a target-versus-`current` test would clear the threshold on the
        # second call exactly as on the first. `None` until the first patch, at
        # which point `state.current` stands in as the session's starting point.
        self._emitted: TurnConfig | None = None
        self._directions: deque[tuple[int, int]] = deque(maxlen=FREEZE_WINDOW_TURNS)
        self._patched_turn = 0
        self._frozen_until_turn = 0
        self._forced_turn = 0
        self._errors = 0

    def decide(self, state: ArbiterInput) -> ConfigPatch | None:
        """Decide whether to patch, and to what. `O(1)`.

        Pure, synchronous and allocation-light: no I/O, no logging to disk, no
        network, no LLM call, and nothing allocated beyond the returned frozen
        dataclass. p99 under 5 ms, mean under 200 µs (INV-2, ARCHITECTURE.md §4).

        Every returned patch carries a `ConfigDecision` with the trigger, the
        inputs, the old value, the new value and the rule id (INV-4).

        Args:
            state: The full decision input for this turn.

        Returns:
            The patch to send, or `None` when hysteresis, the rate cap, the
            freeze guard, the host override or the capability gate suppresses it.

        The order is ADR-020's, and it is the order because §5's table does not
        say: target, then hysteresis against the last emitted config, then
        per-field decay, then §4's repair and clamps again.
        """
        # §6: SAFE holds last known good for one turn, then returns to COLD. The
        # recovery turn itself emits nothing — the arbiter has just caught an
        # exception and has no grounds to move the window on the same turn.
        if self._state is ControllerState.SAFE:
            self._state = ControllerState.COLD
            return None

        # ADR-021's soft side. SessionProxy clamps a per-connection override, so
        # a bad value should not reach here; clamping anyway costs one comparison
        # and keeps §9 property 2 true for a caller that bypassed the proxy.
        ceiling_ms = max(state.ceiling_ms, CEILING_FLOOR_MS)

        if (
            self._state is ControllerState.FROZEN
            and state.turn_order >= self._frozen_until_turn
        ):
            self._state = ControllerState.WARM
        frozen = self._state is ControllerState.FROZEN
        if not frozen:
            self._state = (
                ControllerState.COLD if state.features.cold else ControllerState.WARM
            )

        # FROZEN holds the speaker axis and lets the context axis through, which
        # is the same shape as COLD: base values times the hint (§6).
        target = control_law(
            state.features,
            state.hint,
            cold=state.features.cold or frozen,
            ceiling_ms=ceiling_ms,
        )

        # §5 floor guard, applied to the target before hysteresis so that a
        # boolean turn is never *asked* for a slow minimum. It cannot live in
        # `control_law`, whose signature carries no `ExpectedAnswer`: the law
        # sees only the compiled multipliers, and `boolean`'s 0.7 widens a small
        # number rather than capping a large one.
        if state.expected_answer == "boolean":
            target = replace(
                target,
                min_turn_silence_ms=min(target.min_turn_silence_ms, BOOLEAN_MIN_MS_CAP),
            )

        reference = self._emitted if self._emitted is not None else state.current
        sendable = self._capabilities.updatable_fields - state.host_override_fields

        # §5 rate cap, both halves. The session half is the caller's count; the
        # per-turn half has to be the arbiter's, because the caller passing the
        # same turn twice is precisely the case it exists to stop.
        #
        # This half is also what makes §9 property 5 true under ADR-020 and
        # ADR-022. Decay caps a step, so a large move takes several turns and the
        # law's target survives being reached — "the second emits nothing" cannot
        # mean "the journey completes in one turn", or the step caps would be
        # meaningless. It means one patch per turn: an identical state carries an
        # identical `turn_order`, so the second call is the same turn asking
        # twice, while a *new* turn continues the journey.
        if state.turn_order <= self._patched_turn:
            return None
        if state.patches_sent >= MAX_PATCHES:
            return None
        if not self._moved_enough(target, reference, sendable):
            return None

        proposed, changed = self._decayed(target, reference, state.current, sendable)
        proposed = _repair(proposed)
        # Defence in depth, and honestly labelled: I could not construct an input
        # that reaches this. `_moved_enough` only returns True for a sendable
        # field whose target differs from the reference by more than
        # `HYST_FRACTION`, and for any reference at or above `MIN_MS_FLOOR` a
        # capped step in either direction changes the integer, so `changed` is
        # non-empty and `proposed` differs. It stays because the argument is about
        # three guards agreeing and any one of them moving could break it, and an
        # empty patch on the wire is a socket write that explains nothing (INV-4).
        # Reported as uncovered rather than covered by a contrived test.
        if not changed or proposed == reference:
            return None

        self._note_direction(state.turn_order, proposed, reference)
        self._emitted = proposed
        self._patched_turn = state.turn_order
        return ConfigPatch(
            changed=changed,
            config=proposed,
            decision=ConfigDecision(
                rule_id="speaker+context" if not frozen else "context-only",
                trigger="turn",
                inputs=(
                    ("g_p50_ms", state.features.g_p50_ms),
                    ("g_p90_ms", state.features.g_p90_ms),
                    ("disfluency", state.features.disfluency),
                    ("recent_cuts", state.features.recent_cuts),
                    ("min_mult", state.hint.min_mult),
                    ("max_mult", state.hint.max_mult),
                    ("ceiling_ms", float(ceiling_ms)),
                ),
                old=reference,
                new=proposed,
                state=self._state,
            ),
            t_ms=state.t_ms,
        )

    @staticmethod
    def _moved_enough(
        target: TurnConfig, reference: TurnConfig, sendable: frozenset[str]
    ) -> bool:
        """§5 hysteresis, on the law's target. `O(1)`, two fields.

        ADR-020's reading: the question is whether the law wants a *materially
        different* window, not whether the value this turn would emit differs
        from the one in force. Measured on the target because a post-decay
        comparison makes narrowing unreachable — `NARROW_STEP` at 12 % can never
        clear `HYST_FRACTION` at 15 %, and the controller becomes a one-way
        ratchet.

        Args:
            target: The law's output for this turn.
            reference: The configuration last emitted, or the session's starting
                configuration.
            sendable: Wire names the capability gate and host override allow.

        Returns:
            Whether any sendable field moved by more than `HYST_FRACTION`.
        """
        for wire, attribute in WIRE_FIELDS:
            if wire not in sendable:
                continue
            was = getattr(reference, attribute)
            now = getattr(target, attribute)
            if abs(now - was) > HYST_FRACTION * was:
                return True
        return False

    @staticmethod
    def _decayed(
        target: TurnConfig,
        reference: TurnConfig,
        current: TurnConfig,
        sendable: frozenset[str],
    ) -> tuple[TurnConfig, tuple[str, ...]]:
        """§5 asymmetric decay, per field. `O(1)`, two fields.

        Widening is capped at `WIDEN_STEP` and narrowing at `NARROW_STEP`, both
        as a fraction of the reference (ADR-020 for the ordering, ADR-022 for the
        widening cap). A field the capability gate or the host override excludes
        keeps the value already in force, so a patch never asserts a value for a
        knob it is not sending.

        Args:
            target: The law's output.
            reference: The configuration last emitted, or the starting one.
            current: The configuration actually in force, for excluded fields.
            sendable: Wire names allowed through.

        Returns:
            The configuration to emit, and the wire names that moved.
        """
        values: dict[str, int] = {}
        changed: list[str] = []
        for wire, attribute in WIRE_FIELDS:
            if wire not in sendable:
                values[attribute] = getattr(current, attribute)
                continue
            was = getattr(reference, attribute)
            wanted = getattr(target, attribute)
            if wanted > was:
                stepped = min(wanted, was * (1.0 + WIDEN_STEP))
            else:
                stepped = max(wanted, was * (1.0 - NARROW_STEP))
            values[attribute] = int(stepped)
            if values[attribute] != was:
                changed.append(wire)
        return (
            replace(
                current,
                min_turn_silence_ms=values["min_turn_silence_ms"],
                max_turn_silence_ms=values["max_turn_silence_ms"],
            ),
            tuple(changed),
        )

    def _note_direction(
        self, turn_order: int, proposed: TurnConfig, reference: TurnConfig
    ) -> None:
        """§5 freeze-on-instability. `O(FREEZE_WINDOW_TURNS)`, five entries.

        "3 patches in 5 turns all reverse direction" is counted as
        `FREEZE_REVERSALS` sign changes in `FREEZE_WINDOW_TURNS` turns, which is
        the reading the constant's name supports. The alternative — three patches
        with alternating signs, which is two reversals — would freeze sooner. §9
        does not test this guard, so the reading is recorded rather than derived.

        Args:
            turn_order: The turn this patch belongs to.
            proposed: The configuration being emitted.
            reference: What it is replacing.
        """
        direction = (
            1
            if proposed.max_turn_silence_ms > reference.max_turn_silence_ms
            else -1
            if proposed.max_turn_silence_ms < reference.max_turn_silence_ms
            else 0
        )
        if direction == 0:
            return
        self._directions.append((turn_order, direction))
        recent = [
            sign
            for turn, sign in self._directions
            if turn > turn_order - FREEZE_WINDOW_TURNS
        ]
        reversals = sum(1 for a, b in pairwise(recent) if a != b)
        if reversals >= FREEZE_REVERSALS:
            self._state = ControllerState.FROZEN
            self._frozen_until_turn = turn_order + FREEZE_DURATION_TURNS
            self._directions.clear()

    def should_force_endpoint(self, state: ArbiterInput, trailing_gap_ms: int) -> bool:
        """Decide the confident early endpoint of CONTROL_SPEC.md §4. `O(1)`.

        Rate-limited to once per turn, and disabled entirely above
        `EARLY_ENDPOINT_MAX_DISFLUENCY`.

        Args:
            state: The full decision input for this turn.
            trailing_gap_ms: Silence since the last finalised word, milliseconds.

        Returns:
            Whether to send `ForceEndpoint` rather than wait out the silence.

        The caller must only ask for a turn whose last word is finalised. §4
        requires it and this signature cannot check it: an unfinalised word has no
        trustworthy end, so `trailing_gap_ms` measured against one is measured
        against a timestamp the upstream has not committed to (EC-21).
        """
        if not self._capabilities.supports_force_endpoint:
            return False
        if state.features.cold or self._state is not ControllerState.WARM:
            return False
        if state.features.disfluency > EARLY_ENDPOINT_MAX_DISFLUENCY:
            return False
        if state.turn_order <= self._forced_turn:
            return False
        window = self._emitted if self._emitted is not None else state.current
        threshold = max(
            float(window.max_turn_silence_ms),
            state.features.g_p90_ms * EARLY_ENDPOINT_GP90_MULT,
        )
        if trailing_gap_ms <= threshold:
            return False
        self._forced_turn = state.turn_order
        return True

    def note_error(self, exc: BaseException) -> None:
        """Enter `SAFE` after a controller exception (CONTROL_SPEC.md §6).

        Last known good config, no patches, error counted. Per INV-8 the call
        continues; in `dev` the caller re-raises after the call ends.

        Args:
            exc: The exception that was caught.
        """
        del exc  # The arbiter counts errors; the proxy logs them (ARCHITECTURE §2).
        self._errors += 1
        self._state = ControllerState.SAFE

    @property
    def state(self) -> ControllerState:
        """The current state machine position. `O(1)`."""
        return self._state

    @property
    def errors(self) -> int:
        """Controller exceptions caught this session. `O(1)`. INV-8.

        Exposed rather than logged: `nod_core` does no I/O, and the proxy needs
        the count to decide whether to re-raise after the call in `dev`.
        """
        return self._errors
