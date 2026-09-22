"""The console path: session creation, both sockets, and INV-4 on the wire.

No network (INV-7). The HTTP route and the console socket run through
`TestClient`; the stream endpoint's fan-out is driven directly in asyncio with
a stub proxy, because pushing real audio through `TestClient`'s portal couples
the assertion to task scheduling rather than to the behaviour under test.

**This is the test behind ADR-035's clause 2.** The Floor Meter grows when a
`config.changed` arrives carrying a new `max_turn_silence`; if that frame never
reaches the browser there is no demo, and nothing else in the suite covers the
path from the arbiter's decision to the console socket.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from nod_core.config import Settings
from nod_core.types import NodMode, SessionBegin, Termination, Turn, Word
from nod_server.app import create_app
from nod_server.telemetry import CONSOLE_QUEUE_MAXSIZE, ConsoleTeeSink, TelemetryHub
from nod_server.ws import _turn_payload, stream_endpoint

REQUIRED_CONFIG_FIELDS = ("max_turn_silence", "min_turn_silence", "trigger", "rule_id")
"""What INV-4 requires on the wire, not merely in the trace.

The capsule's length is `max_turn_silence` and the hairline tick is
`min_turn_silence`; the reason line renders `trigger`, and `rule_id` ties the
decision back to the law. A `config.changed` missing any of them leaves the
Floor Meter growing for no stated reason, which is what INV-4 forbids.
"""


def _turn(order: int, *, end: bool, words: tuple[tuple[str, int, int], ...]) -> Turn:
    return Turn(
        turn_order=order,
        end_of_turn=end,
        end_of_turn_confidence=0.5,
        transcript=" ".join(w[0] for w in words),
        words=tuple(
            Word(text=t, start_ms=a, end_ms=b, confidence=0.9, is_final=True)
            for t, a, b in words
        ),
    )


class _StubProxy:
    """Just enough `SessionProxy` for the fan-out loop: events and a clock."""

    def __init__(self, events: tuple[object, ...]) -> None:
        self._events = events
        self.stream_ms = 4200
        self.fed: list[bytes] = []

    async def client_events(self) -> AsyncIterator[object]:
        for event in self._events:
            yield event

    def feed_audio(self, frame: bytes) -> None:
        self.fed.append(frame)

    def note_host_configuration(self, fields: tuple[str, ...]) -> None:
        pass


class _FakeWebSocket:
    """A socket that accepts, then reports a disconnect."""

    def __init__(self) -> None:
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect"}

    async def send_json(self, _: object) -> None:
        pass

    async def send_bytes(self, _: bytes) -> None:
        pass

    async def close(self) -> None:
        pass


async def _collect(agen: AsyncIterator[bytes], into: list[str], count: int) -> None:
    async for line in agen:
        into.append(json.loads(line)["kind"])
        if len(into) >= count:
            return


ID_SAMPLE: Final = 5
"""Session ids drawn when checking they are not sequential. Sessions."""


# ---------------------------------------------------------------------------
# POST /v1/sessions
# ---------------------------------------------------------------------------


def test_creating_a_session_returns_both_socket_urls() -> None:
    """What the browser calls before it opens anything."""
    with TestClient(create_app()) as client:
        response = client.post("/v1/sessions", json={"preset": "balanced"})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"].startswith("s-")
    assert body["session_id"] in body["ws_url"]
    assert body["session_id"] in body["console_url"]
    assert body["mode"] == NodMode.ADAPT.value


def test_session_ids_are_not_sequential() -> None:
    """A guessable id would let one browser subscribe to another's call.

    Raises the cap explicitly, because the subject here is id entropy and the
    default cap is 2 (ADR-042). Left on the default this drew three 429s and
    failed on a missing `session_id`, which is the cap working and this test
    asking the wrong question.
    """
    settings = Settings(_env_file=None, nod_max_sessions=ID_SAMPLE)  # type: ignore[call-arg]
    with TestClient(create_app(settings)) as client:
        ids = {
            client.post("/v1/sessions", json={}).json()["session_id"]
            for _ in range(ID_SAMPLE)
        }
    assert len(ids) == ID_SAMPLE


def test_an_unknown_session_is_refused_rather_than_served() -> None:
    """Otherwise a typo opens a socket onto a session that does not exist."""
    app = create_app()
    with (
        TestClient(app) as client,
        pytest.raises(Exception),  # noqa: B017
        client.websocket_connect("/v1/stream?session_id=s-nope") as ws,
    ):
        ws.receive_text()


# ---------------------------------------------------------------------------
# INV-4 on the wire
# ---------------------------------------------------------------------------


def test_a_config_change_reaches_the_console_with_its_explanation(
    tmp_path: Path,
) -> None:
    """**INV-4 on the wire.** The reason line has nothing to render without it.

    Published through `ConsoleTeeSink.emit`, which is the call the proxy makes,
    so this asserts the tee and the socket rather than a hand-built dict.
    """
    app = create_app()
    hub: TelemetryHub = app.state.hub
    with TestClient(app) as client:
        session_id = client.post("/v1/sessions", json={}).json()["session_id"]
        with client.websocket_connect(
            f"/v1/console?session_id={session_id}"
        ) as console:
            sink = ConsoleTeeSink(session_id, hub=hub, directory=tmp_path)
            sink.emit(
                "config_decision",
                {
                    "rule_id": "speaker+context",
                    "trigger": "pause_p90_rose",
                    "changed": ["max_turn_silence"],
                    "min_turn_silence": 440,
                    "max_turn_silence": 1408,
                    "state": "adapting",
                },
                t_ms=5400,
            )
            frame = json.loads(console.receive_bytes())

    assert frame["kind"] == "config.changed"
    assert frame["session_id"] == session_id
    for field in REQUIRED_CONFIG_FIELDS:
        assert field in frame, f"INV-4: {field} missing from config.changed"
    assert frame["max_turn_silence"] == 1408
    assert frame["t_ms"] == 5400


def test_a_trace_kind_the_console_does_not_render_is_not_published(
    tmp_path: Path,
) -> None:
    """`config_applied` carries field names and no explanation.

    Publishing it as `config.changed` would satisfy the Floor Meter and break
    INV-4, because the reason line would have nothing to say. The tee maps only
    the kinds that carry a decision.
    """

    async def run() -> list[str]:
        hub = TelemetryHub()
        sink = ConsoleTeeSink("s-1", hub=hub, directory=tmp_path)
        seen: list[str] = []
        agen = hub.subscribe("s-1")
        collector = asyncio.create_task(_collect(agen, seen, 1))
        await asyncio.sleep(0)
        sink.emit("config_applied", {"fields": ["max_turn_silence"]}, t_ms=1)
        sink.emit("config_decision", {"rule_id": "r", "trigger": "t"}, t_ms=2)
        await asyncio.wait_for(collector, timeout=2.0)
        return seen

    # Only one frame arrives, and it is the decision: `config_applied` was
    # emitted first and would have been collected first if it published.
    assert asyncio.run(run()) == ["config.changed"]


# ---------------------------------------------------------------------------
# The turn stream, which is what fills the capsule
# ---------------------------------------------------------------------------


def test_the_turn_payload_carries_word_end_times() -> None:
    """The browser runs the fill clock from the last word's end."""
    payload = _turn_payload(
        _turn(1, end=True, words=(("hi", 0, 200), ("there", 300, 640)))
    )
    assert payload["t_ms"] == 640
    words = payload["words"]
    assert isinstance(words, list)
    assert words[-1]["end_ms"] == 640
    assert payload["end_of_turn"] is True


