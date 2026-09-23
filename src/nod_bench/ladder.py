"""The regime ladder: one prefix, four holds, three arms (docs/PILOT_REGIME.md).

**Why this is a module and not a script.** The Gate 4d pilot produced ADR-054 —
the project's headline finding — from a runner that was written into a scratch
directory and deleted with the job. Its four ladder clips survived; nothing else
did. The numbers in ADR-054 exist only as prose, cannot be re-analysed, and
cannot be re-run. That is ADR-052's defect (*"an artifact set is complete when
the analysis can be redone from it without re-acquiring the data"*) landing on
the one result the README leads with, so the tool lives in the tree this time.

**The clock this module measures on, and why it matters.** Every quantity here
is on the **feeder stream clock**: the acoustic end of the prefix is measured
from the wav that is about to be fed, and `fired_at_ms` is `PacedFeeder.now_ms`
when the `end_of_turn` frame arrived. Both ends of the subtraction are therefore
the same clock, and any constant offset between our clock and the service's
cancels out of `fired_at_ms - prefix_end_ms`.

That is deliberate. `LiveBoundary.silence_started_ms` is the service's word
`end_ms`, a *different* clock, and ADR-054 recorded the symptom without
resolving it: silences derived that way came out at 180-435 ms against min gates
of 400 and 800, which is not a possible reading of a gate that is working. This
module reports both, and their difference, so the offset becomes a measured
number rather than a caveat.
"""

from __future__ import annotations

import math
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

# --- constants (CLAUDE.md §6: named, at module top, with the unit) ----------

SILENCE_FLOOR_DBFS: Final = -44.0
"""Energy floor below which a frame counts as silence. dBFS.

The same floor `corpus.INTRINSIC_FLOOR_DBFS` uses, and asserted equal to it by
`test_ladder_floor_matches_the_corpus_floor` rather than derived from it — two
constants that agree by meaning and not by code are a coincidence, not a fact
the codebase knows (CLAUDE.md §5).
"""

ENERGY_FRAME_MS: Final = 10
"""Analysis frame for the silence detector. Milliseconds."""

TAIL_SILENCE_MS: Final = 4030
"""Silence appended after the continuation. Milliseconds.

Must exceed `conservative`'s 3600 ms max gate plus endpoint overhead, or the
widest arm's end-of-clip boundary is cut off by the end of the file and the run
measures the file length instead of the gate. Asserted against the gate itself
in `test_ladder_tail_outlasts_the_widest_arm_gate`, never against this constant.
"""

MEASURED_OVERHEAD_MS: Final = 160
"""Endpoint overhead used to form predictions. Milliseconds.

Measured 159-164 ms across three arms at Gate 4b. This is the *predictor*, not
`arbiter.ENDPOINT_OVERHEAD_MS` (217), which is the top of the spread and exists
to keep the controller's ceiling safe rather than to forecast a firing time.
"""

GRID_MS: Final = 80
"""The service's timestamp resolution (ADR-031). Milliseconds."""

PREDICTION_TOLERANCE_MS: Final = 120
"""How close an observation must sit to a prediction to count as matching it.

`GRID_MS` plus the 40 ms of spread the Gate 4b overhead measurement showed.
"""

Hypothesis = Literal["min_gate", "max_gate", "confidence", "clamped"]


# --- geometry ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpeechRun:
    """One contiguous speech region found by the energy detector."""

    start_ms: int
    end_ms: int


