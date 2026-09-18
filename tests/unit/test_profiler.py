"""`Profiler.observe_turn` and the edge cases CONTROL_SPEC §2 and EDGE_CASES name.

Every test here exercises a branch of the ingest path by id: EC-21 (unfinalised
trailing word), EC-16 and EC-20 (empty and non-speech turns), EC-07 (out-of-order
or duplicated `turn_order`), EC-14 (pathological silence), EC-03 (rotation) and
CONTROL_SPEC §2.5's four surviving cut conditions.

The quantile estimators have their own file, under `tests/property/`.
"""

from __future__ import annotations

import pytest

from nod_core.profiler import (
    AGENT_GRACE_MS,
    GAP_CLAMP_MAX_MS,
    MIN_GAPS_FOR_WARM,
    RESUME_MS,
    ExactQuantile,
    P2Quantile,
    Profiler,
    ProfilerState,
)
from nod_core.types import Cut, Turn, Word


def word(
    text: str = "hello",
    *,
    start: int,
    end: int,
    final: bool = True,
    confidence: float = 0.9,
) -> Word:
    """Build one `Word` with stream-relative timings."""
    return Word(
        text=text, start_ms=start, end_ms=end, confidence=confidence, is_final=final
    )


def turn(
    order: int,
    *words: Word,
    ended: bool = True,
    confidence: float | None = 0.4,
    transcript: str | None = None,
) -> Turn:
    """Build one `Turn`, defaulting the transcript to its own words."""
    return Turn(
        turn_order=order,
        end_of_turn=ended,
        end_of_turn_confidence=confidence,
        transcript=" ".join(w.text for w in words)
        if transcript is None
        else transcript,
        words=words,
    )


def _spaced(order: int, count: int, gap_ms: int) -> Turn:
    """A turn of `count` words separated by `gap_ms`, 100 ms of speech each."""
    words: list[Word] = []
    clock = 0
    for index in range(count):
        words.append(word(f"w{index}", start=clock, end=clock + 100))
        clock += 100 + gap_ms
    return turn(order, *words)


# --- gaps: CONTROL_SPEC §2.1 ------------------------------------------------


def test_gaps_are_measured_between_finalised_words_inside_one_turn() -> None:
    """§2.1: `g_i = words[i].start - words[i-1].end`, three words giving two gaps."""
    profiler = Profiler()
    profiler.observe_turn(
        turn(
            1,
            word("six", start=0, end=200),
            word("one", start=500, end=700),
            word("seven", start=1000, end=1200),
        )
    )
    features = profiler.features()
    assert features.n_gaps == 2
    # Both gaps are 300 ms, so every quantile of them is 300 ms.
    assert features.g_p50_ms == 300.0
    assert features.g_p90_ms == 300.0


def test_no_gap_is_measured_across_a_turn_boundary() -> None:
    """§2.1 says "within a turn", and the silence between turns is the agent's.

    A gap spanning the boundary would fold the agent's own speaking time into the
    caller's pause profile, which would widen `max_turn_silence` in proportion to
    how talkative the agent is.
    """
    profiler = Profiler()
    profiler.observe_turn(
        turn(1, word("yes", start=0, end=200), word("please", start=400, end=600))
    )
    profiler.observe_turn(
        turn(
            2, word("later", start=9000, end=9200), word("today", start=9400, end=9600)
        )
    )
    # Two turns, two words each: one gap per turn and nothing bridging them.
    assert profiler.features().n_gaps == 2
    assert profiler.features().g_p90_ms == 200.0


def test_a_pathological_silence_is_clamped_before_ingestion() -> None:
    """EC-14: a gap past `GAP_CLAMP_MAX_MS` enters the estimator at the clamp."""
    profiler = Profiler()
    profiler.observe_turn(
        turn(
            1,
            word("hello", start=0, end=200),
            word("there", start=60_000, end=60_200),
        )
    )
    assert profiler.features().g_p90_ms == float(GAP_CLAMP_MAX_MS)


def test_the_profiler_is_cold_until_the_eighth_gap() -> None:
    """§2.1: `n_gaps >= 8` before either quantile may be consulted."""
    profiler = Profiler()
    profiler.observe_turn(_spaced(1, MIN_GAPS_FOR_WARM, 250))
    assert profiler.features().n_gaps == MIN_GAPS_FOR_WARM - 1
    assert profiler.features().cold is True
    profiler.observe_turn(_spaced(2, 2, 250))
    assert profiler.features().n_gaps == MIN_GAPS_FOR_WARM
    assert profiler.features().cold is False


