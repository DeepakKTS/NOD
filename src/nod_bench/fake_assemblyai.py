"""`FakeAssemblyAI`: a simulator, not a replayer (ADR-017).

A replay of a recorded event stream reproduces the boundaries recorded under the
config in force at capture time. The three-arm sweep varies exactly that config,
so replay would hold fixed the thing the sweep varies and yield one arm drawn
three times. This endpoints the audio it is actually given, under whatever
configuration it is actually handed.

Three properties are forced by ADR-001, and each is a way this refuses to be
more capable than the service it stands for:

- **Regime comes from the truth sidecar**, never from the audio. Whether a
  silence follows a complete utterance or a fragment is a semantic judgement
  this cannot make and must not guess — a fake that guessed would make the
  benchmark measure the guess.
- **`end_of_turn_confidence_threshold` is accepted and ignored**, because
  ADR-001 measured it inert. A fake where it works would let the bench reward a
  control law exploiting a knob that does not exist.
- **Endpoint overhead is a parameter defaulting to 0**, and at 0 every boundary
  here is early by however long the real service actually takes.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from nod_bench.perturb import Gap

FRAME_MS: Final = 50
"""Frame size, matching the feeder."""

SILENCE_FLOOR_DBFS: Final = -60.0
"""Level below which audio is silence at `vad_threshold = 0`."""

VAD_RANGE_DB: Final = 40.0
"""How far `vad_threshold = 1` raises the silence floor, in decibels."""

DEFAULT_VAD: Final = 0.4
DEFAULT_MIN_TURN_SILENCE_MS: Final = 400
DEFAULT_MAX_TURN_SILENCE_MS: Final = 1280
DEFAULT_CONF_THRESHOLD: Final = 0.4
"""AssemblyAI's documented global defaults, which are also the `balanced`
preset. Accessed 2026-09-17 from
https://www.assemblyai.com/docs/streaming/universal-streaming/turn-detection
and corroborated at
https://www.assemblyai.com/docs/streaming/getting-started/optimizing-accuracy-and-latency
(BENCH_SPEC §3). `vad_threshold` is 0.4, not 0.5 — `fake_session` carried 0.5,
which matched nothing published.
"""

ENDPOINT_OVERHEAD_MS: Final = 0.0
"""Default lag from the configured gate to the emitted boundary. Milliseconds.

**0 means unmeasured, and biases every latency optimistically.** The real
service does not emit a boundary the instant its gate elapses; the P1 matrix saw
a consistently positive lag across every plain silence-gate cell. A simulator
running at 0 fires early by that much on every turn, so every TTL it produces is
better than the service would deliver.

