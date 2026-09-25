"""The health endpoints, and the uvicorn factory `make run` uses.

These are the one Phase 0 exception to "every stub raises": health endpoints are
infrastructure, not business logic. Testing them now means the uvicorn factory,
the container healthcheck (DEPLOYMENT.md §3) and the readiness path
(DEPLOYMENT.md §4) are exercised from day one rather than first tried during
deployment week.

The in-process tests drive the ASGI app through `httpx.ASGITransport` rather than
`starlette.testclient`, which avoids a deprecation warning that
`filterwarnings = ["error"]` would otherwise turn into a failure. Neither opens a
socket, so INV-7 holds trivially.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx
import pytest
from pydantic import SecretStr

from nod_core.config import Settings
from nod_core.types import NodMode
from nod_server.app import READINESS_PROBES, SESSION_REGISTRY_CAP, create_app, readiness

SERVER_BOOT_TIMEOUT_S = 30.0
SERVER_POLL_INTERVAL_S = 0.2


def _settings(**overrides: object) -> Settings:
    """Settings built from explicit values, never from the repository `.env`.

    `_env_file=None` matters: `Settings()` reads `.env`, which on a developer
    machine carries a real `ASSEMBLYAI_API_KEY` and in CI does not. A readiness
    test built on that passes locally and fails in CI for reasons that have
    nothing to do with readiness.
    """
    # `_env_file` is a pydantic-settings init kwarg its generated signature does
    # not declare, and `**overrides` is a heterogeneous splat no annotation fits.
    # Both are runtime-correct and neither is checkable.
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg, arg-type]


def _ready_settings(tmp_path: Path) -> Settings:
    """Every condition of DEPLOYMENT.md §4 satisfiable, on a temp volume."""
    return _settings(
        assemblyai_api_key="test-key-not-a-real-one",
        nod_trace_dir=tmp_path / "traces",
        nod_db_path=tmp_path / "nod.db",
    )


def _client(settings: Settings | None = None) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(
        app=create_app(settings if settings is not None else _settings())
    )
    return httpx.AsyncClient(transport=transport, base_url="http://nod.test")


@pytest.mark.asyncio
async def test_healthz_is_200_ok() -> None:
    """DEPLOYMENT.md §4: green when the process is alive."""
    async with _client() as client:
        response = await client.get("/healthz")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_healthz_never_touches_upstream() -> None:
    """DEPLOYMENT.md §4: liveness never calls AssemblyAI.

    The autouse INV-7 guard in conftest already blocks outbound connections, so
    a `/healthz` that reached upstream would fail here rather than pass quietly.
    """
    async with _client() as client:
        for _ in range(3):
            assert (await client.get("/healthz")).status_code == HTTPStatus.OK


@pytest.mark.asyncio
async def test_readyz_is_200_once_every_condition_is_actually_met(
    tmp_path: Path,
) -> None:
    """ADR-041: the four checks are real, so they can go green.

    This is the direction the previous version of this file could not test at
    all. `ready` was hardcoded `False` in every entry, so `/readyz` returned 503
    for any input and the only available assertion was that it did. A check that
    cannot pass cannot fail either (CLAUDE.md §5).
    """
    async with _client(_ready_settings(tmp_path)) as client:
        response = await client.get("/readyz")

    body = response.json()
    assert response.status_code == HTTPStatus.OK, body["not_ready"]
    assert body["status"] == "ready"
    assert body["not_ready"] == []
    assert {check["name"] for check in body["checks"]} == {
        probe(_ready_settings(tmp_path)).name for probe in READINESS_PROBES
    }
    assert all(check["ready"] for check in body["checks"])


@pytest.mark.asyncio
async def test_readyz_is_503_and_names_a_missing_credential(tmp_path: Path) -> None:
    """DEPLOYMENT.md §4: a 503 has to say which condition failed."""
    settings = _settings(
        nod_trace_dir=tmp_path / "traces", nod_db_path=tmp_path / "nod.db"
    )
    async with _client(settings) as client:
        response = await client.get("/readyz")

    body = response.json()
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert body["status"] == "not_ready"
    assert body["not_ready"] == ["config_loaded"]
    detail = next(c["detail"] for c in body["checks"] if c["name"] == "config_loaded")
    assert "ASSEMBLYAI_API_KEY" in detail


def test_the_data_volume_check_detects_an_unwritable_path(tmp_path: Path) -> None:
    """The check writes rather than consulting the permission bits.

    Pointed at a path *under a regular file*, so `mkdir` raises whatever the
    platform raises and the check has to report it. Chosen over `chmod 0o500`
    because that is a no-op for root, and a test that silently passes in a
    container running as root is worth nothing.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_bytes(b"")
    results = {
        check.name: check
        for check in readiness(
            _settings(
                assemblyai_api_key="k",
                nod_trace_dir=blocker / "traces",
                nod_db_path=tmp_path / "nod.db",
            )
        )
    }
    volume = results["data_volume_writable"]
    assert not volume.ready
    assert "not writable" in volume.detail


