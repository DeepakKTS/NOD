"""An in-memory upstream that endpoints on the audio it is actually given.

INV-7 and BENCH_SPEC.md §4: the bench must run offline on a clean clone with no
API key. This is the object-level precursor to P3's socket-level
`FakeAssemblyAI` — same purpose, one layer up, and it is what lets
`python -m nod_bench.probe --fake` exercise the whole probe without spending a
credit.

It is a real miniature endpointer, not a canned script: it measures the level of
each frame, accumulates a silence run, and ends the turn on the same two rules
the upstream documents — confidence past its threshold once `min_turn_silence`
has elapsed, or `max_turn_silence` as the acoustic fallback. That matters,
because a fake that replayed a fixed answer would make the probe's verdicts
tautological. Here the verdicts have to be earned from the audio.
"""

from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import AsyncIterator, Mapping
from typing import Final

from nod_core.types import JsonValue, SessionBegin, Termination, Turn, Word

FRAME_MS: Final = 50
"""Assumed frame size, matching the feeder."""

SILENCE_FLOOR_DBFS: Final = -60.0
"""Level below which audio is silence at `vad_threshold = 0`."""

VAD_RANGE_DB: Final = 40.0
"""How far `vad_threshold = 1` raises the silence floor, in decibels."""

DEFAULT_VAD: Final = 0.4
"""AssemblyAI's documented default, accessed 2026-09-17 (BENCH_SPEC §3).

Was 0.5, which matched nothing published. A fake carrying a default the service
does not have is a fake that disagrees with it for free.
"""
DEFAULT_MIN_SILENCE_MS: Final = 400
DEFAULT_MAX_SILENCE_MS: Final = 1280
DEFAULT_CONF_THRESHOLD: Final = 0.40

PARTIAL_CONFIDENCES: Final = (0.31, 0.47, 0.62)
"""A confidence trajectory across partials, so §2.4's jitter input is present."""

TURN_CONFIDENCE: Final = 0.62
"""What the model reports at a candidate boundary. Straddles the 0.20/0.95 arms."""


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


