"""Shared pytest configuration.

INV-7: unit and integration tests never touch the network. The autouse fixture
below enforces that structurally rather than by convention — it blocks outbound
connections to anything that is not loopback, so `FakeAssemblyAI` on 127.0.0.1
still works while a real endpoint raises.

Tests marked `live` are excluded by `-m 'not live'` in the pytest `addopts`, and
are exempted here so they behave normally when run deliberately.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest

from nod_core.config import get_settings

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", ""})


def _is_loopback(address: Any) -> bool:
    """Return whether a socket address refers to the local machine."""
    if not isinstance(address, tuple) or not address:
        # AF_UNIX paths and the like never leave the machine.
        return True
    host = address[0]
    if not isinstance(host, str):
        return False
    if host in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_external_network(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Block outbound non-loopback connections for every non-live test (INV-7)."""
    if request.node.get_closest_marker("live") is not None:
        yield
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if not _is_loopback(address):
            msg = (
                f"INV-7: tests never touch the network; "
                f"blocked connection to {address!r}. "
                f"Mark the test @pytest.mark.live if it genuinely needs one."
            )
            raise RuntimeError(msg)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if not _is_loopback(address):
            msg = (
                f"INV-7: tests never touch the network; "
                f"blocked connection to {address!r}."
            )
            raise RuntimeError(msg)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Clear the process-wide settings cache around every test.

    `get_settings` is `lru_cache`d so `.env` is parsed once per process. Without
    this, a test that resolves a credential leaves it cached for every later
    test, and a test asserting the *absence* of a key passes or fails depending
    on collection order.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
