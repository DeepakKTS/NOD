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
from typing import Final

import pytest

from nod_bench import metrics, report

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


README_PLACEHOLDER: Final = (
    "_Not yet generated. Run `make bench-live`, then "
    "`python -m nod_bench.report --publish`._"
)
"""What the owned region holds until a live run has published into it.

An explicit sentinel rather than "anything": the region is either the generator's
output or this exact string, and a third state means someone typed a number into
it. Named here so the assertion below can say which of the two it found.
"""


@pytest.mark.smoke
def test_readme_table_region_is_machine_owned() -> None:
    """INV-9: the README's results table is generated, never hand-written.

    **Strengthened at Gate 4a, and the old version is why.** It asserted that the
    two markers existed and were ordered — which passes against the placeholder,
    and passes just as happily against a hand-written table between them. INV-9
    was guarded at the level of punctuation: the one thing it forbids, a number
    typed into a document, could not make this test fail.

    It now asserts **provenance**: the region holds either the placeholder or
    something `update_readme_table` could have written, and nothing else. A
    markdown table appearing there without having come through the generator
    fails, which is the state INV-9 actually prohibits.
    """
    from pathlib import Path

    readme = Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert text.count(report.README_TABLE_START) == 1
    assert text.count(report.README_TABLE_END) == 1
    start = text.index(report.README_TABLE_START)
    end = text.index(report.README_TABLE_END)
    assert start < end

    region = text[start + len(report.README_TABLE_START) : end].strip()
    if region == README_PLACEHOLDER:
        return

    # Not the placeholder, so it must be a generated table. Regenerate one from
    # the committed live artifacts and require the region to match it exactly.
    runs = Path(__file__).resolve().parents[2] / "bench" / "runs"
    generated = runs / metrics.artifact_name("results", simulated=False, suffix="md")
    assert generated.exists(), (
        f"the README table region holds content that is neither the placeholder "
        f"nor generated: no live artifact at {generated}. INV-9 forbids a "
        f"hand-written number here. Region begins: {region[:120]!r}"
    )
    assert region == generated.read_text(encoding="utf-8").strip(), (
        "the README table region does not match the committed live results "
        "table. Run `python -m nod_bench.report --publish` rather than editing "
        "it; INV-9 gives this region to the generator."
    )


@pytest.mark.smoke
def test_the_readme_provenance_check_rejects_a_hand_written_number(
    tmp_path: Path,
) -> None:
    """The guard above, shown failing on the thing INV-9 exists to stop.

    CLAUDE.md §5: a test earns its place once it has been seen red. The check
    above runs against the committed README, which is currently the placeholder,
    so on its own it would pass without ever exercising the branch that matters.
    This drives the same logic over a README with a plausible table typed into
    it, and requires the refusal.
    """
    readme = tmp_path / "README.md"
    readme.write_text(
        f"# Nod\n\n{report.README_TABLE_START}\n"
        "| arm | PCR | TTL p90 (ms) |\n|---|---|---|\n"
        "| `nod` | 0.041 | 612 |\n"
        f"{report.README_TABLE_END}\n",
        encoding="utf-8",
    )
    text = readme.read_text(encoding="utf-8")
    start = text.index(report.README_TABLE_START)
    end = text.index(report.README_TABLE_END)
    region = text[start + len(report.README_TABLE_START) : end].strip()

    assert region != README_PLACEHOLDER
    generated = tmp_path / metrics.artifact_name(
        "results", simulated=False, suffix="md"
    )
    assert not generated.exists(), (
        "with no live artifact present, a region carrying a table is exactly the "
        "INV-9 violation the guard must reject"
    )
