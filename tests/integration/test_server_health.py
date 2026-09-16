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

import httpx
import pytest

from nod_core.config import Settings
from nod_server.app import READINESS_CHECKS, create_app

SERVER_BOOT_TIMEOUT_S = 30.0
SERVER_POLL_INTERVAL_S = 0.2


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(Settings()))
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
async def test_readyz_is_503_until_every_subsystem_lands() -> None:
    """Phase 0 meets none of the four conditions of DEPLOYMENT.md §4."""
    async with _client() as client:
        response = await client.get("/readyz")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    body = response.json()
    assert body["status"] == "not_ready"

    reported = {check["name"] for check in body["checks"]}
    assert reported == {check.name for check in READINESS_CHECKS}
    assert set(body["not_ready"]) == reported


@pytest.mark.asyncio
async def test_readyz_names_what_is_missing_rather_than_being_opaque() -> None:
    """A 503 that does not say what is missing is not worth serving."""
    async with _client() as client:
        body = (await client.get("/readyz")).json()

    for check in body["checks"]:
        assert check["detail"], f"{check['name']} has no explanation"
        assert check["implemented_in"], f"{check['name']} has no phase"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/metrics", "/v1/voices", "/v1/presets"])
async def test_other_routes_still_raise_not_implemented(path: str) -> None:
    """The Phase 0 exception covers `/healthz` and `/readyz` only."""
    transport = httpx.ASGITransport(
        app=create_app(Settings()), raise_app_exceptions=True
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://nod.test") as c:
        with pytest.raises(NotImplementedError):
            await c.get(path)


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


def test_make_run_binds_and_serves_health(uvicorn_server: str) -> None:
    """`make run` binds a port and serves both health endpoints.

    This drives `uvicorn --factory nod_server.app:create_app`, the same target
    string the Makefile uses, so a broken factory path fails here rather than
    during deployment.
    """
    health = httpx.get(f"{uvicorn_server}/healthz", timeout=5.0)
    assert health.status_code == HTTPStatus.OK
    assert health.json() == {"status": "ok"}

    ready = httpx.get(f"{uvicorn_server}/readyz", timeout=5.0)
    assert ready.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert ready.json()["status"] == "not_ready"
