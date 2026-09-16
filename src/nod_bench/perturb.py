"""Controlled perturbations with exact ground truth.

BENCH_SPEC.md §2: because the harness inserts the pause, ground truth is exact
rather than judged. The generator is seeded and deterministic — the same seed and
`GENERATOR_VERSION` produce byte-identical audio.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

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


class TruthSpan(BaseModel):
    """One utterance in a `.truth.json` sidecar (BENCH_SPEC.md §2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: int
    end_ms: int
    text: str
    perturbation: Mapping[str, str | int | float]


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


def noise(
    audio: Audio,
    sr: int,
    *,
    snr_db: float,
    rng: np.random.Generator,
) -> tuple[Audio, TruthSpan]:
    """Add stationary background noise at a set signal-to-noise ratio.

    Args:
        audio: Source samples.
        sr: Sample rate.
        snr_db: Target SNR in decibels.
        rng: Seeded generator.

    Returns:
        The perturbed audio and its truth span.
    """
    raise NotImplementedError


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
    raise NotImplementedError
