"""FastAPI application and HTTP routes.

Routes follow ARCHITECTURE.md §7 exactly. There is deliberately no module-level
`app`: `make run` uses `uvicorn --factory`, so importing this module never runs
application construction.

Scope exception to the Phase 0 "every stub raises" rule, granted explicitly and
limited to this file: `create_app`, `/healthz` and `/readyz` are real. Health
endpoints are infrastructure, not business logic, and leaving them unimplemented
would mean the uvicorn factory, the container healthcheck and the readiness path
go unexercised until deployment week. Every other route here still raises
`NotImplementedError`, including `/metrics`.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from nod_adapters.llm.anthropic import AnthropicClient
from nod_adapters.protocols import Message
from nod_core.arbiter import DEFAULT_CEILING_MS
from nod_core.config import Settings
from nod_core.types import JsonValue, NodMode, Voice
from nod_server.telemetry import ConsoleTeeSink, TelemetryHub
from nod_server.ws import console_endpoint, stream_endpoint

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from nod_core.proxy import SessionProxy

type Runner = Callable[[], Coroutine[None, None, None]]
"""What a factory hands back to be driven as a task: the proxy's `run`."""

CONSOLE_HTML: Final = Path(__file__).parent / "static" / "index.html"
"""The one demo screen. Served from the API container; no second deploy target."""

SAMPLE_RATE_HZ: Final = 16000
"""Caller audio is mono 16 kHz PCM16 (ARCHITECTURE.md §7)."""

router = APIRouter(prefix="/v1")
health_router = APIRouter()


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One condition `/readyz` reports on (DEPLOYMENT.md §4)."""

    name: str
    ready: bool
    detail: str
    implemented_in: str


READINESS_CHECKS: Final = (
    ReadinessCheck(
        name="config_loaded",
        ready=False,
        detail="nod_core.config.get_settings is not implemented",
        implemented_in="P0",
    ),
    ReadinessCheck(
        name="data_volume_writable",
        ready=False,
        detail="NOD_TRACE_DIR writability is unchecked; the trace sink lands in P6",
        implemented_in="P6",
    ),
    ReadinessCheck(
        name="sqlite_reachable",
        ready=False,
        detail="the SQLite index of ARCHITECTURE.md §6 is not opened yet",
        implemented_in="P6",
    ),
    ReadinessCheck(
        name="capability_probe_cached",
        ready=False,
        detail="nod_core.capabilities.probe lands in P1 (ADR-001 is still pending)",
        implemented_in="P1",
    ),
)
"""The four conditions of DEPLOYMENT.md §4, none of them met at Phase 0.

