"""Context axis: dialogue state to window hints.

ARCHITECTURE.md §2: this module never looks at the caller. It reads a declared
dialogue state and nothing else, which is why its only inputs are an
`ExpectedAnswer` and a policy file.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal

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
    """A policy compiled once into a dict, for `O(1)` lookup per turn."""

    def hint_for(self, expected: ExpectedAnswer | None) -> WindowHint:
        """Return the hint for a declared dialogue state. `O(1)`.

        The hint applies for exactly one turn and is then released; it never
        persists into the speaker profile (CONTROL_SPEC.md §3, EC-13).

        Args:
            expected: The host's declared expected answer, or `None`.

        Returns:
            The matching hint, or the policy default.
        """
        raise NotImplementedError


def compile_policy(policy: PolicyFile) -> CompiledPolicy:
    """Compile a validated policy into its lookup form. `O(P)` once at load.

    Args:
        policy: A validated policy document.

    Returns:
        The compiled policy.
    """
    raise NotImplementedError


def load_policy(source: Path) -> CompiledPolicy:
    """Read, validate and compile a policy file.

    Parsed with `yaml.safe_load` into `PolicyFile`, then compiled. Cached by the
    sha256 of the file content, `POLICY_CACHE_SIZE` entries, LRU
    (ARCHITECTURE.md §5).

    Args:
        source: Path to the policy YAML.

    Returns:
        The compiled policy.
    """
    raise NotImplementedError