def test_a_profiler_with_no_gaps_reports_placeholders_and_stays_cold() -> None:
    """`features()` must return a record even before the first gap exists.

    The two quantiles read 0.0, which is a placeholder and not a measurement —
    `cold` is what tells §4 to skip the speaker axis. The estimators themselves
    raise rather than inventing a value, so the placeholder cannot escape
    `features()`; this test pins that the flag and the placeholder travel together.
    """
    features = Profiler().features()
    assert features.n_gaps == 0
    assert features.cold is True
    assert features.g_p50_ms == 0.0
    assert features.g_p90_ms == 0.0


# --- EC-21: the unfinalised trailing word -----------------------------------


def test_an_unfinalised_trailing_word_contributes_no_gap_yet() -> None:
    """EC-21: no gap is computed against a word whose `end_ms` is not trustworthy."""
    profiler = Profiler()
    profiler.observe_turn(
        turn(
            1,
            word("six", start=0, end=200),
            word("one", start=500, end=700, final=False),
            ended=False,
        )
    )
    assert profiler.features().n_gaps == 0


def test_a_word_finalised_in_a_later_partial_is_picked_up_then() -> None:
    """EC-21 again: skipped is not dropped. The word is reconsidered next event.

    This is the property that a naive "advance the index past everything we have
    seen" implementation gets wrong: it would skip the word permanently and lose
    one gap per turn, which on short turns is most of them.
    """
    profiler = Profiler()
    profiler.observe_turn(
        turn(
            1,
            word("six", start=0, end=200),
            word("one", start=500, end=700, final=False),
            ended=False,
        )
    )
    assert profiler.features().n_gaps == 0
    profiler.observe_turn(
        turn(
            1,
            word("six", start=0, end=200),
            word("one", start=500, end=700),
            ended=False,
        )
    )
    assert profiler.features().n_gaps == 1
    assert profiler.features().g_p50_ms == 300.0


def test_words_already_ingested_are_never_counted_twice() -> None:
    """ARCHITECTURE §4: `O(ΔW)` per partial, only new words examined.

    A growing partial replays every word it has already sent. Re-ingesting them
    would double-count gaps into the quantiles and inflate `n_gaps`, warming the
    profiler on a single turn.
    """
    profiler = Profiler()
    words = [
        word("a", start=0, end=100),
        word("b", start=300, end=400),
        word("c", start=600, end=700),
    ]
    for size in (1, 2, 3, 3, 3):
        profiler.observe_turn(turn(1, *words[:size], ended=False))
    assert profiler.features().n_gaps == 2


# --- EC-16 and EC-20: turns that carry no speech ----------------------------


@pytest.mark.parametrize(
    "empty",
    [
        Turn(
            turn_order=1,
            end_of_turn=True,
            end_of_turn_confidence=0.4,
            transcript="",
            words=(),
        ),
        Turn(
            turn_order=1,
            end_of_turn=True,
            end_of_turn_confidence=0.4,
            transcript="   ",
            words=(
                Word(text="x", start_ms=0, end_ms=10, confidence=0.1, is_final=True),
            ),
        ),
    ],
    ids=["no-transcript-no-words", "whitespace-transcript"],
)
def test_an_empty_turn_is_excluded_from_profiling_entirely(empty: Turn) -> None:
    """EC-16, and EC-20 routes DTMF and hold music here."""
    profiler = Profiler()
    assert profiler.observe_turn(empty) is None
    assert profiler.features().n_gaps == 0
    assert profiler.ignored_empty_turns == 1


def test_an_empty_turn_does_not_advance_the_turn_order_bar() -> None:
    """EC-16 before EC-07, and the order matters.

    "Excluded from profiling entirely" has to include the out-of-order bookkeeping.
    If a noise-only turn 5 raised `last_turn_order` to 5, the real turn 5 arriving
    next would be dropped by EC-07 as a duplicate — a silent loss caused by hold
    music.
    """
    profiler = Profiler()
    profiler.observe_turn(
        Turn(
            turn_order=5,
            end_of_turn=True,
            end_of_turn_confidence=0.4,
            transcript="",
            words=(),
        )
    )
    profiler.observe_turn(
        turn(5, word("six", start=0, end=200), word("one", start=500, end=700))
    )
    assert profiler.features().n_gaps == 1
    assert profiler.out_of_order_turns == 0


