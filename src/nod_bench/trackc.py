"""Track C ingestion: a recorded call becomes a scorable corpus.

`docs/TRACK_C_SCRIPT.md` plus ADR-030 and ADR-034. A Track C recording is
caller audio only — the agent's prompts are read off-mic and cut, leaving a
silent **seam** where each one was, and that seam is the only thing that ends a
turn.

Three jobs, in order, and the first gates the other two:

1. **The seam check** (ADR-030). Every inter-answer silence must be at least
   `MIN_SEAM_MS`. Shorter, and `conservative` sees fewer turns than
   `aggressive`; by `gaps = words - turns` the arms then read different
   profiles and the headline delta is partly an artifact of the edit.
2. **Segmentation.** The seams cut the call into caller turns, one
   `UtteranceSpan` each, carrying the class declared for that turn's prompt.
3. **Gap promotion** (ADR-034). Every transcript inter-word gap becomes a
   `Gap` labelled `fragment`/`ambiguous` by the standing rule. Without it a
   hesitant pause falls through `regime_at` to `complete` and `min_turn_silence`
   judges a pause `max_turn_silence` should have — the wider gate, the one Nod
   moves, would never bind on any arm.

CLI: `python -m nod_bench.trackc build --script A --audio call.wav --out
data/corpus/trackC`. Transcription needs `ASSEMBLYAI_API_KEY`; `make bench`
does not, because the sidecars are committed exactly as the `.wav` files are.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import numpy as np
import soundfile as sf
from pydantic import BaseModel, ConfigDict

from nod_bench.corpus import BuiltCorpus, GeneratedClip, _final_speech_ms
from nod_bench.perturb import Audio, Gap, TruthSpan, TruthWord, UtteranceSpan
from nod_core.types import ExpectedAnswer

SAMPLE_RATE: Final = 16000
"""Corpus audio is mono 16 kHz PCM16 throughout."""

FRAME_MS: Final = 50
"""Silence-detection frame. Milliseconds. Matches `corpus._intrinsic_gaps`."""

MIN_SEAM_MS: Final = 4500
"""Shortest admissible inter-turn seam. Milliseconds. ADR-030.

Asserted against **the requirement** — `conservative`'s documented 3600 ms
`max_turn_silence` (BENCH_SPEC §3) — plus 900 ms for frame quantisation in the
editor and endpoint overhead above the configured gate (the P1 matrix measured
boundaries landing 172-217 ms late, ADR-026).

**Deliberately not derived from any constant the tooling holds.** Tying it to
`corpus.TAIL_SILENCE_MS` or `MIN_INTRINSIC_GAP_MS` would reproduce the defect
CLAUDE.md §5 records, where a test compared a generated tail against the very
constant that produced it and so passed for any value. `test_the_seam_floor_
clears_the_widest_arm_gate` asserts the relation to 3600 instead, and goes red
if either number drifts toward the other.
"""

SEAM_FLOOR_DBFS: Final = -44.0
"""Level below which a frame counts as seam silence.

The same floor `fake_assemblyai.Endpointer` uses at the default
`vad_threshold`, because the seam has to end the turn *in the simulator* and a
seam the simulator cannot hear is not a seam. Asserted equal to it by
`test_the_seam_floor_matches_the_simulators`, never derived from it: ADR-018's
lesson is that two constants agreeing by meaning and not by code are a
coincidence, and deriving would make them agree forever when one may need to be
frozen.
"""


class SeamError(RuntimeError):
    """A seam is shorter than `MIN_SEAM_MS`, so the arms would see it differently."""


class ScriptError(RuntimeError):
    """The audio does not hold the number of turns the script declares."""


class TurnPrompt(BaseModel):
    """One scripted prompt and the class declared for it (ADR-029)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order: int
    prompt: str
    expected_answer: ExpectedAnswer | None


