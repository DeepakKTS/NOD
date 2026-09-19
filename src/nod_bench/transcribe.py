"""Transcription pass that fills a corpus sidecar's word timings (ADR-031).

The sidecar's `words` come from the real service, not from acoustic silence
detection. The pilot recorded in ADR-031 measured why: `_intrinsic_gaps` found
0 of 21 inter-word gaps on connected speech and 4 of 49 on the committed Track A
source, because `INTRINSIC_FLOOR_DBFS` at -44 dBFS is calibrated for
generator-inserted digital silence and the intervals between words in real
speech never get that quiet.

**What moves and what does not.** Word timings become service-derived. Regime
labelling — `Gap.preceding` and `Gap.certainty`, the axis PCR scores and the axis
ADR-017 protects — stays generator-owned, from the insertion record or from
`_intrinsic_gaps`'s standing `fragment`/`ambiguous` rule. Nothing in this module
writes a `Gap`.

The audio is never touched. `apply_words` rewrites sidecars and `corpus.json`
only, and verifies every clip's recorded `sha256` still matches the `.wav` on
disk before it writes anything, so "only sidecars move" is asserted rather than
asserted-in-prose.

CLI: `python -m nod_bench.transcribe --corpus data/corpus/trackA`
Needs `ASSEMBLYAI_API_KEY`. `make bench` does not: the sidecars are committed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import numpy as np
import soundfile as sf

from nod_bench.corpus import BuiltCorpus
from nod_bench.perturb import TruthWord

SAMPLE_RATE: Final = 16000
"""Corpus audio is mono 16 kHz PCM16 throughout."""

FRAME_MS: Final = 50
"""Frame size sent upstream. Milliseconds."""

CONCURRENCY: Final = 1
"""Simultaneous upstream sessions during a corpus pass.

One. Four was tried first and the service answered `1008 policy violation` part
way through a 120-clip pass, so the account's concurrent-session limit is the
binding constraint rather than throughput. A pass is therefore serial and takes
about fifteen minutes, which is a build step run once per corpus.
"""

RETRIES: Final = 5
"""Attempts per clip before a pass gives up."""

COOLDOWN_S: Final = 3.0
"""Pause between clips in a corpus pass. Seconds.

The service answered `1008 Unauthorized Connection: Too many concurrent
sessions` on the sixth consecutive clip at concurrency 1, so a closed socket is
not immediately a released session. This is the gap that makes a serial pass
serial from the *service's* point of view and not just ours.
"""

RETRY_BACKOFF_S: Final = 5.0
"""Base delay between attempts, doubled each time. Seconds.

A corpus pass is long and all-or-nothing by design — `apply_words` writes only
after every clip has returned — so one transient socket error would otherwise
discard a quarter of an hour of work.
"""

TAIL_MS: Final = 1200
"""Silence appended before terminating, so the last word finalises. Milliseconds.