# --- EC-07: out-of-order and duplicated turns -------------------------------


def test_a_replayed_turn_order_is_ignored() -> None:
    """EC-07: ignore any turn with `turn_order <= last_seen`."""
    profiler = Profiler()
    profiler.observe_turn(
        turn(1, word("a", start=0, end=100), word("b", start=300, end=400))
    )
    profiler.observe_turn(
        turn(2, word("c", start=900, end=1000), word("d", start=1200, end=1300))
    )
    before = profiler.features().n_gaps
    assert (
        profiler.observe_turn(
            turn(1, word("a", start=0, end=100), word("b", start=300, end=400))
        )
        is None
    )
    assert profiler.features().n_gaps == before
    assert profiler.out_of_order_turns == 1


def test_an_out_of_order_turn_is_counted_for_the_proxy_to_log() -> None:
    """EC-07 asks for one log line per session; `nod_core` does no I/O.

    The count is exposed so the proxy can satisfy EC-07 at session close without
    the ingest path acquiring a logger (ARCHITECTURE §2, CLAUDE.md §6).

    Two of the three, not three: turns 3 and 4 are below the current turn and are
    dropped, while the repeated 9 is a continuation of the turn in progress and is
    ingested idempotently. EC-07's literal `<=` would drop all three and with them
    every partial of every turn — see the reading recorded at the guard itself.
    """
    profiler = Profiler()
    profiler.observe_turn(turn(9, word("a", start=0, end=100)))
    for order in (3, 4, 9):
        profiler.observe_turn(turn(order, word("b", start=200, end=300)))
    assert profiler.out_of_order_turns == 2


# --- CONTROL_SPEC §2.5: the four surviving cut conditions -------------------


def _cut_pair(
    *,
    ended: bool = True,
    resume_gap: int = 400,
    agent_ms: int = 0,
    first_token: str = "and",  # noqa: S107 — a transcript token, not a credential
) -> tuple[Profiler, Cut | None]:
    """Drive a two-turn sequence shaped to be a cut, with one knob flipped."""
    profiler = Profiler()
    profiler.observe_turn(
        turn(
            1,
            word("my", start=0, end=200),
            word("number", start=400, end=800),
            ended=ended,
        )
    )
    resumed = profiler.observe_turn(
        turn(
            2,
            word(first_token, start=800 + resume_gap, end=1000 + resume_gap),
            word("then", start=1300 + resume_gap, end=1500 + resume_gap),
        ),
        agent_audio_ms=agent_ms,
    )
    return profiler, resumed


def test_a_quick_resumption_after_an_ended_turn_is_a_cut() -> None:
    """§2.5 conditions 1 to 4 all satisfied."""
    profiler, cut = _cut_pair()
    assert cut is not None
    assert cut.turn_order == 1, (
        "the cut names the turn that was cut, not the resumption"
    )
    assert cut.resume_gap_ms == 400
    assert profiler.features().recent_cuts == pytest.approx(1 / 3)


def test_condition_1_a_turn_that_never_ended_was_not_cut() -> None:
    """§2.5 condition 1: there is no premature boundary if there was no boundary."""
    _, cut = _cut_pair(ended=False)
    assert cut is None


def test_condition_2_a_slow_resumption_is_a_new_turn_not_a_cut() -> None:
    """§2.5 condition 2: the caller must resume inside `RESUME_MS`."""
    _, cut = _cut_pair(resume_gap=RESUME_MS + 1)
    assert cut is None


def test_condition_2_holds_at_the_boundary() -> None:
    """Strictly `< RESUME_MS`, so exactly `RESUME_MS` is not a cut."""
    assert _cut_pair(resume_gap=RESUME_MS)[1] is None
    assert _cut_pair(resume_gap=RESUME_MS - 1)[1] is not None


def test_condition_3_the_agent_having_spoken_means_the_turn_was_taken() -> None:
    """§2.5 condition 3: agent audio at or past `AGENT_GRACE_MS` disqualifies it.

    The caller heard the agent start and stopped for it. That is a normal turn
    exchange, not the controller cutting them off.
    """
    assert _cut_pair(agent_ms=AGENT_GRACE_MS)[1] is None
    assert _cut_pair(agent_ms=AGENT_GRACE_MS - 1)[1] is not None


