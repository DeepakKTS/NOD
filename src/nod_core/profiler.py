"""Per-session rhythm statistics.

ARCHITECTURE.md §2: this module never touches config. It observes and reports;
the arbiter decides.

Every constant below is transcribed from CONTROL_SPEC.md §2 and ARCHITECTURE.md
§4. Changing one is an ADR, not a commit (CONTROL_SPEC.md §0).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Final

from nod_core.types import Cut, SpeakerFeatures, Turn, Word

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

P2_MARKERS: Final = 5
"""Markers of state per estimator (ADR-002). The algorithm is defined on five."""

_MULTI_WORD_FILLERS: Final = tuple(
    tuple(phrase.split()) for phrase in FILLER_TOKENS if " " in phrase
)
"""Filler entries that span more than one token, as bigrams.

`FILLER_TOKENS` carries `you know`, which no single-token comparison can ever
match. Derived from the set rather than listed again so that adding a locale
phrase with a space in it does not silently become dead configuration
(CONTROL_SPEC.md §2.3, EC-19).
"""

_MAX_FILLER_WORDS: Final = max((len(words) for words in _MULTI_WORD_FILLERS), default=1)
"""Longest filler phrase in tokens. Bounds the token ring, so memory stays `O(1)`."""


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp `value` into `[low, high]`. `O(1)`.

    Args:
        value: The value to clamp.
        low: Lower bound, inclusive.
        high: Upper bound, inclusive.

    Returns:
        The clamped value.
    """
    return min(max(value, low), high)


# B005/RUF001: a per-character strip set is exactly what is wanted here, and the
# en and em dashes belong in it — `universal-3-5-pro` formats digit groups with
# them, which ADR-015 had to widen the redaction rules for, and a token arriving
# as `617—` must normalise to `617` for the repeat and filler tests to see it.
_TOKEN_PUNCTUATION: Final = ".,!?;:\"'()[]…-–— "  # noqa: RUF001, S105
"""Characters stripped from a token's edges before the §2.3 token features."""


def _normalise(token: str) -> str:
    """Lower-case a token and strip surrounding punctuation. `O(len(token))`.

    Used for the two token features of CONTROL_SPEC.md §2.3, adjacent repeats and
    filler-set membership. Only the edges are stripped: an internal apostrophe is
    part of the word, and removing it would fold `we'll` onto `well`.

    Args:
        token: The raw word text.

    Returns:
        The normalised token, possibly empty.
    """
    return token.strip(_TOKEN_PUNCTUATION).lower()


def _median_duration_ms(characters: int) -> int:
    """The five-bucket median word duration for a length. `O(1)`, five buckets.

    Args:
        characters: Length of the word's text in characters.

    Returns:
        The median duration in milliseconds for that length bucket.
    """
    for upper, median_ms in DURATION_MEDIAN_BY_LENGTH_MS:
        if characters <= upper:
            return median_ms
    return DURATION_MEDIAN_BY_LENGTH_MS[-1][1]


def _nearest_rank(ordered: list[float], q: float) -> float:
    """The `q`-quantile of a sorted list by nearest-rank, inclusive. `O(1)`.

    `p_q = ordered[ceil(q * n) - 1]`, the definition
    `nod_bench.metrics.QUANTILE_METHOD` states and every published latency figure
    in this project uses — not linear interpolation, which returns a value no
    sample ever took and flatters the tail (ADR-018).

    Reimplemented here rather than imported: `nod_core` may not import
    `nod_bench` (ARCHITECTURE.md §2, asserted by `tests/unit/test_boundaries.py`),
    so the two copies cannot be reduced to one. They are two constants agreeing by
    meaning rather than by code, which CLAUDE.md §5 says to *assert* rather than
    leave to prose — `test_exact_quantile_matches_the_published_definition` is that
    assertion, and it is the only thing standing between these two and a silent
    divergence.

    Args:
        ordered: Samples in ascending order. Must be non-empty.
        q: Quantile in `(0, 1)`.

    Returns:
        An observed sample, never an interpolated value.
    """
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


