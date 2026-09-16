"""Module-boundary rules that ruff cannot express, enforced by AST inspection.

ARCHITECTURE.md §2 says `nod_core` imports the adapter protocols and never a
concrete adapter. That single rule is what makes `FakeAssemblyAI` and offline
tests possible (INV-7). CLAUDE.md §6 says controller timestamps are
stream-relative milliseconds and never `time.time()` (EC-08). CLAUDE.md §3 says
`librosa` and `soundfile` are bench-only and stay out of the runtime image.

Ruff's banned-api table is global, so none of these can be scoped to a package
there. They live here instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

NOD_CORE = Path(__file__).resolve().parents[2] / "src" / "nod_core"

BANNED_CORE_IMPORTS = (
    "nod_adapters.assemblyai",
    "nod_adapters.llm",
    "nod_adapters.tts",
    "nod_bench",
    "nod_server",
    "librosa",
    "soundfile",
)


def _core_modules() -> list[Path]:
    return sorted(NOD_CORE.glob("*.py"))


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_nod_core_modules_exist() -> None:
    assert _core_modules(), "nod_core has no modules; the AST rules below are vacuous"


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: p.name)
def test_nod_core_imports_no_concrete_adapter(module: Path) -> None:
    """ARCHITECTURE.md §2: nod_core imports Protocols, never a concrete adapter."""
    imported = _imported_names(ast.parse(module.read_text(encoding="utf-8")))
    for banned in BANNED_CORE_IMPORTS:
        offenders = {name for name in imported if name.startswith(banned)}
        assert not offenders, f"{module.name} imports {offenders}, banned by §2"


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: p.name)
def test_nod_core_never_reads_the_wall_clock(module: Path) -> None:
    """CLAUDE.md §6 and EC-08: stream-relative ms only; wall clock is telemetry."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"time", "time_ns"}:
            value = node.value
            assert not (isinstance(value, ast.Name) and value.id == "time"), (
                f"{module.name} reads the wall clock; use stream-relative ms (EC-08)"
            )


def test_arbiter_decide_is_synchronous() -> None:
    """INV-1 and INV-2: a sync `decide` cannot be awaited from the audio pump."""
    source = (NOD_CORE / "arbiter.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "decide"
    ]
    assert found, "arbiter.decide is missing"
    for node in found:
        assert not isinstance(node, ast.AsyncFunctionDef), (
            "decide() must be `def`, not `async def`: INV-1 relies on it being "
            "impossible to await from pump_audio_up"
        )