@pytest.mark.parametrize(
    "token", ["yes", "no", "correct", "wait", "sorry", "Yes.", "NO"]
)
def test_condition_4_an_affirmation_opens_a_genuine_new_turn(token: str) -> None:
    """§2.5 condition 4, and normalisation is part of it.

    `Yes.` and `NO` have to be recognised: the upstream capitalises and punctuates,
    so a case-sensitive membership test would let every sentence-initial `Yes.`
    through and label it a cut.
    """
    assert _cut_pair(first_token=token)[1] is None


def test_recent_cuts_is_normalised_and_clamped() -> None:
    """§2.5: count over the last `CUT_WINDOW` turns, divided by 3, clamped to 1."""
    profiler = Profiler()
    clock = 0
    for order in range(1, 7):
        profiler.observe_turn(
            turn(
                order,
                word("and", start=clock, end=clock + 200),
                word("then", start=clock + 400, end=clock + 600),
            ),
            agent_audio_ms=0,
        )
        clock += 700
    assert profiler.features().recent_cuts == 1.0


# --- CONTROL_SPEC §2.2 and §2.3 ---------------------------------------------


def test_speech_rate_is_words_per_voiced_second() -> None:
    """§2.2: `finalised_words / voiced_ms * 1000`, EWMA-seeded by its first turn."""
    profiler = Profiler()
    # Four words, 250 ms of voice each: 1000 ms voiced, so 4 words per second.
    profiler.observe_turn(
        turn(
            1,
            word("a", start=0, end=250),
            word("b", start=500, end=750),
            word("c", start=1000, end=1250),
            word("d", start=1500, end=1750),
        )
    )
    assert profiler.features().speech_rate == pytest.approx(4.0)


def test_an_adjacent_repeat_counts_as_a_disfluency() -> None:
    """§2.3, first count. `the the` is the spec's own example."""
    fluent = Profiler()
    fluent.observe_turn(
        turn(1, word("the", start=0, end=200), word("cat", start=300, end=500))
    )
    stammered = Profiler()
    stammered.observe_turn(
        turn(1, word("the", start=0, end=200), word("the", start=300, end=500))
    )
    assert stammered.features().disfluency > fluent.features().disfluency


def test_a_multi_word_filler_is_recognised() -> None:
    """`you know` is in `FILLER_TOKENS` and no unigram test can ever see it.

    Without the bigram check that entry is dead configuration: the set would list a
    filler the code could not detect, and the disfluency feature would silently
    under-count every caller who uses it.
    """
    plain = Profiler()
    plain.observe_turn(
        turn(1, word("we", start=0, end=200), word("went", start=300, end=500))
    )
    hedged = Profiler()
    hedged.observe_turn(
        turn(1, word("you", start=0, end=200), word("know", start=300, end=500))
    )
    assert hedged.features().disfluency > plain.features().disfluency


def test_a_long_word_for_its_length_counts_as_a_duration_outlier() -> None:
    """§2.3, third count: over `2.5 x` the five-bucket median for its length."""
    normal = Profiler()
    normal.observe_turn(turn(1, word("hi", start=0, end=180)))
    drawled = Profiler()
    drawled.observe_turn(turn(1, word("hi", start=0, end=900)))
    assert drawled.features().disfluency > normal.features().disfluency


def test_disfluency_is_a_density_and_stays_within_the_unit_interval() -> None:
    """§2.3 clamps to `[0, 1]`; a turn of nothing but fillers must not exceed 1."""
    profiler = Profiler()
    profiler.observe_turn(
        turn(
            1,
            word("um", start=0, end=900),
            word("um", start=1000, end=1900),
            word("um", start=2000, end=2900),
        )
    )
    assert 0.0 <= profiler.features().disfluency <= 1.0


def test_jitter_is_computed_and_stays_within_the_unit_interval() -> None:
    """§2.4: a logged feature at weight 0 (ADR-011), still bounded.

    Two readings of "normalising against a constant" are open — variance over
    `JITTER_SCALE`, or standard deviation over it. Variance is implemented, matching
    the docstring's "Welford running variance". The choice cannot affect any output
    while `JITTER_GAIN` is 0 and §9 property 10 pins that, so it is recorded rather
    than resolved; raising the weight is an ADR that has to pick one.
    """
    profiler = Profiler()
    for index, confidence in enumerate((0.0, 0.5, 1.0, 0.25), start=1):
        profiler.observe_turn(
            turn(
                1,
                word("a", start=0, end=100),
                ended=index == 4,
                confidence=confidence,
            )
        )
    assert 0.0 <= profiler.features().jitter <= 1.0


