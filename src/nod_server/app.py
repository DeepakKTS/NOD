"""FastAPI application and HTTP routes.

Routes follow ARCHITECTURE.md §7 exactly. There is deliberately no module-level
`app`: `make run` uses `uvicorn --factory`, so importing this module never runs
application construction.

Scope exception to the Phase 0 "every stub raises" rule, granted explicitly and
limited to this file: `create_app` and the three health endpoints are real. Health
endpoints are infrastructure, not business logic, and leaving them unimplemented
would mean the uvicorn factory, the container healthcheck and the readiness path
go unexercised until deployment week.

`/metrics` joined them at Gate 4a — it is the only stub on DEPLOYMENT.md §4's
path, and the registry it renders was already fully built. `/readyz`'s four checks
became real conditions in the same gate (ADR-041); they were hardcoded `False`, so
it returned 503 unconditionally against an exit criterion that asks for green
health checks. Every other route here still raises `NotImplementedError`.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST

from nod_adapters.llm.anthropic import FALLBACK, AnthropicClient
from nod_adapters.protocols import Message
from nod_core.arbiter import DEFAULT_CEILING_MS
from nod_core.config import Settings
from nod_core.policy import load_policy
from nod_core.types import ExpectedAnswer, JsonValue, NodMode, Voice
from nod_server.context import DeclaredContext, classify_prompt
from nod_server.telemetry import (
    SESSION_ACTIVE,
    ConsoleTeeSink,
    TelemetryHub,
    configure_logging,
    render_metrics,
)
from nod_server.ws import console_endpoint, stream_endpoint

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from nod_core.proxy import SessionProxy

type Runner = Callable[[], Coroutine[None, None, None]]
"""What a factory hands back to be driven as a task: the proxy's `run`."""

CONSOLE_HTML: Final = Path(__file__).parent / "static" / "index.html"
"""The one demo screen. Served from the API container; no second deploy target."""

SAMPLE_RATE_HZ: Final = 16000
"""Caller audio is mono 16 kHz PCM16 (ARCHITECTURE.md §7)."""

SESSION_REGISTRY_CAP: Final = 32
"""Created-but-unconnected session records kept before the oldest is evicted.

A record is a dict entry and costs nothing; the credit is spent by the
**stream** socket, which is where `NOD_MAX_SESSIONS` binds. Bounded anyway so
a long-running server cannot grow one entry per page load for ever (INV-3's
spirit, applied to the registry rather than to a call).
"""

WS_CLOSE_UNKNOWN_SESSION: Final = 4404
"""WebSocket close code for an unknown `session_id`. Mirrors HTTP 404."""

WS_CLOSE_AT_CAPACITY: Final = 4429
"""WebSocket close code when `NOD_MAX_SESSIONS` is reached. Mirrors HTTP 429.

A WebSocket cannot carry an HTTP status once the upgrade has been accepted, so the
code is refused before `accept()` and the number is chosen to read as the status it
stands for, exactly as `WS_CLOSE_UNKNOWN_SESSION` does.
"""

SQLITE_BUSY_TIMEOUT_S: Final = 5.0
"""Busy timeout on the index connection. Seconds (ARCHITECTURE.md §6)."""

SQLITE_SYNCHRONOUS: Final = "NORMAL"
"""`PRAGMA synchronous` for the index (ARCHITECTURE.md §6)."""

