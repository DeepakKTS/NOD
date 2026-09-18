"""Per-session rhythm statistics.

ARCHITECTURE.md §2: this module never touches config. It observes and reports;
the arbiter decides.

Every constant below is transcribed from CONTROL_SPEC.md §2 and ARCHITECTURE.md
§4. Changing one is an ADR, not a commit (CONTROL_SPEC.md §0).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from nod_core.types import Cut, SpeakerFeatures, Turn

GAP_CLAMP_MAX_MS: Final = 6000
"""Inter-word gaps are clamped to [0, this] before ingestion. Milliseconds.

Stops one pathological silence from poisoning the estimator (CONTROL_SPEC.md
§2.1, EC-14).
"""

MIN_GAPS_FOR_WARM: Final = 8
"""Gaps required before the quantiles may be consulted (CONTROL_SPEC.md §2.1)."""

GAP_RING_CAPACITY: Final = 256
"""`G` in the complexity table of ARCHITECTURE.md §4. Samples."""

SPEECH_RATE_ALPHA: Final = 0.2
"""EWMA weight for speech rate (CONTROL_SPEC.md §2.2)."""

DISFLUENCY_ALPHA: Final = 0.3
"""EWMA weight for disfluency density (CONTROL_SPEC.md §2.3)."""

DURATION_OUTLIER_MULT: Final = 2.5
"""A word longer than this times its median-for-length is a duration outlier."""

JITTER_SCALE: Final = 0.04
"""Normalising constant for confidence jitter (CONTROL_SPEC.md §2.4).

The feature is computed and logged but carries weight 0 in the law (ADR-011).
"""

JITTER_ALPHA: Final = 0.25
"""EWMA weight for jitter across turns (CONTROL_SPEC.md §2.4)."""

RESUME_MS: Final = 1200
"""Cut condition 2: the caller resumed within this. Milliseconds."""

AGENT_GRACE_MS: Final = 300
"""Cut condition 3: agent audio below this does not disqualify a cut. Milliseconds."""

# Cut condition 5 was dropped (CONTROL_SPEC.md §2.5, ADR-011). It admitted a
# candidate cut when the ended turn's confidence was below 0.85; the 69-session P1
# matrix measured 184 real boundaries and 144 of them (78 %) fall below that,
# median 0.443, so it admitted four turns in five and discriminated nothing.
# Conditions 1 to 4 carry the label. (These supersede the 67-of-75 at median 0.352
# quoted here before the clean matrix; the sample is larger and spans both regimes,
# and the conclusion is unchanged.)

CUT_WINDOW: Final = 5
"""Cuts are counted over this many recent turns (CONTROL_SPEC.md §2.5)."""

CUT_NORMALISER: Final = 3.0
"""`recent_cuts` is the window count divided by this, clamped to [0, 1]."""

FILLER_TOKENS: Final = frozenset(
    {"um", "uh", "er", "like", "you know", "hmm"},
)
"""Filler set for the disfluency feature. Configurable per locale (EC-19)."""

AFFIRMATION_TOKENS: Final = frozenset({"yes", "no", "correct", "wait", "sorry"})
"""Cut condition 4: these open a genuine new turn, not a resumption."""

DURATION_MEDIAN_BY_LENGTH_MS: Final = (
    (2, 180),
    (4, 260),
    (6, 340),
    (9, 430),
    (99, 540),
)
"""Five-bucket median word duration by character length (CONTROL_SPEC.md §2.3).

Each pair is (inclusive upper bound on character length, median milliseconds).
"""


class P2Quantile:
    """Streaming quantile estimator, five markers of state (ADR-002)."""

    def __init__(self, q: float) -> None:
        """Initialise an estimator for quantile `q`.

        Args:
            q: The quantile to track, in (0, 1).
        """
        raise NotImplementedError

    def update(self, x: float) -> None:
        """Ingest one sample. `O(1)` time, `O(1)` space.

        Args:
            x: The sample.
        """
        raise NotImplementedError

    @property
    def value(self) -> float:
        """The current estimate. `O(1)`."""
        raise NotImplementedError


class ExactQuantile:
    """Exact quantile over a fixed-capacity ring, behind `NOD_EXACT_QUANTILES`.

    Exists to validate P² drift in the bench, which asserts the two agree within
    5 % on the corpora (ADR-002). Slower; never the production path.
    """

    def __init__(self, q: float, capacity: int = GAP_RING_CAPACITY) -> None:
        """Initialise an exact estimator.

        Args:
            q: The quantile to track, in (0, 1).
            capacity: Ring capacity in samples. Fixed, so memory stays constant.
        """
        raise NotImplementedError

    def update(self, x: float) -> None:
        """Ingest one sample into the ring. `O(1)` amortised.

        Args:
            x: The sample.
        """
        raise NotImplementedError

    @property
    def value(self) -> float:
        """The exact quantile over the ring. `O(G log G)`."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ProfilerState:
    """A profiler's full state, small enough to carry across a rotation (EC-03)."""

    n_gaps: int
    g_p50_ms: float
    g_p90_ms: float
    speech_rate: float
    disfluency: float
    jitter: float
    recent_cuts: float
    last_turn_order: int


class Profiler:
    """Speaker-axis features, updated incrementally.

    Memory is constant in call length (INV-3): the only accumulators are the two
    quantile estimators and a handful of scalars.
    """

    def __init__(self, *, exact_quantiles: bool = False) -> None:
        """Initialise a profiler for one session.

        Args:
            exact_quantiles: Use `ExactQuantile` instead of `P2Quantile`. For
                validation only; slower (ADR-002).
        """
        raise NotImplementedError

    def observe_turn(self, turn: Turn, *, agent_audio_ms: int = 0) -> Cut | None:
        """Ingest one partial or final `Turn`. `O(ΔW)` time, `O(1)` space.

        Only new words are examined; `word_is_final` marks the boundary already
        processed. Unfinalised trailing words are skipped (EC-21), empty turns
        are ignored (EC-16), and a `turn_order` at or below the last seen is
        ignored (EC-07).

        Args:
            turn: The event to ingest.
            agent_audio_ms: Milliseconds of agent audio produced for the previous
                turn, needed by cut condition 3 (CONTROL_SPEC.md §2.5).

        Returns:
            The `Cut` if this turn completed one, otherwise `None`.
        """
        raise NotImplementedError

    def features(self) -> SpeakerFeatures:
        """Return the current speaker axis. `O(1)`.

        Returns:
            Features with `cold` set until `n_gaps >= MIN_GAPS_FOR_WARM`.
        """
        raise NotImplementedError

    def snapshot(self) -> ProfilerState:
        """Capture state for a proactive session rotation. `O(1)`. EC-03.

        Returns:
            The state to carry onto the new socket.
        """
        raise NotImplementedError

    @classmethod
    def restore(
        cls, state: ProfilerState, *, exact_quantiles: bool = False
    ) -> Profiler:
        """Rebuild a profiler from a snapshot. `O(1)`. EC-03.

        Args:
            state: A snapshot from `snapshot`.
            exact_quantiles: As for `__init__`.

        Returns:
            A profiler continuing from `state`.
        """
        raise NotImplementedError