def test_a_turn_with_no_confidence_field_leaves_jitter_alone() -> None:
    """§7's degradation matrix: no `end_of_turn_confidence` disables the feature."""
    profiler = Profiler()
    profiler.observe_turn(turn(1, word("a", start=0, end=100), confidence=None))
    assert profiler.features().jitter == 0.0


# --- EC-03: rotation --------------------------------------------------------


def test_a_snapshot_carries_the_turn_order_across_a_rotation() -> None:
    """EC-03: the new socket must not re-accept turns the old one already saw."""
    profiler = Profiler()
    profiler.observe_turn(
        turn(4, word("a", start=0, end=100), word("b", start=300, end=400))
    )
    revived = Profiler.restore(profiler.snapshot())
    assert (
        revived.observe_turn(
            turn(4, word("a", start=0, end=100), word("b", start=300, end=400))
        )
        is None
    )
    assert revived.out_of_order_turns == 1


def test_the_exact_estimator_is_selectable_and_survives_a_rotation() -> None:
    """ADR-002: `NOD_EXACT_QUANTILES` swaps the estimator, including on restore."""
    profiler = Profiler(exact_quantiles=True)
    profiler.observe_turn(_spaced(1, 10, 250))
    before = profiler.features()
    after = Profiler.restore(profiler.snapshot(), exact_quantiles=True).features()
    assert before.g_p90_ms == after.g_p90_ms
    assert before.n_gaps == after.n_gaps


# --- degenerate inputs the upstream can still produce -----------------------


def test_a_zero_duration_word_does_not_divide_by_zero() -> None:
    """§2.2 divides by `voiced_ms`, which a zero-length word can leave at 0.

    `start_ms == end_ms` is not hypothetical: the upstream emits it for a word
    whose audio fell inside one frame. Dividing there would raise inside the
    ingest path, and INV-8 would turn a caller's odd syllable into a
    `controller_error` for the rest of the call.
    """
    profiler = Profiler()
    profiler.observe_turn(
        turn(1, word("a", start=100, end=100), word("b", start=400, end=400))
    )
    features = profiler.features()
    assert features.speech_rate == 0.0
    assert features.n_gaps == 1


def test_a_turn_of_one_unfinalised_word_folds_nothing_in() -> None:
    """Every per-turn EWMA must tolerate a turn that finalised no words at all."""
    profiler = Profiler()
    profiler.observe_turn(turn(1, word("a", start=0, end=200, final=False), ended=True))
    features = profiler.features()
    assert features.speech_rate == 0.0
    assert features.disfluency == 0.0
    assert features.n_gaps == 0


def test_a_resumption_whose_words_are_all_unfinalised_is_not_yet_a_cut() -> None:
    """Cut condition 2 needs a trustworthy first-word start, and EC-21 denies it.

    Guessing from an unfinalised word would time the resumption against a
    timestamp the upstream has not committed to, and label a cut on it.
    """
    profiler = Profiler()
    profiler.observe_turn(
        turn(1, word("my", start=0, end=200), word("number", start=400, end=800))
    )
    cut = profiler.observe_turn(
        turn(2, word("and", start=1000, end=1200, final=False), ended=False)
    )
    assert cut is None


def test_a_very_long_token_falls_into_the_last_duration_bucket() -> None:
    """§2.3's table is five buckets ending at 99 characters; something must catch
    a token past it. The upstream concatenates on occasion, and a 120-character
    token with no bucket would raise inside the disfluency count.
    """
    profiler = Profiler()
    profiler.observe_turn(turn(1, word("x" * 120, start=0, end=300)))
    assert profiler.features().disfluency == 0.0


def test_an_exact_estimator_needs_a_positive_capacity() -> None:
    """A zero-capacity ring would silently answer every quantile from nothing."""
    with pytest.raises(ValueError, match="capacity"):
        ExactQuantile(0.9, capacity=0)


def test_both_estimators_count_every_sample_they_were_given() -> None:
    """`seen` is ingestion, not retention, and EC-03 restores from it.

    `restore` seeds `seen` so a rotated session stays warm; a `seen` that counted
    only what the ring kept would re-cool a long call at the rotation.
    """
    p2, exact = P2Quantile(0.9), ExactQuantile(0.9, capacity=4)
    for sample in range(10):
        p2.update(float(sample))
        exact.update(float(sample))
    assert p2.seen == exact.seen == 10


