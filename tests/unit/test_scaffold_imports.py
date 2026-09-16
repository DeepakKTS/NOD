"""Every module in the scaffold imports cleanly.

A module that raises at import time would be invisible to the stub-contract test
below it, and would break `uvicorn --factory` and the bench CLIs.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

MODULES = (
    "nod_core",
    "nod_core.arbiter",
    "nod_core.capabilities",
    "nod_core.config",
    "nod_core.policy",
    "nod_core.profiler",
    "nod_core.proxy",
    "nod_core.trace",
    "nod_core.types",
    "nod_adapters",
    "nod_adapters.protocols",
    "nod_adapters.assemblyai",
    "nod_adapters.llm",
    "nod_adapters.tts",
    "nod_server",
    "nod_server.app",
    "nod_server.auth",
    "nod_server.telemetry",
    "nod_server.ws",
    "nod_bench",
    "nod_bench.corpus",
    "nod_bench.metrics",
    "nod_bench.perturb",
    "nod_bench.replay",
    "nod_bench.report",
)


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


@pytest.mark.parametrize(
    "package",
    ["nod_core", "nod_adapters", "nod_server", "nod_bench"],
)
def test_package_is_typed(package: str) -> None:
    """PEP 561: the py.typed marker ships, so consumers get our annotations."""
    module = importlib.import_module(package)
    assert module.__file__ is not None
    assert (Path(module.__file__).parent / "py.typed").is_file()