class CallScript(BaseModel):
    """One of `docs/TRACK_C_SCRIPT.md` §4's five scripts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    script_id: str
    condition: str
    """`fluent` or `hesitant`. The same words either way (§5)."""

    turns: tuple[TurnPrompt, ...]


def silent_runs(
    audio: Audio, sr: int, *, floor_dbfs: float = SEAM_FLOOR_DBFS
) -> tuple[tuple[int, int], ...]:
    """Every run of silence in the clip, as `(start_ms, end_ms)`. Pure. `O(n)`.

    Args:
        audio: Mono samples.
        sr: Sample rate.
        floor_dbfs: Level at or below which a frame is silent.

    Returns:
        Runs in stream order. A clip ending in silence yields a final run.
    """
    frame = sr * FRAME_MS // 1000
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(0, len(audio) - frame + 1, frame):
        block = audio[index : index + frame]
        rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
        quiet = rms <= 0.0 or 20 * np.log10(rms) <= floor_dbfs
        at_ms = round(index * 1000 / sr)
        if quiet:
            start = at_ms if start is None else start
            continue
        if start is not None:
            runs.append((start, at_ms))
            start = None
    if start is not None:
        runs.append((start, round(len(audio) * 1000 / sr)))
    return tuple(runs)


def check_seams(
    audio: Audio, sr: int, *, clip_id: str, expected_turns: int
) -> tuple[tuple[int, int], ...]:
    """Identify the inter-turn seams and assert each clears `MIN_SEAM_MS`. ADR-030.

    Run on the **edited audio, before it becomes a corpus**. A convention in
    the recording notes is a check that cannot fail: nothing reads it, and a
    mis-edited clip enters the corpus looking like every other clip.

    **A seam is identified by length, not by being an interior silence.** ADR-030
    says to assert "every run that separates two answers" and does not say how to
    tell those runs apart; the naive reading — every interior silence run — is
    wrong and would reject every hesitant recording, because
    `docs/TRACK_C_SCRIPT.md` §6.2 asks the reader to hold intra-turn pauses at
    1000-2500 ms and those are interior silence runs too.

    Length is a sound discriminator precisely because of what `MIN_SEAM_MS` is
    for: a silence at or above it ends the turn on **every** arm, including
    `conservative`. So a run that long *is* a turn boundary as far as any
    endpointer is concerned, whatever the reader intended, and a run below it is
    not one on the widest arm. The two failures are reported separately:

    - **too few** seam-length runs: a seam was edited short, so the arms would
      disagree about the turn count (ADR-030's original concern);
    - **too many**: an intra-turn pause reached seam length, so it ends the turn
      on every arm and the call really does have more turns than the script
      says. The script's gap budget is void either way, and the recording needs
      a retake rather than a relabel.

    The trailing run is excluded: it follows the last answer and separates
    nothing, so the tail requirement governs it, not this one.

    Args:
        audio: The edited call.
        sr: Sample rate.
        clip_id: Named in the error, so a failing ingest says which file.
        expected_turns: How many turns the script declares.

    Returns:
        The seams, in stream order. Exactly `expected_turns - 1` of them.

    Raises:
        SeamError: a seam is below `MIN_SEAM_MS`, or a pause reached it.
    """
    final_ms = _final_speech_ms(audio, sr)
    interior = [
        (start, end) for start, end in silent_runs(audio, sr) if end <= final_ms
    ]
    seams = tuple((s, e) for s, e in interior if e - s >= MIN_SEAM_MS)
    wanted = expected_turns - 1
    if len(seams) == wanted:
        return seams

    if len(seams) < wanted:
        candidates = sorted(
            ((e - s, s, e) for s, e in interior if e - s < MIN_SEAM_MS), reverse=True
        )
        listed = ", ".join(f"{s}-{e} ms ({d} ms)" for d, s, e in candidates[:5])
        msg = (
            f"{clip_id}: {len(seams)} seam(s) of at least {MIN_SEAM_MS} ms, but "
            f"the script declares {expected_turns} turns and so needs {wanted}. "
            f"Longest sub-threshold silences: {listed or 'none'}. "
            f"conservative's max_turn_silence is 3600 ms, so a seam shorter than "
            f"that ends the turn on some arms and not others, and the arms would "
            f"then be measured on different gap counts (ADR-030)."
        )
        raise SeamError(msg)

    listed = ", ".join(f"{s}-{e} ms ({e - s} ms)" for s, e in seams[:8])
    msg = (
        f"{clip_id}: {len(seams)} silences of at least {MIN_SEAM_MS} ms, but the "
        f"script declares {expected_turns} turns and so expects {wanted}. An "
        f"intra-turn pause reached seam length and will end the turn on every "
        f"arm, so this call has more turns than the script says and its gap "
        f"budget (`gaps = words - turns`) is void. Silences: {listed}. "
        f"docs/TRACK_C_SCRIPT.md §6.2 asks for 1000-2500 ms pauses; retake "
        f"rather than relabel."
    )
    raise SeamError(msg)


def promote_word_gaps(
    words: Sequence[TruthWord], *, described: Sequence[Gap]
) -> tuple[Gap, ...]:
    """Every transcript inter-word gap as a `Gap`. Pure. `O(w·g)`. ADR-034.

    Labelled by the standing rule — `fragment` because the speaker is
    mid-utterance by construction, `ambiguous` because a natural pause can fall
    where a clause ends and this rule cannot tell. **Every transcript-derived
    gap is `ambiguous`, without exception**: the rule that produced the label
    cannot distinguish the two cases, and `Gap.certainty` exists to carry that.

    Deduplicated against gaps already described, exactly as
    `corpus._intrinsic_gaps` does, so a seam or an utterance end keeps its own
    label and is never shadowed by a promoted one.

    The service supplies **geometry** here and nothing else. `preceding` and
    `certainty` come from this rule, and letting either take a value from
    `end_of_turn`, `end_of_turn_confidence` or any model judgement about
    completeness would breach ADR-017 — which ADR-031 declined to license in
    advance and ADR-034 declines again.

    Args:
        words: Transcribed words in stream order.
        described: Gaps already in the sidecar.

    Returns:
        The promoted gaps, in stream order.
    """
    gaps: list[Gap] = []
    for before, after in itertools.pairwise(words):
        start, end = before.end_ms, after.start_ms
        if end <= start:
            continue
        # **Overlap, not containment of the start.** `regime_at` returns the
        # *first* gap covering a time, so a promoted gap that merely clips the
        # edge of a described one can still win the lookup and relabel it. That
        # is not hypothetical: measured on Track A, the transcript's last word
        # in `seed_seg0_pause_000` runs 5040-5120 while the acoustic
        # `final_word_end_ms` is 5024, so the promoted gap 4720-5040 overlaps
        # `utterance_end` by 16 ms. Containment-based dedup admitted it, it
        # sorted first, and the utterance-end boundary moved from the min gate
        # to the max gate — TTL p90 on `conservative` went 826 to 3596 ms.
        # A `fragment` label after the speaker has finished is simply wrong.
        if any(start <= g.end_ms and g.start_ms <= end for g in described):
            continue
        gaps.append(
            Gap(
                start_ms=start,
                end_ms=end,
                origin="transcript_interword",
                preceding="fragment",
                certainty="ambiguous",
                basis=(
                    "an inter-word silence measured by the transcriber; the "
                    "speaker is mid-utterance, but a natural pause can fall "
                    "where a clause ends and the standing rule cannot tell "
                    "(ADR-034)"
                ),
            )
        )
    return tuple(gaps)


def _turn_bounds(
    seams: Sequence[tuple[int, int]], *, final_ms: int
) -> tuple[tuple[int, int], ...]:
    """Turn spans as `(start_ms, end_of_speech_ms)`. Pure. `O(t)`."""
    starts = [0, *(end for _, end in seams)]
    ends = [*(start for start, _ in seams), final_ms]
    return tuple(zip(starts, ends, strict=True))


def build_clip(
    audio_path: Path,
    script: CallScript,
    words: Sequence[TruthWord],
    *,
    truth_path: Path,
) -> GeneratedClip:
    """Turn one recorded call into a scorable clip. `O(n + w)`.

    Args:
        audio_path: The edited caller-only `.wav`.
        script: The prompts and their declared classes.
        words: The transcription of this call.
        truth_path: Where the sidecar goes.

    Returns:
        The clip, with one `UtteranceSpan` per caller turn.

    Raises:
        SeamError: a seam is below `MIN_SEAM_MS`.
        ScriptError: the turn count disagrees with the script, or no words.
    """
    raw, sr = sf.read(audio_path, dtype="float32", always_2d=False)
    samples: Audio = np.asarray(raw, dtype=np.float32)
    if sr != SAMPLE_RATE:
        msg = f"{audio_path.name}: expected {SAMPLE_RATE} Hz, found {sr}"
        raise ScriptError(msg)
    if not words:
        msg = f"{audio_path.name}: no transcribed words, so there is no ground truth"
        raise ScriptError(msg)

    clip_id = audio_path.stem
    seams = check_seams(samples, sr, clip_id=clip_id, expected_turns=len(script.turns))
    final_ms = _final_speech_ms(samples, sr)
    total_ms = round(len(samples) * 1000 / sr)
    bounds = _turn_bounds(seams, final_ms=final_ms)

    # The seams first, so promotion dedupes against them and a seam keeps the
    # `complete` label: the caller has finished the answer, which is what the
    # cut-out agent prompt is responding to.
    described: list[Gap] = [
        Gap(
            start_ms=start,
            end_ms=end,
            origin="turn_seam",
            preceding="complete",
            certainty="certain",
            basis=(
                "the agent's prompt was cut from here; the caller had finished "
                "the answer, so the semantic gate governs (ADR-030)"
            ),
        )
        for start, end in seams
    ]
    described.append(
        Gap(
            start_ms=final_ms,
            end_ms=total_ms,
            origin="utterance_end",
            preceding="complete",
            certainty="certain",
            basis="the speaker has finished; the semantic gate fires",
        )
    )
    described.extend(promote_word_gaps(words, described=described))
    described.sort(key=lambda g: g.start_ms)

    utterances = tuple(
        UtteranceSpan(
            start_ms=start,
            final_word_end_ms=end,
            expected_answer=turn.expected_answer,
        )
        for (start, end), turn in zip(bounds, script.turns, strict=True)
    )
    truth = TruthSpan(
        start_ms=0,
        end_ms=total_ms,
        gaps=tuple(described),
        words=tuple(words),
    )
    truth_path.write_text(truth.model_dump_json(indent=1))
    return GeneratedClip(
        clip_id=clip_id,
        audio_path=audio_path,
        truth_path=truth_path,
        sha256=hashlib.sha256(audio_path.read_bytes()).hexdigest(),
        final_word_end_ms=final_ms,
        total_ms=total_ms,
        truth=truth,
        utterances=utterances,
    )


def load_script(path: Path) -> CallScript:
    """Read and validate one call script.

    Args:
        path: The script JSON.

    Returns:
        The validated script.
    """
    return CallScript.model_validate_json(path.read_text())


def build(
    pairs: Sequence[tuple[Path, CallScript]],
    words_by_clip: dict[str, tuple[TruthWord, ...]],
    *,
    out: Path,
) -> BuiltCorpus:
    """Ingest every recorded call into a `BuiltCorpus`. `O(calls · n)`.

    Args:
        pairs: Each call's audio and its script.
        words_by_clip: Transcribed words per `clip_id`.
        out: Output directory, holding `corpus.json` and the sidecars.

    Returns:
        The corpus, identified by a hash over every clip.

    Raises:
        ScriptError: a clip has fewer than two turns, so it is not a call.
    """
    from nod_bench.perturb import GENERATOR_VERSION

    out.mkdir(parents=True, exist_ok=True)
    clips: list[GeneratedClip] = []
    source = hashlib.sha256()
    for audio_path, script in pairs:
        clip = build_clip(
            audio_path,
            script,
            words_by_clip.get(audio_path.stem, ()),
            truth_path=out / f"{audio_path.stem}.truth.json",
        )
        # A single-utterance clip is Track A's shape and cannot warm the
        # profiler (ADR-022: 0 of 120 reach MIN_GAPS_FOR_WARM). Refused here
        # rather than discovered as a flat chart.
        if len(clip.utterances) < 2:
            msg = (
                f"{clip.clip_id}: {len(clip.utterances)} utterance(s). A Track C "
                f"clip is a multi-turn call; one utterance is Track A's shape "
                f"and leaves the profiler cold for the whole recording."
            )
            raise ScriptError(msg)
        source.update(clip.sha256.encode())
        clips.append(clip)

    corpus_hash = hashlib.sha256()
    for clip in clips:
        corpus_hash.update(clip.sha256.encode())
    built = BuiltCorpus(
        corpus_id="trackC",
        corpus_sha256=corpus_hash.hexdigest(),
        generator_version=GENERATOR_VERSION,
        seed=0,
        source_sha256=source.hexdigest(),
        clips=clips,
    )
    (out / "corpus.json").write_text(built.model_dump_json(indent=1))
    return built


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Track C ingestion CLI.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit code. 1 on a seam or script failure, 2 on a missing key.
    """
    import os

    from nod_bench.transcribe import transcribe_clip

    parser = argparse.ArgumentParser(prog="python -m nod_bench.trackc")
    parser.add_argument("command", choices=("build", "check"))
    parser.add_argument("--audio", type=Path, nargs="+", required=True)
    parser.add_argument("--script", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, default=Path("data/corpus/trackC"))
    parser.add_argument("--model", default="universal-streaming-english")
    parser.add_argument("--words", type=Path, default=None)
    args = parser.parse_args(argv)

    if len(args.audio) != len(args.script):
        sys.stderr.write("--audio and --script must pair one to one\n")
        return 1
    pairs = [(a, load_script(s)) for a, s in zip(args.audio, args.script, strict=True)]

    if args.command == "check":
        for audio_path, script in pairs:
            raw, sr = sf.read(audio_path, dtype="float32", always_2d=False)
            try:
                seams = check_seams(
                    np.asarray(raw, dtype=np.float32),
                    sr,
                    clip_id=audio_path.stem,
                    expected_turns=len(script.turns),
                )
            except (SeamError, ScriptError) as exc:
                sys.stderr.write(f"{exc}\n")
                return 1
            sys.stdout.write(f"{audio_path.stem}: {len(seams)} seams, all clear\n")
        return 0

    words: dict[str, tuple[TruthWord, ...]] = {}
    if args.words and args.words.exists():
        raw_words = json.loads(args.words.read_text())
        words = {
            k: tuple(TruthWord.model_validate(w) for w in v)
            for k, v in raw_words.items()
        }
    else:
        key = os.environ.get("ASSEMBLYAI_API_KEY", "")
        if not key:
            sys.stderr.write(
                "ASSEMBLYAI_API_KEY is not set and --words was not given\n"
            )
            return 2
        for audio_path, _ in pairs:
            words[audio_path.stem] = asyncio.run(
                transcribe_clip(audio_path, api_key=key, model=args.model)
            )

    try:
        built = build(pairs, words, out=args.out)
    except (SeamError, ScriptError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    turns = sum(len(c.utterances) for c in built.clips)
    promoted = sum(
        1
        for c in built.clips
        for g in c.truth.gaps
        if g.origin == "transcript_interword"
    )
    sys.stdout.write(
        f"ingested {len(built.clips)} calls, {turns} turns, "
        f"{promoted} promoted gaps -> {args.out}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