router = APIRouter(prefix="/v1")
health_router = APIRouter()


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One condition `/readyz` reports on (DEPLOYMENT.md §4)."""

    name: str
    ready: bool
    detail: str
    implemented_in: str


type ReadinessProbe = Callable[[Settings], ReadinessCheck]
"""A condition evaluated per request, against the settings actually in force."""


def _check_config_loaded(settings: Settings) -> ReadinessCheck:
    """Settings parsed, and the one credential with no default is present."""
    missing = [] if settings.assemblyai_api_key else ["ASSEMBLYAI_API_KEY"]
    return ReadinessCheck(
        name="config_loaded",
        ready=not missing,
        detail=(
            f"parsed; model {settings.nod_model}, "
            f"mode {settings.nod_mode_default.value}"
            if not missing
            else f"missing required setting(s): {', '.join(missing)}"
        ),
        implemented_in="P4",
    )


def _check_path_writable(name: str, path: Path) -> ReadinessCheck:
    """Create `path` and write a probe file into it, then remove the probe.

    Asserting writability by *writing* rather than by `os.access`, which reports
    the permission bits and not the outcome: a read-only mount, a full disk and an
    SELinux denial all pass `os.access` and fail the first write. The check that
    matters is the one the trace sink will actually perform.
    """
    probe = path / ".readyz-probe"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
    except OSError as exc:
        return ReadinessCheck(
            name=name,
            ready=False,
            detail=f"{path} not writable: {exc}",
            implemented_in="P4",
        )
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return ReadinessCheck(
        name=name, ready=True, detail=f"{path} writable", implemented_in="P4"
    )


def _check_data_volume_writable(settings: Settings) -> ReadinessCheck:
    """`NOD_TRACE_DIR` exists and accepts a write (DEPLOYMENT.md §4)."""
    return _check_path_writable("data_volume_writable", settings.nod_trace_dir)


def _check_sqlite_reachable(settings: Settings) -> ReadinessCheck:
    """Open `NOD_DB_PATH`, set the §6 pragmas, and read one back.

    **Reachability only. The index is not populated in this build**, and saying so
    here is the point: ARCHITECTURE §6 specifies `sessions`, `config_changes` and
    `bench_runs`, nothing writes them, and traces are JSONL on disk. Reporting
    "ready" for a schema that does not exist would be the flattering direction, so
    the detail names what was actually verified — that the file opens, WAL mode
    takes, and the directory accepts a write.

    Uses stdlib `sqlite3` rather than `aiosqlite` because this opens and closes one
    connection and awaits nothing; the call is wrapped in `to_thread` by the route.
    """
    try:
        settings.nod_db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(settings.nod_db_path, timeout=SQLITE_BUSY_TIMEOUT_S) as db:
            mode = str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            db.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS}")
    except (OSError, sqlite3.Error) as exc:
        return ReadinessCheck(
            name="sqlite_reachable",
            ready=False,
            detail=f"{settings.nod_db_path} unreachable: {exc}",
            implemented_in="P4",
        )
    if mode.lower() != "wal":
        return ReadinessCheck(
            name="sqlite_reachable",
            ready=False,
            detail=f"WAL refused; journal_mode is {mode!r} (ARCHITECTURE.md §6)",
            implemented_in="P4",
        )
    return ReadinessCheck(
        name="sqlite_reachable",
        ready=True,
        detail=(
            f"{settings.nod_db_path} opens, journal_mode=wal; "
            "reachability only, the §6 index is unpopulated in this build"
        ),
        implemented_in="P4",
    )


def _check_capability_probe_cached(settings: Settings) -> ReadinessCheck:
    """ADR-001's verdicts are available, and cover both knobs §4 emits.

    The "cache" is `capabilities.MEASURED_CAPABILITIES`, a committed constant
    rather than a live probe result — ADR-001 measured it once and the bench and
    the server deliberately read the same object. So this asserts the thing that
    can actually go wrong: that the verdicts still cover every field `WIRE_FIELDS`
    intends to send. A knob dropped to `INERT` there would silently stop the
    controller patching, which is the product failing quietly.
    """
    from nod_core.arbiter import WIRE_FIELDS
    from nod_core.capabilities import MEASURED_CAPABILITIES
    from nod_core.types import KnobVerdict

    verdicts = dict(MEASURED_CAPABILITIES.knobs)
    not_live = [
        wire for wire, _ in WIRE_FIELDS if verdicts.get(wire) is not KnobVerdict.LIVE
    ]
    return ReadinessCheck(
        name="capability_probe_cached",
        ready=not not_live,
        detail=(
            f"ADR-001 verdicts for {settings.nod_model}; "
            f"{len(verdicts)} knobs, both wire fields LIVE"
            if not not_live
            else f"knob(s) not LIVE, the controller cannot patch them: {not_live}"
        ),
        implemented_in="P4",
    )


READINESS_PROBES: Final[tuple[ReadinessProbe, ...]] = (
    _check_config_loaded,
    _check_data_volume_writable,
    _check_sqlite_reachable,
    _check_capability_probe_cached,
)
"""The four conditions of DEPLOYMENT.md §4, each evaluated per request (ADR-041).

