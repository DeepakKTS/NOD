"""The bench leg of `make check` (CLAUDE.md §5).

At Phase 0 this asserts the harness is addressable: the five `nod_bench` modules
of CLAUDE.md §3 import, and each CLI entry point exists and honours the P0 stub
contract. At P3 this becomes the real offline bench run against
`FakeAssemblyAI`, which is what makes `make bench` work on a clean clone with no
API key (BENCH_SPEC.md §7).
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from nod_bench import metrics, report

REPO: Final = Path(__file__).resolve().parents[2]

CONFIDENCE_MS: Final = 590.0
"""The commit point ADR-055 publishes. Mirrors `scripts/pilot_ladder.py`."""

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
    generated = runs / metrics.artifact_name("published", simulated=False, suffix="md")
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
        "published", simulated=False, suffix="md"
    )
    assert not generated.exists(), (
        "with no live artifact present, a region carrying a table is exactly the "
        "INV-9 violation the guard must reject"
    )


def test_the_readme_mutation_census_matches_the_tree() -> None:
    """The honest-scope census must not drift away from the catalogue.

    ADR-053 published "24 of 38 test files ... 264 of 565 test definitions" and
    the README carried it. Two gates later the tree had 39 files and 587
    definitions and the README still said 38 and 565 — stale, and stale in the
    **flattering** direction, because the share of the suite that has never been
    given anything to catch had grown while the printed figure had not.

    These are the drift-prone numbers precisely because they are boring: nobody
    recounts them, and a reader cannot tell a current census from a two-week-old
    one. Recomputed here from `tools/mutate.py`'s catalogue and the test tree,
    so the README goes red rather than quietly wrong.

    Deliberately not asserting the passing-test count: that changes on every
    added test and would turn this into a chore that gets silenced. The census
    changes only when mutation coverage genuinely moves.
    """
    sys.path.insert(0, str(REPO / "tools"))
    import mutate as mutate_tool

    tests_root = REPO / "tests"
    files = sorted(tests_root.rglob("test_*.py"))
    targeted = {
        REPO / t
        for name, muts in mutate_tool.CATALOGUE.items()
        if name != "selftest"
        for m in muts
        for t in m.tests
    }
    untargeted_files = [f for f in files if f not in targeted]
    definitions = {
        f: len(re.findall(r"^(?:async )?def test_", f.read_text(), re.M)) for f in files
    }
    total_defs = sum(definitions.values())
    untargeted_defs = sum(definitions[f] for f in untargeted_files)

    readme = (REPO / "README.md").read_text()
    assert f"{len(untargeted_files)} of {len(files)} test" in readme, (
        f"README's untargeted-file census is stale: tree says "
        f"{len(untargeted_files)} of {len(files)}"
    )
    assert f"{untargeted_defs} of {total_defs} test definitions" in readme, (
        f"README's definition census is stale: tree says "
        f"{untargeted_defs} of {total_defs}"
    )
    catalogued = sum(
        len(m) for name, m in mutate_tool.CATALOGUE.items() if name != "selftest"
    )
    assert f"{catalogued} mutations" in readme, (
        f"README's mutation count is stale: catalogue holds {catalogued}"
    )


def test_the_clamp_model_still_fits_every_committed_in_hold_row() -> None:
    """ADR-055's headline, guarded against the artifacts it was read from.

    The deck, the README and the video all say "two parameters, thirteen live
    rows, all inside 120 ms". That sentence was true when it was computed in a
    shell, and nothing in the suite would have noticed it going false — if
    `predict`'s clamp branch drifted, or `CONFIDENCE_MS` moved, or an
    observations file were regenerated from a different run, the claim would
    stay in three documents and stop being true.

    The 13 is asserted explicitly rather than derived from the files: deriving
    it would make this pass for any number of rows, including zero, which is
    the vacuous-invariant shape CLAUDE.md §5 opens with. A run that produced
    fewer in-hold boundaries is a different experiment and should fail here.
    """
    from nod_bench.ladder import LadderGeometry, LadderRow, predict, scores
    from nod_bench.replay import STATIC_ARMS

    runs = REPO / "bench" / "runs"
    checked = 0
    for label in ("say", "sweep"):
        observed = json.loads((runs / f"pilot_ladder.{label}.live.json").read_text())
        pre = json.loads(
            (runs / f"pilot_ladder.{label}.live.predictions.json").read_text()
        )
        geometry = {
            g["hold_label_ms"]: LadderGeometry.model_validate(g)
            for g in pre["geometry"]
        }
        arms = pre.get("arms")
        for raw in observed["rows"]:
            row = LadderRow.model_validate(raw)
            if row.in_hold is None:
                continue
            settings = arms[row.arm] if arms else None
            min_gate = (
                int(settings["min_turn_silence"])
                if settings
                else STATIC_ARMS[row.arm].min_turn_silence
            )
            max_gate = (
                int(settings["max_turn_silence"])
                if settings
                else STATIC_ARMS[row.arm].max_turn_silence
            )
            prediction = predict(
                geometry[row.hold_label_ms],
                row.arm,
                min_gate,
                max_gate,
                "clamped",
                confidence_ms=CONFIDENCE_MS,
            )
            assert scores(row, prediction), (
                f"{label}/{row.arm}/hold {row.hold_label_ms}: observed "
                f"{row.in_hold.fired_at_ms:.0f} vs predicted "
                f"{prediction.fired_at_ms:.0f}"
            )
            checked += 1
    assert checked == 13, f"the published claim is about 13 rows, found {checked}"


PUBLIC_FACING: Final = (
    "README.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "docs/deck.html",
    "docs/VIDEO_SCRIPT.md",
    "docs/SUBMISSION.md",
    "docs/PILOT_REGIME.md",
)
"""Files a judge or a stranger reads first. The claim surface.