class LadderGeometry(BaseModel):
    """Where the speech and the hold sit in one built ladder clip.

    Every field is on the feeder stream clock, in milliseconds from the first
    audio frame.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hold_label_ms: int
    """The hold this row was asked for. Milliseconds."""

    hold_measured_ms: int
    """The silence actually present between prefix and continuation.

    Equal to `hold_label_ms` when the holds were synthesised, and different when
    they were read by a human, which is the case the ladder exists to cover.
    """

    prefix_end_ms: int
    """Acoustic end of the prefix. The instant the hold begins."""

    continuation_start_ms: int
    continuation_end_ms: int
    total_ms: int


def speech_runs(
    samples: np.ndarray,
    sample_rate: int,
    *,
    floor_dbfs: float = SILENCE_FLOOR_DBFS,
    frame_ms: int = ENERGY_FRAME_MS,
) -> tuple[SpeechRun, ...]:
    """Contiguous regions above the energy floor. Pure. `O(n)`.

    Args:
        samples: Mono float samples in `[-1, 1]`.
        sample_rate: Hertz.
        floor_dbfs: Frames at or below this are silence.
        frame_ms: Analysis frame length.

    Returns:
        Speech runs in order, quantised to `frame_ms`.
    """
    frame = int(sample_rate * frame_ms / 1000)
    count = len(samples) // frame
    loud: list[bool] = []
    for i in range(count):
        block = samples[i * frame : (i + 1) * frame]
        rms = float(np.sqrt(np.mean(block * block))) + 1e-12
        loud.append(20.0 * math.log10(rms) > floor_dbfs)

    runs: list[SpeechRun] = []
    start: int | None = None
    for i, is_loud in enumerate(loud):
        if is_loud and start is None:
            start = i
        elif not is_loud and start is not None:
            runs.append(SpeechRun(start * frame_ms, i * frame_ms))
            start = None
    if start is not None:
        runs.append(SpeechRun(start * frame_ms, count * frame_ms))
    return tuple(runs)


def read_mono(path: Path) -> tuple[np.ndarray, int]:
    """Read a mono PCM16 wav as floats. `O(n)`.

    Args:
        path: The wav to read.

    Returns:
        Samples in `[-1, 1]` and the sample rate.

    Raises:
        ValueError: The file is not mono PCM16.
    """
    with wave.open(str(path)) as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(
                f"{path} is {handle.getnchannels()}ch/"
                f"{handle.getsampwidth() * 8}bit; ladder needs mono PCM16"
            )
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0, rate


def write_mono(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    """Write mono PCM16. `O(n)`."""
    clipped = np.clip(samples, -1.0, 1.0)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes((clipped * 32767.0).astype(np.int16).tobytes())


def _silence(ms: int, sample_rate: int) -> np.ndarray:
    """`ms` of digital silence. `O(n)`."""
    return np.zeros(round(ms * sample_rate / 1000.0), dtype=np.float64)


def build_from_halves(
    prefix: np.ndarray,
    continuation: np.ndarray,
    sample_rate: int,
    hold_ms: int,
) -> tuple[np.ndarray, LadderGeometry]:
    """One ladder clip with a synthesised hold. `O(n)`.

    The Gate 4d shape, kept so ADR-054 is reproducible: two recorded halves and
    an exact silence between them. `hold_measured_ms` equals `hold_ms` here by
    construction, which is the whole reason this mode exists — it removes the
    hold from the list of things that could vary.

    Args:
        prefix: The non-completable prefix, trimmed.
        continuation: What follows the hold, trimmed.
        sample_rate: Hertz, shared by both halves.
        hold_ms: Silence to insert between them.

    Returns:
        The clip and where everything in it sits.
    """
    audio = np.concatenate(
        [
            prefix,
            _silence(hold_ms, sample_rate),
            continuation,
            _silence(TAIL_SILENCE_MS, sample_rate),
        ]
    )
    # The hold begins where the *audio* goes quiet, not where the file ends.
    # `say` leaves a few tens of ms of sub-threshold tail on the prefix, and
    # anchoring to the file length would put every prediction that much late
    # while the service's VAD had already started counting.
    file_end = round(len(prefix) * 1000.0 / sample_rate)
    runs = speech_runs(prefix, sample_rate)
    prefix_end = runs[-1].end_ms if runs else file_end
    cont_start = file_end + hold_ms
    cont_end = cont_start + round(len(continuation) * 1000.0 / sample_rate)
    return audio, LadderGeometry(
        hold_label_ms=hold_ms,
        hold_measured_ms=cont_start - prefix_end,
        prefix_end_ms=prefix_end,
        continuation_start_ms=cont_start,
        continuation_end_ms=cont_end,
        total_ms=round(len(audio) * 1000.0 / sample_rate),
    )


class TakeSegmentationError(RuntimeError):
    """A single-take recording did not hold the expected number of rows.

    Raised rather than guessed at. The segmenter's job is to find four
    prefix/continuation pairs; a take with a different number of speech runs
    could be salvaged by a heuristic, and a heuristic here would silently
    decide which silence was the hold — the one quantity the ladder measures.
    INV-8's fail-loud direction.
    """


def segment_take(
    samples: np.ndarray,
    sample_rate: int,
    holds: tuple[int, ...],
    *,
    floor_dbfs: float = SILENCE_FLOOR_DBFS,
) -> tuple[tuple[np.ndarray, LadderGeometry], ...]:
    """Split one continuous take into one clip per hold. `O(n)`.

    Expects `2 * len(holds)` speech runs: prefix, continuation, prefix,
    continuation, and so on. The silence *inside* each pair is that row's hold
    and is **measured, not assumed** — a human reading "two seconds" does not
    produce 2000 ms, and scoring against the requested number rather than the
    delivered one would compare a firing time to a hold that never happened.

    Each extracted clip is re-tailed to `TAIL_SILENCE_MS` so every row ends the
    same way and the end-of-clip boundary is comparable across rows.

    Args:
        samples: The whole take, mono float.
        sample_rate: Hertz.
        holds: The hold lengths the take was read against, in order.
        floor_dbfs: Energy floor for the detector.

    Returns:
        One `(audio, geometry)` per hold, in order.

    Raises:
        TakeSegmentationError: The run count does not match `2 * len(holds)`.
    """
    runs = speech_runs(samples, sample_rate, floor_dbfs=floor_dbfs)
    wanted = 2 * len(holds)
    if len(runs) != wanted:
        found = ", ".join(f"{r.start_ms}-{r.end_ms}" for r in runs)
        raise TakeSegmentationError(
            f"expected {wanted} speech runs for {len(holds)} holds, found "
            f"{len(runs)}: [{found}]. Re-record, or pass --holds to match."
        )

    out: list[tuple[np.ndarray, LadderGeometry]] = []
    for i, hold in enumerate(holds):
        head, tail = runs[2 * i], runs[2 * i + 1]
        lo = round(head.start_ms * sample_rate / 1000.0)
        hi = round(tail.end_ms * sample_rate / 1000.0)
        body = samples[lo:hi]
        audio = np.concatenate([body, _silence(TAIL_SILENCE_MS, sample_rate)])
        prefix_end = head.end_ms - head.start_ms
        cont_start = tail.start_ms - head.start_ms
        cont_end = tail.end_ms - head.start_ms
        out.append(
            (
                audio,
                LadderGeometry(
                    hold_label_ms=hold,
                    hold_measured_ms=cont_start - prefix_end,
                    prefix_end_ms=prefix_end,
                    continuation_start_ms=cont_start,
                    continuation_end_ms=cont_end,
                    total_ms=round(len(audio) * 1000.0 / sample_rate),
                ),
            )
        )
    return tuple(out)


# --- predictions ------------------------------------------------------------


class Prediction(BaseModel):
    """What one hypothesis says one arm does on one row, before the run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: str
    hold_label_ms: int
    hypothesis: Hypothesis
    fires_in_hold: bool
    """Whether a boundary falls inside the hold at all.

    The discriminating field. A hypothesis that predicts no in-hold boundary is
    saying the turn survives the pause, which is the regime the controller
    exists for, and it is refuted by any boundary landing there.
    """

    fired_at_ms: float
    """Predicted feeder-clock firing time of the first boundary after the prefix.

    When `fires_in_hold` is false this is the boundary after the *continuation*
    instead, so every prediction names a time and none of them is unfalsifiable.
    """


