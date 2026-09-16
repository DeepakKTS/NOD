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

from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

from fastapi import APIRouter, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from nod_core.config import Settings
from nod_core.types import JsonValue, Voice

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
    app.include_router(health_router)
    app.include_router(router)
    return app


@router.post("/sessions")
async def create_session(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Create a session. Returns its id and WebSocket URL."""
    raise NotImplementedError


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, JsonValue]:
    """Return session metadata and its config timeline."""
    raise NotImplementedError


@router.get("/sessions/{session_id}/trace")
async def get_session_trace(session_id: str) -> Response:
    """Download the session trace as JSONL, redacted unless authorised (INV-6)."""
    raise NotImplementedError


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