They were a static tuple with `ready=False` written into every entry, which made
`/readyz` return 503 unconditionally — against a Phase 4 exit criterion that asks
for green health checks, and worse, with no way for any of them to ever go green
or for a *real* failure to be distinguished from the placeholder. A check whose
result does not depend on the system is the §5 shape: it cannot fail, because it
cannot pass either.
"""


def readiness(settings: Settings) -> tuple[ReadinessCheck, ...]:
    """Evaluate every condition. `O(1)` calls, each doing one small disk probe.

    Args:
        settings: The settings in force for this process.

    Returns:
        One result per probe, in `READINESS_PROBES` order.
    """
    return tuple(probe(settings) for probe in READINESS_PROBES)


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
            context=record.context,
        )
        return proxy, proxy.run

    return factory


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Wires CORS deny-by-default from `NOD_ALLOWED_ORIGINS` (never `*`) and the
    routes of ARCHITECTURE.md §7.

    **There is no bearer auth and that is a decision** (ADR-042). `nod_server.auth`
    stays a stub and `NOD_AUTH` now defaults to `off` rather than to a `required`
    nothing honoured. What a public URL actually needed was a bound on cost, not on
    access, so `NOD_MAX_SESSIONS` refuses over its limit instead.

    Args:
        settings: Process settings. Built from the environment when omitted;
            `get_settings` is the cached accessor.

    Returns:
        The configured application.
    """
    resolved = settings if settings is not None else Settings()
    configure_logging(resolved.nod_log_level)

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
    app.state.live_streams = 0
    app.state.hub = TelemetryHub()
    app.state.last_reply_text = ""
    app.state.proxy_factory = default_proxy_factory(resolved)
    # Compiled once per app rather than per session: `hint_for` is on the
    # controller's per-turn path and re-reading a YAML file there would be I/O
    # inside the decision loop (INV-2).
    try:
        app.state.policy = load_policy(Path("config/policy.yaml"))
    except (OSError, ValueError):
        # A missing or malformed policy leaves the axis neutral rather than
        # refusing to boot — INV-8's direction applied to configuration.
        app.state.policy = None
    app.state.llm = AnthropicClient(
        resolved.llm_api_key.get_secret_value() if resolved.llm_api_key else ""
    )
    app.include_router(health_router)
    app.include_router(router)

    @app.websocket("/v1/stream")
    async def _stream(  # pragma: no cover - exercised by the integration test
        websocket: WebSocket, session_id: str
    ) -> None:
        """Route `WS /v1/stream` onto a live `SessionProxy`.

        **This is where the cap has to bind**, not on `POST /v1/sessions`. That
        route only adds a dict entry and costs nothing; the upstream socket — and
        therefore the credit — is opened by the factory below. Capping only the
        POST would leave a deployed URL able to spend the key from any browser.
        """
        record = app.state.sessions.get(session_id)
        if record is None:
            await websocket.close(code=WS_CLOSE_UNKNOWN_SESSION)
            return
        limit: int = app.state.settings.nod_max_sessions
        if app.state.live_streams >= limit:
            await websocket.close(code=WS_CLOSE_AT_CAPACITY)
            return
        factory = app.state.proxy_factory
        proxy, run = await factory(record, app.state.hub)
        app.state.live_streams += 1
        SESSION_ACTIVE.inc()
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
            app.state.live_streams -= 1
            SESSION_ACTIVE.dec()
            # A session is single-use: its record exists to carry preset/mode
            # from the POST to the socket, and once the socket is gone nothing
            # can reach it again. Dropping it here is what keeps the registry
            # from being a leak (ADR-058).
            app.state.sessions.pop(session_id, None)
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
        """Route `WS /v1/console` onto the fan-out.

        **Unknown ids are refused**, matching `/v1/stream`. Accepting them made
        a broken client look healthy: a browser that had failed to create a
        session connected `?session_id=undefined`, the socket was accepted, the
        status pill went green, and nothing ever arrived because no such
        session existed. A console that subscribes to nothing must say so
        (ADR-058).
        """
        if session_id not in app.state.sessions:
            await websocket.close(code=WS_CLOSE_UNKNOWN_SESSION)
            return
        await console_endpoint(websocket, session_id, hub=app.state.hub)

    return app


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One created session, before its socket connects."""

    session_id: str
    preset: str
    mode: NodMode
    ceiling_ms: int
    context: DeclaredContext | None = None
    """The context axis for this session, or `None` when not opted in.

    **Opt-in, and off by default.** The axis multiplies the window the speaker
    profile computed, so switching it on changes the behaviour the demo was
    verified against. `?context=on` keeps the filmed path byte-identical while
    the new one is exercised beside it (Gate 4k)."""


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
    registry_now: dict[str, SessionRecord] = request.app.state.sessions
    # **The cap does not belong here, and putting it here was a lockout.**
    # This route adds a dict entry; `WS /v1/stream` opens the upstream socket
    # and is where `NOD_MAX_SESSIONS` binds (see `_stream`). Records were never
    # removed, so capping on registry *size* refused every page load after the
    # second one, permanently, until the process restarted — and the browser
    # read `session_id` off the 429 body as `undefined` and connected a console
    # to a session that did not exist (ADR-058). Bound by eviction instead.
    while len(registry_now) >= SESSION_REGISTRY_CAP:
        registry_now.pop(next(iter(registry_now)))
    want_context = bool(body.get("context", False))
    preset = str(body.get("preset", "balanced"))
    # **`NOD_MODE_DEFAULT` was read by `/readyz` and applied by nothing.** The
    # default here was the literal `NodMode.ADAPT`, so a deployment configured
    # for `observe` reported "mode observe" on its readiness endpoint and
    # created every session in `adapt` — patching live calls on the setting the
    # README calls "the safe first step in any real deployment". A setting that
    # describes behaviour nothing implements is CLAUDE.md §5's recurring defect;
    # this one had an instrument confirming the configuration that was not in
    # force, which is worse than silence (ADR-063).
    default_mode: NodMode = request.app.state.settings.nod_mode_default
    mode = NodMode(str(body.get("mode", default_mode.value)))
    ceiling = int(body.get("ceiling_ms", DEFAULT_CEILING_MS))  # type: ignore[arg-type]
    record = SessionRecord(
        session_id=new_session_id(),
        preset=preset,
        mode=mode,
        ceiling_ms=ceiling,
        context=DeclaredContext(request.app.state.policy) if want_context else None,
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
        "context": record.context is not None,
    }


def _unbuilt(name: str) -> NoReturn:
    """Refuse a designed-but-unbuilt route with 501, never 500.

    **These eight routes shipped registered and raising `NotImplementedError`.**
    FastAPI turns an uncaught exception into `500 Internal Server Error`, and the
    OpenAPI schema advertised all eight at `/docs` as though they worked. A
    stranger on the deployed URL would have read eight endpoints off the schema
    and got a server error from every one — the API claiming a surface the
    process does not have (ADR-060).

    501 is the exact code: the route is part of the design in ARCHITECTURE.md
    §API and is recognised, it is simply not built. The body says so rather than
    leaving the caller to guess from a status line.

    Args:
        name: The route, for the message.

    Raises:
        HTTPException: always, 501.
    """
    raise HTTPException(
        status_code=int(HTTPStatus.NOT_IMPLEMENTED),
        detail={
            "error": "not_implemented",
            "route": name,
            "detail": (
                f"{name} is designed in docs/ARCHITECTURE.md and not built in "
                "this build. It is excluded from the OpenAPI schema so nothing "
                "advertises it."
            ),
        },
    )


def unbuilt[F: Callable[..., object]](fn: F) -> F:
    """Mark a route as designed-but-unbuilt, for the schema guard to find.

    `test_no_unbuilt_route_reaches_the_public_schema` reads this marker. The
    marker is the cheap half; the test also scans every registered endpoint for
    a bare `NotImplementedError`, so a future stub that forgets to mark itself
    is caught anyway.
    """
    fn.__nod_unbuilt__ = True  # type: ignore[attr-defined]
    return fn


@router.get("/sessions/{session_id}", include_in_schema=False)
@unbuilt
async def get_session(session_id: str) -> dict[str, JsonValue]:
    """Return session metadata and its config timeline."""
    _unbuilt("GET /v1/sessions/{session_id}")


@router.get("/sessions/{session_id}/trace", include_in_schema=False)
@unbuilt
async def get_session_trace(session_id: str) -> Response:
    """Download the session trace as JSONL, redacted unless authorised (INV-6)."""
    _unbuilt("GET /v1/sessions/{session_id}/trace")


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
    record = request.app.state.sessions.get(session_id)
    client = request.app.state.llm
    text = await client.reply([Message(role="user", content=transcript)])

    # **The scripted intake agent, on the opted-in path only.** Without an LLM
    # key the brain returns one constant, which asks nothing, so the context
    # axis would be switched on and still never fire. The script asks real
    # intake questions; the classifier reads them exactly as it would read a
    # model's. A configured model wins — `reply()` returns `FALLBACK` only when
    # there is no client at all.
    if record is not None and record.context is not None and text == FALLBACK:
        text = record.context.next_prompt()

    # **The agent declares what its own question invites.** Off the turn-timing
    # path by construction: this runs after a turn ended, and the class applies
    # to the *next* one (CLAUDE.md §7, and the route docstring above).
    # Kept so a test can assert the declared class was derived from *this*
    # reply rather than from a constant it also hard-codes.
    request.app.state.last_reply_text = text
    declared: ExpectedAnswer | None = None
    if record is not None and record.context is not None:
        declared = classify_prompt(text)
        record.context.declare(declared)

    request.app.state.hub.publish(
        session_id,
        "agent.state",
        {"state": "speaking", "text": text, "expects": declared or "", "t_ms": 0},
    )
    return {"text": text, "expects": declared or ""}


@router.get("/voices", include_in_schema=False)
@unbuilt
async def list_voices() -> Sequence[Voice]:
    """List available TTS voices with provider, latency class and cost class."""
    _unbuilt("GET /v1/voices")


@router.post("/sessions/{session_id}/voice", include_in_schema=False)
@unbuilt
async def switch_voice(session_id: str, body: dict[str, JsonValue]) -> Response:
    """Switch voice mid-session without dropping the call (EC-28)."""
    _unbuilt("POST /v1/sessions/{session_id}/voice")


@router.get("/presets", include_in_schema=False)
@unbuilt
async def get_presets() -> Sequence[dict[str, JsonValue]]:
    """Read the saved presets."""
    _unbuilt("GET /v1/presets")


@router.post("/presets", include_in_schema=False)
@unbuilt
async def save_preset(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Save a preset, validated against the same closed schema as a policy."""
    _unbuilt("POST /v1/presets")


