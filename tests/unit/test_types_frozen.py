"""Every dataclass crossing a module boundary is frozen and slotted.

CLAUDE.md §6 states the rule; this makes breaking it a test failure. INV-3 leans
on `slots=True` for a bounded per-session footprint, and INV-4 leans on
`ConfigPatch.decision` being a required field.
"""

from __future__ import annotations

import dataclasses

import pytest

from nod_adapters import protocols
from nod_core import arbiter, profiler, types

MODULES = (types, protocols, profiler, arbiter)


def _dataclasses() -> list[type]:
    found: list[type] = []
    for module in MODULES:
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and dataclasses.is_dataclass(obj):
                found.append(obj)
    return found


def test_there_are_dataclasses_to_check() -> None:
    assert _dataclasses()


@pytest.mark.parametrize("cls", _dataclasses(), ids=lambda c: c.__name__)
def test_boundary_dataclasses_are_frozen_and_slotted(cls: type) -> None:
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen, f"{cls.__name__} must be frozen=True (CLAUDE.md §6)"
    assert "__slots__" in cls.__dict__, f"{cls.__name__} must be slots=True"


def test_config_patch_requires_a_decision() -> None:
    """INV-4: a patch cannot exist without its explanation."""
    fields = {f.name: f for f in dataclasses.fields(types.ConfigPatch)}
    decision = fields["decision"]
    assert decision.default is dataclasses.MISSING
    assert decision.default_factory is dataclasses.MISSING


def test_config_decision_carries_the_full_explanation() -> None:
    """INV-4: trigger, inputs, old value, new value and rule id."""
    names = {f.name for f in dataclasses.fields(types.ConfigDecision)}
    assert {"rule_id", "trigger", "inputs", "old", "new"} <= names


def test_controller_state_has_the_four_states() -> None:
    """CONTROL_SPEC.md §6, including SAFE which INV-8 depends on."""
    assert {s.value for s in types.ControllerState} == {
        "cold",
        "warm",
        "frozen",
        "safe",
    }


def test_nod_mode_has_the_three_modes() -> None:
    """ARCHITECTURE.md §7. `observe` is the zero-risk first deployment step."""
    assert {m.value for m in types.NodMode} == {"adapt", "observe", "off"}


def test_frozen_dataclasses_reject_mutation() -> None:
    config = types.TurnConfig(
        min_turn_silence_ms=400,
        max_turn_silence_ms=1280,
        end_of_turn_confidence_threshold=0.4,
        vad_threshold=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.min_turn_silence_ms = 500  # type: ignore[misc]
