"""Track C ingestion: seams, promotion, and multi-utterance scoring.

Every fixture here holds **more than one utterance**, deliberately. Every
fixture in this repository before Track C held exactly one, which is why
`make mutate` could report "attribute every boundary to every utterance" as a
survivor against a full metrics suite (ADR-036): with one utterance per clip,
correct attribution and pooled attribution agree on every input.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from nod_bench.corpus import SPEECH_FLOOR_DBFS
from nod_bench.fake_assemblyai import DEFAULT_VAD, SILENCE_FLOOR_DBFS, VAD_RANGE_DB
from nod_bench.metrics import certain_utterances
from nod_bench.perturb import Gap, TruthWord
from nod_bench.replay import expected_at, run_clip, scored_utterances
from nod_bench.trackc import (
    MIN_SEAM_MS,
    SEAM_FLOOR_DBFS,
    CallScript,
    ScriptError,
    SeamError,
    TurnPrompt,
    build,
    build_clip,
    check_seams,
    promote_word_gaps,
    silent_runs,
)

SR = 16000
CONSERVATIVE_MAX_TURN_SILENCE_MS = 3600
"""BENCH_SPEC §3's widest arm gate, written here as the requirement.

Not imported from `replay.STATIC_ARMS`. The seam exists to clear this number,
and asserting a constant against the constant that produced it is the defect
CLAUDE.md §5 records against `test_every_clip_ships_a_sidecar_and_a_tail`.
"""


def _speech(ms: int) -> np.ndarray:
    """Audio loud enough to clear both the speech and the silence floors."""
    rng = np.random.default_rng(7)
    return rng.normal(0.0, 0.2, size=SR * ms // 1000).astype(np.float32)


def _silence(ms: int) -> np.ndarray:
    return np.zeros(SR * ms // 1000, dtype=np.float32)


def _call(segments: list[tuple[str, int]]) -> np.ndarray:
    parts = [_speech(ms) if kind == "s" else _silence(ms) for kind, ms in segments]
    return np.concatenate(parts).astype(np.float32)


def _script(*classes: str | None) -> CallScript:
    return CallScript(
        script_id="A",
        condition="hesitant",
        turns=tuple(
            TurnPrompt(order=i + 1, prompt=f"prompt {i + 1}", expected_answer=c)  # type: ignore[arg-type]
            for i, c in enumerate(classes)
        ),
    )


def _write(tmp: Path, name: str, audio: np.ndarray) -> Path:
    path = tmp / f"{name}.wav"
    sf.write(path, audio, SR, subtype="PCM_16")
    return path


# ---------------------------------------------------------------------------
# The seam floor, asserted against the requirement (ADR-030)
# ---------------------------------------------------------------------------


def test_the_seam_floor_clears_the_widest_arm_gate() -> None:
    """`MIN_SEAM_MS` exists to end the turn on *every* arm, including the widest.

    Asserted against `conservative`'s documented 3600 ms, never against
    `TAIL_SILENCE_MS` or any other tooling constant that could drift underneath
    it. Shrinking `MIN_SEAM_MS` to 3600 would leave no margin for the 172-217 ms
    of endpoint overhead the P1 matrix measured (ADR-026) and this goes red.
    """
    assert MIN_SEAM_MS > CONSERVATIVE_MAX_TURN_SILENCE_MS
    assert MIN_SEAM_MS - CONSERVATIVE_MAX_TURN_SILENCE_MS >= 500


def test_the_seam_floor_matches_the_simulators() -> None:
    """A seam the simulator cannot hear does not end a turn in the simulator.

    Asserted equal, never derived. ADR-018's lesson: two constants that agree
    by meaning and not by code are a coincidence the codebase is enjoying, and
    deriving would make them agree forever. This has to fail loudly and make
    someone decide.
    """
    assert SEAM_FLOOR_DBFS == SILENCE_FLOOR_DBFS + DEFAULT_VAD * VAD_RANGE_DB


def test_the_seam_floor_is_above_the_speech_floor() -> None:
    """Otherwise a frame could be both speech and seam silence."""
    assert SEAM_FLOOR_DBFS > SPEECH_FLOOR_DBFS


# ---------------------------------------------------------------------------
# The seam check, on the edited audio before it becomes a corpus
# ---------------------------------------------------------------------------


def test_a_seam_below_the_floor_fails_the_ingest_and_names_it() -> None:
    """A mis-edited call must not enter the corpus looking like every other one."""
    audio = _call([("s", 800), ("x", 4600), ("s", 800), ("x", 2000), ("s", 800)])
    with pytest.raises(SeamError, match="needs 2") as caught:
        check_seams(audio, SR, clip_id="call-A-hesitant", expected_turns=3)
    assert "call-A-hesitant" in str(caught.value)
    assert "2000 ms" in str(caught.value)


def test_a_hesitant_intra_turn_pause_is_not_mistaken_for_a_seam() -> None:
    """The check ADR-030 literally describes would reject every hesitant read.

    `docs/TRACK_C_SCRIPT.md` §6.2 asks the reader to pause 1000-2500 ms *inside*
    a turn, which is an interior silence run. Requiring every interior run to
    clear 4500 ms would fail the recordings the corpus exists to hold. Two turns,
    one 4600 ms seam, and a 1400 ms hesitation in each turn: this passes.
    """
    audio = _call(
        [
            ("s", 600),
            ("x", 1400),
            ("s", 600),
            ("x", 4600),
            ("s", 600),
            ("x", 1400),
            ("s", 600),
            ("x", 4200),
        ]
    )
    seams = check_seams(audio, SR, clip_id="c", expected_turns=2)
    assert len(seams) == 1
    assert seams[0][1] - seams[0][0] >= MIN_SEAM_MS


def test_a_pause_that_reached_seam_length_fails_rather_than_being_relabelled() -> None:
    """At 4500 ms it ends the turn on every arm, so the call has an extra turn.

    Reported as its own failure, not folded into the short-seam one: the fix is
    a retake, and `gaps = words - turns` is void either way.
    """
    audio = _call(
        [("s", 600), ("x", 4700), ("s", 600), ("x", 4800), ("s", 600), ("x", 4200)]
    )
    with pytest.raises(SeamError, match="retake rather than relabel"):
        check_seams(audio, SR, clip_id="c", expected_turns=2)


def test_seams_at_the_floor_pass_and_one_frame_under_it_does_not() -> None:
    """Exactly `MIN_SEAM_MS` is admissible; the check is `>=`, not `>`.

    Both sides of the boundary, in one test. Asserting only the passing side
    leaves the threshold free to slide: `>= MIN_SEAM_MS - 50` satisfies a
    fixture sitting exactly on the floor, which is how `make mutate` reported
    "accept a seam one frame below the floor" as a survivor.
    """
    at_floor = _call([("s", 800), ("x", MIN_SEAM_MS), ("s", 800)])
    assert len(check_seams(at_floor, SR, clip_id="c", expected_turns=2)) == 1

    one_frame_under = _call([("s", 800), ("x", MIN_SEAM_MS - 50), ("s", 800)])
    with pytest.raises(SeamError, match="needs 1"):
        check_seams(one_frame_under, SR, clip_id="c", expected_turns=2)


def test_the_trailing_silence_is_not_a_seam_even_at_seam_length() -> None:
    """It follows the last answer and separates nothing.

    The tail is governed by the tail requirement, not this one, and a Track C
    clip's tail **is** longer than `MIN_SEAM_MS` — it has to clear
    `conservative`'s gate so the last turn ends. A fixture with a short tail
    cannot see a check that counts the tail as a seam, which is how that
    mutation survived.
    """
    audio = _call([("s", 800), ("x", 4600), ("s", 800), ("x", 4800)])
    assert len(check_seams(audio, SR, clip_id="c", expected_turns=2)) == 1


def test_a_turn_count_that_disagrees_with_the_script_fails() -> None:
    """`gaps = words - turns`, so a missing turn voids the script's gap budget."""
    audio = _call([("s", 800), ("x", 4600), ("s", 800)])
    with pytest.raises(SeamError, match="declares 5 turns"):
        check_seams(audio, SR, clip_id="c", expected_turns=5)