class P2Quantile:
    """Streaming quantile estimator, five markers of state (ADR-002).

    Jain and Chlamtac's P² algorithm: five markers track the minimum, the
    `q/2`, `q` and `(1+q)/2` quantiles and the maximum, and each new sample nudges
    the interior markers towards their desired positions by a parabolic
    prediction, falling back to linear when the parabola would step outside its
    neighbours. `O(1)` time and `O(1)` space per sample, which is what INV-3 needs
    and what a per-word update inside a 5 ms budget needs (INV-2).

    Below `P2_MARKERS` samples the algorithm is undefined — it has no markers to
    move — so the first five are buffered and `value` reports the exact nearest-
    rank quantile over them. That is not a fallback for correctness but the honest
    answer: with four samples in hand there is nothing to approximate.
    CONTROL_SPEC.md §2.1 requires `n_gaps >= 8` before either quantile is
    consulted, so the production path never reads an estimate from this regime.
    """

    __slots__ = ("_desired", "_increment", "_positions", "_q", "_seen", "_values")

    def __init__(self, q: float) -> None:
        """Initialise an estimator for quantile `q`.

        Args:
            q: The quantile to track, in (0, 1).

        Raises:
            ValueError: `q` is outside `(0, 1)`.
        """
        if not 0.0 < q < 1.0:
            msg = f"q={q} is outside (0, 1); a quantile of 0 or 1 is min or max"
            raise ValueError(msg)
        self._q = q
        self._seen = 0
        self._values: list[float] = []
        self._positions: list[float] = []
        self._desired: list[float] = []
        self._increment: tuple[float, ...] = (
            0.0,
            q / 2.0,
            q,
            (1.0 + q) / 2.0,
            1.0,
        )

    def update(self, x: float) -> None:
        """Ingest one sample. `O(1)` time, `O(1)` space.

        Args:
            x: The sample.
        """
        self._seen += 1
        if self._seen <= P2_MARKERS:
            self._values.append(x)
            self._values.sort()
            if self._seen == P2_MARKERS:
                self._positions = [1.0, 2.0, 3.0, 4.0, 5.0]
                self._desired = [
                    1.0 + increment * (P2_MARKERS - 1) for increment in self._increment
                ]
            return

        cell = self._locate(x)
        for index in range(cell + 1, P2_MARKERS):
            self._positions[index] += 1.0
        for index in range(P2_MARKERS):
            self._desired[index] += self._increment[index]
        self._adjust()

    def _locate(self, x: float) -> int:
        """Return the cell `x` falls in, extending the extremes. `O(1)`, five cells.

        Args:
            x: The sample.

        Returns:
            The index `k` with `values[k] <= x < values[k + 1]`, in `0..3`.
        """
        if x < self._values[0]:
            self._values[0] = x
            return 0
        if x >= self._values[P2_MARKERS - 1]:
            self._values[P2_MARKERS - 1] = x
            return P2_MARKERS - 2
        for index in range(P2_MARKERS - 1):
            if self._values[index] <= x < self._values[index + 1]:
                return index
        # Unreachable: the two guards above cover everything outside the interior
        # cells, and the loop covers the interior. Kept as a loud failure rather
        # than a silent `return 0`, which would corrupt the estimate quietly.
        msg = f"x={x} fell in no cell of {self._values}"  # pragma: no cover
        raise AssertionError(msg)  # pragma: no cover

    def _adjust(self) -> None:
        """Move each interior marker at most one position. `O(1)`, three markers."""
        for index in range(1, P2_MARKERS - 1):
            offset = self._desired[index] - self._positions[index]
            gap_above = self._positions[index + 1] - self._positions[index]
            gap_below = self._positions[index - 1] - self._positions[index]
            moves_up = offset >= 1.0 and gap_above > 1.0
            moves_down = offset <= -1.0 and gap_below < -1.0
            if not (moves_up or moves_down):
                continue
            step = 1.0 if offset > 0.0 else -1.0
            candidate = self._parabolic(index, step)
            if not self._values[index - 1] < candidate < self._values[index + 1]:
                candidate = self._linear(index, step)
            self._values[index] = candidate
            self._positions[index] += step

    def _parabolic(self, index: int, step: float) -> float:
        """The parabolic prediction for one marker. `O(1)`.

        Args:
            index: Interior marker index, `1..3`.
            step: `+1.0` or `-1.0`.

        Returns:
            The predicted marker value.
        """
        values, positions = self._values, self._positions
        span = positions[index + 1] - positions[index - 1]
        above = positions[index + 1] - positions[index]
        below = positions[index] - positions[index - 1]
        return values[index] + step / span * (
            (below + step) * (values[index + 1] - values[index]) / above
            + (above - step) * (values[index] - values[index - 1]) / below
        )

    def _linear(self, index: int, step: float) -> float:
        """The linear fallback when the parabola leaves the neighbours. `O(1)`.

        Args:
            index: Interior marker index, `1..3`.
            step: `+1.0` or `-1.0`.

        Returns:
            The predicted marker value.
        """
        neighbour = index + int(step)
        values, positions = self._values, self._positions
        return values[index] + step * (values[neighbour] - values[index]) / (
            positions[neighbour] - positions[index]
        )

    @property
    def value(self) -> float:
        """The current estimate. `O(1)` warm, `O(1)` over at most five samples cold.

        Returns:
            The estimated `q`-quantile.

        Raises:
            ValueError: No sample has been ingested. A quantile of nothing is not
                `0.0`, and returning one would put a fabricated gap into
                `g_p50_ms` where nothing would ever contradict it.
        """
        if self._seen == 0:
            msg = "no samples: a quantile of nothing is not 0.0"
            raise ValueError(msg)
        if self._seen < P2_MARKERS:
            return _nearest_rank(self._values, self._q)
        return self._values[P2_MARKERS // 2]

    @property
    def seen(self) -> int:
        """Samples ingested. `O(1)`."""
        return self._seen

    @classmethod
    def seeded(cls, q: float, value: float, *, seen: int) -> P2Quantile:
        """Build an estimator whose `value` is exactly `value`. `O(1)`. EC-03.

        All five markers are placed at `value`, which is the state a stream of
        identical samples would have produced. `value` therefore returns `value`
        exactly, and subsequent updates proceed normally: with the markers tied,
        the parabolic prediction is rejected by its own strict bounds check and the
        linear fallback is a no-op, so the estimator sits still until a sample
        outside the range arrives and then spreads from there.

        **What this does not restore is the marker geometry.** `ProfilerState`
        carries eight scalars, not two arrays of five, so a rotation preserves the
        *reported* quantiles and not the estimator's internal shape. Post-rotation
        trajectories therefore differ from an unrotated session's. That is EC-03's
        accepted cost: the alternative changes the shape of a frozen dataclass that
        crosses a module boundary, and EC-03 asks for state small enough to carry.

        Args:
            q: The quantile to track, in (0, 1).
            value: The estimate to resume from.
            seen: Sample count to resume from, so `cold` survives the rotation.

        Returns:
            The seeded estimator.
        """
        estimator = cls(q)
        estimator._seen = max(seen, P2_MARKERS)
        estimator._values = [value] * P2_MARKERS
        estimator._positions = [1.0, 2.0, 3.0, 4.0, 5.0]
        estimator._desired = [
            1.0 + increment * (P2_MARKERS - 1) for increment in estimator._increment
        ]
        return estimator


class ExactQuantile:
    """Exact quantile over a fixed-capacity ring, behind `NOD_EXACT_QUANTILES`.

    Exists to validate P² drift in the bench, which asserts the two agree within
    5 % on the corpora (ADR-002). Slower; never the production path.

    The ring is what keeps this inside INV-3: it holds the last
    `GAP_RING_CAPACITY` samples and no more, so a four-hour call and a
    forty-second call use the same memory. It also means this is only exact *over
    the window*, which is the honest description — on a stream longer than the
    ring it is an exact quantile of a suffix, not of the stream.
    """

    __slots__ = ("_capacity", "_q", "_ring", "_seen")

    def __init__(self, q: float, capacity: int = GAP_RING_CAPACITY) -> None:
        """Initialise an exact estimator.

        Args:
            q: The quantile to track, in (0, 1).
            capacity: Ring capacity in samples. Fixed, so memory stays constant.

        Raises:
            ValueError: `q` is outside `(0, 1)`, or `capacity` is not positive.
        """
        if not 0.0 < q < 1.0:
            msg = f"q={q} is outside (0, 1); a quantile of 0 or 1 is min or max"
            raise ValueError(msg)
        if capacity < 1:
            msg = f"capacity={capacity} must be at least 1"
            raise ValueError(msg)
        self._q = q
        self._capacity = capacity
        self._ring: deque[float] = deque(maxlen=capacity)
        self._seen = 0

    def update(self, x: float) -> None:
        """Ingest one sample into the ring. `O(1)` amortised.

        Args:
            x: The sample.
        """
        self._seen += 1
        self._ring.append(x)

    @property
    def value(self) -> float:
        """The exact quantile over the ring. `O(G log G)`.

        Returns:
            The nearest-rank quantile of the retained window.

        Raises:
            ValueError: No sample has been ingested.
        """
        if not self._ring:
            msg = "no samples: a quantile of nothing is not 0.0"
            raise ValueError(msg)
        return _nearest_rank(sorted(self._ring), self._q)

    @property
    def seen(self) -> int:
        """Samples ingested, including those the ring has since evicted. `O(1)`."""
        return self._seen

    @classmethod
    def seeded(cls, q: float, value: float, *, seen: int) -> ExactQuantile:
        """Build an estimator whose `value` is exactly `value`. `O(1)`. EC-03.

        Args:
            q: The quantile to track, in (0, 1).
            value: The estimate to resume from.
            seen: Sample count to resume from.

        Returns:
            The seeded estimator, holding one sample.
        """
        estimator = cls(q)
        estimator._ring.append(value)
        estimator._seen = max(seen, 1)
        return estimator


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

    Nothing here does I/O and nothing here reads a clock. Every timestamp is a
    stream-relative millisecond from AssemblyAI word timings (CLAUDE.md §6,
    EC-08), and the two counters EC-07 asks to be logged are exposed as
    properties for the proxy to log rather than logged from inside the ingest
    path (ARCHITECTURE.md §2: this module observes and reports).
    """

    __slots__ = (
        "_cut_ring",
        "_disfluency",
        "_disfluency_seen",
        "_exact",
        "_g_p50",
        "_g_p90",
        "_ignored_empty",
        "_jitter",
        "_jitter_seen",
        "_last_turn_order",
        "_n_gaps",
        "_out_of_order",
        "_prev_turn_confidence",
        "_prev_turn_ended",
        "_prev_turn_last_end_ms",
        "_prev_turn_order",
        "_speech_rate",
        "_speech_rate_seen",
        "_turn",
    )

    def __init__(self, *, exact_quantiles: bool = False) -> None:
        """Initialise a profiler for one session.

        Args:
            exact_quantiles: Use `ExactQuantile` instead of `P2Quantile`. For
                validation only; slower (ADR-002).
        """
        self._exact = exact_quantiles
        self._g_p50 = self._new_estimator(0.50)
        self._g_p90 = self._new_estimator(0.90)
        self._n_gaps = 0
        self._speech_rate = 0.0
        self._speech_rate_seen = False
        self._disfluency = 0.0
        self._disfluency_seen = False
        self._jitter = 0.0
        self._jitter_seen = False
        self._cut_ring: deque[bool] = deque(maxlen=CUT_WINDOW)
        self._last_turn_order = 0
        self._out_of_order = 0
        self._ignored_empty = 0
        self._prev_turn_order = 0
        self._prev_turn_ended = False
        self._prev_turn_confidence = 0.0
        self._prev_turn_last_end_ms: int | None = None
        self._turn = _TurnAccumulator()

    def _new_estimator(self, q: float) -> P2Quantile | ExactQuantile:
        """Build the estimator this profiler is configured for. `O(1)`.

        Args:
            q: The quantile to track.

        Returns:
            A `P2Quantile`, or an `ExactQuantile` under `NOD_EXACT_QUANTILES`.
        """
        return ExactQuantile(q) if self._exact else P2Quantile(q)

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
        # EC-16 first, and deliberately before EC-07. An empty turn is "excluded
        # from profiling entirely", so it must not advance `last_turn_order`
        # either — otherwise a noise-only turn would silently raise the bar and
        # make the next real turn look out of order.
        if not turn.transcript.strip() or not turn.words:
            self._ignored_empty += 1
            return None

        # EC-07, and its `<=` needs one qualification or it eats every partial.
        #
        # EC-07 says "ignore any turn with `turn_order <= last_seen`", while
        # ARCHITECTURE §2 says "partials matter as much as finals" — and every
        # partial of the current turn arrives carrying the turn order already
        # seen. Read literally, EC-07 drops all but the first partial of every
        # turn and the profiler ingests one word per turn. The two lines are each
        # coherent alone; this is ADR-016's pattern, found by building the
        # consumer, and it is recorded as its seventh instance.
        #
        # The reading that satisfies both: a turn order *below* the current turn
        # is an out-of-order or duplicated turn and is dropped, while one *equal*
        # to it is a continuation and is ingested. EC-07's protective purpose
        # survives because a literal re-send of the current turn is idempotent
        # anyway — `_ingest_words` resumes from `words_done`, so already-counted
        # words cannot be counted twice, which
        # `test_words_already_ingested_are_never_counted_twice` pins.
        #
        # `_last_turn_order` rather than the accumulator is the floor, so that a
        # profiler restored across a rotation still rejects turns the old socket
        # had already finished with (EC-03): the fresh accumulator's `words_done`
        # is 0 and would otherwise re-ingest all of them.
        if (
            turn.turn_order <= self._last_turn_order
            and turn.turn_order != self._turn.turn_order
        ):
            self._out_of_order += 1
            return None

        cut: Cut | None = None
        if turn.turn_order != self._turn.turn_order:
            # A genuinely new turn. Close the previous one and test for a cut
            # before resetting, because cut detection compares across the
            # boundary (CONTROL_SPEC.md §2.5 conditions 1 and 2).
            cut = self._close_turn(turn, agent_audio_ms=agent_audio_ms)
            self._turn = _TurnAccumulator(turn_order=turn.turn_order)

        self._ingest_words(turn.words)
        self._ingest_confidence(turn.end_of_turn_confidence)
        self._last_turn_order = max(self._last_turn_order, turn.turn_order)

        if turn.end_of_turn:
            self._finalise_turn(turn)
        return cut

    def _ingest_words(self, words: tuple[Word, ...]) -> None:
        """Fold the words this event newly finalised. `O(ΔW)`.

        Walks forward from the last finalised index and stops at the first
        unfinalised word. EC-21: a word that is not final has no trustworthy
        `end_ms`, so no gap is computed against it and it is reconsidered on the
        next event rather than skipped permanently.

        Args:
            words: The event's full word array.
        """
        index = self._turn.words_done
        while index < len(words) and words[index].is_final:
            word = words[index]
            if self._turn.last_end_ms is not None:
                gap = _clamp(
                    float(word.start_ms - self._turn.last_end_ms),
                    0.0,
                    float(GAP_CLAMP_MAX_MS),
                )
                self._g_p50.update(gap)
                self._g_p90.update(gap)
                self._n_gaps += 1
            self._turn.last_end_ms = word.end_ms
            self._turn.words += 1
            self._turn.voiced_ms += max(0, word.end_ms - word.start_ms)
            if self._turn.first_start_ms is None:
                self._turn.first_start_ms = word.start_ms
            if self._turn.first_token is None:
                self._turn.first_token = _normalise(word.text)
            self._count_disfluency(word)
            index += 1
        self._turn.words_done = index

    def _count_disfluency(self, word: Word) -> None:
        """Accumulate the three §2.3 disfluency counts for one word. `O(1)`.

        Args:
            word: A newly finalised word.
        """
        token = _normalise(word.text)
        ring = self._turn.tokens
        if (token and ring and token == ring[-1]) or token in FILLER_TOKENS:
            self._turn.disfluent += 1
        elif _MULTI_WORD_FILLERS and ring:
            # `you know` is in the filler set and no unigram test can see it.
            phrase = (ring[-1], token)
            if phrase in _MULTI_WORD_FILLERS:
                self._turn.disfluent += 1
        duration = word.end_ms - word.start_ms
        if duration > DURATION_OUTLIER_MULT * _median_duration_ms(len(word.text)):
            self._turn.disfluent += 1
        ring.append(token)

    def _ingest_confidence(self, confidence: float | None) -> None:
        """Welford update over the partial sequence. `O(1)`. §2.4, weight 0.

        Args:
            confidence: This event's `end_of_turn_confidence`, or `None`.
        """
        if confidence is None:
            return
        self._turn.conf_n += 1
        delta = confidence - self._turn.conf_mean
        self._turn.conf_mean += delta / self._turn.conf_n
        self._turn.conf_m2 += delta * (confidence - self._turn.conf_mean)

    def _finalise_turn(self, turn: Turn) -> None:
        """Fold this turn's per-turn features into their EWMAs. `O(1)`.

        Args:
            turn: The event carrying `end_of_turn`.
        """
        accumulated = self._turn
        if accumulated.voiced_ms > 0 and accumulated.words > 0:
            rate = accumulated.words / accumulated.voiced_ms * 1000.0
            self._speech_rate = self._ewma(
                self._speech_rate, rate, SPEECH_RATE_ALPHA, seen=self._speech_rate_seen
            )
            self._speech_rate_seen = True
        if accumulated.words > 0:
            density = _clamp(
                accumulated.disfluent / max(accumulated.words, 1), 0.0, 1.0
            )
            self._disfluency = self._ewma(
                self._disfluency, density, DISFLUENCY_ALPHA, seen=self._disfluency_seen
            )
            self._disfluency_seen = True
        if accumulated.conf_n > 1:
            variance = accumulated.conf_m2 / (accumulated.conf_n - 1)
            self._jitter = self._ewma(
                self._jitter,
                _clamp(variance / JITTER_SCALE, 0.0, 1.0),
                JITTER_ALPHA,
                seen=self._jitter_seen,
            )
            self._jitter_seen = True
        self._prev_turn_order = turn.turn_order
        self._prev_turn_ended = True
        self._prev_turn_confidence = turn.end_of_turn_confidence or 0.0
        self._prev_turn_last_end_ms = accumulated.last_end_ms

    @staticmethod
    def _ewma(current: float, sample: float, alpha: float, *, seen: bool) -> float:
        """One EWMA step, seeded by its first sample. `O(1)`.

        The first sample *becomes* the average rather than being blended into a
        zero. Blending into zero would make every feature start wrong and decay
        towards right over several turns, which on a short call is most of it.

        Args:
            current: The running value.
            sample: The new observation.
            alpha: EWMA weight.
            seen: Whether any sample has been folded in yet.

        Returns:
            The updated running value.
        """
        return sample if not seen else (1.0 - alpha) * current + alpha * sample

    def _close_turn(self, turn: Turn, *, agent_audio_ms: int) -> Cut | None:
        """Apply cut conditions 1 to 4 across a turn boundary. `O(1)`. §2.5.

        Args:
            turn: The turn that has just started.
            agent_audio_ms: Agent audio produced for the previous turn.

        Returns:
            The `Cut` if the previous turn was cut short, otherwise `None`.
        """
        if self._turn.turn_order == 0:
            return None
        first_start = _first_final_start_ms(turn.words)
        previous_end = self._prev_turn_last_end_ms
        is_cut = (
            # 1. the previous turn ended
            self._prev_turn_ended
            # 2. the caller resumed inside RESUME_MS
            and previous_end is not None
            and first_start is not None
            and (first_start - previous_end) < RESUME_MS
            # 3. the agent had produced little or no audio for it
            and agent_audio_ms < AGENT_GRACE_MS
            # 4. the resumption is not an affirmation or new-topic marker
            and _normalise(turn.words[0].text) not in AFFIRMATION_TOKENS
        )
        self._cut_ring.append(is_cut)
        if not is_cut:
            return None
        # `turn_order` names the turn that was cut, not the one that resumed:
        # the label is a property of the boundary the controller got wrong.
        assert previous_end is not None and first_start is not None  # noqa: S101
        return Cut(
            turn_order=self._prev_turn_order,
            resume_gap_ms=first_start - previous_end,
            confidence=self._prev_turn_confidence,
        )

    def features(self) -> SpeakerFeatures:
        """Return the current speaker axis. `O(1)`.

        Returns:
            Features with `cold` set until `n_gaps >= MIN_GAPS_FOR_WARM`.

            With no gap ingested at all the two quantiles report `0.0`. That is a
            placeholder and not an estimate: `cold` is true, and CONTROL_SPEC.md
            §4 skips the speaker axis entirely when cold, which is what the flag
            is on this dataclass for. The estimators themselves raise rather than
            invent a value, so the placeholder cannot leak past this method.
        """
        has_gaps = self._n_gaps > 0
        return SpeakerFeatures(
            n_gaps=self._n_gaps,
            g_p50_ms=self._g_p50.value if has_gaps else 0.0,
            g_p90_ms=self._g_p90.value if has_gaps else 0.0,
            speech_rate=self._speech_rate,
            disfluency=self._disfluency,
            jitter=self._jitter,
            recent_cuts=self._recent_cuts(),
            cold=self._n_gaps < MIN_GAPS_FOR_WARM,
        )

    def _recent_cuts(self) -> float:
        """Cuts in the last `CUT_WINDOW` turns, normalised. `O(CUT_WINDOW)`. §2.5.

        Returns:
            `count / CUT_NORMALISER`, clamped to `[0, 1]`.
        """
        return _clamp(sum(self._cut_ring) / CUT_NORMALISER, 0.0, 1.0)

    def snapshot(self) -> ProfilerState:
        """Capture state for a proactive session rotation. `O(1)`. EC-03.

        Returns:
            The state to carry onto the new socket.
        """
        current = self.features()
        return ProfilerState(
            n_gaps=current.n_gaps,
            g_p50_ms=current.g_p50_ms,
            g_p90_ms=current.g_p90_ms,
            speech_rate=current.speech_rate,
            disfluency=current.disfluency,
            jitter=current.jitter,
            recent_cuts=current.recent_cuts,
            last_turn_order=self._last_turn_order,
        )

    @classmethod
    def restore(
        cls, state: ProfilerState, *, exact_quantiles: bool = False
    ) -> Profiler:
        """Rebuild a profiler from a snapshot. `O(1)`. EC-03.

        `features()` on the result equals `features()` on the profiler the
        snapshot came from — that is the contract EC-03 needs, since the caller
        sees nothing and the arbiter must not see the window jump on a rotation.

        What is *not* restored is the shape of each estimator's internal markers,
        nor which of the last `CUT_WINDOW` turns were cuts. `ProfilerState` is
        eight scalars by design, so both are reconstructed from their reported
        values: the estimators are seeded to return exactly what they reported,
        and the cut ring is refilled with `round(recent_cuts * CUT_NORMALISER)`
        flags. The cut reconstruction is exact on every reachable input — with
        `CUT_NORMALISER` at 3 and the result clamped, `recent_cuts` can only ever
        be one of 0, ⅓, ⅔ or 1 — and lossy only for a hand-built state that no
        profiler could have produced.

        Args:
            state: A snapshot from `snapshot`.
            exact_quantiles: As for `__init__`.

        Returns:
            A profiler continuing from `state`.
        """
        profiler = cls(exact_quantiles=exact_quantiles)
        if state.n_gaps > 0:
            factory = ExactQuantile.seeded if exact_quantiles else P2Quantile.seeded
            profiler._g_p50 = factory(0.50, state.g_p50_ms, seen=state.n_gaps)
            profiler._g_p90 = factory(0.90, state.g_p90_ms, seen=state.n_gaps)
        profiler._n_gaps = state.n_gaps
        profiler._speech_rate = state.speech_rate
        profiler._speech_rate_seen = True
        profiler._disfluency = state.disfluency
        profiler._disfluency_seen = True
        profiler._jitter = state.jitter
        profiler._jitter_seen = True
        profiler._last_turn_order = state.last_turn_order
        cuts = min(round(state.recent_cuts * CUT_NORMALISER), CUT_WINDOW)
        for _ in range(cuts):
            profiler._cut_ring.append(True)
        return profiler

    @property
    def out_of_order_turns(self) -> int:
        """Turns dropped by EC-07. `O(1)`.

        Exposed rather than logged: EC-07 asks for one log line per session, and
        `nod_core` does no I/O. The proxy reads this at session close and logs it.
        """
        return self._out_of_order

    @property
    def ignored_empty_turns(self) -> int:
        """Turns dropped by EC-16, including DTMF and hold music (EC-20). `O(1)`."""
        return self._ignored_empty


class _TurnAccumulator:
    """Per-turn scratch state. Reset on every new `turn_order`.

    Not a frozen dataclass: it is mutated on every word and never crosses a
    module boundary, so CLAUDE.md §6's `frozen=True, slots=True` rule does not
    reach it. Slotted anyway, because there is one per turn.
    """

    __slots__ = (
        "conf_m2",
        "conf_mean",
        "conf_n",
        "disfluent",
        "first_start_ms",
        "first_token",
        "last_end_ms",
        "tokens",
        "turn_order",
        "voiced_ms",
        "words",
        "words_done",
    )

    def __init__(self, turn_order: int = 0) -> None:
        """Start a fresh accumulator.

        Args:
            turn_order: The turn this accumulates, or 0 before any turn.
        """
        self.turn_order = turn_order
        self.words = 0
        self.words_done = 0
        self.voiced_ms = 0
        self.disfluent = 0
        self.last_end_ms: int | None = None
        self.first_start_ms: int | None = None
        self.first_token: str | None = None
        self.tokens: deque[str] = deque(maxlen=_MAX_FILLER_WORDS)
        self.conf_n = 0
        self.conf_mean = 0.0
        self.conf_m2 = 0.0


def _first_final_start_ms(words: tuple[Word, ...]) -> int | None:
    """The start of the first finalised word. `O(ΔW)` worst case, `O(1)` typical.

    Cut condition 2 measures from the resumption's first word. An unfinalised
    first word has no trustworthy timing (EC-21), so it is skipped rather than
    used.

    Args:
        words: A turn's word array.

    Returns:
        The start timestamp in stream-relative milliseconds, or `None`.
    """
    for word in words:
        if word.is_final:
            return word.start_ms
    return None