@router.post("/bench/runs", include_in_schema=False)
@unbuilt
async def start_bench_run(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Start a bench run. Returns the run id."""
    _unbuilt("POST /v1/bench/runs")


@router.get("/bench/runs/{run_id}", include_in_schema=False)
@unbuilt
async def get_bench_run(run_id: str) -> dict[str, JsonValue]:
    """Return bench run status and results."""
    _unbuilt("GET /v1/bench/runs/{run_id}")


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
async def readyz(request: Request) -> Response:
    """Readiness: config loaded, `/data` writable, SQLite reachable, probe cached.

    Deliberately does not call AssemblyAI. An upstream outage must not take the
    container out of rotation, because `observe` mode and replay mode still work
    (DEPLOYMENT.md §4).

    Each condition is evaluated against the settings actually in force (ADR-041),
    so a 503 now means a real failure and names it. The probes touch the disk, so
    they run in a thread rather than on the event loop.

    Returns:
        `200` once every check passes, otherwise `503` listing what is missing.
    """
    settings: Settings = request.app.state.settings
    results = await asyncio.to_thread(readiness, settings)
    checks = [
        {
            "name": check.name,
            "ready": check.ready,
            "detail": check.detail,
            "implemented_in": check.implemented_in,
        }
        for check in results
    ]
    ready = all(check.ready for check in results)
    status = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=status,
        content={
            "status": "ready" if ready else "not_ready",
            "not_ready": [c.name for c in results if not c.ready],
            "checks": checks,
        },
    )


@health_router.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics. Always green (DEPLOYMENT.md §4).

    "Always" is the contract: §4's table gives no condition under which this is
    allowed to fail, because a scrape endpoint that 503s during an incident
    removes the telemetry exactly when it is needed. It reads a process-local
    registry and touches neither upstream nor disk, so there is nothing here to
    be unavailable.
    """
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)