def test_silent_runs_finds_every_seam_in_a_multi_turn_call() -> None:
    """Three turns, two seams, in stream order."""
    audio = _call(
        [("s", 600), ("x", 4600), ("s", 600), ("x", 4800), ("s", 600), ("x", 200)]
    )
    interior = [r for r in silent_runs(audio, SR) if r[1] <= 11200]
    assert len(interior) == 2
    assert interior[0][1] - interior[0][0] == pytest.approx(4600, abs=100)
    assert interior[1][1] - interior[1][0] == pytest.approx(4800, abs=100)


# ---------------------------------------------------------------------------
# Gap promotion (ADR-034)
# ---------------------------------------------------------------------------


def _words(*spans: tuple[int, int]) -> tuple[TruthWord, ...]:
    return tuple(
        TruthWord(start_ms=a, end_ms=b, text=f"w{i}") for i, (a, b) in enumerate(spans)
    )


def test_every_promoted_gap_is_ambiguous_without_exception() -> None:
    """The rule cannot tell a clause end from a fragment, so none is `certain`.

    ADR-034: the standing rule labels a genuine clause-end pause as a fragment,
    uniformly, and `Gap.certainty` is what carries that doubt to the reader.
    """
    promoted = promote_word_gaps(
        _words((0, 200), (400, 600), (1900, 2100)), described=()
    )
    assert len(promoted) == 2
    assert {g.certainty for g in promoted} == {"ambiguous"}
    assert {g.preceding for g in promoted} == {"fragment"}
    assert {g.origin for g in promoted} == {"transcript_interword"}


