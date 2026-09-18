"""Corpus manifests and the Track A generator.

BENCH_SPEC.md §2. Source clips are CC-licensed read speech listed in a manifest
with license fields; Track B ships a download script and a manifest, never audio.

CLI: `python -m nod_bench.corpus build --seed 7 --out data/corpus/trackA`
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final, Literal

import numpy as np
import soundfile as sf
from pydantic import BaseModel, ConfigDict

from nod_bench.perturb import (
    BURST_SWEEP,
    CORRECT_TEMPLATES,
    GENERATOR_VERSION,
    NOISE_SNR_DB_SWEEP,
    PAUSE_SWEEP_MS,
    PROLONG_SWEEP,
    REPEAT_SWEEP,
    Audio,
    Gap,
    PerturbationKind,
    TruthSpan,
    apply,
)

TAIL_SILENCE_MS: Final = 4200
"""Silence appended after the utterance so every arm's gate can elapse.

Must exceed the widest `max_turn_silence` under test (`conservative`, 3600 ms)
plus room for the endpoint overhead, or the conservative arm would never end its
turn and the comparison would measure the clip length instead of the arm.
"""

SPEECH_FLOOR_DBFS: Final = -45.0
"""Level above which a frame counts as speech when locating the utterance end."""


def sweep() -> Iterator[tuple[PerturbationKind, dict[str, float]]]:
    """Every (kind, params) pair in the declared sweeps. Pure. `O(1)`.

    BENCH_SPEC §2's sweeps, in a fixed order so a corpus is reproducible.
    `at_ms` is a fraction of the source clip rather than an absolute offset, so
    the same sweep applies to source clips of different lengths.
    """
    for len_ms in PAUSE_SWEEP_MS:
        yield "pause", {"at_frac": 0.5, "len_ms": float(len_ms)}
    for times in REPEAT_SWEEP:
        yield "repeat", {"at_frac": 0.4, "times": float(times)}
    for factor in PROLONG_SWEEP:
        yield "prolong", {"at_frac": 0.4, "factor": factor}
    for template in range(len(CORRECT_TEMPLATES)):
        yield "correct", {"at_frac": 0.5, "template": float(template)}
    for bursts in BURST_SWEEP:
        yield "burst", {"bursts": float(bursts)}
    for snr in NOISE_SNR_DB_SWEEP:
        yield "noise", {"snr_db": float(snr)}


class SourceClip(BaseModel):
    """One licensed source clip (BENCH_SPEC.md §2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus: str
    license: str
    file: Path
    sha256: str


class CorpusManifest(BaseModel):
    """The manifest for one track.

    A corpus file that changed since the manifest fails the run loudly (EC-39).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    track: str
    clips: Sequence[SourceClip]


class GeneratedClip(BaseModel):
    """One generated clip and the ground truth needed to score it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clip_id: str
    audio_path: Path
    truth_path: Path
    sha256: str
    final_word_end_ms: int
    """When speech actually stops. PCR and TTL are both measured against this."""

    total_ms: int
    truth: TruthSpan


class BuiltCorpus(BaseModel):
    """A generated corpus, identified so a run manifest can cite it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_id: str
    corpus_sha256: str
    """Hash over every generated clip's hash, in order. Identifies the corpus as
    a whole, so a manifest citing it pins the exact bytes that were scored."""

    generator_version: str
    seed: int
    source_sha256: str
    clips: Sequence[GeneratedClip]


MIN_INTRINSIC_GAP_MS: Final = 100
"""Shortest source-intrinsic silence recorded in a sidecar. Milliseconds.

Below the smallest `min_turn_silence` any arm uses (160 ms, `aggressive`), so
every silence that could possibly bind a gate is described.
"""

INTRINSIC_FLOOR_DBFS: Final = -44.0
"""The simulator's silence floor at the default `vad_threshold` of 0.4."""


def _intrinsic_gaps(
    audio: Audio, sr: int, *, final_ms: int, described: Sequence[Gap]
) -> tuple[Gap, ...]:
    """Silences already in the source speech, which the generator did not make.

    Without these the sidecar describes only the gaps *we* inserted, and
    `regime_at` falls back to `complete` for every natural inter-word pause — so
    a pause in the middle of an utterance is scored as if the speaker had
    finished, and `min_turn_silence` governs where `max_turn_silence` should.
    Measured on this corpus that inflated PCR badly: a 250 ms pause inside one
    seed segment fired a boundary on the `aggressive` arm, whose minimum is
    160 ms, and it was counted as a premature cutoff of an utterance the speaker
    was still in the middle of.

    Labelled `fragment`, because the speaker is by construction mid-utterance,
    and `ambiguous`, for the same reason `pause` is: a natural pause can fall
    where a clause ends, and this function cannot tell.

    `O(n)`.
    """
    frame = sr * 50 // 1000
    found: list[tuple[int, int]] = []
    run_start: int | None = None
    for index in range(0, len(audio) - frame + 1, frame):
        block = audio[index : index + frame]
        rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
        quiet = rms <= 0.0 or 20 * np.log10(rms) <= INTRINSIC_FLOOR_DBFS
        at_ms = round(index * 1000 / sr)
        if quiet:
            run_start = at_ms if run_start is None else run_start
            continue
        if run_start is not None:
            found.append((run_start, at_ms))
            run_start = None
    if run_start is not None:
        found.append((run_start, round(len(audio) * 1000 / sr)))

    gaps: list[Gap] = []
    for start, end in found:
        if end - start < MIN_INTRINSIC_GAP_MS or start >= final_ms:
            continue
        if any(g.start_ms <= start <= g.end_ms for g in described):
            continue
        gaps.append(
            Gap(
                start_ms=start,
                end_ms=end,
                origin="source_intrinsic",
                preceding="fragment",
                certainty="ambiguous",
                basis=(
                    "a pause already present in the source speech; the speaker "
                    "is mid-utterance, but a natural pause can fall where a "
                    "clause ends and this is detected acoustically, not read"
                ),
            )
        )
    return tuple(gaps)


def _final_speech_ms(audio: Audio, sr: int) -> int:
    """Where speech stops, in ms. `O(n)`.

    Scanned from the end at frame granularity rather than taken from the
    perturbation's own arithmetic, so a bug in a perturbation cannot also write
    the ground truth that would have caught it.
    """
    frame = sr * 50 // 1000
    for start in range(len(audio) - frame, -1, -frame):
        block = audio[start : start + frame]
        if block.size == 0:
            continue
        rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
        if rms > 0.0 and 20 * np.log10(rms) > SPEECH_FLOOR_DBFS:
            return round((start + frame) * 1000 / sr)
    return 0


def load_manifest(path: Path) -> CorpusManifest:
    """Read and validate a corpus manifest.

    Args:
        path: Path to the manifest JSON.

    Returns:
        The validated manifest.
    """
    data = json.loads(path.read_text())
    return CorpusManifest.model_validate(data)


def build(manifest: CorpusManifest, *, seed: int, out: Path) -> BuiltCorpus:
    """Generate a perturbed corpus with `.truth.json` sidecars.

    Deterministic: the same `seed` and `GENERATOR_VERSION` produce byte-identical
    audio, asserted by a test that hashes the output. The seed is recorded in the
    run manifest.

    Args:
        manifest: The source clips.
        seed: Generator seed.
        out: Output directory.

    Returns:
        The generated corpus, identified by a hash over every clip.
    """
    out.mkdir(parents=True, exist_ok=True)
    clips: list[GeneratedClip] = []
    source_hash = hashlib.sha256()

    for source in manifest.clips:
        audio, sr = sf.read(source.file, dtype="float32", always_2d=False)
        samples: Audio = np.asarray(audio, dtype=np.float32)
        source_hash.update(source.sha256.encode())

        for index, (kind, params) in enumerate(sweep()):
            # One generator per clip, derived from the run seed and the clip's
            # position, so adding a source clip cannot shift the draws of the
            # ones before it.
            rng = np.random.default_rng([seed, index, len(clips)])
            call = dict(params)
            if "at_frac" in call:
                frac = call.pop("at_frac")
                call["at_ms"] = float(round(len(samples) * 1000 / sr * frac))
            perturbed, truth = apply(samples, sr, kind, rng=rng, **call)

            final_ms = _final_speech_ms(perturbed, sr)
            tail = np.zeros(sr * TAIL_SILENCE_MS // 1000, dtype=np.float32)
            clip: Audio = np.concatenate((perturbed, tail)).astype(np.float32)
            total_ms = round(len(clip) * 1000 / sr)

            # The trailing silence is the one gap the perturbations do not own:
            # it follows the finished utterance, so `min_turn_silence` governs.
            truth = truth.model_copy(
                update={
                    "end_ms": total_ms,
                    "gaps": (
                        *truth.gaps,
                        *_intrinsic_gaps(
                            perturbed, sr, final_ms=final_ms, described=truth.gaps
                        ),
                        Gap(
                            start_ms=final_ms,
                            end_ms=total_ms,
                            origin="utterance_end",
                            preceding="complete",
                            certainty="certain",
                            basis="the speaker has finished; the semantic gate fires",
                        ),
                    ),
                }
            )

            clip_id = f"{source.file.stem}_{kind}_{index:03d}"
            audio_path = out / f"{clip_id}.wav"
            truth_path = out / f"{clip_id}.truth.json"
            sf.write(audio_path, clip, sr, subtype="PCM_16")
            digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()
            truth_path.write_text(truth.model_dump_json(indent=1))

            clips.append(
                GeneratedClip(
                    clip_id=clip_id,
                    audio_path=audio_path,
                    truth_path=truth_path,
                    sha256=digest,
                    final_word_end_ms=final_ms,
                    total_ms=total_ms,
                    truth=truth,
                )
            )

    corpus_hash = hashlib.sha256()
    for generated in clips:
        corpus_hash.update(generated.sha256.encode())

    built = BuiltCorpus(
        corpus_id=manifest.track,
        corpus_sha256=corpus_hash.hexdigest(),
        generator_version=GENERATOR_VERSION,
        seed=seed,
        source_sha256=source_hash.hexdigest(),
        clips=clips,
    )
    (out / "corpus.json").write_text(built.model_dump_json(indent=1))
    return built


def main(argv: Sequence[str] | None = None) -> int:
    """Run the corpus CLI.

    Args:
        argv: Arguments, defaulting to `sys.argv[1:]`.

    Returns:
        Process exit code.
    """
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
