"""Merge the two axes, apply the guards, emit a `ConfigPatch`.

ARCHITECTURE.md §2: this module never does I/O. `decide` is declared `def`, not
`async def`, so the audio pump cannot await it (INV-1), and its signature carries
no logger, sink or socket, so there is nothing to do I/O with (INV-2).

Every constant below is transcribed from CONTROL_SPEC.md §4 and §5. Changing one
is an ADR with the bench delta in the commit message, not a commit
(CONTROL_SPEC.md §0 and §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from nod_core.types import (
    Capabilities,
    ConfigPatch,
    ControllerState,
    ExpectedAnswer,
    SpeakerFeatures,
    TurnConfig,
    WindowHint,
)

# --- CONTROL_SPEC.md §4: the control law ------------------------------------

BASE_MIN_MS: Final = 400
"""Balanced starting point for `min_turn_silence`. Milliseconds."""

BASE_MAX_MS: Final = 1280
"""Balanced starting point for `max_turn_silence`. Milliseconds."""

BASE_CONF: Final = 0.40
"""Balanced starting point for `end_of_turn_confidence_threshold`."""

MIN_MS_FROM_P50_GAIN: Final = 0.6
MIN_MS_OFFSET: Final = 120
"""`min_ms = MIN_MS_FROM_P50_GAIN * g_p50 + MIN_MS_OFFSET`. Milliseconds."""

MAX_MS_FROM_P90_GAIN: Final = 1.6
MAX_MS_OFFSET: Final = 250
"""`max_ms = MAX_MS_FROM_P90_GAIN * g_p90 + MAX_MS_OFFSET`. Milliseconds."""

CONF_DISFLUENCY_GAIN: Final = 0.45
CONF_RECENT_CUTS_GAIN: Final = 0.15
CONF_JITTER_GAIN: Final = 0.10
"""Speaker-axis weights on the confidence threshold."""

MIN_MS_FLOOR: Final = 160
MIN_MS_CEIL: Final = 900
MAX_MS_FLOOR: Final = 400
MAX_MS_CEIL: Final = 4000
CONF_FLOOR: Final = 0.30
CONF_CEIL: Final = 0.90
"""Hard, absolute clamps. Milliseconds for the window, unitless for confidence."""

INVARIANT_GAP_MS: Final = 200
"""Invariant repair: `max_ms >= min_ms + INVARIANT_GAP_MS`. Milliseconds."""

DEFAULT_CEILING_MS: Final = 2600
"""Default latency ceiling. Per-deployment via `NOD_CEILING_MS`. Milliseconds."""

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
"""Asymmetric decay: widening is immediate, narrowing is capped at this per turn."""

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
    """
    raise NotImplementedError


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
        """
        raise NotImplementedError

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
        """
        raise NotImplementedError

    def should_force_endpoint(self, state: ArbiterInput, trailing_gap_ms: int) -> bool:
        """Decide the confident early endpoint of CONTROL_SPEC.md §4. `O(1)`.

        Rate-limited to once per turn, and disabled entirely above
        `EARLY_ENDPOINT_MAX_DISFLUENCY`.

        Args:
            state: The full decision input for this turn.
            trailing_gap_ms: Silence since the last finalised word, milliseconds.

        Returns:
            Whether to send `ForceEndpoint` rather than wait out the silence.
        """
        raise NotImplementedError

    def note_error(self, exc: BaseException) -> None:
        """Enter `SAFE` after a controller exception (CONTROL_SPEC.md §6).

        Last known good config, no patches, error counted. Per INV-8 the call
        continues; in `dev` the caller re-raises after the call ends.

        Args:
            exc: The exception that was caught.
        """
        raise NotImplementedError

    @property
    def state(self) -> ControllerState:
        """The current state machine position. `O(1)`."""
        raise NotImplementedError
