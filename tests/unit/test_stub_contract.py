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
(CONTROL_SPEC §9). What remains here is `proxy.py`, which Gate 4 owns.

The coverage this file was holding up had to be replaced, not merely removed. Each
gate's delta is in its report.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, cast

import pytest

from nod_core import arbiter, capabilities, profiler, proxy
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

SYNC_STUBS: tuple[tuple[str, Callable[[], object]], ...] = (
    (
        "proxy.SessionProxy.__init__",
        lambda: proxy.SessionProxy(
            upstream=cast(Any, None),
            profiler=cast(Any, None),
            arbiter=cast(Any, None),
            trace=cast(Any, None),
            mode=cast(Any, None),
            ceiling_ms=2600,
        ),
    ),
)

ASYNC_STUBS: tuple[tuple[str, Callable[[], Coroutine[Any, Any, object]]], ...] = (
    ("proxy.SessionProxy.run", lambda: _uninitialised(proxy.SessionProxy).run()),
    (
        "proxy.SessionProxy.pump_audio_up",
        lambda: _uninitialised(proxy.SessionProxy).pump_audio_up(),
    ),
    (
        "proxy.SessionProxy.pump_events_down",
        lambda: _uninitialised(proxy.SessionProxy).pump_events_down(),
    ),
    (
        "proxy.SessionProxy.run_controller",
        lambda: _uninitialised(proxy.SessionProxy).run_controller(),
    ),
    (
        "proxy.SessionProxy.send_patch_upstream",
        lambda: _uninitialised(proxy.SessionProxy).send_patch_upstream(("min",)),
    ),
    ("proxy.SessionProxy.rotate", lambda: _uninitialised(proxy.SessionProxy).rotate()),
    ("proxy.SessionProxy.aclose", lambda: _uninitialised(proxy.SessionProxy).aclose()),
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
