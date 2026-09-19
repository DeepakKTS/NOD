"""The sidecar's transcribed words, and the closed loop that consumes them.

Gate 7 / ADR-031. Two things are tested here that nothing tested before:

- `replay._turn_from_words` replays real `Word` records instead of inverting a
  gap list into placeholder tokens, which is what made §2.3's disfluency
  features score 0 on every clip of every run (ADR-028);
- `replay.run_nod_clip` — the closed loop, the code that puts the `nod` arms on
  the chart — which had **no test at all** before this file. It feeds a
  published number, so CLAUDE.md §5 puts it in the first tier.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from nod_bench.corpus import BuiltCorpus, CorpusManifest, SourceClip, build
from nod_bench.perturb import TruthWord
from nod_bench.replay import MissingWordsError, _turn_from_words, run_nod_clip
from nod_bench.transcribe import CorpusDriftedError, TranscriptionError, apply_words
from nod_core.policy import CompiledPolicy
from nod_core.profiler import MIN_GAPS_FOR_WARM, Profiler
from nod_core.types import WindowHint

SR = 16_000


def _corpus(tmp_path: Path, *, seconds: float = 2.0) -> BuiltCorpus:
    t = np.arange(int(seconds * SR), dtype=np.float32) / SR
    tone = (0.25 * np.cos(2 * np.pi * 220.0 * t)).astype(np.float32)
    tone[int(0.8 * SR) : int(1.1 * SR)] = 0.0
    path = tmp_path / "src.wav"
    sf.write(path, tone, SR, subtype="PCM_16")
    manifest = CorpusManifest(
        version=1,
        track="trackA",
        clips=[SourceClip(corpus="t", license="t", file=path, sha256="0" * 64)],
    )
    return build(manifest, seed=7, out=tmp_path / "corpus")


def _words(texts: list[str], *, gap_ms: int, dur_ms: int = 80) -> tuple[TruthWord, ...]:
    """Words laid out with a fixed inter-word gap, starting at 0."""
    out, cursor = [], 0
    for text in texts:
        out.append(TruthWord(start_ms=cursor, end_ms=cursor + dur_ms, text=text))
        cursor += dur_ms + gap_ms
    return tuple(out)


def test_turn_from_words_refuses_a_wordless_sidecar(tmp_path: Path) -> None:
    """A clip whose sidecar predates the transcription pass must fail loudly."""
    corpus = _corpus(tmp_path)
    clip = corpus.clips[0]
    assert clip.truth.words == (), "fixture should start with no transcribed words"
    with pytest.raises(MissingWordsError, match="no transcribed words"):
        _turn_from_words(clip, 0, 10_000.0, 1)


def test_the_replayed_turn_carries_the_transcript_not_placeholders(
    tmp_path: Path,
) -> None:
    """The tokens reach the profiler as the service sent them.

    Asserted against the *words*, never against a generated placeholder scheme:
    under the old reconstruction every token was `w0, w1, …`, so no token could
    ever equal its predecessor or sit in `FILLER_TOKENS`, and §2.3 scored 0 on
    every clip. This is the assertion that would have caught that.
    """
    corpus = _corpus(tmp_path)
    spoken = ["the", "um", "um", "number", "is"]
    clip = corpus.clips[0]
    spoken_words = _words(spoken, gap_ms=120)
    clip = clip.model_copy(
        update={"truth": clip.truth.model_copy(update={"words": spoken_words})}
    )
    turn, consumed = _turn_from_words(clip, 0, 10_000.0, 1)
    assert turn is not None
    assert [w.text for w in turn.words] == spoken
    assert turn.transcript == " ".join(spoken)
    assert consumed == len(spoken)


def test_a_turn_of_w_words_hands_the_profiler_w_minus_one_gaps(
    tmp_path: Path,
) -> None:
    """The arithmetic `docs/TRACK_C_SCRIPT.md` §1 rests on, checked end to end.

    Asserted against the requirement — one gap per adjacent pair — and not
    against `len(clip.truth.words)`, which would restate the fixture.
    """
    corpus = _corpus(tmp_path)
    spoken = [f"word{i}" for i in range(12)]
    clip = corpus.clips[0]
    spoken_words = _words(spoken, gap_ms=200)
    clip = clip.model_copy(
        update={"truth": clip.truth.model_copy(update={"words": spoken_words})}
    )
    turn, _ = _turn_from_words(clip, 0, 10_000.0, 1)
    assert turn is not None
    profiler = Profiler()
    profiler.observe_turn(turn)
    assert profiler.features().n_gaps == len(spoken) - 1 == 11


def test_successive_turns_deliver_every_word_exactly_once(tmp_path: Path) -> None:
    """`consumed` advances by words, so no word is replayed twice or dropped."""
    corpus = _corpus(tmp_path)
    spoken = [f"w{i}" for i in range(10)]
    words = _words(spoken, gap_ms=100)
    clip = corpus.clips[0]
    clip = clip.model_copy(
        update={"truth": clip.truth.model_copy(update={"words": words})}
    )
    seen: list[str] = []
    consumed = 0
    for boundary in (words[3].end_ms, words[7].end_ms, 10_000.0):
        turn, consumed = _turn_from_words(clip, consumed, float(boundary), 1)
        if turn is not None:
            seen.extend(w.text for w in turn.words)
    assert seen == spoken


def test_the_closed_loop_emits_a_patch_once_the_profiler_warms(
    tmp_path: Path,
) -> None:
    """`run_nod_clip` is the product: a patch written onto the live endpointer.

    A hesitant profile — gaps far above `BASE_MAX_MS`'s implied operating point —
    must move `max_turn_silence`, which CONTROL_SPEC §0.2 names as the only knob
    that tolerates a mid-sentence pause. Asserted on the *direction* and on the
    patch count, not on a specific value, so a re-derived constant does not
    silently rewrite the test's expectation.
    """
    # Long enough that a hesitant profile fits before the clip's only boundary:
    # 30 words at 980 ms apiece needs about 30 s of timeline, and a two-second
    # source would hand the profiler three words and leave it cold.
    corpus = _corpus(tmp_path, seconds=32.0)
    clip = corpus.clips[0]
    spoken = [f"w{i}" for i in range(MIN_GAPS_FOR_WARM + 6)]
    spoken_words = _words(spoken, gap_ms=900)
    clip = clip.model_copy(
        update={"truth": clip.truth.model_copy(update={"words": spoken_words})}
    )
    policy = CompiledPolicy(hints={}, default=WindowHint(min_mult=1.0, max_mult=1.0))
    run = run_nod_clip(clip, "nod", policy=policy, expected_answer=None)
    assert run.turns >= 1
    assert run.patches >= 1, "a warm hesitant profile emitted no patch at all"
    assert run.final_max_ms > 1280, (
        f"max_turn_silence stayed at {run.final_max_ms} on a hesitant profile"
    )


def test_the_closed_loop_refuses_a_wordless_clip(tmp_path: Path) -> None:
    """The arm fails rather than quietly measuring `balanced` under nod's name."""
    corpus = _corpus(tmp_path)
    policy = CompiledPolicy(hints={}, default=WindowHint(min_mult=1.0, max_mult=1.0))
    with pytest.raises(MissingWordsError):
        run_nod_clip(corpus.clips[0], "nod", policy=policy, expected_answer=None)


