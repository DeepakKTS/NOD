"""Controlled perturbations with exact ground truth.

BENCH_SPEC.md §2: because the harness inserts the pause, ground truth is exact
rather than judged. The generator is seeded and deterministic — the same seed and
`GENERATOR_VERSION` produce byte-identical audio.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Final, Literal, assert_never

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

from nod_core.types import ExpectedAnswer

GENERATOR_VERSION: Final = "0.1.0"
"""Hashed into corpus determinism and recorded in every `.truth.json` sidecar.

BENCH_SPEC.md §2's sidecar sample shows `1.2.0`; that is illustrative JSON, and
starting at a version that never ran would misreport a rebuild.
"""

PAUSE_SWEEP_MS: Final = tuple(range(200, 3001, 200))
"""`pause`: inserted silence, 200 to 3000 ms in 200 ms steps."""

REPEAT_SWEEP: Final = (1, 2, 3)
"""`repeat`: repetitions of the duplicated word."""

PROLONG_SWEEP: Final = (1.5, 2.5, 4.0)
"""`prolong`: time-stretch factors, no pitch shift."""

BURST_SWEEP: Final = (2, 3, 4)
"""`burst`: number of bursts one utterance is split into."""

NOISE_SNR_DB_SWEEP: Final = (30, 20, 12)
"""`noise`: stationary background SNR. Decibels."""

type PerturbationKind = Literal[
    "pause", "repeat", "prolong", "correct", "burst", "noise"
]

type Audio = NDArray[np.float32]

REPEAT_WORD_MS: Final = 320
"""`repeat`: how much audio at `at_ms` counts as "the word". Milliseconds."""

REPEAT_GAP_MS: Final = 120
"""`repeat`: silence between copies. Short enough to sit under every arm's
`min_turn_silence`, so a disfluency is not silently also a pause probe."""

PROLONG_SEGMENT_MS: Final = 240
"""`prolong`: length of the segment stretched. Milliseconds."""

CORRECT_TEMPLATES: Final = (
    (260, 400, 0),
    (320, 560, 180),
    (180, 800, 0),
)
"""`correct`: `(gap before the restart, restarted length, gap after)`. Milliseconds.

Three structural templates, not three scripts. BENCH_SPEC §2 illustrates this
perturbation as splicing words ("change my, no, cancel my"), but `apply`'s
contract is that no perturbation changes speech content, only its timing, so a
correction here is a **restart**: the speaker breaks off and re-utters what they
just said. That is built from the clip's own audio and introduces no vocabulary
the source did not have.
"""

BURST_GAP_MS_RANGE: Final = (200, 900)
"""`burst`: uneven gap lengths are drawn from this closed range. Milliseconds."""

BURST_EDGE_MS: Final = 300
"""`burst`: no split within this distance of either end, so every burst has audio."""


type Regime = Literal["complete", "fragment"]
"""Which endpointing gate governs the silence that follows (ADR-001, EC-50).

After a **complete** utterance the model's semantic gate fires and
`min_turn_silence` decides when. After a **fragment** it keeps waiting and
`max_turn_silence` is the only thing that ends the turn.
"""

type Certainty = Literal["certain", "ambiguous"]
"""Whether `Gap.preceding` is also true semantically, not just structurally."""


class Gap(BaseModel):
    """One silence in a generated clip, and which gate governs it.

    **`preceding` is the field `FakeAssemblyAI` reads** to choose a gate
    (ADR-017). It is the reason this model exists: the simulator cannot judge
    semantic completeness from audio, and must not, because a fake that guessed
    would make the benchmark measure the guess.

    `preceding` is grounded in *construction*, not in syntax: `fragment` means
    the generator cut inside a source utterance, `complete` means it cut at the
    end of one. That is a proxy for what the real model does, which is judge the
    transcript semantically, and the two can disagree — a cut at a word boundary
    can land exactly where a clause happens to end. `certainty` marks where that
    divergence is possible, so a consumer can report metrics with and without
    the doubtful gaps instead of inheriting a convention it cannot see.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: int
    end_ms: int
    origin: str
    """The perturbation that created this silence, or `utterance_end`."""

    preceding: Regime
    certainty: Certainty
    basis: str
    """Why this gap carries this label, in one phrase, for a human reader."""