Left at 0 deliberately rather than filled in: the value comes from `make bench`
(INV-9), and a hand-written constant here would be a published number smuggled
into a default. Callers that know the figure pass it; `RunManifest` records
whatever was used, so a run can never be read without it.
"""

type BindingGate = Literal["min_turn_silence", "max_turn_silence"]


@dataclass(frozen=True, slots=True)
class SimulatedBoundary:
    """One emitted `end_of_turn`, and what caused it."""

    fired_at_ms: float
    gate: BindingGate
    silence_started_ms: float
    regime: Literal["complete", "fragment"]


def _rms_dbfs(frame: bytes) -> float:
    """Level of one PCM16 frame, in dBFS. `O(n)`."""
    count = len(frame) // 2
    if count == 0:
        return SILENCE_FLOOR_DBFS
    values = struct.unpack(f"<{count}h", frame[: count * 2])
    mean_square = sum(v * v for v in values) / count
    if mean_square <= 0:
        return -math.inf
    return 10 * math.log10(mean_square / (32767.0**2))


def regime_at(gaps: Sequence[Gap], t_ms: float) -> Literal["complete", "fragment"]:
    """Which regime governs the silence beginning at `t_ms`. Pure. `O(g)`.

    Args:
        gaps: The clip's `.truth.json` gaps.
        t_ms: When the silence run started.

    Returns:
        The sidecar's label, or `complete` when no gap covers this silence.

    The fallback is deliberate and stated rather than left implicit: a silence
    the sidecar does not describe is the end of the clip, where the speaker has
    finished and the semantic gate would fire. Guessing `fragment` there would
    make every clip end on `max_turn_silence` and quietly inflate TTL.
    """
    for gap in gaps:
        if gap.start_ms <= t_ms <= gap.end_ms:
            return gap.preceding
    return "complete"


class Endpointer:
    """The simulator core: silence in, boundaries out.

    Not a canned script. It measures the level of each frame, accumulates a
    silence run, and ends the turn on the gate the regime selects — the same two
    rules the upstream documents, with the semantic judgement supplied by ground
    truth instead of invented.
    """

    def __init__(
        self,
        *,
        gaps: Sequence[Gap] = (),
        min_turn_silence: float = DEFAULT_MIN_TURN_SILENCE_MS,
        max_turn_silence: float = DEFAULT_MAX_TURN_SILENCE_MS,
        vad_threshold: float = DEFAULT_VAD,
        end_of_turn_confidence_threshold: float = DEFAULT_CONF_THRESHOLD,
        endpoint_overhead_ms: float = ENDPOINT_OVERHEAD_MS,
    ) -> None:
        """Configure one simulated session.

        Args:
            gaps: The clip's truth-sidecar gaps, supplying regime.
            min_turn_silence: Gate after a complete utterance. Milliseconds.
            max_turn_silence: Gate after a fragment. Milliseconds.
            vad_threshold: Raises the silence floor, 0 to 1.
            end_of_turn_confidence_threshold: **Accepted and ignored** (ADR-001).
            endpoint_overhead_ms: Added to every boundary; 0 fires early.
        """
        self._gaps = tuple(gaps)
        self.min_turn_silence = float(min_turn_silence)
        self.max_turn_silence = float(max_turn_silence)
        self.vad_threshold = float(vad_threshold)
        # Retained so a caller can read back what it set, and so the signature
        # matches the real service. Never consulted (ADR-001, ADR-017).
        self.end_of_turn_confidence_threshold = float(end_of_turn_confidence_threshold)
        self.endpoint_overhead_ms = float(endpoint_overhead_ms)
        self._elapsed_ms = 0.0
        self._silence_ms = 0.0
        self._silence_started_ms = 0.0
        self._armed = False

    @property
    def silence_floor_dbfs(self) -> float:
        """Level below which a frame counts as silence. `O(1)`."""
        return SILENCE_FLOOR_DBFS + self.vad_threshold * VAD_RANGE_DB

    def update_configuration(self, patch: dict[str, float]) -> None:
        """Apply a mid-stream config change. `O(1)`.

        `end_of_turn_confidence_threshold` is stored and never acted on, exactly
        as the real service behaves on `universal-streaming-english` (ADR-001).
        """
        for key, value in patch.items():
            if key == "min_turn_silence":
                self.min_turn_silence = float(value)
            elif key == "max_turn_silence":
                self.max_turn_silence = float(value)
            elif key == "vad_threshold":
                self.vad_threshold = float(value)
            elif key == "end_of_turn_confidence_threshold":
                self.end_of_turn_confidence_threshold = float(value)

    def feed(self, frame: bytes) -> SimulatedBoundary | None:
        """Consume one frame and report a boundary if one fired. `O(n)`.

        Args:
            frame: PCM16 mono samples, `FRAME_MS` long.

        Returns:
            The boundary, or `None`.
        """
        self._elapsed_ms += FRAME_MS
        if _rms_dbfs(frame) > self.silence_floor_dbfs:
            self._silence_ms = 0.0
            self._armed = True
            return None
        if not self._armed:
            return None

        if self._silence_ms == 0.0:
            self._silence_started_ms = self._elapsed_ms - FRAME_MS
        self._silence_ms += FRAME_MS

        regime = regime_at(self._gaps, self._silence_started_ms)
        gate: BindingGate = (
            "min_turn_silence" if regime == "complete" else "max_turn_silence"
        )
        threshold = (
            self.min_turn_silence
            if gate == "min_turn_silence"
            else self.max_turn_silence
        )
        if self._silence_ms < threshold:
            return None

        self._armed = False
        fired = self._silence_started_ms + threshold + self.endpoint_overhead_ms
        self._silence_ms = 0.0
        return SimulatedBoundary(
            fired_at_ms=fired,
            gate=gate,
            silence_started_ms=self._silence_started_ms,
            regime=regime,
        )
