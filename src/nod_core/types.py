"""Frozen, slotted dataclasses that cross module boundaries.

ARCHITECTURE.md §2: this module never imports anything heavy. It is the shared
vocabulary of `nod_core` and the only `nod_core` module that adapters may import.

Per CLAUDE.md §6 every dataclass here is ``frozen=True, slots=True``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal

# A JSON document, spelled without `Any` so that `nod_core` can satisfy
# `disallow_any_explicit` (CLAUDE.md §3). Trace payloads use this type.
type JsonValue = (
    str | int | float | bool | Sequence[JsonValue] | Mapping[str, JsonValue] | None
)

# CONTROL_SPEC.md §1: the dialogue state the host declares for the context axis.
type ExpectedAnswer = Literal[
    "free",
    "boolean",
    "entity_id",
    "entity_date",
    "entity_address",
    "entity_list",
    "spelling",
    "number",
]

# ARCHITECTURE.md §6: every persisted trace line carries this in its `v` field.
TRACE_SCHEMA_VERSION: Final = 1


class ControllerState(Enum):
    """The four-state machine of CONTROL_SPEC.md §6."""

    COLD = "cold"
    WARM = "warm"
    FROZEN = "frozen"
    SAFE = "safe"


class NodMode(Enum):
    """The `nod_mode` connection parameter of ARCHITECTURE.md §7."""

    ADAPT = "adapt"
    OBSERVE = "observe"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class Word:
    """One word from a `Turn` event's `words` array (ARCHITECTURE.md §2).

    Timings are stream-relative milliseconds from AssemblyAI word timings, never
    wall clock (CLAUDE.md §6).
    """

    text: str
    start_ms: int
    end_ms: int
    confidence: float
    is_final: bool


@dataclass(frozen=True, slots=True)
class Turn:
    """A `Turn` event, partial or final (ARCHITECTURE.md §2).

    Partials matter as much as finals: the confidence trajectory across partials
    is a first-class controller input.
    """

    turn_order: int
    end_of_turn: bool
    end_of_turn_confidence: float | None
    transcript: str
    words: tuple[Word, ...]


@dataclass(frozen=True, slots=True)
class SessionBegin:
    """A `Begin` event: session id and expiry (ARCHITECTURE.md §2)."""

    session_id: str
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class Termination:
    """A `Termination` event (ARCHITECTURE.md §2)."""

    session_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Cut:
    """A premature cutoff, the unsupervised label of CONTROL_SPEC.md §2.5."""

    turn_order: int
    resume_gap_ms: int
    confidence: float


@dataclass(frozen=True, slots=True)
class WindowHint:
    """One compiled row of the context policy (CONTROL_SPEC.md §3).

    Applies for exactly one turn, then is released. A hint never persists into
    the speaker profile.
    """

    min_mult: float
    max_mult: float
    conf_delta: float


@dataclass(frozen=True, slots=True)
class SpeakerFeatures:
    """The speaker axis at one instant (CONTROL_SPEC.md §2).

    `cold` is true until `n_gaps` reaches the warm threshold, in which case the
    speaker axis is skipped entirely (CONTROL_SPEC.md §4).
    """

    n_gaps: int
    g_p50_ms: float
    g_p90_ms: float
    speech_rate: float
    disfluency: float
    jitter: float
    recent_cuts: float
    cold: bool


@dataclass(frozen=True, slots=True)
class TurnConfig:
    """The four turn-detection knobs of CONTROL_SPEC.md §0."""

    min_turn_silence_ms: int
    max_turn_silence_ms: int
    end_of_turn_confidence_threshold: float
    vad_threshold: float | None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What the chosen model actually exposes (CONTROL_SPEC.md §7).

    `capabilities.probe` fills this in; nothing else may assume a knob exists.
    """

    updatable_fields: frozenset[str]
    has_end_of_turn_confidence: bool
    supports_force_endpoint: bool
    has_word_timings: bool


@dataclass(frozen=True, slots=True)
class ConfigDecision:
    """Why a config change was made (INV-4).

    No `UpdateConfiguration` is sent without one of these. The dashboard and the
    trace both render it.
    """

    rule_id: str
    trigger: str
    inputs: tuple[tuple[str, float], ...]
    old: TurnConfig
    new: TurnConfig
    state: ControllerState


@dataclass(frozen=True, slots=True)
class ConfigPatch:
    """A config change on its way to the live socket.

    `decision` is required and non-optional: INV-4 is a type error to omit rather
    than a convention to remember. `inputs` and `changed` are tuples rather than
    mappings so that `Arbiter.decide` allocates nothing beyond this object
    (INV-2).
    """

    changed: tuple[str, ...]
    config: TurnConfig
    decision: ConfigDecision
    t_ms: int


@dataclass(frozen=True, slots=True)
class Voice:
    """A resolved TTS voice (ARCHITECTURE.md §8).

    `pacing_hint_ms` feeds the arbiter ceiling so that switching voice does not
    silently change perceived responsiveness (PRD.md F-14).
    """

    provider: str
    voice_id: str
    latency_class: str
    cost_class: str
    sample_rate: int
    pacing_hint_ms: int
