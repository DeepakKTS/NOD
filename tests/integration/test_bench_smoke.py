"""The bench leg of `make check` (CLAUDE.md §5).

At Phase 0 this asserts the harness is addressable: the five `nod_bench` modules
of CLAUDE.md §3 import, and each CLI entry point exists and honours the P0 stub
contract. At P3 this becomes the real offline bench run against
`FakeAssemblyAI`, which is what makes `make bench` work on a clean clone with no
API key (BENCH_SPEC.md §7).
"""

from __future__ import annotations

import importlib

import pytest

from nod_bench import report

BENCH_MODULES = (
    "nod_bench.corpus",
    "nod_bench.metrics",
    "nod_bench.perturb",
    "nod_bench.replay",
    "nod_bench.report",
)

CLI_MODULES = (
    "nod_bench.corpus",
    "nod_bench.metrics",
    "nod_bench.replay",
    "nod_bench.report",
)


@pytest.mark.smoke
@pytest.mark.parametrize("name", BENCH_MODULES)
def test_bench_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


@pytest.mark.smoke
@pytest.mark.parametrize("name", CLI_MODULES)
def test_bench_cli_entry_point_exists(name: str) -> None:
    """Each `python -m nod_bench.<mod>` target has a `main`."""
    module = importlib.import_module(name)
    main = module.main
    assert callable(main)
    with pytest.raises(NotImplementedError):
        main([])


@pytest.mark.smoke
def test_readme_table_region_is_machine_owned() -> None:
    """INV-9: the README's results table is generated, never hand-written."""
    from pathlib import Path

    readme = Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert report.README_TABLE_START in text
    assert report.README_TABLE_END in text
    assert text.index(report.README_TABLE_START) < text.index(report.README_TABLE_END)
