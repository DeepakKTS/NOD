"""`policy.py`: the context axis, CONTROL_SPEC §3 and ARCHITECTURE §5 and §10.

CLAUDE.md §5 puts this module in the non-negotiable tier in full, and under the
*published-number* rule rather than the arbiter rule: `hint.min_mult` multiplies
`min_ms`, `min_ms` gates responsiveness after a complete utterance, and TTL is
measured off exactly those boundaries. A wrong multiplier is not a wrong hint, it
is a wrong published latency arriving with no symptom — 0.7 where 1.4 belongs
reads as a working controller that is simply faster than it should be.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
import yaml
from pydantic import ValidationError

from nod_core.policy import (
    POLICY_CACHE_SIZE,
    CompiledPolicy,
    PolicyFile,
    _compile_content,
    compile_policy,
    load_policy,
)
from nod_core.types import WindowHint

SPEC: Final = Path(__file__).resolve().parents[2] / "docs" / "CONTROL_SPEC.md"

MINIMAL: Final = """
version: 1
default: {min_mult: 1.0, max_mult: 1.0}
answers:
  boolean: {min_mult: 0.7, max_mult: 0.7}
  spelling: {min_mult: 1.5, max_mult: 2.4}
"""


def _spec_policy_text() -> str:
    """Extract §3's policy example from the spec itself.

    The point of reading it out of the document rather than copying it here: a
    YAML block in a spec is never executed, so *reading* it cannot fail. §3's
    `answers:` key had been lost in an edit and nothing noticed until something
    tried to consume it (ADR-016's fourth instance). This is the consumer.
    """
    match = re.search(
        r"A policy file compiles to.*?```yaml\n(.*?)```", SPEC.read_text("utf-8"), re.S
    )
    assert match is not None, "CONTROL_SPEC §3's policy block has moved or gone"
    return match.group(1)


def _write(tmp_path: Path, text: str, name: str = "policy.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- the spec's own example has to be loadable ------------------------------


def test_the_spec_example_parses_validates_and_compiles(tmp_path: Path) -> None:
    """CONTROL_SPEC §3's block, consumed rather than read. ADR-016 instance four.

    Asserts against the spec's **stated multipliers**, not against whatever the
    file currently holds, so a silent edit to §3's table goes red here. The
    values are §3's own rationale made checkable: identifiers widen because a
    caller reading a member number pauses between groups of digits, and `boolean`
    narrows because yes/no must stay snappy.
    """
    compiled = load_policy(_write(tmp_path, _spec_policy_text()))
    assert compiled.covered == {
        "boolean",
        "free",
        "number",
        "entity_id",
        "entity_date",
        "entity_address",
        "entity_list",
        "spelling",
    }
    assert compiled.hint_for("boolean") == WindowHint(min_mult=0.7, max_mult=0.7)
    assert compiled.hint_for("free") == WindowHint(min_mult=1.0, max_mult=1.0)
    assert compiled.hint_for("spelling") == WindowHint(min_mult=1.5, max_mult=2.4)
    assert compiled.hint_for("entity_list") == WindowHint(min_mult=1.4, max_mult=2.2)


def test_every_spec_class_widens_max_at_least_as_much_as_min(tmp_path: Path) -> None:
    """§3's shape claim, not just its numbers.

    §3 says the context axis "lands on `min_ms`" and that wide-answer classes
    "widen `max_ms` as well". So no class in the shipped table may widen the
    responsiveness knob *more* than the mid-utterance one — that would make the
    agent slower to answer a complete utterance than it is to tolerate an
    incomplete one, inverting which regime the hint is for (ADR-011).
    """
    compiled = load_policy(_write(tmp_path, _spec_policy_text()))
    for answer in sorted(compiled.covered):
        hint = compiled.hint_for(answer)
        assert hint.max_mult >= hint.min_mult, (
            f"{answer} widens min_mult={hint.min_mult} above "
            f"max_mult={hint.max_mult}, inverting §3's two regimes"
        )


# --- lookup semantics: CONTROL_SPEC §3, EC-13 -------------------------------


def test_an_unnamed_class_and_no_class_both_get_the_default(tmp_path: Path) -> None:
    """Both fall back, and the fallback is the policy's `default`, not a built-in."""
    compiled = load_policy(_write(tmp_path, MINIMAL))
    assert compiled.hint_for(None) == WindowHint(min_mult=1.0, max_mult=1.0)
    assert compiled.hint_for("entity_id") == WindowHint(min_mult=1.0, max_mult=1.0)
    assert "entity_id" not in compiled.covered


def test_the_default_is_the_policys_own_and_not_a_hardcoded_one() -> None:
    """A policy declaring a non-neutral default must get it.

    If `hint_for` fell back to `WindowHint(1.0, 1.0)` rather than to the parsed
    default, a deployment that globally widened its windows would silently get
    neutral ones — a wrong published latency with no symptom, which is why this
    module is first-tier.
    """
    compiled = compile_policy(
        PolicyFile.model_validate(
            yaml.safe_load(
                "version: 1\n"
                "default: {min_mult: 1.3, max_mult: 1.9}\n"
                "answers:\n  boolean: {min_mult: 0.7, max_mult: 0.7}\n"
            )
        )
    )
    assert compiled.hint_for(None) == WindowHint(min_mult=1.3, max_mult=1.9)
    assert compiled.hint_for("spelling") == WindowHint(min_mult=1.3, max_mult=1.9)
    assert compiled.hint_for("boolean") == WindowHint(min_mult=0.7, max_mult=0.7)


def test_a_lookup_holds_no_state_between_turns(tmp_path: Path) -> None:
    """EC-13: a hint applies for one turn and never persists into the profile.

    `CompiledPolicy` carries no per-turn state at all, which is what makes the
    guarantee structural rather than remembered — there is nothing for a previous
    turn's hint to be stored in. Asserted by interleaving lookups.
    """
    compiled = load_policy(_write(tmp_path, MINIMAL))
    for _ in range(3):
        assert compiled.hint_for("spelling").max_mult == 2.4
        assert compiled.hint_for("boolean").max_mult == 0.7
        assert compiled.hint_for(None).max_mult == 1.0


def test_a_compiled_policy_is_not_mutated_by_its_source_mapping() -> None:
    """The compiled form copies, so a caller holding the mapping cannot edit it."""
    source: dict[str, WindowHint] = {"boolean": WindowHint(min_mult=0.7, max_mult=0.7)}
    compiled = CompiledPolicy(
        hints=source,  # type: ignore[arg-type]  # a Literal key, spelled loosely
        default=WindowHint(min_mult=1.0, max_mult=1.0),
    )
    source["boolean"] = WindowHint(min_mult=9.0, max_mult=9.0)
    assert compiled.hint_for("boolean") == WindowHint(min_mult=0.7, max_mult=0.7)


# --- ARCHITECTURE §10: a closed schema, parsed safely -----------------------


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    """`extra="forbid"`: a policy cannot smuggle a field past the schema."""
    with pytest.raises(ValidationError):
        load_policy(
            _write(
                tmp_path,
                "version: 1\ndefault: {min_mult: 1.0, max_mult: 1.0}\n"
                "answers: {}\nrun_this: rm -rf /\n",
                "extra.yaml",
            )
        )


def test_an_unknown_answer_class_is_refused(tmp_path: Path) -> None:
    """The keys are `ExpectedAnswer`, so a typo is a load error and not a silent
    class that never matches and quietly serves the default forever."""
    with pytest.raises(ValidationError):
        load_policy(
            _write(
                tmp_path,
                "version: 1\ndefault: {min_mult: 1.0, max_mult: 1.0}\n"
                "answers:\n  boolen: {min_mult: 0.7, max_mult: 0.7}\n",
                "typo.yaml",
            )
        )


def test_a_future_version_is_refused(tmp_path: Path) -> None:
    """`version: Literal[1]`, so a v2 document fails rather than being half-read."""
    with pytest.raises(ValidationError):
        load_policy(
            _write(
                tmp_path,
                "version: 2\ndefault: {min_mult: 1.0, max_mult: 1.0}\nanswers: {}\n",
                "v2.yaml",
            )
        )


def test_a_missing_answers_key_is_refused(tmp_path: Path) -> None:
    """The exact defect ADR-016's fourth instance was: `answers` is required."""
    with pytest.raises(ValidationError):
        load_policy(
            _write(
                tmp_path,
                "version: 1\ndefault: {min_mult: 1.0, max_mult: 1.0}\n",
                "noanswers.yaml",
            )
        )


def test_a_yaml_tag_is_not_constructed(tmp_path: Path) -> None:
    """`yaml.safe_load` only (ARCHITECTURE §10). A tag must not build an object."""
    with pytest.raises((yaml.YAMLError, ValidationError)):
        load_policy(
            _write(
                tmp_path,
                "version: 1\ndefault: !!python/object/apply:os.system ['echo x']\n"
                "answers: {}\n",
                "tagged.yaml",
            )
        )


# --- ARCHITECTURE §5: the content-hash LRU ----------------------------------


def test_identical_content_at_two_paths_shares_one_compilation(
    tmp_path: Path,
) -> None:
    """Keyed on the sha256 of the content, so the path is not part of the key."""
    _compile_content.cache_clear()
    first = load_policy(_write(tmp_path, MINIMAL, "a.yaml"))
    second = load_policy(_write(tmp_path, MINIMAL, "b.yaml"))
    assert first is second
    assert _compile_content.cache_info().hits == 1


def test_an_edited_file_is_recompiled(tmp_path: Path) -> None:
    """A miss on the new digest, not a stale hit.

    The failure this prevents is a policy edited in place and reloaded, serving
    the old multipliers for the rest of the process — a wrong published latency
    with no symptom, and indistinguishable from the operator's edit not having
    been saved.
    """
    _compile_content.cache_clear()
    path = _write(tmp_path, MINIMAL)
    before = load_policy(path)
    assert before.hint_for("boolean").min_mult == 0.7
    path.write_text(
        MINIMAL.replace("0.7, max_mult: 0.7", "0.4, max_mult: 0.5"), "utf-8"
    )
    after = load_policy(path)
    assert after is not before
    assert after.hint_for("boolean") == WindowHint(min_mult=0.4, max_mult=0.5)


def test_the_cache_is_bounded_at_the_documented_size() -> None:
    """ARCHITECTURE §5 says `POLICY_CACHE_SIZE` entries, LRU. INV-3 is why.

    An unbounded cache keyed on file content grows with every distinct policy a
    long-lived process is ever handed, which is the unbounded-growth INV-3 exists
    to forbid — and it would be invisible, because nothing about a cache hit
    looks different from a cache miss.
    """
    assert _compile_content.cache_info().maxsize == POLICY_CACHE_SIZE
    _compile_content.cache_clear()
    for index in range(POLICY_CACHE_SIZE + 4):
        text = MINIMAL.replace("1.0, max_mult: 1.0", f"1.0, max_mult: {1.0 + index}")
        _compile_content(str(index), text)
    assert _compile_content.cache_info().currsize == POLICY_CACHE_SIZE