def predict(
    geometry: LadderGeometry,
    arm: str,
    min_gate_ms: int,
    max_gate_ms: int,
    hypothesis: Hypothesis,
    *,
    confidence_ms: float,
    overhead_ms: float = MEASURED_OVERHEAD_MS,
) -> Prediction:
    """What one hypothesis predicts for one arm on one row. Pure. `O(1)`.

    The three hypotheses, and what separates them:

    - `min_gate` — ADR-054's stated reading: the semantic gate is inoperative,
      so a turn ends once `min_turn_silence` of silence has passed.
    - `max_gate` — the regime *is* reached: the service judges the prefix
      incomplete and waits out `max_turn_silence`. This is PILOT_REGIME's PASS.
    - `confidence` — the service reaches end-of-turn confidence after a roughly
      fixed silence, and fires at `max(min_gate, confidence)`. Distinguished
      from `min_gate` only on arms whose min gate is *shorter* than that
      confidence time, which is why `aggressive` is the decisive arm and not
      `conservative`, where the two hypotheses agree.
    - `clamped` — `confidence`, with the max gate applied as a ceiling as well:
      `clamp(confidence, min_gate, max_gate)`. **Post-hoc**, and labelled so.
      It was not among the three pre-registered for the `say` run; it was
      written down *after* that run showed `aggressive` firing at its 400 ms
      max gate rather than at either pre-registered time, which is `max_gate`
      binding at a mid-utterance pause and is the thing ADR-054 said does not
      happen. A model fitted to the data it explains proves nothing, so it is
      pre-registered against the `min`-sweep before that runs.

    Args:
        geometry: Where the hold sits in this row.
        arm: Arm label, carried through to the record.
        min_gate_ms: The arm's `min_turn_silence`.
        max_gate_ms: The arm's `max_turn_silence`.
        hypothesis: Which reading to evaluate.
        confidence_ms: Silence at which confidence is assumed to arrive.
        overhead_ms: Endpoint overhead to add.

    Returns:
        The prediction, in-hold or after the continuation.
    """
    if hypothesis == "min_gate":
        silence_ms = float(min_gate_ms)
    elif hypothesis == "max_gate":
        silence_ms = float(max_gate_ms)
    elif hypothesis == "confidence":
        silence_ms = max(float(min_gate_ms), confidence_ms)
    else:
        silence_ms = min(max(float(min_gate_ms), confidence_ms), float(max_gate_ms))

    wait_ms = silence_ms + overhead_ms
    in_hold = wait_ms < geometry.hold_measured_ms
    anchor = geometry.prefix_end_ms if in_hold else geometry.continuation_end_ms
    return Prediction(
        arm=arm,
        hold_label_ms=geometry.hold_label_ms,
        hypothesis=hypothesis,
        fires_in_hold=in_hold,
        fired_at_ms=anchor + wait_ms,
    )