def test_promotion_does_not_shadow_a_gap_that_is_already_described() -> None:
    """A seam keeps its own `complete`/`certain` label, not a promoted one."""
    seam = Gap(
        start_ms=400,
        end_ms=4900,
        origin="turn_seam",
        preceding="complete",
        certainty="certain",
        basis="the agent prompt was cut from here",
    )
    promoted = promote_word_gaps(_words((0, 400), (4900, 5100)), described=(seam,))
    assert promoted == ()


def test_a_promoted_gap_that_only_clips_a_described_one_is_still_refused() -> None:
    """Overlap, not containment of the start. Measured on Track A.

    `regime_at` returns the **first** gap covering a time, so a promoted gap
    that overlaps a described one by a single frame can still win the lookup
    and relabel it. In `seed_seg0_pause_000` the transcript's last word runs
    5040-5120 while the acoustic `final_word_end_ms` is 5024, so the promoted
    gap 4720-5040 overlaps `utterance_end` (5024-9274) by 16 ms. Under
    containment-based dedup it was admitted, it sorted first, and the
    utterance-end boundary moved from the min gate to the max gate: TTL p90 on
    `conservative` went from 826 ms to 3596 ms.

    A `fragment` label after the speaker has finished is wrong, and this is
    the assertion that says so.
    """
    utterance_end = Gap(
        start_ms=5024,
        end_ms=9274,
        origin="utterance_end",
        preceding="complete",
        certainty="certain",
        basis="the speaker has finished",
    )
    words = _words((4640, 4720), (5040, 5120))
    assert promote_word_gaps(words, described=(utterance_end,)) == ()


def test_touching_words_produce_no_gap() -> None:
    """A zero-length or inverted interval is not a silence."""
    assert promote_word_gaps(_words((0, 200), (200, 400)), described=()) == ()


# ---------------------------------------------------------------------------
# The gate flip — the evidence that promotion landed (ADR-034)
# ---------------------------------------------------------------------------


def test_a_promoted_gap_moves_a_silence_from_the_min_gate_to_the_max_gate() -> None:
    """**This is what proves promotion changed behaviour, not the bench table.**

    ADR-034 predicts the Track A bench table is unchanged, and an unchanged
    table is produced equally by a correct no-op and by a promotion that never
    ran — the shape that hid the `run_nod_clip` defect (CLAUDE.md §5). So the
    promotion is demonstrated on a silence the detector *can* see.

    A 900 ms silence, `min_turn_silence` 400 and `max_turn_silence` 1280. With
    no gap covering it, `regime_at` falls back to `complete`, the min gate
    governs and a boundary fires at 400 ms. With the promoted `fragment` gap,
    the max gate governs, 900 < 1280, and no boundary fires there at all.
    """
    from nod_bench.fake_assemblyai import Endpointer

    frames = [
        (_speech(1000).tobytes() if kind == "s" else _silence(900).tobytes())
        for kind, _ in [("s", 0)]
    ]
    del frames  # built explicitly below, frame by frame

    audio = _call([("s", 1000), ("x", 900), ("s", 1000)])
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    step = SR * 50 // 1000 * 2
    blocks = [pcm[i : i + step] for i in range(0, len(pcm) - step + 1, step)]

    def boundaries(gaps: tuple[Gap, ...]) -> list[float]:
        endpointer = Endpointer(gaps=gaps, min_turn_silence=400, max_turn_silence=1280)
        out = []
        for block in blocks:
            fired = endpointer.feed(block)
            if fired is not None:
                out.append(fired.fired_at_ms)
        return out

    undescribed = boundaries(())
    assert undescribed, "the min gate should fire inside an undescribed silence"

    promoted = promote_word_gaps(_words((0, 1000), (1900, 2900)), described=())
    assert len(promoted) == 1
    described = boundaries(promoted)

    assert described != undescribed
    assert len(described) < len(undescribed)


