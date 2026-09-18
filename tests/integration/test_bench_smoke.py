"""The bench leg of `make check` (CLAUDE.md §5).

At Phase 0 this asserts the harness is addressable: the five `nod_bench` modules
of CLAUDE.md §3 import, and each CLI entry point exists and honours the P0 stub
contract. At P3 this becomes the real offline bench run against
`FakeAssemblyAI`, which is what makes `make bench` work on a clean clone with no
API key (BENCH_SPEC.md §7).
"""

from __future__ import annotations

import importlib
from pathlib import Path

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
    """Each `python -m nod_bench.<mod>` target has a callable `main`."""
    module = importlib.import_module(name)
    assert callable(module.main)


@pytest.mark.smoke
def test_the_replay_cli_refuses_a_missing_corpus(tmp_path: Path) -> None:
    """`make bench` on a clean clone, where no corpus has been built.

    It must fail with an explanation and a non-zero code, not a traceback and
    not a silent empty chart. This replaces the stub tripwire that asserted
    `main` raised `NotImplementedError`, which failed the moment Gate E
    implemented it — which is what it was for.

    `tmp_path` matters: calling `main([])` with defaults would run a real bench
    and write into `bench/runs`, so the test would mutate the repository it is
    checking.
    """
    from nod_bench.replay import main

    code = main(
        ["--fake", "--corpus", str(tmp_path / "absent"), "--out", str(tmp_path)]
    )
    assert code == 2


@pytest.mark.smoke
def test_the_replay_cli_refuses_a_live_run(tmp_path: Path) -> None:
    """BENCH_SPEC §4 reserves live runs for Phase 4; this driver is offline."""
    from nod_bench.replay import main

    assert main(["--live", "--out", str(tmp_path)]) == 2


@pytest.mark.smoke
def test_readme_table_region_is_machine_owned() -> None:
    """INV-9: the README's results table is generated, never hand-written."""
    from pathlib import Path

    readme = Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert report.README_TABLE_START in text
    assert report.README_TABLE_END in text
    assert text.index(report.README_TABLE_START) < text.index(report.README_TABLE_END)