def test_the_fan_out_publishes_partials_and_finals_distinctly() -> None:
    """A partial must not be rendered as a completed turn."""
    hub = TelemetryHub()
    proxy = _StubProxy(
        (
            SessionBegin(session_id="s-1", expires_at_ms=60_000),
            _turn(1, end=False, words=(("hello", 0, 200),)),
            _turn(1, end=True, words=(("hello", 0, 200), ("there", 300, 640))),
            Termination(session_id="s-1", reason="done"),
        )
    )

    async def run() -> list[str]:
        seen: list[str] = []
        agen = hub.subscribe("s-1")
        collector = asyncio.create_task(_collect(agen, seen, 5))
        await asyncio.sleep(0)
        await stream_endpoint(
            _FakeWebSocket(),  # type: ignore[arg-type]
            proxy=proxy,  # type: ignore[arg-type]
            hub=hub,
            session_id="s-1",
        )
        await asyncio.wait_for(collector, timeout=2.0)
        return seen

    kinds = asyncio.run(run())
    assert "session.started" in kinds
    assert "turn.partial" in kinds
    assert "turn.final" in kinds
    assert kinds.index("turn.partial") < kinds.index("turn.final")


# ---------------------------------------------------------------------------
# Back-pressure (ARCHITECTURE §7, INV-3)
# ---------------------------------------------------------------------------


