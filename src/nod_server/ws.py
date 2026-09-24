"""WebSocket endpoints.

`/v1/stream` mirrors AssemblyAI's streaming contract so an existing client can
switch URL without changing code (PRD.md Mode A).

**Thinner than the first tier, and said so deliberately** (CLAUDE.md §5). This
is server plumbing: a broken audio pump is a silent call and a broken socket is
a dropped session, both of which fail visibly. The two paths that fail
*invisibly* — `send_patch_upstream` and the `SAFE` fallback — live in
`nod_core.proxy` and keep their full mutation coverage there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Final

from fastapi import WebSocket, WebSocketDisconnect

from nod_core.arbiter import DEFAULT_CEILING_MS
from nod_core.types import JsonValue, NodMode, SessionBegin, Termination, Turn
from nod_server.telemetry import TURNS_TOTAL

_log = logging.getLogger("nod.stream")

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from nod_core.proxy import SessionProxy
    from nod_server.telemetry import TelemetryHub

AUDIO_FRAME_MS: Final = 50
"""Frame size the client is expected to send. Milliseconds."""


def _turn_payload(turn: Turn) -> dict[str, JsonValue]:
    """The console view of one turn. Pure. `O(words)`.

    Carries word end times because the Floor Meter fills in **real time** from
    the last word's end: the browser runs its own clock rather than the server
    ticking at it, which would put a timer per session on the event loop for a
    purely cosmetic animation.
    """
    return {
        "turn_order": turn.turn_order,
        "end_of_turn": turn.end_of_turn,
        "transcript": turn.transcript,
        "words": [
            {"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms}
            for w in turn.words
        ],
        "t_ms": turn.words[-1].end_ms if turn.words else 0,
    }


async def stream_endpoint(
    ws: WebSocket,
    *,
    proxy: SessionProxy,
    hub: TelemetryHub,
    session_id: str,
    nod_preset: str = "balanced",
    nod_mode: NodMode = NodMode.ADAPT,
    nod_ceiling_ms: int = DEFAULT_CEILING_MS,
    nod_events: bool = False,
) -> None:
    """Serve `WS /v1/stream` (ARCHITECTURE.md §7).

    Binary audio is forwarded verbatim. JSON control frames pass through
    unchanged except `UpdateConfiguration`, which is merged with Nod's own patch
    rather than fought: the host wins on any field it set explicitly in the last
    five seconds (EC-33).

    **`feed_audio` is called synchronously and never awaited** (INV-1). The
    proxy owns a bounded ring in each direction; this coroutine only moves
    bytes between the socket and those rings.

    Args:
        ws: The client socket.
        proxy: The session's proxy, already wired to an upstream.
        hub: Console fan-out. Turn events are published here because the proxy
            does not trace them (see `ConsoleTeeSink`).
        session_id: The console topic for this session.
        nod_preset: Starting configuration.
        nod_mode: `adapt`, `observe` or `off`. `observe` sends no patches.
        nod_ceiling_ms: Latency ceiling for the arbiter.
        nod_events: Also send `NodEvent` frames to the client.
    """
    await ws.accept()
    hub.publish(
        session_id,
        "session.started",
        {
            "preset": nod_preset,
            "mode": nod_mode.value,
            "ceiling_ms": nod_ceiling_ms,
            "t_ms": 0,
        },
    )

    async def downstream() -> None:
        """Forward upstream frames to the client and the console."""
        async for event in proxy.client_events():
            if isinstance(event, Turn):
                kind = "turn.final" if event.end_of_turn else "turn.partial"
                if event.end_of_turn and event.words:
                    # Wordless finalised turns are the `Terminate` flush, not a
                    # caller turn (ADR-047); counting them put FRAG at exactly
                    # 2.000 on every arm once already.
                    TURNS_TOTAL.inc()
                hub.publish(session_id, kind, _turn_payload(event))
            elif isinstance(event, SessionBegin):
                hub.publish(
                    session_id, "agent.state", {"state": "listening", "t_ms": 0}
                )
            elif isinstance(event, Termination):
                hub.publish(session_id, "session.ended", {"t_ms": proxy.stream_ms})
            if nod_events:
                with contextlib.suppress(RuntimeError):
                    await ws.send_json(
                        {"type": "NodEvent", "kind": type(event).__name__}
                    )

    pump = asyncio.create_task(downstream())
    # **Counted server-side on purpose.** Whether the browser is sending audio
    # at all is the one question a silent failure makes unanswerable, and the
    # browser is exactly the component under suspicion when it is asked. This
    # number comes from the socket, so it is true regardless of what the client
    # believes about itself.
    frames = 0
    peak = 0  # loudest sample seen, 0..32767
    loud = 0  # frames whose RMS clears a whisper
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if (frame := message.get("bytes")) is not None:
                frames += 1
                # **Measure what arrived, not just that something did.** A
                # granted microphone that captures silence sends perfectly
                # well-formed frames, and every counter upstream of the audio
                # agrees it is working. Amplitude is the field that separates
                # "the client is broken" from "the client is fine and the room
                # is quiet" — and it is cheap: one pass over 800 samples.
                block = memoryview(frame).cast("h")
                block_peak = max(
                    (abs(v) for v in block[::8]), default=0
                )  # every 8th sample is enough to spot silence
                peak = max(peak, block_peak)
                if block_peak > 150:
                    loud += 1
                if frames in (1, 20) or frames % 200 == 0:
                    _log.info(
                        "audio_frames session=%s frames=%d bytes=%d peak=%d",
                        session_id,
                        frames,
                        len(frame),
                        block_peak,
                    )
                proxy.feed_audio(frame)
            elif (text := message.get("text")) is not None:
                await _handle_control(proxy, text)
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        _log.info(
            "stream_closed session=%s frames_received=%d loud_frames=%d "
            "peak=%d stream_ms=%d",
            session_id,
            frames,
            loud,
            peak,
            proxy.stream_ms,
        )
        hub.publish(session_id, "session.ended", {"t_ms": proxy.stream_ms})


async def _handle_control(proxy: SessionProxy, text: str) -> None:
    """Pass a JSON control frame through, noting host configuration. EC-33."""
    import json

    with contextlib.suppress(ValueError):
        message = json.loads(text)
        if isinstance(message, dict) and message.get("type") == "UpdateConfiguration":
            fields = [k for k in message if k != "type"]
            proxy.note_host_configuration(tuple(fields))


async def console_endpoint(
    ws: WebSocket, session_id: str, *, hub: TelemetryHub
) -> None:
    """Serve `WS /v1/console` (ARCHITECTURE.md §7).

    Server to client only, per-session subscription. A console client that falls
    behind is disconnected rather than allowed to buffer — the hub drops it and
    this loop ends.

    Args:
        ws: The console socket.
        session_id: The session to subscribe to.
        hub: The fan-out to subscribe against.
    """
    await ws.accept()
    try:
        async for line in hub.subscribe(session_id):
            await ws.send_bytes(line)
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(RuntimeError):
            await ws.close()