Unrelated to `corpus.TAIL_SILENCE_MS`, which sizes a gate; this only has to
outlast the transcriber's own flush, and the clips already carry their own tail.
"""


class TranscriptionError(RuntimeError):
    """A clip could not be transcribed, so the corpus pass cannot be completed."""


class CorpusDriftedError(RuntimeError):
    """A `.wav` no longer matches the hash `corpus.json` recorded for it."""


def _pcm(path: Path) -> tuple[bytes, int]:
    """Read a clip as PCM16 bytes and its length in whole frames. `O(n)`."""
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    samples = np.asarray(audio, dtype=np.float32)
    if sr != SAMPLE_RATE:
        msg = f"{path.name}: expected {SAMPLE_RATE} Hz, found {sr}"
        raise TranscriptionError(msg)
    tail = np.zeros(SAMPLE_RATE * TAIL_MS // 1000, dtype=np.float32)
    joined = np.concatenate((samples, tail))
    return (np.clip(joined, -1.0, 1.0) * 32767).astype("<i2").tobytes(), sr


async def transcribe_clip(
    path: Path, *, api_key: str, model: str
) -> tuple[TruthWord, ...]:
    """Transcribe one clip and return its finalised words. `O(frames)`.

    Fed unpaced. Word timings are stream-relative and were measured identical at
    1x, 4x and unpaced on a corpus clip, so real-time pacing would add ten
    minutes to a corpus pass and change no number. Pacing is what the *live
    bench* needs, for boundary timing; this pass reads timings only.

    Args:
        path: The clip's `.wav`.
        api_key: Server-side credential (INV-5).
        model: `speech_model` to transcribe against.

    Returns:
        Every finalised word, in stream order.

    Raises:
        TranscriptionError: the clip yielded no words.
    """
    from nod_adapters.assemblyai.session import AssemblyAISession
    from nod_core.types import Turn

    pcm, _ = _pcm(path)
    frame_bytes = SAMPLE_RATE * FRAME_MS // 1000 * 2
    frames = [pcm[i : i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]

    words: list[TruthWord] = []
    session = AssemblyAISession(
        api_key=api_key,
        model=model,
        sample_rate=SAMPLE_RATE,
        config={"min_turn_silence": 800, "max_turn_silence": 3600},
    )
    async with session:

        async def pump() -> None:
            for frame in frames:
                await session.send_audio(frame)
            await session.terminate()

        task = asyncio.create_task(pump())
        async for event in session.events():
            if isinstance(event, Turn) and event.end_of_turn:
                words.extend(
                    TruthWord(start_ms=w.start_ms, end_ms=w.end_ms, text=w.text)
                    for w in event.words
                    if w.is_final
                )
        await task

    if not words:
        msg = f"{path.name}: the transcriber returned no words"
        raise TranscriptionError(msg)
    return tuple(words)


async def transcribe_corpus(
    corpus: BuiltCorpus, *, api_key: str, model: str, concurrency: int = CONCURRENCY
) -> dict[str, tuple[TruthWord, ...]]:
    """Transcribe every clip in a corpus. `O(clips)` sessions, bounded fan-out.

    Args:
        corpus: The built corpus to transcribe.
        api_key: Server-side credential.
        model: `speech_model`.
        concurrency: Simultaneous upstream sessions.

    Returns:
        Words per `clip_id`.
    """
    limit = asyncio.Semaphore(concurrency)
    out: dict[str, tuple[TruthWord, ...]] = {}

    async def one(clip_id: str, path: Path) -> None:
        async with limit:
            for attempt in range(RETRIES):
                try:
                    out[clip_id] = await transcribe_clip(
                        path, api_key=api_key, model=model
                    )
                except Exception:  # a build-time network pass; see RETRIES
                    if attempt == RETRIES - 1:
                        raise
                    await asyncio.sleep(RETRY_BACKOFF_S * (2**attempt))
                else:
                    await asyncio.sleep(COOLDOWN_S)
                    return

    await asyncio.gather(*(one(c.clip_id, c.audio_path) for c in corpus.clips))
    return out


def apply_words(
    corpus_dir: Path, words_by_clip: Mapping[str, Sequence[TruthWord]]
) -> int:
    """Write transcribed words into the sidecars and `corpus.json`. `O(clips)`.

    Audio is not touched. Every clip's recorded `sha256` is checked against the
    `.wav` on disk *before* anything is written, so a corpus whose audio drifted
    fails loudly instead of having new ground truth attached to the wrong bytes.

    Args:
        corpus_dir: Directory holding `corpus.json` and the sidecars.
        words_by_clip: Words per `clip_id`.

    Returns:
        The number of clips updated.

    Raises:
        CorpusDriftedError: a `.wav` no longer matches its recorded hash.
        TranscriptionError: a clip in the corpus has no words supplied.
    """
    corpus = BuiltCorpus.model_validate_json((corpus_dir / "corpus.json").read_text())

    for clip in corpus.clips:
        digest = hashlib.sha256(clip.audio_path.read_bytes()).hexdigest()
        if digest != clip.sha256:
            msg = (
                f"{clip.clip_id}: audio hash {digest[:12]} does not match the "
                f"{clip.sha256[:12]} recorded in corpus.json. Transcribing this "
                f"corpus would attach new ground truth to changed bytes."
            )
            raise CorpusDriftedError(msg)
        if clip.clip_id not in words_by_clip:
            msg = f"{clip.clip_id}: no words supplied for a clip in the corpus"
            raise TranscriptionError(msg)

    updated: list[object] = []
    for clip in corpus.clips:
        words = tuple(words_by_clip[clip.clip_id])
        truth = clip.truth.model_copy(update={"words": words})
        clip.truth_path.write_text(truth.model_dump_json(indent=1))
        updated.append(clip.model_copy(update={"truth": truth}))

    rebuilt = corpus.model_copy(update={"clips": tuple(updated)})
    (corpus_dir / "corpus.json").write_text(rebuilt.model_dump_json(indent=1))
    return len(updated)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the transcription CLI.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--model", default="universal-streaming-english")
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = parser.parse_args(argv)

    import os

    key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not key:
        sys.stderr.write(
            "ASSEMBLYAI_API_KEY is not set; this pass needs the service.\n"
        )
        return 2

    corpus = BuiltCorpus.model_validate_json((args.corpus / "corpus.json").read_text())
    words = asyncio.run(
        transcribe_corpus(
            corpus, api_key=key, model=args.model, concurrency=args.concurrency
        )
    )
    count = apply_words(args.corpus, words)
    total = sum(len(w) for w in words.values())
    sys.stdout.write(f"transcribed {count} clips, {total} words\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
