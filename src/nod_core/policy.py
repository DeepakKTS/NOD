"""Context axis: dialogue state to window hints.

ARCHITECTURE.md §2: this module never looks at the caller. It reads a declared
dialogue state and nothing else, which is why its only inputs are an
`ExpectedAnswer` and a policy file.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from nod_core.types import ExpectedAnswer, WindowHint

POLICY_CACHE_SIZE: Final = 8
"""Compiled policies retained, keyed by content hash, LRU (ARCHITECTURE.md §5)."""


class WindowHintModel(BaseModel):  # type: ignore[explicit-any]  # pydantic's own Any
    """One `answers` row of the policy file (CONTROL_SPEC.md §3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_mult: float
    max_mult: float


class PolicyFile(BaseModel):  # type: ignore[explicit-any]  # pydantic's own Any
    """The whole policy document, as a closed schema.

    ARCHITECTURE.md §10: parsed with `yaml.safe_load` into a pydantic model with
    a closed schema. No `eval`, no dynamic import.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    default: WindowHintModel
    answers: Mapping[ExpectedAnswer, WindowHintModel]


class CompiledPolicy:
    """A policy compiled once into a dict, for `O(1)` lookup per turn.

    Immutable after construction, and holding `WindowHint` rather than the
    pydantic model: a hint crosses into the arbiter, where CLAUDE.md §6 requires
    a frozen slotted dataclass, and converting per turn would allocate inside the
    decision path (INV-2).
    """

    __slots__ = ("_default", "_hints")

    def __init__(
        self, hints: Mapping[ExpectedAnswer, WindowHint], default: WindowHint
    ) -> None:
        """Build a compiled policy.

        Args:
            hints: One hint per declared `expected_answer`.
            default: The hint for an answer class the policy does not name, and
                for a turn with no declared class at all.
        """
        self._hints: Mapping[ExpectedAnswer, WindowHint] = dict(hints)
        self._default = default

    def hint_for(self, expected: ExpectedAnswer | None) -> WindowHint:
        """Return the hint for a declared dialogue state. `O(1)`.

        The hint applies for exactly one turn and is then released; it never
        persists into the speaker profile (CONTROL_SPEC.md §3, EC-13). That
        release is the caller's: this method holds no per-turn state, which is
        what makes the guarantee structural rather than remembered.

        `None` and an unnamed class both return the default, and they mean
        different things — "the host declared nothing" against "the host declared
        something this policy does not cover". Both are correctly served by the
        default, so they are not distinguished here; a policy that wants them to
        differ has to name the class.

        Args:
            expected: The host's declared expected answer, or `None`.

        Returns:
            The matching hint, or the policy default.
        """
        if expected is None:
            return self._default
        return self._hints.get(expected, self._default)

    @property
    def covered(self) -> frozenset[ExpectedAnswer]:
        """The answer classes this policy names explicitly. `O(P)`.

        Exposed so a caller can tell "covered and equal to the default" from
        "not covered", which `hint_for` deliberately does not.
        """
        return frozenset(self._hints)


def compile_policy(policy: PolicyFile) -> CompiledPolicy:
    """Compile a validated policy into its lookup form. `O(P)` once at load.

    Args:
        policy: A validated policy document.

    Returns:
        The compiled policy.
    """
    return CompiledPolicy(
        hints={
            answer: WindowHint(min_mult=row.min_mult, max_mult=row.max_mult)
            for answer, row in policy.answers.items()
        },
        default=WindowHint(
            min_mult=policy.default.min_mult, max_mult=policy.default.max_mult
        ),
    )


@lru_cache(maxsize=POLICY_CACHE_SIZE)
def _compile_content(digest: str, content: str) -> CompiledPolicy:
    """Parse, validate and compile one policy document, memoised on its digest.

    `digest` is the cache key that matters and `content` is what the body needs;
    two paths holding identical bytes therefore share one compiled policy, and a
    file edited in place misses on its new digest rather than serving the old
    compilation.

    This is the one piece of module-level mutable state in `nod_core`, against
    CLAUDE.md §6's "no module-level state except constants". It is here because
    ARCHITECTURE.md §5 specifies it — a cache keyed by content hash, LRU,
    `POLICY_CACHE_SIZE` entries — and because a cache whose key is a hash of its
    own input cannot make the function impure: the same argument always returns an
    equal policy, evicted or not. It is called out rather than left implicit
    because the next reader will check it against §6.

    Args:
        digest: sha256 of `content`, hex.
        content: The raw YAML.

    Returns:
        The compiled policy.
    """
    document = yaml.safe_load(content)
    return compile_policy(PolicyFile.model_validate(document))


def load_policy(source: Path) -> CompiledPolicy:
    """Read, validate and compile a policy file.

    Parsed with `yaml.safe_load` into `PolicyFile`, then compiled. Cached by the
    sha256 of the file content, `POLICY_CACHE_SIZE` entries, LRU
    (ARCHITECTURE.md §5).

    `yaml.safe_load` and nothing else: `yaml.load` is in ruff's banned-api table
    for this repository (ARCHITECTURE.md §10), and `PolicyFile` is a closed schema
    under `extra="forbid"`, so a policy file cannot smuggle a field past it.

    Args:
        source: Path to the policy YAML.

    Returns:
        The compiled policy.

    Raises:
        ValidationError: The document does not match `PolicyFile`.
        OSError: `source` cannot be read.
    """
    content = source.read_text(encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return _compile_content(digest, content)