`docs/deck.html` stands in for `docs/nod-deck.pdf`: the PDF is generated from
it by `make deck`, so checking the source checks the artifact, and the PDF's
compressed text streams are not greppable anyway.
"""

SHA_PATTERN: Final = re.compile(r"\b[0-9a-f]{7,40}\b")


def test_every_commit_sha_in_a_public_file_resolves() -> None:
    """A fabricated hash reached a shot list; nothing said it was alone.

    Gate 4f cited "committed in `54dcd0a`" in `docs/VIDEO_SCRIPT.md`. No such
    object exists — it was produced to fit a sentence, and a reader who checked
    would have found the project inventing its own provenance. Resolving it
    took one `git log`, which is the whole argument for doing this by machine.

    Hex strings that are not commits are skipped rather than failed: these
    files legitimately contain sha256 digests and hex colours. The check is
    "every string that **looks like a short SHA and is referenced as one**
    resolves", approximated as: a 7-to-40 character hex run that is not a
    64-character digest and not preceded by `#` must be a real commit. That
    approximation is deliberately loose in the direction of *more* checking —
    a false positive here costs a rename, a false negative costs credibility.
    """
    # **A shallow clone cannot answer this question, and must not pretend to.**
    # GitHub Actions checks out with `fetch-depth: 1` by default, so
    # `git cat-file` finds none of the cited commits and every correct document
    # looks broken. That is the instrument-scope failure in CLAUDE.md §5 —
    # `git cat-file` observes the local object store, and "is this a real commit
    # in the project's history" is outside that set when the store is a stump.
    # Checked first so the failure names the clone rather than the docs.
    shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert shallow.stdout.strip() == "false", (
        "this repository is a shallow clone, so no commit SHA can be resolved "
        "and this test can say nothing about the documents. Set "
        "`fetch-depth: 0` on actions/checkout."
    )

    unresolved: list[str] = []
    checked = 0
    for name in PUBLIC_FACING:
        text = (REPO / name).read_text()
        for match in SHA_PATTERN.finditer(text):
            token = match.group(0)
            if len(token) == 64:
                continue  # a sha256 digest, not a commit
            if match.start() and text[match.start() - 1] == "#":
                continue  # a CSS hex colour
            checked += 1
            resolved = subprocess.run(  # noqa: S603
                ["git", "cat-file", "-e", f"{token}^{{commit}}"],  # noqa: S607
                cwd=REPO,
                capture_output=True,
                check=False,
            )
            if resolved.returncode != 0:
                unresolved.append(f"{name}: {token}")
    assert not unresolved, f"unresolvable commit SHAs: {unresolved}"
    assert checked > 0, "the scan matched nothing; the pattern or file list is broken"