class TruthWord(BaseModel):
    """One transcribed word in a `.truth.json` sidecar (ADR-031).

    Timings are **service-derived**: the real transcriber measures where each
    word starts and ends far better than an energy threshold does, and asking
    what the words were and when is not asking what the right answer is. Regime
    labelling — the axis PCR scores, and the axis ADR-017 protects — stays
    generator-owned on `Gap`, and nothing here touches it.

    Timings arrive on an 80 ms grid; that is the service's resolution, not a
    rounding this model applies.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: int
    end_ms: int
    text: str


class TruthSpan(BaseModel):
    """One utterance in a `.truth.json` sidecar (BENCH_SPEC.md §2).

    `gaps` is an addition for ADR-017: PCR and FRAG need utterance boundaries,
    but the simulator needs every *silence* and its regime, which the utterance
    list alone does not carry — a `burst` puts two or three of them inside a
    single utterance.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: int
    end_ms: int
    perturbation: Mapping[str, str | int | float] = {}
    """What the generator did to this span. Empty for unperturbed material.

    Defaulted for Track C, which is recorded or synthesised speech that the
    perturbation generator never touched. Writing `{"type": "recorded"}` there
    was rejected: it would put a perturbation that does not exist into ground
    truth, and `report` breaks figures down by `perturbation["type"]`.
    """

    gaps: tuple[Gap, ...] = ()
    text: str = ""
    """Filled by `corpus.build` from the source manifest.

    Defaulted because a perturbation function is handed samples, not a
    transcript, and inventing one here would put a guess into ground truth.
    """

    words: tuple[TruthWord, ...] = ()
    """Transcribed word timings, filled by `corpus.transcribe` (ADR-031).

    Empty until the transcription pass has run. `replay` refuses to run a
    controlled arm against a clip with no words rather than falling back to
    reconstructing them from gaps: the fallback is what ADR-031 removed, and a
    silent one would put the old behaviour back under the new name.
    """