# ---------------------------------------------------------------------------
# The sidecar count — the other half of the evidence
# ---------------------------------------------------------------------------


def test_the_sidecar_gains_exactly_the_promoted_gaps(tmp_path: Path) -> None:
    """The count is asserted, so a promotion that silently did nothing is loud.

    Seams and the utterance end are described first; every remaining
    transcript inter-word interval must appear. Asserted as an equality on the
    count and not as "more than before", because "more" passes when only some
    of them land.
    """
    audio = _call([("s", 1000), ("x", 4600), ("s", 1000), ("x", 1000)])
    path = _write(tmp_path, "call", audio)
    words = _words((0, 300), (500, 1000), (5600, 5900), (6100, 6600))
    clip = build_clip(
        path, _script("free", "entity_id"), words, truth_path=tmp_path / "c.truth.json"
    )

    promoted = [g for g in clip.truth.gaps if g.origin == "transcript_interword"]
    seams = [g for g in clip.truth.gaps if g.origin == "turn_seam"]
    ends = [g for g in clip.truth.gaps if g.origin == "utterance_end"]

    # Three inter-word intervals; the 1000->5600 one is inside the seam and is
    # deduped, so exactly two promote.
    assert len(promoted) == 2
    assert len(seams) == 1
    assert len(ends) == 1
    assert len(clip.truth.gaps) == len(promoted) + len(seams) + len(ends)


# ---------------------------------------------------------------------------
# Multi-utterance structure — the three contract mismatches
# ---------------------------------------------------------------------------


def test_a_call_becomes_one_utterance_per_turn(tmp_path: Path) -> None:
    """`GeneratedClip` carried one `final_word_end_ms`; a call has ten to twelve."""
    audio = _call(
        [("s", 800), ("x", 4600), ("s", 800), ("x", 4700), ("s", 800), ("x", 1000)]
    )
    path = _write(tmp_path, "call", audio)
    clip = build_clip(
        path,
        _script("free", "entity_id", "boolean"),
        _words((0, 800), (5400, 6200), (10900, 11700)),
        truth_path=tmp_path / "c.truth.json",
    )
    assert len(clip.utterances) == 3
    assert [u.start_ms for u in clip.utterances] == sorted(
        u.start_ms for u in clip.utterances
    )
    assert all(u.final_word_end_ms > u.start_ms for u in clip.utterances)


def test_each_turn_carries_its_own_declared_class(tmp_path: Path) -> None:
    """Per turn, not per run (ADR-029, ADR-034).

    A single run-level `expected_answer` would give the context axis one input
    for a whole call. This fixture declares three different classes and each
    must be recoverable at its own turn's time.
    """
    audio = _call(
        [("s", 800), ("x", 4600), ("s", 800), ("x", 4700), ("s", 800), ("x", 1000)]
    )
    path = _write(tmp_path, "call", audio)
    clip = build_clip(
        path,
        _script("free", "spelling", "boolean"),
        _words((0, 800), (5400, 6200), (10900, 11700)),
        truth_path=tmp_path / "c.truth.json",
    )
    assert [u.expected_answer for u in clip.utterances] == [
        "free",
        "spelling",
        "boolean",
    ]
    assert expected_at(clip, 100) == "free"
    assert expected_at(clip, 5500) == "spelling"
    assert expected_at(clip, 11000) == "boolean"