`/readyz` reports every one of them so the 503 says what is missing rather than
being opaque. As each subsystem lands, flip its entry to a live check.
"""


def default_proxy_factory(
    settings: Settings,
) -> Callable[[SessionRecord, TelemetryHub], Awaitable[tuple[SessionProxy, Runner]]]:
    """Build the factory that gives each session a proxy over a real upstream.

    Returned rather than inlined so a test can replace `app.state.proxy_factory`
    with one over `FakeAssemblyAI` and never touch the network (INV-7).

    The sink is a `ConsoleTeeSink`, which is the whole console wiring: the proxy
    already emits every record INV-4 and INV-8 require, and teeing them costs no
    change to the controller path.

    Args:
        settings: Process settings, holding the upstream key and trace dir.

    Returns:
        An async factory from a session record to `(proxy, run)`.
    """

    async def factory(
        record: SessionRecord, hub: TelemetryHub
    ) -> tuple[SessionProxy, Runner]:
        from nod_adapters.assemblyai.session import AssemblyAISession
        from nod_core.arbiter import Arbiter
        from nod_core.capabilities import MEASURED_CAPABILITIES
        from nod_core.profiler import Profiler
        from nod_core.proxy import SessionProxy as _Proxy

        upstream = AssemblyAISession(
            api_key=(
                settings.assemblyai_api_key.get_secret_value()
                if settings.assemblyai_api_key
                else ""
            ),
            model=settings.nod_model,
            sample_rate=SAMPLE_RATE_HZ,
            config={},
        )
        await upstream.__aenter__()
        proxy = _Proxy(
            upstream=upstream,
            profiler=Profiler(),
            arbiter=Arbiter(
                capabilities=MEASURED_CAPABILITIES, ceiling_ms=record.ceiling_ms
            ),
            trace=ConsoleTeeSink(
                record.session_id, hub=hub, directory=settings.nod_trace_dir
            ),
            mode=record.mode,
            ceiling_ms=record.ceiling_ms,
        )
        return proxy, proxy.run

    return factory


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Wires CORS deny-by-default from `NOD_ALLOWED_ORIGINS` (never `*`) and the
    routes of ARCHITECTURE.md §7. Bearer auth on mutating routes lands with
    `nod_server.auth` in P6.

    Args:
        settings: Process settings. Built from the environment when omitted;
            `get_settings` is the cached accessor and is still a stub.

    Returns:
        The configured application.
    """
    resolved = settings if settings is not None else Settings()

    app = FastAPI(
        title="Nod",
        description="Adaptive turn-timing controller for voice agents.",
        version="0.1.0",
    )

    # CORS defaults to deny; the console origin is allow-listed explicitly and
    # never `*` (ARCHITECTURE.md §7, INV-5).
    if resolved.nod_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.nod_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.state.settings = resolved
    app.state.sessions = {}
    app.state.hub = TelemetryHub()
    app.state.proxy_factory = default_proxy_factory(resolved)
    app.state.llm = AnthropicClient(
        resolved.llm_api_key.get_secret_value() if resolved.llm_api_key else ""
    )
    app.include_router(health_router)
    app.include_router(router)

    @app.websocket("/v1/stream")
    async def _stream(  # pragma: no cover - exercised by the integration test
        websocket: WebSocket, session_id: str
    ) -> None:
        """Route `WS /v1/stream` onto a live `SessionProxy`."""
        record = app.state.sessions.get(session_id)
        if record is None:
            await websocket.close(code=4404)
            return
        factory = app.state.proxy_factory
        proxy, run = await factory(record, app.state.hub)
        task = asyncio.create_task(run())
        try:
            await stream_endpoint(
                websocket,
                proxy=proxy,
                hub=app.state.hub,
                session_id=session_id,
                nod_preset=record.preset,
                nod_mode=record.mode,
                nod_ceiling_ms=record.ceiling_ms,
            )
        finally:
            task.cancel()
            await proxy.aclose()

    @app.get("/", include_in_schema=False)
    async def _console_page() -> Response:
        """Serve the one demo screen (ROADMAP §3, ADR-038).

        Read per request rather than cached: the file is 8 KB and the demo is
        edited live during rehearsal, where a stale cache costs more than the
        read does.
        """
        return HTMLResponse(CONSOLE_HTML.read_text(encoding="utf-8"))

    @app.websocket("/v1/console")
    async def _console(  # pragma: no cover - exercised by the integration test
        websocket: WebSocket, session_id: str
    ) -> None:
        """Route `WS /v1/console` onto the fan-out."""
        await console_endpoint(websocket, session_id, hub=app.state.hub)

    return app


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One created session, before its socket connects."""

    session_id: str
    preset: str
    mode: NodMode
    ceiling_ms: int


def new_session_id() -> str:
    """A session id. `O(1)`. Opaque, URL-safe and not guessable."""
    return f"s-{secrets.token_urlsafe(9)}"


@router.post("/sessions")
async def create_session(
    body: dict[str, JsonValue], request: Request
) -> dict[str, JsonValue]:
    """Create a session. Returns its id and WebSocket URL.

    The record is held in memory: ARCHITECTURE §6's SQLite index is for
    completed sessions and CLAUDE.md §7 forbids a second state store before
    the roadmap says so.

    Args:
        body: Optional `preset`, `mode` and `ceiling_ms`.
        request: For the app-scoped registry.

    Returns:
        `session_id`, `ws_url` and `console_url`.
    """
    preset = str(body.get("preset", "balanced"))
    mode = NodMode(str(body.get("mode", NodMode.ADAPT.value)))
    ceiling = int(body.get("ceiling_ms", DEFAULT_CEILING_MS))  # type: ignore[arg-type]
    record = SessionRecord(
        session_id=new_session_id(), preset=preset, mode=mode, ceiling_ms=ceiling
    )
    registry: dict[str, SessionRecord] = request.app.state.sessions
    registry[record.session_id] = record
    return {
        "session_id": record.session_id,
        "ws_url": f"/v1/stream?session_id={record.session_id}",
        "console_url": f"/v1/console?session_id={record.session_id}",
        "preset": record.preset,
        "mode": record.mode.value,
        "ceiling_ms": record.ceiling_ms,
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, JsonValue]:
    """Return session metadata and its config timeline."""
    raise NotImplementedError


@router.get("/sessions/{session_id}/trace")
async def get_session_trace(session_id: str) -> Response:
    """Download the session trace as JSONL, redacted unless authorised (INV-6)."""
    raise NotImplementedError


@router.post("/sessions/{session_id}/reply")
async def agent_reply(
    session_id: str, body: dict[str, JsonValue], request: Request
) -> dict[str, JsonValue]:
    """The reference intake agent's next line (ADR-035 clause 1, thinned).

    Text in, text out. The browser speaks it with `speechSynthesis`, so no
    audio crosses this boundary and no TTS key exists to leak (INV-5).

    **Off the turn-timing path by construction** (CLAUDE.md §7): this is a
    separate request the console makes *after* a turn has already ended. The
    arbiter has decided and the patch has been sent before this is called.

    Args:
        session_id: The session, for the console topic.
        body: `{"transcript": str}` — what the caller just said.
        request: For the app-scoped client and hub.

    Returns:
        `{"text": ...}`. Never an error: a broken brain must not drop a call.
    """
    transcript = str(body.get("transcript", "")).strip()
    if not transcript:
        return {"text": ""}
    client = request.app.state.llm
    text = await client.reply([Message(role="user", content=transcript)])
    request.app.state.hub.publish(
        session_id, "agent.state", {"state": "speaking", "text": text, "t_ms": 0}
    )
    return {"text": text}


@router.get("/voices")
async def list_voices() -> Sequence[Voice]:
    """List available TTS voices with provider, latency class and cost class."""
    raise NotImplementedError


@router.post("/sessions/{session_id}/voice")
async def switch_voice(session_id: str, body: dict[str, JsonValue]) -> Response:
    """Switch voice mid-session without dropping the call (EC-28)."""
    raise NotImplementedError


@router.get("/presets")
async def get_presets() -> Sequence[dict[str, JsonValue]]:
    """Read the saved presets."""
    raise NotImplementedError


@router.post("/presets")
async def save_preset(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Save a preset, validated against the same closed schema as a policy."""
    raise NotImplementedError


