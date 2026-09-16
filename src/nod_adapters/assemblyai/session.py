"""AssemblyAI Universal-Streaming session, implementing `SttSession`.

Nod runs on the Realtime STT path, never the Voice Agent API (ADR-003).

Raw `websockets` rather than the vendor SDK, for one reason: the probe's job is to
find out what this API actually does, and an SDK that parses known message types
and discards the rest would hide exactly the evidence we are looking for. Every
frame is handed to `on_frame` verbatim before any typing is attempted, so an
undocumented acknowledgement message survives to the trace instead of vanishing
inside a parser that did not expect it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Final, cast
from urllib.parse import urlencode

import websockets

from nod_core.types import JsonValue, SessionBegin, Termination, Turn, Word

STREAMING_URL: Final = "wss://streaming.assemblyai.com/v3/ws"
"""The Universal-Streaming v3 endpoint."""

MS_PER_SECOND: Final = 1000


class UpstreamError(RuntimeError):
    """The server sent an `Error` frame and closed the socket.

    Carries `error_code` verbatim so a rejection can be attributed to the one
    field the session was testing, rather than recorded as a generic failure.
    """

    def __init__(self, error_code: int | None, message: str) -> None:
        """Record the server's own words.

        Args:
            error_code: The upstream `error_code`, if one was present.
            message: The upstream `error` string.
        """
        self.error_code = error_code
        self.message = message
        super().__init__(f"upstream error {error_code}: {message}")


def _as_int(value: object, default: int = 0) -> int:
    """Coerce a JSON scalar to int without trusting the payload's shape."""
    return int(value) if isinstance(value, (int, float)) else default


def _as_float(value: object) -> float | None:
    """Coerce a JSON scalar to float, or `None` when the key was absent."""
    return float(value) if isinstance(value, (int, float)) else None


def _as_str(value: object, default: str = "") -> str:
    """Coerce a JSON scalar to str."""
    return value if isinstance(value, str) else default


def _parse_words(raw: object) -> tuple[Word, ...]:
    """Build the words array, skipping entries the payload malformed. `O(W)`."""
    if not isinstance(raw, list):
        return ()
    words: list[Word] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        words.append(
            Word(
                text=_as_str(item.get("text")),
                start_ms=_as_int(item.get("start")),
                end_ms=_as_int(item.get("end")),
                confidence=_as_float(item.get("confidence")) or 0.0,
                is_final=bool(item.get("word_is_final", False)),
            )
        )
    return tuple(words)


class AssemblyAISession:
    """One live Universal-Streaming session over a raw WebSocket."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        sample_rate: int = 16000,
        config: Mapping[str, float] | None = None,
        on_frame: Callable[[Mapping[str, JsonValue]], None] | None = None,
        on_sent: Callable[[Mapping[str, JsonValue]], None] | None = None,
    ) -> None:
        """Describe a session without opening it.

        Args:
            api_key: Server-side only; never reaches a browser (INV-5).
            model: `speech_model`, e.g. `universal-streaming-english`.
            sample_rate: Samples per second of the PCM16 stream.
            config: Connect-time turn-detection parameters, pinned explicitly so
                no knob is left at an undocumented default during a probe.
            on_frame: Called with every decoded server frame, verbatim, including
                message types this adapter does not recognise.
            on_sent: Called with every JSON control frame sent upstream.
        """
        self._api_key = api_key
        self._model = model
        self._sample_rate = sample_rate
        self._config = dict(config or {})
        self._on_frame = on_frame
        self._on_sent = on_sent
        self._socket: websockets.ClientConnection | None = None
        self.id = ""

    @property
    def url(self) -> str:
        """The connect URL, credential-free: the key travels in a header."""
        params: dict[str, str] = {
            "sample_rate": str(self._sample_rate),
            "encoding": "pcm_s16le",
            "speech_model": self._model,
            "format_turns": "false",
        }
        params.update({k: str(v) for k, v in self._config.items()})
        return f"{STREAMING_URL}?{urlencode(params)}"

    async def __aenter__(self) -> AssemblyAISession:
        """Open the socket."""
        self._socket = await websockets.connect(
            self.url,
            additional_headers={"Authorization": self._api_key},
            max_size=None,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the socket."""
        await self.aclose()

    async def send_audio(self, frame: bytes) -> None:
        """Forward one PCM16 frame verbatim. Must not block on anything else."""
        if self._socket is None:
            return
        await self._socket.send(frame)

    async def _send_json(self, message: Mapping[str, JsonValue]) -> None:
        """Send one JSON control frame and record it verbatim."""
        if self._socket is None:
            return
        if self._on_sent is not None:
            self._on_sent(message)
        await self._socket.send(json.dumps(message))

    async def update_configuration(self, patch: Mapping[str, float]) -> None:
        """Apply a mid-stream config change on this same socket.

        No reconnect, no session loss (CONTROL_SPEC.md §0). The server answers a
        success with silence, so the caller must establish behaviourally that the
        change took effect; see `nod_core.capabilities`.
        """
        message: dict[str, JsonValue] = {"type": "UpdateConfiguration"}
        message.update(patch)
        await self._send_json(message)

    async def force_endpoint(self) -> None:
        """End the current turn now, rather than waiting out the silence."""
        await self._send_json({"type": "ForceEndpoint"})

    async def terminate(self) -> None:
        """Ask the server to end the session gracefully."""
        await self._send_json({"type": "Terminate"})

    async def events(self) -> AsyncIterator[SessionBegin | Turn | Termination]:
        """Yield upstream frames, partials included.

        Every frame reaches `on_frame` before it is typed, so unrecognised
        message types are recorded rather than dropped.

        Raises:
            UpstreamError: The server sent an `Error` frame.
        """
        if self._socket is None:
            return
        async for raw in self._socket:
            if isinstance(raw, bytes):
                continue
            decoded: object = json.loads(raw)
            if not isinstance(decoded, dict):
                continue
            frame = cast("dict[str, JsonValue]", decoded)
            if self._on_frame is not None:
                self._on_frame(frame)

            kind = frame.get("type")
            if kind == "Error":
                raise UpstreamError(
                    _as_int(frame.get("error_code"), 0) or None,
                    _as_str(frame.get("error"), "unspecified"),
                )
            if kind == "Begin":
                self.id = _as_str(frame.get("id"))
                yield SessionBegin(
                    session_id=self.id,
                    expires_at_ms=_as_int(frame.get("expires_at")) * MS_PER_SECOND,
                )
            elif kind == "Turn":
                yield Turn(
                    turn_order=_as_int(frame.get("turn_order")),
                    end_of_turn=bool(frame.get("end_of_turn", False)),
                    end_of_turn_confidence=_as_float(
                        frame.get("end_of_turn_confidence")
                    ),
                    transcript=_as_str(frame.get("transcript")),
                    words=_parse_words(frame.get("words")),
                )
            elif kind == "Termination":
                yield Termination(session_id=self.id, reason="terminated")

    async def aclose(self) -> None:
        """Release the socket."""
        if self._socket is not None:
            await self._socket.close()
            self._socket = None