class UtteranceSpan(BaseModel):
    """One caller turn inside a multi-utterance clip (ADR-034, Track C).

    **Track A clips hold exactly one utterance and Track C clips hold ten to
    twelve**, which is the contract mismatch this model resolves.
    `GeneratedClip` carries a single `final_word_end_ms`, and TTL measured from
    one utterance end per *call* rather than per *turn* would be meaningless.

    `expected_answer` lives here, per turn, and not on the run. CONTROL_SPEC §3
    declares it as what the host expects of the *dialogue state*, which changes
    every turn; `docs/TRACK_C_SCRIPT.md` declares one per prompt. Passing a
    single value for a whole call would leave the context axis with one input
    for eleven turns, which is the degenerate case ADR-029 warns about.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: int
    final_word_end_ms: int
    """When speech stops in this turn. PCR and TTL are both measured from it."""

    expected_answer: ExpectedAnswer | None = None
    """The class declared for the *prompt* that drew this turn (ADR-029).

    Judged from the prompt and never from the answer. `None` means undeclared
    and yields the policy default, which is Track A's case.
    """


def _samples(ms: float, sr: int) -> int:
    """Milliseconds to whole samples. `O(1)`."""
    return round(ms * sr / 1000.0)


def _ms(samples: int, sr: int) -> int:
    """Whole samples to milliseconds. `O(1)`."""
    return round(samples * 1000.0 / sr)


def _silence(ms: float, sr: int) -> Audio:
    """Digital silence. `O(n)`."""
    return np.zeros(_samples(ms, sr), dtype=np.float32)


def _join(*parts: Audio) -> Audio:
    """Concatenate, preserving float32. `O(n)`."""
    return np.concatenate(parts).astype(np.float32, copy=False)


def _require_inside(audio: Audio, sr: int, at_ms: int) -> int:
    """Validate a split point and return it in samples. `O(1)`.

    Raises:
        ValueError: The point is not strictly inside the audio.
    """
    cut = _samples(at_ms, sr)
    if not 0 < cut < len(audio):
        msg = f"at_ms={at_ms} is not inside a clip of {_ms(len(audio), sr)} ms"
        raise ValueError(msg)
    return cut


def pause(audio: Audio, sr: int, *, at_ms: int, len_ms: int) -> tuple[Audio, TruthSpan]:
    """Insert silence at a word boundary mid-utterance.

    Args:
        audio: Source samples.
        sr: Sample rate.
        at_ms: Word boundary to split at.
        len_ms: Silence to insert.

    Returns:
        The perturbed audio and its truth span.
    """
    cut = _require_inside(audio, sr, at_ms)
    out = _join(audio[:cut], _silence(len_ms, sr), audio[cut:])
    gap = Gap(
        start_ms=at_ms,
        end_ms=at_ms + len_ms,
        origin="pause",
        preceding="fragment",
        certainty="ambiguous",
        basis=(
            "cut at a word boundary inside one source utterance; structurally a "
            "fragment, but a word boundary can coincide with a clause end, which "
            "the model would read as complete"
        ),
    )
    return out, TruthSpan(
        start_ms=0,
        end_ms=_ms(len(out), sr),
        perturbation={"type": "pause", "at_ms": at_ms, "len_ms": len_ms},
        gaps=(gap,),
    )


def repeat(audio: Audio, sr: int, *, at_ms: int, times: int) -> tuple[Audio, TruthSpan]:
    """Duplicate a word with a natural short gap.

    Args:
        audio: Source samples.
        sr: Sample rate.
        at_ms: Start of the word to duplicate.
        times: Repetitions.

    Returns:
        The perturbed audio and its truth span.
    """
    cut = _require_inside(audio, sr, at_ms)
    word = audio[cut : cut + _samples(REPEAT_WORD_MS, sr)]
    if len(word) == 0:
        msg = f"at_ms={at_ms} leaves no audio to duplicate"
        raise ValueError(msg)

    parts: list[Audio] = [audio[:cut]]
    gaps: list[Gap] = []
    cursor = at_ms
    for _ in range(times):
        parts.extend((word, _silence(REPEAT_GAP_MS, sr)))
        spoken = _ms(len(word), sr)
        gaps.append(
            Gap(
                start_ms=cursor + spoken,
                end_ms=cursor + spoken + REPEAT_GAP_MS,
                origin="repeat",
                preceding="fragment",
                certainty="certain",
                basis=(
                    "a duplicated word mid-utterance; the speaker is audibly "
                    "still in the same utterance and the gap is shorter than "
                    "any arm's min_turn_silence"
                ),
            )
        )
        cursor += spoken + REPEAT_GAP_MS
    parts.append(audio[cut:])

    out = _join(*parts)
    return out, TruthSpan(
        start_ms=0,
        end_ms=_ms(len(out), sr),
        perturbation={"type": "repeat", "at_ms": at_ms, "times": times},
        gaps=tuple(gaps),
    )


def prolong(
    audio: Audio,
    sr: int,
    *,
    at_ms: int,
    factor: float,
) -> tuple[Audio, TruthSpan]:
    """Time-stretch a vowel segment without pitch shift.

    Args:
        audio: Source samples.
        sr: Sample rate.
        at_ms: Start of the segment to stretch.
        factor: Stretch factor.

    Returns:
        The perturbed audio and its truth span.
    """
    import librosa

    cut = _require_inside(audio, sr, at_ms)
    end = min(cut + _samples(PROLONG_SEGMENT_MS, sr), len(audio))
    segment = audio[cut:end]
    if len(segment) == 0:
        msg = f"at_ms={at_ms} leaves no audio to stretch"
        raise ValueError(msg)

    stretched = librosa.effects.time_stretch(segment, rate=1.0 / factor)
    out = _join(audio[:cut], stretched.astype(np.float32), audio[end:])
    return out, TruthSpan(
        start_ms=0,
        end_ms=_ms(len(out), sr),
        perturbation={"type": "prolong", "at_ms": at_ms, "factor": factor},
        gaps=(),
    )


def correct(
    audio: Audio,
    sr: int,
    *,
    at_ms: int,
    template: int,
) -> tuple[Audio, TruthSpan]:
    """Splice in a self-correction, for example "change my, no, cancel my".

    Args:
        audio: Source samples.
        sr: Sample rate.
        at_ms: Splice point.
        template: Which of the three templates to use.

    Returns:
        The perturbed audio and its truth span.
    """
    if not 0 <= template < len(CORRECT_TEMPLATES):
        msg = f"template={template} is not one of {len(CORRECT_TEMPLATES)}"
        raise ValueError(msg)
    pre_gap_ms, restart_ms, post_gap_ms = CORRECT_TEMPLATES[template]
    cut = _require_inside(audio, sr, at_ms)

    restart = audio[max(0, cut - _samples(restart_ms, sr)) : cut]
    basis = (
        "the speaker broke off and re-uttered; structurally a fragment, but the "
        "break can land after a clause the model would score as complete"
    )
    gaps = [
        Gap(
            start_ms=at_ms,
            end_ms=at_ms + pre_gap_ms,
            origin="correct",
            preceding="fragment",
            certainty="ambiguous",
            basis=basis,
        )
    ]
    parts: list[Audio] = [audio[:cut], _silence(pre_gap_ms, sr), restart]
    after = at_ms + pre_gap_ms + _ms(len(restart), sr)
    if post_gap_ms:
        parts.append(_silence(post_gap_ms, sr))
        gaps.append(
            Gap(
                start_ms=after,
                end_ms=after + post_gap_ms,
                origin="correct",
                preceding="fragment",
                certainty="ambiguous",
                basis=basis,
            )
        )
    parts.append(audio[cut:])

    out = _join(*parts)
    return out, TruthSpan(
        start_ms=0,
        end_ms=_ms(len(out), sr),
        perturbation={"type": "correct", "at_ms": at_ms, "template": template},
        gaps=tuple(gaps),
    )


def burst(
    audio: Audio,
    sr: int,
    *,
    bursts: int,
    rng: np.random.Generator,
) -> tuple[Audio, TruthSpan]:
    """Split one utterance into bursts with uneven gaps.

    Args:
        audio: Source samples.
        sr: Sample rate.
        bursts: Number of bursts, 2 to 4.
        rng: Seeded generator, so gap placement is reproducible.

    Returns:
        The perturbed audio and its truth span.
    """
    if not 2 <= bursts <= 4:
        msg = f"bursts={bursts} is outside 2 to 4"
        raise ValueError(msg)
    total_ms = _ms(len(audio), sr)
    if total_ms <= 2 * BURST_EDGE_MS:
        msg = f"a {total_ms} ms clip is too short to split into bursts"
        raise ValueError(msg)

    low, high = BURST_EDGE_MS, total_ms - BURST_EDGE_MS
    cuts = sorted(
        int(x) for x in rng.integers(low, high, size=bursts - 1, endpoint=False)
    )
    lengths = [
        int(x)
        for x in rng.integers(
            BURST_GAP_MS_RANGE[0], BURST_GAP_MS_RANGE[1], size=bursts - 1, endpoint=True
        )
    ]

    parts: list[Audio] = []
    gaps: list[Gap] = []
    previous, shift = 0, 0
    for cut_ms, gap_ms in zip(cuts, lengths, strict=True):
        parts.extend(
            (
                audio[_samples(previous, sr) : _samples(cut_ms, sr)],
                _silence(gap_ms, sr),
            )
        )
        gaps.append(
            Gap(
                start_ms=cut_ms + shift,
                end_ms=cut_ms + shift + gap_ms,
                origin="burst",
                preceding="fragment",
                certainty="ambiguous",
                basis=(
                    "an utterance split at a drawn offset; structurally a "
                    "fragment, but the offset is not aligned to clause "
                    "boundaries and may land on one"
                ),
            )
        )
        previous, shift = cut_ms, shift + gap_ms
    parts.append(audio[_samples(previous, sr) :])

    out = _join(*parts)
    return out, TruthSpan(
        start_ms=0,
        end_ms=_ms(len(out), sr),
        perturbation={"type": "burst", "bursts": bursts},
        gaps=tuple(gaps),
    )


def noise(
    audio: Audio,
    sr: int,
    *,
    snr_db: float,
    rng: np.random.Generator,
) -> tuple[Audio, TruthSpan]:
    """Add stationary background noise at a set signal-to-noise ratio.

    Emits **no gaps**, and that is a claim about this function only: it changes
    no timing, so it creates no silence and relabels nothing.

    It does, however, undermine gap labels made by *other* perturbations, which
    is why it must be composed carefully rather than layered freely. A `Gap` is
    grounded in construction — where the generator cut — and remains true no
    matter how loud the bed is. What noise breaks is the *bridge* from that
    label to the model's behaviour: `Gap.preceding` predicts which gate binds
    only while the model still perceives the silence as silence. A noise bed at
    or above the VAD floor is not silence to the endpointer, so the gate never
    engages and the label predicts a boundary that cannot happen.

    That failure is a property of the pair `(snr_db, vad_threshold)`, not of the
    gap, so it is deliberately **not** folded into `Gap.certainty`: marking gaps
    ambiguous here would conflate "we cut somewhere a clause might end" with
    "the bed is louder than the floor", which have different causes and
    different fixes. `NOISE_SNR_DB_SWEEP` stays at 30/20/12 dB, all well above
    the silence floor, so the bed stays below it — but a future sweep that goes
    lower must re-derive the labels rather than inherit them.

    Args:
        audio: Source samples.
        sr: Sample rate.
        snr_db: Target SNR in decibels.
        rng: Seeded generator.

    Returns:
        The perturbed audio and its truth span. `gaps` is always empty.
    """
    if len(audio) == 0:
        msg = "cannot add noise to an empty clip"
        raise ValueError(msg)
    signal_power = float(np.mean(audio.astype(np.float64) ** 2))
    if signal_power <= 0.0:
        msg = "cannot set an SNR against silence"
        raise ValueError(msg)

    target_power = signal_power / (10.0 ** (snr_db / 10.0))
    bed = rng.standard_normal(len(audio))
    bed *= math.sqrt(target_power / float(np.mean(bed**2)))
    out = (audio.astype(np.float64) + bed).astype(np.float32)

    return out, TruthSpan(
        start_ms=0,
        end_ms=_ms(len(out), sr),
        perturbation={"type": "noise", "snr_db": snr_db},
        gaps=(),
    )


def apply(
    audio: Audio,
    sr: int,
    kind: PerturbationKind,
    *,
    rng: np.random.Generator,
    **params: float,
) -> tuple[Audio, TruthSpan]:
    """Dispatch to one perturbation.

    No perturbation changes total speech content; only its timing.

    Args:
        audio: Source samples.
        sr: Sample rate.
        kind: Which perturbation to apply.
        rng: Seeded generator.
        **params: Perturbation-specific parameters.

    Returns:
        The perturbed audio and its truth span.
    """
    if kind == "pause":
        return pause(
            audio, sr, at_ms=int(params["at_ms"]), len_ms=int(params["len_ms"])
        )
    if kind == "repeat":
        return repeat(audio, sr, at_ms=int(params["at_ms"]), times=int(params["times"]))
    if kind == "prolong":
        return prolong(
            audio, sr, at_ms=int(params["at_ms"]), factor=float(params["factor"])
        )
    if kind == "correct":
        return correct(
            audio, sr, at_ms=int(params["at_ms"]), template=int(params["template"])
        )
    if kind == "burst":
        return burst(audio, sr, bursts=int(params["bursts"]), rng=rng)
    if kind == "noise":
        return noise(audio, sr, snr_db=float(params["snr_db"]), rng=rng)
    # Exhaustive over `PerturbationKind`; mypy proves this unreachable, and will
    # stop proving it the moment a kind is added without a branch here.
    assert_never(kind)