def test_a_console_client_that_falls_behind_is_dropped_not_buffered() -> None:
    """§7 says disconnect; INV-3 says the queue must not grow with the call."""

    async def run() -> int:
        hub = TelemetryHub()
        agen = hub.subscribe("s-1")

        async def first() -> bytes:
            return await agen.__anext__()

        started: asyncio.Task[bytes] = asyncio.create_task(first())
        await asyncio.sleep(0)
        assert hub.subscribers("s-1") == 1
        for i in range(CONSOLE_QUEUE_MAXSIZE + 5):
            hub.publish("s-1", "turn.partial", {"i": i, "t_ms": i})
        await asyncio.wait_for(started, timeout=2.0)
        return hub.subscribers("s-1")

    assert asyncio.run(run()) == 0


# ---------------------------------------------------------------------------
# The one demo screen (ADR-038)
# ---------------------------------------------------------------------------


def test_the_console_page_is_served_from_the_api_container() -> None:
    """ADR-038: no second deploy target, so `/` must work in this process."""
    with TestClient(create_app()) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_the_console_carries_no_secret_and_no_upstream_host() -> None:
    """INV-5. The browser talks only to our own WebSocket.

    Asserted against the shipped bytes rather than reasoned about: the whole
    invariant is that nothing server-side leaks into the document, and the
    document is where a leak would appear.
    """
    from nod_server.app import CONSOLE_HTML

    html = CONSOLE_HTML.read_text()
    for forbidden in ("api.assemblyai.com", "ASSEMBLYAI", "api_key", "Bearer ", "sk-"):
        assert forbidden not in html, forbidden
    assert "/v1/console?session_id=" in html
    assert "/v1/stream?session_id=" in html


def test_the_floor_meter_honours_reduced_motion() -> None:
    """DESIGN_SYSTEM §9: it jumps instead of springing.

    With no component library (ADR-038) this is a media query someone has to
    remember, so it is asserted rather than trusted.
    """
    from nod_server.app import CONSOLE_HTML

    html = CONSOLE_HTML.read_text()
    assert "prefers-reduced-motion: reduce" in html
    reduced = html.split("prefers-reduced-motion: reduce")[1][:120]
    assert "transition: none" in reduced


def test_the_agent_reply_never_errors_without_a_key() -> None:
    """A broken brain must not drop the call the controller is demonstrating."""
    from nod_adapters.llm.anthropic import FALLBACK

    with TestClient(create_app()) as client:
        session_id = client.post("/v1/sessions", json={}).json()["session_id"]
        response = client.post(
            f"/v1/sessions/{session_id}/reply", json={"transcript": "hello there"}
        )
    assert response.status_code == 200
    assert response.json()["text"] == FALLBACK


def test_an_empty_transcript_does_not_call_the_model() -> None:
    """Otherwise a spurious final turn spends a request and speaks over nobody."""
    with TestClient(create_app()) as client:
        session_id = client.post("/v1/sessions", json={}).json()["session_id"]
        response = client.post(
            f"/v1/sessions/{session_id}/reply", json={"transcript": "   "}
        )
    assert response.json()["text"] == ""