# --- observations -----------------------------------------------------------


class ObservedBoundary(BaseModel):
    """One `end_of_turn`, on both clocks, with the offset left visible."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fired_at_ms: float
    """Feeder stream clock. The quantity every prediction is compared to."""

    silence_started_ms: float
    """The service's last-word `end_ms`. A *different* clock. See the module
    docstring: subtracting this from `fired_at_ms` produced ADR-054's impossible
    180-435 ms silences, and it is recorded here so the offset can be measured
    rather than carried as a caveat."""

    turn_order: int
    word_count: int
    text: str


class LadderRow(BaseModel):
    """One arm over one hold: what was predicted, and what happened."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: str
    hold_label_ms: int
    hold_measured_ms: int
    prefix_end_ms: int
    boundaries: tuple[ObservedBoundary, ...]
    flush_turns: int

    @property
    def in_hold(self) -> ObservedBoundary | None:
        """The first boundary falling inside the hold, if any. `O(b)`."""
        hold_end = self.prefix_end_ms + self.hold_measured_ms
        for b in self.boundaries:
            if self.prefix_end_ms < b.fired_at_ms < hold_end:
                return b
        return None

    @property
    def silence_at_fire_ms(self) -> float | None:
        """Feeder-clock silence the service waited before firing in the hold.

        `None` when no boundary fell inside the hold. Offset-free: both terms
        are on the feeder clock.
        """
        b = self.in_hold
        return None if b is None else b.fired_at_ms - self.prefix_end_ms


def scores(
    row: LadderRow,
    prediction: Prediction,
    *,
    tolerance_ms: float = PREDICTION_TOLERANCE_MS,
) -> bool:
    """Whether one prediction survived one row. Pure. `O(b)`.

    **A prediction has to get the in-hold question right first.** Saying "fires
    at 2990 ms, inside the hold" and observing nothing in the hold is a miss
    however close 2990 is to some later boundary, because the in-hold question
    is the one that separates the three readings. Only when both agree that a
    boundary fell inside the hold does the time comparison run.

    The `fires_in_hold=False` case scores on agreement alone rather than on the
    predicted after-the-continuation time. That is deliberately the *lenient*
    direction for `max_gate`, the hypothesis that would overturn ADR-054: it
    gets credit for a correct no-boundary call without also having to place the
    late boundary, so the reading that makes the published result better is the
    one held to the weaker standard.

    Args:
        row: What the arm actually did on this hold.
        prediction: What one hypothesis said it would do.
        tolerance_ms: How far the observed firing may sit from the predicted.

    Returns:
        Whether this row is a hit for this hypothesis.
    """
    observed = row.in_hold
    if prediction.fires_in_hold != (observed is not None):
        return False
    if observed is None:
        return True
    return abs(observed.fired_at_ms - prediction.fired_at_ms) <= tolerance_ms