class FakeProbeSession:
    """A deterministic upstream with the same constructor as the real adapter.

    Substitutable for `AssemblyAISession` as a session factory, so the probe CLI
    and its tests drive identical code paths.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        sample_rate: int = 16000,
        config: Mapping[str, float] | None = None,
        on_frame: object = None,
        on_sent: object = None,
        honour_updates: bool = True,
    ) -> None:
        """Configure the fake exactly as the real session would be configured.

        Args:
            api_key: Ignored; present so the signatures match.
            model: Recorded, and reported on the `Begin` frame.
            sample_rate: Samples per second.
            config: Connect-time turn-detection parameters.
            on_frame: Called with every emitted server frame, as the adapter does.
            on_sent: Called with every control frame received.
            honour_updates: When false, `UpdateConfiguration` is accepted and
                silently ignored — the exact failure the probe exists to catch.
        """
        settings = dict(config or {})
        self.id = f"fake-{model}"
        self._model = model
        self._on_frame = on_frame
        self._on_sent = on_sent
        self._honour = honour_updates
        self._min_silence = float(
            settings.get("min_turn_silence", DEFAULT_MIN_SILENCE_MS)
        )
        self._max_silence = float(
            settings.get("max_turn_silence", DEFAULT_MAX_SILENCE_MS)
        )
        self._conf_threshold = float(
            settings.get("end_of_turn_confidence_threshold", DEFAULT_CONF_THRESHOLD)
        )
        self._vad = float(settings.get("vad_threshold", DEFAULT_VAD))

        self._events: asyncio.Queue[SessionBegin | Turn | Termination | None] = (
            asyncio.Queue()
        )
        self._stream_ms = 0
        self._silence_ms = 0.0
        self._turn_order = 1
        self._turn_open = False
        self._partials = 0
        self._last_word_end = 0
        self.updates: list[dict[str, float]] = []
        self.forced = False
        self.closed = False

    @property
    def url(self) -> str:
        """A URL-shaped string, so the trace's `meta` line looks the same."""
        return f"ws://fake.invalid/v3/ws?speech_model={self._model}"

    @property
    def _silence_floor(self) -> float:
        """The level below which this session calls a frame silent. `O(1)`."""
        return SILENCE_FLOOR_DBFS + self._vad * VAD_RANGE_DB

    async def __aenter__(self) -> FakeProbeSession:
        """Start the session and emit `Begin`."""
        self._emit(SessionBegin(session_id=self.id, expires_at_ms=3_600_000))
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the session."""
        await self.aclose()

    def _emit(self, event: SessionBegin | Turn | Termination) -> None:
        """Queue one event and mirror it to `on_frame`, as the adapter does."""
        if callable(self._on_frame):
            payload: dict[str, JsonValue] = {"type": type(event).__name__}
            if isinstance(event, Turn):
                payload |= {
                    "turn_order": event.turn_order,
                    "end_of_turn": event.end_of_turn,
                    "end_of_turn_confidence": event.end_of_turn_confidence,
                    "transcript": event.transcript,
                }
            self._on_frame(payload)
        self._events.put_nowait(event)

    def _turn(self, *, end: bool, confidence: float) -> Turn:
        return Turn(
            turn_order=self._turn_order,
            end_of_turn=end,
            end_of_turn_confidence=confidence,
            transcript="the fake speaker said something",
            words=(
                Word(
                    text="something",
                    start_ms=max(0, self._last_word_end - 120),
                    end_ms=self._last_word_end,
                    confidence=0.9,
                    is_final=True,
                ),
            ),
        )

    async def send_audio(self, frame: bytes) -> None:
        """Consume one frame and decide, from its level, whether a turn ends.

        This is the whole model: speech opens a turn and resets the silence run;
        silence accumulates until one of the two documented rules fires.
        """
        self._stream_ms += FRAME_MS
        if _rms_dbfs(frame) > self._silence_floor:
            self._silence_ms = 0.0
            self._last_word_end = self._stream_ms
            if not self._turn_open:
                self._turn_open = True
                self._partials = 0
            elif self._partials < len(PARTIAL_CONFIDENCES):
                self._emit(
                    self._turn(
                        end=False, confidence=PARTIAL_CONFIDENCES[self._partials]
                    )
                )
                self._partials += 1
            return

        if not self._turn_open:
            return

        self._silence_ms += FRAME_MS
        confident = (
            self._silence_ms >= self._min_silence
            and self._conf_threshold <= TURN_CONFIDENCE
        )
        if confident or self._silence_ms >= self._max_silence:
            self._end_turn()

    def _end_turn(self) -> None:
        """Close the open turn and reset for the next one."""
        self._emit(self._turn(end=True, confidence=TURN_CONFIDENCE))
        self._turn_open = False
        self._silence_ms = 0.0
        self._turn_order += 1

    async def update_configuration(self, patch: Mapping[str, float]) -> None:
        """Accept a mid-stream change, and apply it only if told to."""
        if callable(self._on_sent):
            self._on_sent({"type": "UpdateConfiguration", **dict(patch)})
        self.updates.append(dict(patch))
        if not self._honour:
            return
        for key, value in patch.items():
            if key == "min_turn_silence":
                self._min_silence = float(value)
            elif key == "max_turn_silence":
                self._max_silence = float(value)
            elif key == "end_of_turn_confidence_threshold":
                self._conf_threshold = float(value)
            elif key == "vad_threshold":
                self._vad = float(value)

    async def force_endpoint(self) -> None:
        """End the open turn immediately."""
        if callable(self._on_sent):
            self._on_sent({"type": "ForceEndpoint"})
        self.forced = True
        if self._turn_open:
            self._end_turn()

    async def terminate(self) -> None:
        """End the session."""
        if callable(self._on_sent):
            self._on_sent({"type": "Terminate"})
        self._emit(Termination(session_id=self.id, reason="terminated"))
        self._events.put_nowait(None)

    async def events(self) -> AsyncIterator[SessionBegin | Turn | Termination]:
        """Yield queued events until the session terminates."""
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def aclose(self) -> None:
        """Release the session."""
        self.closed = True
        self._events.put_nowait(None)