# --- the sidecar rewrite ----------------------------------------------------


def test_applying_words_moves_sidecars_and_leaves_the_audio_byte_identical(
    tmp_path: Path,
) -> None:
    """ADR-031's "only sidecars move", asserted on the bytes rather than stated."""
    corpus = _corpus(tmp_path)
    before = {
        c.clip_id: hashlib.sha256(c.audio_path.read_bytes()).hexdigest()
        for c in corpus.clips
    }
    supplied = {c.clip_id: _words(["a", "b", "c"], gap_ms=150) for c in corpus.clips}
    count = apply_words(tmp_path / "corpus", supplied)
    assert count == len(corpus.clips)
    after = {
        c.clip_id: hashlib.sha256(c.audio_path.read_bytes()).hexdigest()
        for c in corpus.clips
    }
    assert after == before, "the transcription pass changed audio bytes"
    reloaded = BuiltCorpus.model_validate_json(
        (tmp_path / "corpus" / "corpus.json").read_text()
    )
    assert all(len(c.truth.words) == 3 for c in reloaded.clips)


def test_applying_words_refuses_a_corpus_whose_audio_drifted(tmp_path: Path) -> None:
    """New ground truth must never be attached to changed bytes."""
    corpus = _corpus(tmp_path)
    target = corpus.clips[0].audio_path
    audio, sr = sf.read(target, dtype="float32", always_2d=False)
    sf.write(target, np.asarray(audio, dtype=np.float32) * 0.5, sr, subtype="PCM_16")
    supplied = {c.clip_id: _words(["a", "b"], gap_ms=100) for c in corpus.clips}
    with pytest.raises(CorpusDriftedError, match="does not match"):
        apply_words(tmp_path / "corpus", supplied)


def test_applying_words_refuses_a_clip_with_nothing_supplied(tmp_path: Path) -> None:
    """A partial pass must not half-write the corpus."""
    corpus = _corpus(tmp_path)
    supplied = {c.clip_id: _words(["a", "b"], gap_ms=100) for c in corpus.clips[1:]}
    with pytest.raises(TranscriptionError, match="no words supplied"):
        apply_words(tmp_path / "corpus", supplied)


def test_a_word_ending_exactly_on_the_boundary_belongs_to_that_turn(
    tmp_path: Path,
) -> None:
    """The boundary is inclusive, and the off-by-one is silent in both directions.

    An exclusive comparison would hold the last word of every turn back to the
    next one. Nothing crashes and no count looks wrong — the word is still
    delivered exactly once — but the gap it would have contributed is computed
    across a turn boundary instead of within one, and CONTROL_SPEC §2.1 does not
    compute gaps across boundaries. So the profiler silently loses one gap per
    turn, which is the quantity `docs/TRACK_C_SCRIPT.md` §1 budgets in.

    Asserted by placing a word's `end_ms` *exactly* on the boundary, because that
    is the only input where `<=` and `<` differ.
    """
    corpus = _corpus(tmp_path)
    words = _words(["alpha", "bravo", "charlie"], gap_ms=100)
    clip = corpus.clips[0]
    clip = clip.model_copy(
        update={"truth": clip.truth.model_copy(update={"words": words})}
    )
    boundary = float(words[1].end_ms)

    turn, consumed = _turn_from_words(clip, 0, boundary, 1)

    assert turn is not None
    assert [w.text for w in turn.words] == ["alpha", "bravo"], (
        "the word ending exactly on the boundary was held back to the next turn"
    )
    assert consumed == 2
    profiler = Profiler()
    profiler.observe_turn(turn)
    assert profiler.features().n_gaps == 1