# --- gaps found by the Gate 2 mutation run ----------------------------------


def test_a_new_minimum_pulls_the_estimate_down() -> None:
    """P²'s lowest marker has to track a falling stream, or nothing can descend.

    Added because a mutation removing `self._values[0] = x` from `_locate`
    survived every other test here. The range property could not see it: the
    reported value is the *middle* marker, which stays inside the observed range
    whether or not the minimum marker follows. Nothing else fed a stream whose
    minimum moved.

    The behaviour matters directly. A caller who settles into a rhythm after a
    hesitant opening is a caller whose gaps fall, and an estimator whose floor is
    stuck at the opening keeps `max_turn_silence` wide for the rest of the call —
    the agent stays sluggish for someone who is now fluent, which is the failure
    the asymmetric-decay guard exists to *bound* and not to cause.
    """
    estimator = P2Quantile(0.50)
    for _ in range(5):
        estimator.update(1000.0)
    for _ in range(200):
        estimator.update(10.0)
    assert estimator.value < 100.0, (
        f"p50 stuck at {estimator.value} after 200 samples at 10 ms; the lowest "
        "marker is not tracking downwards"
    )


def test_overlapping_word_timings_cannot_produce_a_negative_gap() -> None:
    """§2.1's lower clamp, exercised through `observe_turn` rather than the estimator.

    Added because a mutation replacing the clamp's `0.0` floor with `-inf`
    survived. `test_g_p90_can_never_be_negative_or_beyond_the_clamp` looked like
    it covered this and did not: it drives the *estimator* over a non-negative
    domain, so it never reaches the clamp in the ingest path at all. That is
    ADR-015's lesson again — a unit test of a function does not cover the wiring
    that reaches it.

    Overlapping timings are not invented for the test. The upstream revises word
    boundaries as a turn finalises, and a later word starting before its
    predecessor's recorded end is the ordinary result. Unclamped it enters the
    quantiles as a negative pause, and `1.6 * g_p90 + 250` then returns a
    `max_turn_silence` shorter than the base for a caller who paused.
    """
    profiler = Profiler()
    profiler.observe_turn(
        turn(
            1,
            word("six", start=0, end=500),
            word("one", start=300, end=700),
            word("seven", start=600, end=900),
        )
    )
    features = profiler.features()
    assert features.n_gaps == 2
    assert features.g_p50_ms == 0.0, "an overlap must clamp to 0, not go negative"
    assert features.g_p90_ms >= 0.0


def test_features_clamps_an_inverted_profile_it_is_handed() -> None:
    """ADR-023's repair, exercised deterministically because it can no longer be
    exercised statistically.

    Added at Gate 3 because the repair became **unfalsifiable by the property
    test that was meant to guard it**. `test_features_never_reports_an_inverted_profile`
    draws realistic turn streams, and ADR-022 raised `MIN_GAPS_FOR_WARM` from 8 to
    24 in the same sitting — measured, the inversion runs 0.120 % of streams at
    `n` in `[8, 23]` and **0 of 20 000** at `n` in `[24, 400]`. So the property
    passes with the repair removed: the condition simply does not arise in its
    domain, which is CLAUDE.md §5's test that cannot go red.

    Two independently derived ADRs, one substantially subsuming the other, and
    neither noticed. The repair stays — it is one comparison, 0 of 20 000 is an
    upper bound rather than a proof, and `features()` is called for cold profiles
    too, where `n_gaps` is below the threshold by definition. But it needs a test
    that can fail, and the only deterministic route to an inverted profile is to
    hand one in: `restore` accepts a `ProfilerState` built by a caller, and
    `restore`'s own docstring already notes that a hand-built state no profiler
    could have produced is the one case it does not round-trip.
    """
    inverted = ProfilerState(
        n_gaps=40,
        g_p50_ms=800.0,
        g_p90_ms=400.0,
        speech_rate=2.4,
        disfluency=0.1,
        jitter=0.0,
        recent_cuts=0.0,
        last_turn_order=6,
    )
    features = Profiler.restore(inverted).features()
    assert features.g_p50_ms == 800.0
    assert features.g_p90_ms == 800.0, (
        f"g_p90 reported as {features.g_p90_ms} below g_p50=800; the ADR-023 "
        "repair did not fire on a profile it was handed"
    )
    assert features.g_p90_ms >= features.g_p50_ms