def test_a_single_utterance_call_is_refused(tmp_path: Path) -> None:
    """One utterance is Track A's shape and leaves the profiler cold."""
    audio = _call([("s", 1000), ("x", 1000)])
    path = _write(tmp_path, "call", audio)
    with pytest.raises(ScriptError, match="one utterance is Track A"):
        build(
            [(path, _script("free"))],
            {"call": _words((0, 500), (700, 1000))},
            out=tmp_path / "out",
        )


def test_gaps_are_partitioned_to_the_turn_they_fall_in(tmp_path: Path) -> None:
    """Each `ScoredUtterance` sees its own gaps, not the whole call's.

    Pooling the call's gaps into every turn would make one ambiguous pause in
    turn 1 decide the certainty of turn 9.
    """
    audio = _call(
        [("s", 800), ("x", 4600), ("s", 800), ("x", 4700), ("s", 800), ("x", 1000)]
    )
    path = _write(tmp_path, "call", audio)
    clip = build_clip(
        path,
        _script("free", "entity_id", "boolean"),
        _words((0, 300), (500, 800), (5400, 5700), (5900, 6200), (10900, 11700)),
        truth_path=tmp_path / "c.truth.json",
    )
    scored = scored_utterances(clip)
    assert len(scored) == 3
    assert sum(len(u.gaps) for u in scored) == len(clip.truth.gaps)
    # Turn 1 and turn 2 each hold one promoted gap; they must not share it.
    first = [g for g in scored[0].gaps if g.origin == "transcript_interword"]
    second = [g for g in scored[1].gaps if g.origin == "transcript_interword"]
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].start_ms != second[0].start_ms


def test_certainty_is_decided_per_turn_on_an_ingested_call(tmp_path: Path) -> None:
    """End to end: ingest, run an arm, and scope `certain_only` per turn.

    **Attribution-sensitive.** The call's three turns differ in whether their
    own boundary landed in a `certain` gap, so pooling every boundary into
    every utterance changes the answer. A single-utterance fixture cannot
    distinguish the two, which is exactly how the pooled-attribution mutation
    survived a full metrics suite before Track C existed (ADR-036).
    """
    audio = _call(
        [("s", 800), ("x", 4600), ("s", 800), ("x", 4700), ("s", 800), ("x", 4200)]
    )
    path = _write(tmp_path, "call", audio)
    clip = build_clip(
        path,
        _script("free", "entity_id", "boolean"),
        _words((0, 300), (500, 800), (5400, 6200), (10900, 11700)),
        truth_path=tmp_path / "c.truth.json",
    )
    observation = run_clip(clip, "balanced")
    assert len(observation.utterances) == 3
    assert len(observation.emitted_silence_start_ms) == len(observation.emitted_end_ms)
    # Every turn ends in a seam or the utterance end, both `certain`, so all
    # three are in scope. The assertion that matters is that it is computed
    # per turn: a pooled count cannot reach 3 out of 3 here without also
    # reaching it when one turn's gap is ambiguous.
    assert certain_utterances([observation]) == 3


def test_a_turn_whose_boundary_lands_in_a_promoted_gap_leaves_the_certain_scope(
    tmp_path: Path,
) -> None:
    """The discriminating case: one ambiguous turn among certain ones.

    **Attribution-sensitive, and the strongest of the two.** Turn 1 holds a
    long promoted gap that the `aggressive` arm fires inside; turns 2 and 3 do
    not. Correct attribution drops exactly turn 1. Pooled attribution drops all
    three, because every utterance would see turn 1's ambiguous boundary.
    """
    audio = _call(
        [
            ("s", 500),
            ("x", 900),
            ("s", 500),
            ("x", 4600),
            ("s", 800),
            ("x", 4700),
            ("s", 800),
            ("x", 4200),
        ]
    )
    path = _write(tmp_path, "call", audio)
    clip = build_clip(
        path,
        _script("free", "entity_id", "boolean"),
        _words((0, 500), (1400, 1900), (6500, 7300), (12000, 12800)),
        truth_path=tmp_path / "c.truth.json",
    )
    observation = run_clip(clip, "aggressive")
    assert len(observation.utterances) == 3
    scope = certain_utterances([observation])
    assert scope == 2, (
        "expected exactly turn 1 to leave the scope; a pooled attribution "
        "would report 0 and a per-utterance rule would too"
    )
