"""Phase 0 contract: every public callable in `nod_core` raises NotImplementedError.

docs/PROMPTS.md P0: "Do not implement any logic yet. Every stub raises
NotImplementedError." This file tests that claim.

It also carries the 85 % coverage gate honestly. The alternative — adding
`raise NotImplementedError` to coverage's `exclude_lines` — makes the gate pass
on a stub repository by making it measure nothing, which is exactly wrong at the
moment the gate should bite. Executing every stub instead gives real coverage of
a real contract.

As each stub is implemented, delete its entry here and replace it with the test
that exercises the behaviour.

**`profiler.py`'s eleven entries went at Gate 2**, replaced by
`tests/unit/test_profiler.py` (the ingest path and its edge cases by id) and
`tests/property/test_profiler_quantiles.py` (the two estimators, differentially
and structurally). `ProfilerState` never had an entry: it is a real frozen
dataclass, not a stub, and `PROFILER_STATE` below is still built from it.

**`policy.py`'s three and `arbiter.py`'s six went at Gate 3**, replaced by
`tests/unit/test_policy.py`, `tests/unit/test_arbiter.py` (§4's two gates and
every §5 guard with the polarity reversed) and `tests/property/test_control_law.py`
(CONTROL_SPEC §9). **`proxy.py`'s eight went at Gate 4**, the last of them,
replaced by `tests/unit/test_proxy.py`.

The coverage this file was holding up had to be replaced, not merely removed. Each
gate's delta is in its report.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Final

import pytest

from nod_core import arbiter, capabilities, profiler
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    KnobVerdict,
    SpeakerFeatures,
    TurnConfig,
    WindowHint,
)


def _uninitialised[T](cls: type[T]) -> T:
    """Build an instance without running `__init__`, which itself raises."""
    return object.__new__(cls)


FEATURES = SpeakerFeatures(
    n_gaps=10,
    g_p50_ms=220.0,
    g_p90_ms=800.0,
    speech_rate=2.4,
    disfluency=0.1,
    jitter=0.2,
    recent_cuts=0.0,
    cold=False,
)
HINT = WindowHint(min_mult=1.0, max_mult=1.0)
CONFIG = TurnConfig(
    min_turn_silence_ms=400,
    max_turn_silence_ms=1280,
    end_of_turn_confidence_threshold=0.4,
    vad_threshold=None,
)
CAPS = Capabilities(
    knobs=tuple((f, KnobVerdict.LIVE) for f in capabilities.UPDATABLE_FIELDS),
    confidence_field=ConfidenceField.VARYING,
    force_endpoint=KnobVerdict.LIVE,
    has_word_timings=True,
)
ARBITER_INPUT = arbiter.ArbiterInput(
    features=FEATURES,
    hint=HINT,
    expected_answer="free",
    current=CONFIG,
    capabilities=CAPS,
    ceiling_ms=arbiter.DEFAULT_CEILING_MS,
    turn_order=1,
    t_ms=1000,
    host_override_fields=frozenset(),
    patches_sent=0,
)
PROFILER_STATE = profiler.ProfilerState(
    n_gaps=10,
    g_p50_ms=220.0,
    g_p90_ms=800.0,
    speech_rate=2.4,
    disfluency=0.1,
    jitter=0.2,
    recent_cuts=0.0,
    last_turn_order=1,
)

SYNC_STUBS: tuple[tuple[str, Callable[[], object]], ...] = ()

ASYNC_STUBS: tuple[tuple[str, Callable[[], Coroutine[Any, Any, object]]], ...] = ()
"""Empty as of Phase 2 Gate 4: `proxy.py`'s eight entries were the last of them.

Replaced by `tests/unit/test_proxy.py`. Two paths there carry CLAUDE.md §5's
first tier — `send_patch_upstream` and `run_controller`'s `SAFE` path — and their
tests assert what §5 asks for rather than what is easy: that the patch reached
the socket, and that entering `SAFE` is loud. The rest of the module is the
accepted-thinner tier.

**This file has now done its whole job and is kept for the shape of it.** Every
`nod_core` stub it once guarded has landed, so the parametrisations below are
empty and the file asserts nothing. That is not a reason to keep it green by
accident: `test_no_stub_survives_unlisted` fails if a new
`raise NotImplementedError` appears in `nod_core` without an entry here, which
turns an empty list from a vacuous pass into a live claim.
"""

NOD_CORE: Final = Path(__file__).resolve().parents[2] / "src" / "nod_core"


def test_no_stub_survives_unlisted() -> None:
    """Every `raise NotImplementedError` in `nod_core` must be listed above.

    This is what stops the two empty tuples from being a vacuous pass. With every
    stub landed there is nothing left to parametrise, so the two tests below run
    zero times each and assert nothing at all — which is exactly the shape
    CLAUDE.md §5 warns about, a check that cannot fail sitting where coverage
    used to be.

    So the claim is inverted. Instead of "each listed stub raises", this asserts
    "no unlisted stub exists": a new `raise NotImplementedError` appearing in
    `nod_core` fails here until someone either implements it or adds it to the
    lists. That keeps the file honest in both directions — it went from guarding
    28 stubs to guarding the absence of them, and it can still go red.

    `NotImplementedError` raised from an abstract base or a deliberate
    "unsupported operation" would trip this too. That is intended: `nod_core` has
    neither today, and a module that acquires one should have to say so here.
    """
    offenders: list[str] = []
    listed = {name for name, _ in SYNC_STUBS} | {name for name, _ in ASYNC_STUBS}
    for module in sorted(NOD_CORE.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            raised = node.exc
            name = (
                raised.id
                if isinstance(raised, ast.Name)
                else raised.func.id
                if isinstance(raised, ast.Call) and isinstance(raised.func, ast.Name)
                else ""
            )
            if name == "NotImplementedError":
                offenders.append(f"{module.name}:{node.lineno}")
    assert not offenders or listed, (
        f"unlisted stubs in nod_core: {offenders}. Implement them, or add an "
        "entry to SYNC_STUBS/ASYNC_STUBS so the contract covers them."
    )
    assert not offenders, (
        f"unlisted stubs in nod_core: {offenders}. Every stub must be listed "
        "above or implemented; an unguarded stub is a promise nothing checks."
    )


@pytest.mark.parametrize(
    ("name", "call"),
    SYNC_STUBS,
    ids=[name for name, _ in SYNC_STUBS],
)
def test_sync_stub_raises_not_implemented(
    name: str, call: Callable[[], object]
) -> None:
    with pytest.raises(NotImplementedError):
        call()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "call"),
    ASYNC_STUBS,
    ids=[name for name, _ in ASYNC_STUBS],
)
async def test_async_stub_raises_not_implemented(
    name: str,
    call: Callable[[], Coroutine[Any, Any, object]],
) -> None:
    with pytest.raises(NotImplementedError):
        await call()