def test_the_sqlite_check_confirms_wal_rather_than_only_opening(
    tmp_path: Path,
) -> None:
    """ARCHITECTURE.md §6 specifies WAL; opening the file does not establish it."""
    results = {check.name: check for check in readiness(_ready_settings(tmp_path))}
    sqlite_check = results["sqlite_reachable"]
    assert sqlite_check.ready
    assert "journal_mode=wal" in sqlite_check.detail
    assert "unpopulated" in sqlite_check.detail, (
        "the detail must not imply the ARCHITECTURE §6 index exists; it does not"
    )


def test_the_capability_check_goes_red_when_a_wire_knob_is_not_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A knob demoted to INERT stops the controller patching, silently.

    The whole reason this condition is on `/readyz` rather than assumed: the
    verdicts are a committed constant, so the failure arrives by edit rather than
    by outage, and nothing else in the running process would notice.
    """
    from nod_core import capabilities
    from nod_core.types import Capabilities, ConfidenceField, KnobVerdict

    demoted = Capabilities(
        knobs=(
            ("min_turn_silence", KnobVerdict.LIVE),
            ("max_turn_silence", KnobVerdict.INERT),
        ),
        confidence_field=ConfidenceField.VARYING,
        force_endpoint=KnobVerdict.LIVE,
        has_word_timings=True,
    )
    monkeypatch.setattr(capabilities, "MEASURED_CAPABILITIES", demoted)

    results = {check.name: check for check in readiness(_ready_settings(tmp_path))}
    probe = results["capability_probe_cached"]
    assert not probe.ready
    assert "max_turn_silence" in probe.detail


@pytest.mark.asyncio
async def test_readyz_names_what_is_missing_rather_than_being_opaque() -> None:
    """A 503 that does not say what is missing is not worth serving."""
    async with _client() as client:
        body = (await client.get("/readyz")).json()

    for check in body["checks"]:
        assert check["detail"], f"{check['name']} has no explanation"
        assert check["implemented_in"], f"{check['name']} has no phase"


@pytest.mark.asyncio
async def test_metrics_renders_prometheus_text() -> None:
    """DEPLOYMENT.md §4: `/metrics` is green unconditionally, Prometheus format."""
    async with _client() as client:
        response = await client.get("/metrics")

    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "nod_turns_total" in body
    assert "nod_decide_seconds" in body
    assert "# HELP" in body and "# TYPE" in body


@pytest.mark.asyncio
async def test_metrics_is_green_even_when_readiness_is_not() -> None:
    """A scrape endpoint that 503s during an incident removes the telemetry.

    §4's table gives no condition under which `/metrics` may fail, and this is
    that clause asserted rather than assumed: the same app that returns 503 from
    `/readyz` must still serve metrics.
    """
    async with _client() as client:
        assert (await client.get("/readyz")).status_code == (
            HTTPStatus.SERVICE_UNAVAILABLE
        )
        assert (await client.get("/metrics")).status_code == HTTPStatus.OK


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return port


@pytest.fixture
def uvicorn_server() -> Iterator[str]:
    """Run the exact server invocation behind `make run`, on a free port."""
    port = _free_port()
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "--factory",
        "nod_server.app:create_app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    base_url = f"http://127.0.0.1:{port}"

    # The context manager closes the stdout pipe on exit; leaving it open trips
    # the ResourceWarning that `filterwarnings = ["error"]` escalates.
    with subprocess.Popen(  # noqa: S603
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        try:
            deadline = time.monotonic() + SERVER_BOOT_TIMEOUT_S
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("uvicorn exited before binding a port")
                try:
                    httpx.get(f"{base_url}/healthz", timeout=1.0)
                except httpx.HTTPError:
                    time.sleep(SERVER_POLL_INTERVAL_S)
                else:
                    break
            else:
                pytest.fail(f"uvicorn did not bind within {SERVER_BOOT_TIMEOUT_S}s")
            yield base_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


READYZ_CHECKS: Final = frozenset(
    {
        "config_loaded",
        "data_volume_writable",
        "sqlite_reachable",
        "capability_probe_cached",
    }
)
"""The four conditions DEPLOYMENT §4 requires `/readyz` to report (ADR-041).

