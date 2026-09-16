"""FastAPI application and HTTP routes.

Routes follow ARCHITECTURE.md §7 exactly. There is deliberately no module-level
`app`: `make run` uses `uvicorn --factory`, so importing this module never runs
application construction.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, FastAPI, Response

from nod_core.config import Settings
from nod_core.types import JsonValue, Voice

router = APIRouter(prefix="/v1")
health_router = APIRouter()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Wires CORS deny-by-default from `NOD_ALLOWED_ORIGINS` (never `*`), bearer
    auth on mutating routes when `NOD_AUTH=required`, and the routes of
    ARCHITECTURE.md §7.

    Args:
        settings: Process settings; read from the environment when omitted.

    Returns:
        The configured application.
    """
    raise NotImplementedError


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
    """Liveness. Green when the process is alive; never touches upstream."""
    raise NotImplementedError


@health_router.get("/readyz")
async def readyz() -> Response:
    """Readiness: config loaded, `/data` writable, SQLite reachable, probe cached.

    Deliberately does not call AssemblyAI. An upstream outage must not take the
    container out of rotation, because `observe` and replay still work
    (DEPLOYMENT.md §4).
    """
    raise NotImplementedError


@health_router.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics. Always green (DEPLOYMENT.md §4)."""
    raise NotImplementedError