@router.post("/bench/runs")
async def start_bench_run(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Start a bench run. Returns the run id."""
    raise NotImplementedError


@router.get("/bench/runs/{run_id}")
async def get_bench_run(run_id: str) -> dict[str, JsonValue]:
    """Return bench run status and results."""
    raise NotImplementedError


@health_router.get("/healthz")
async def healthz() -> Response:
    """Liveness. Green when the process is alive; never touches upstream.

    DEPLOYMENT.md §4: this endpoint never calls AssemblyAI, SQLite or the disk,
    now or later. It answers exactly one question — is this process running.

    Returns:
        `200 {"status": "ok"}`.
    """
    return JSONResponse(status_code=HTTPStatus.OK, content={"status": "ok"})


@health_router.get("/readyz")
async def readyz() -> Response:
    """Readiness: config loaded, `/data` writable, SQLite reachable, probe cached.

    Deliberately does not call AssemblyAI. An upstream outage must not take the
    container out of rotation, because `observe` mode and replay mode still work
    (DEPLOYMENT.md §4).

    At Phase 0 none of the four conditions is met, so this reports every one of
    them by name rather than returning a bare 503.

    Returns:
        `200` once every check passes, otherwise `503` listing what is missing.
    """
    checks = [
        {
            "name": check.name,
            "ready": check.ready,
            "detail": check.detail,
            "implemented_in": check.implemented_in,
        }
        for check in READINESS_CHECKS
    ]
    ready = all(check.ready for check in READINESS_CHECKS)
    status = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=status,
        content={
            "status": "ready" if ready else "not_ready",
            "not_ready": [c.name for c in READINESS_CHECKS if not c.ready],
            "checks": checks,
        },
    )


@health_router.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics. Always green (DEPLOYMENT.md §4)."""
    raise NotImplementedError