def hold_invariance_ms(rows: tuple[LadderRow, ...]) -> float | None:
    """Spread of in-hold firing times across holds, for one arm. `O(r)`.

    **This is ADR-054's decisive statistic and the reason it survives the clock
    problem.** If the service were waiting out `max_turn_silence`, the firing
    time could not be the same for a 1000 ms hold and a 3500 ms hold, because
    the 1000 ms hold is shorter than `balanced`'s gate and the turn would have
    to run past the continuation. A small spread therefore refutes `max_gate`
    without needing either clock to be correct in absolute terms.

    Args:
        rows: Rows for a single arm, one per hold.

    Returns:
        Max minus min of the in-hold firing silences, or `None` if fewer than
        two rows produced an in-hold boundary.
    """
    values = [r.silence_at_fire_ms for r in rows if r.silence_at_fire_ms is not None]
    return max(values) - min(values) if len(values) >= 2 else None


# --- rendering ---------------------------------------------------------------

SVG_WIDTH: Final = 1000
"""Timeline width in px. Milliseconds are scaled to fit."""

ROW_HEIGHT: Final = 74
"""Vertical px per arm row."""


def timeline_svg(
    rows: Sequence[LadderRow],
    envelope: Sequence[float],
    total_ms: float,
    *,
    caption: str,
) -> str:
    """One arm per row, boundaries marked against the audio. Pure. `O(r + n)`.

    **The caption is not decoration.** A chart leaves the repository as an image
    and a markdown caption does not survive a screenshot (ADR-016), so the
    provenance — which audio, which arms, which run — is drawn *inside* the
    SVG. The turn count per row is drawn too, because "two turns" versus "one
    turn" is the entire claim this picture makes and a reader should not have to
    count tick marks to check it.

    Args:
        rows: One per arm, in draw order. All must share a clip.
        envelope: Per-bucket RMS in `[0, 1]`, left to right.
        total_ms: Clip duration, mapping ms to px.
        caption: Provenance line drawn into the image.

    Returns:
        A self-contained SVG document.
    """
    height = 58 + ROW_HEIGHT * len(rows)
    scale = SVG_WIDTH / total_ms if total_ms > 0 else 0.0
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_WIDTH + 260}" '
        f'height="{height}" font-family="ui-monospace,Menlo,monospace">',
        f'<rect width="{SVG_WIDTH + 260}" height="{height}" fill="#0b0f14"/>',
        f'<text x="12" y="22" fill="#7d8b9a" font-size="13">{caption}</text>',
    ]
    bucket = SVG_WIDTH / max(len(envelope), 1)
    for i, row in enumerate(rows):
        top = 44 + ROW_HEIGHT * i
        mid = top + 26
        out.append(
            f'<text x="12" y="{mid + 4}" fill="#e6edf3" font-size="13">{row.arm}</text>'
        )
        for j, amp in enumerate(envelope):
            h = max(1.0, amp * 21)
            out.append(
                f'<rect x="{140 + j * bucket:.1f}" y="{mid - h:.1f}" '
                f'width="{bucket:.2f}" height="{2 * h:.1f}" fill="#2c3b4a"/>'
            )
        hold_x = 140 + row.prefix_end_ms * scale
        hold_w = row.hold_measured_ms * scale
        out.append(
            f'<rect x="{hold_x:.1f}" y="{top + 4}" width="{hold_w:.1f}" '
            f'height="44" fill="#1d4ed8" opacity="0.18"/>'
        )
        for b in row.boundaries:
            x = 140 + b.fired_at_ms * scale
            inside = (
                row.in_hold is not None and b.fired_at_ms == row.in_hold.fired_at_ms
            )
            colour = "#f87171" if inside else "#4ade80"
            out.append(
                f'<line x1="{x:.1f}" y1="{top + 2}" x2="{x:.1f}" '
                f'y2="{top + 50}" stroke="{colour}" stroke-width="2.5"/>'
            )
        turns = len(row.boundaries)
        out.append(
            f'<text x="{SVG_WIDTH + 252}" y="{mid + 4}" fill="#7d8b9a" '
            f'font-size="12" text-anchor="end">{turns} turn'
            f"{'' if turns == 1 else 's'}</text>"
        )
    out.append("</svg>")
    return "\n".join(out)


def envelope_of(samples: np.ndarray, *, buckets: int = 300) -> tuple[float, ...]:
    """Normalised RMS per bucket, for drawing. Pure. `O(n)`."""
    if len(samples) == 0:
        return ()
    size = max(1, len(samples) // buckets)
    vals = [
        float(np.sqrt(np.mean(samples[i : i + size] ** 2)))
        for i in range(0, len(samples), size)
    ]
    peak = max(vals) or 1.0
    return tuple(min(1.0, v / peak) for v in vals)