Named here rather than read back from the response, so dropping a check is a
failure rather than a smaller set that still agrees with itself.
"""


def test_make_run_binds_and_serves_health(uvicorn_server: str) -> None:
    """`make run` binds a port and serves both health endpoints.

    This drives `uvicorn --factory nod_server.app:create_app`, the same target
    string the Makefile uses, so a broken factory path fails here rather than
    during deployment.
    """
    health = httpx.get(f"{uvicorn_server}/healthz", timeout=5.0)
    assert health.status_code == HTTPStatus.OK
    assert health.json() == {"status": "ok"}

    # **Not asserted: which verdict `/readyz` returns.** It used to assert
    # `503 not_ready`, which held only because no `.env` existed — the moment
    # the app was configured well enough to actually run (Gate 4h-prep), the
    # paths became writable, the probe cache resolved, and the same server the
    # docstring is about started returning `200 ready`. A test that fails when
    # the software is configured correctly is testing the developer's machine.
    #
    # What is asserted is the part that is environment-independent and is what
    # this test is for: the endpoint is served, the document is well formed,
    # and the verdict **agrees with its own checks**. That last line is the
    # aggregation invariant, and it is stronger than the status code was.
    ready = httpx.get(f"{uvicorn_server}/readyz", timeout=5.0)
    assert ready.status_code in {HTTPStatus.OK, HTTPStatus.SERVICE_UNAVAILABLE}
    body = ready.json()
    assert {c["name"] for c in body["checks"]} == READYZ_CHECKS
    failing = [c["name"] for c in body["checks"] if not c["ready"]]
    assert sorted(body["not_ready"]) == sorted(failing)
    assert body["status"] == ("ready" if not failing else "not_ready")
    assert (ready.status_code == HTTPStatus.OK) == (not failing)


# --- NOD_MAX_SESSIONS, the cost bound that replaced auth (ADR-042) ----------


def test_the_session_cap_is_derived_from_the_measured_upstream_limit() -> None:
    """ADR-042: the default is arithmetic over two measured facts, not a choice.

    Asserted as the relation rather than as `== 2`, so re-measuring the account
    limit moves the cap and this test follows. The previous value was `64`, which
    was twelve times what the account permits and was not derived from anything.
    """
    from nod_core.config import (
        MAX_CONCURRENT_SESSIONS,
        UPSTREAM_CONCURRENCY_LIMIT,
        UPSTREAM_SOCKETS_PER_SESSION_PEAK,
    )

    assert (
        MAX_CONCURRENT_SESSIONS * UPSTREAM_SOCKETS_PER_SESSION_PEAK
        <= UPSTREAM_CONCURRENCY_LIMIT
    ), (
        "every live session can hold "
        f"{UPSTREAM_SOCKETS_PER_SESSION_PEAK} upstream sockets during rotation, so "
        f"{MAX_CONCURRENT_SESSIONS} sessions can exceed the "
        f"{UPSTREAM_CONCURRENCY_LIMIT}-session account limit and refuse a rotation "
        "mid-call"
    )
    assert _settings().nod_max_sessions == MAX_CONCURRENT_SESSIONS


@pytest.mark.asyncio
async def test_creating_sessions_is_never_refused_by_registry_size() -> None:
    """`POST /v1/sessions` must not lock a server out, and it used to.

    This test replaces one that asserted the third POST returns 429 against
    `nod_max_sessions = 2`. That behaviour was real and it was a **permanent
    lockout**: no record was ever removed from the registry, so the third page
    load of the server's life 429'd and every load after it did too, until the
    process restarted. `_stream`'s own docstring already said the cap belongs on
    the socket — "that route only adds a dict entry and costs nothing" — and the
    POST capped anyway, on a number that only ever grew (ADR-058).

    The old test passed, which is the point worth keeping: it asserted the
    symptom as though it were the contract.
    """
    settings = _settings(nod_max_sessions=2)
    async with _client(settings) as client:
        codes = [
            (await client.post("/v1/sessions", json={})).status_code for _ in range(6)
        ]
    assert codes == [HTTPStatus.OK] * 6, "three times the cap, none refused"


@pytest.mark.asyncio
async def test_the_session_registry_is_bounded_by_eviction() -> None:
    """Unbounded is the other failure, so the registry evicts rather than grows.

    One entry per page load, for ever, is a leak on a long-running server.
    Asserted against `SESSION_REGISTRY_CAP` by *behaviour* — the oldest id stops
    resolving — rather than by reading `len()`, so a cap that is counted but not
    enforced fails here.
    """
    settings = _settings(nod_max_sessions=2)
    async with _client(settings) as client:
        first = (await client.post("/v1/sessions", json={})).json()["session_id"]
        for _ in range(SESSION_REGISTRY_CAP):
            await client.post("/v1/sessions", json={})
        newest = (await client.post("/v1/sessions", json={})).json()["session_id"]
        registry = client._transport.app.state.sessions  # type: ignore[attr-defined]

    assert first not in registry, "the oldest record should have been evicted"
    assert newest in registry, "the newest record must survive"
    assert len(registry) <= SESSION_REGISTRY_CAP


def _every_api_route(app: object) -> list[object]:
    """Every endpoint-bearing route, walking included routers.

    This FastAPI version keeps `include_router` results as nested
    `_IncludedRouter` objects rather than flattening them into `app.routes`, so
    a single-level scan sees one route and reports a clean bill. That is the
    disconnected-instrument shape (CLAUDE.md §5): the first version of this
    helper returned 1 route and would have passed against all eight defects.
    """
    found: list[object] = []
    stack = list(getattr(app, "routes", []))
    seen: set[int] = set()
    while stack:
        route = stack.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        stack.extend(getattr(route, "routes", None) or [])
        nested = getattr(route, "original_router", None)
        stack.extend(getattr(nested, "routes", None) or [])
        if hasattr(route, "endpoint"):
            found.append(route)
    return found


def test_no_unbuilt_route_reaches_the_public_schema() -> None:
    """The schema must not advertise a route the process cannot serve.

    Eight routes shipped registered and raising `NotImplementedError`. FastAPI
    renders an uncaught exception as **500**, and `/docs` listed all eight as
    working endpoints — so a judge opening the OpenAPI schema on the deployed
    URL would read a surface the build does not have and get a server error
    from every one of them (ADR-060).

    This is the census guard's shape one domain over: the fact was right where
    it lived — the docstrings never claimed these were built — and wrong where
    it travelled, which was the published schema.

    Two checks, because the marker alone is the weak half:

    1. Nothing marked `@unbuilt` appears in `app.openapi()["paths"]`.
    2. **No registered endpoint's source raises `NotImplementedError` at all.**
       A future stub that forgets the marker is caught by this one.
    """
    import inspect

    app = create_app()
    schema_paths = set(app.openapi()["paths"])
    routes = _every_api_route(app)
    assert len(routes) > 5, f"the walk found only {len(routes)} routes; it is broken"

    marked = [r for r in routes if getattr(r.endpoint, "__nod_unbuilt__", False)]  # type: ignore[attr-defined]
    assert marked, "no route carries the unbuilt marker; the guard has nothing to check"
    leaked = sorted(
        r.path  # type: ignore[attr-defined]
        for r in marked
        if any(sp.endswith(r.path) for sp in schema_paths)  # type: ignore[attr-defined]
    )
    assert not leaked, f"unbuilt routes advertised in the public schema: {leaked}"

    raisers = []
    for route in routes:
        try:
            src = inspect.getsource(route.endpoint)  # type: ignore[attr-defined]
        except (OSError, TypeError):  # pragma: no cover - builtin endpoints
            continue
        if "raise NotImplementedError" in src:
            raisers.append(route.path)  # type: ignore[attr-defined]
    assert not raisers, (
        f"registered endpoints still raise NotImplementedError, which FastAPI "
        f"renders as 500: {sorted(raisers)}"
    )


@pytest.mark.asyncio
async def test_an_unbuilt_route_answers_501_and_says_which(tmp_path: Path) -> None:
    """501, not 500, and the body names the route rather than leaving a guess.

    500 tells a caller the server broke. 501 tells them the route is recognised
    and not built, which is the true statement and the one a judge can act on.
    """
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/v1/voices")
    assert response.status_code == HTTPStatus.NOT_IMPLEMENTED
    detail = response.json()["detail"]
    assert detail["error"] == "not_implemented"
    assert detail["route"] == "GET /v1/voices"
    assert "ARCHITECTURE.md" in detail["detail"]


@pytest.mark.asyncio
async def test_a_session_is_created_in_the_configured_mode_not_a_literal() -> None:
    """`NOD_MODE_DEFAULT=observe` must reach the session, not just `/readyz`.

    The default was the literal `NodMode.ADAPT`, so a deployment configured for
    `observe` reported "mode observe" on its readiness endpoint and created
    every session in `adapt`. README calls observe "the safe first step in any
    real deployment ... it cannot change a call's behaviour", and it silently
    could (ADR-063).

    The fixture sets `observe` precisely because it is **not** the code's
    literal default: asserting against `adapt` would pass whether the setting
    were read or ignored, which is the vacuous-assertion shape CLAUDE.md §5
    exists to stop.
    """
    app = create_app(
        Settings(
            assemblyai_api_key=SecretStr("k" * 32),
            nod_mode_default=NodMode.OBSERVE,
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        created = await client.post("/v1/sessions", json={})
        ready = await client.get("/readyz")
    assert created.json()["mode"] == "observe", (
        "the session ignored NOD_MODE_DEFAULT; a deployment set to observe "
        "would patch live calls"
    )
    assert "mode observe" in ready.text, "readiness and the session must agree"
    # an explicit request still wins over the configured default
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        forced = await client.post("/v1/sessions", json={"mode": "adapt"})
    assert forced.json()["mode"] == "adapt"
