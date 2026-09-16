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
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import pytest

from nod_core import arbiter, capabilities, config, policy, profiler, proxy
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    KnobVerdict,
    SpeakerFeatures,
    Turn,
    TurnConfig,
    WindowHint,
    Word,
)


def _uninitialised[T](cls: type[T]) -> T:
    """Build an instance without running `__init__`, which itself raises."""
    return object.__new__(cls)


WORD = Word(text="hello", start_ms=0, end_ms=200, confidence=0.9, is_final=True)
TURN = Turn(
    turn_order=1,
    end_of_turn=True,
    end_of_turn_confidence=0.7,
    transcript="hello",
    words=(WORD,),
)
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
HINT = WindowHint(min_mult=1.0, max_mult=1.0, conf_delta=0.0)
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
    ("config.get_settings", config.get_settings),
    ("profiler.P2Quantile.__init__", lambda: profiler.P2Quantile(0.5)),
    (
        "profiler.P2Quantile.update",
        lambda: _uninitialised(profiler.P2Quantile).update(1.0),
    ),
    ("profiler.P2Quantile.value", lambda: _uninitialised(profiler.P2Quantile).value),
    ("profiler.ExactQuantile.__init__", lambda: profiler.ExactQuantile(0.9)),
    (
        "profiler.ExactQuantile.update",
        lambda: _uninitialised(profiler.ExactQuantile).update(1.0),
    ),
    (
        "profiler.ExactQuantile.value",
        lambda: _uninitialised(profiler.ExactQuantile).value,
    ),
    ("profiler.Profiler.__init__", profiler.Profiler),
    (
        "profiler.Profiler.observe_turn",
        lambda: _uninitialised(profiler.Profiler).observe_turn(TURN),
    ),
    (
        "profiler.Profiler.features",
        lambda: _uninitialised(profiler.Profiler).features(),
    ),
    (
        "profiler.Profiler.snapshot",
        lambda: _uninitialised(profiler.Profiler).snapshot(),
    ),
    ("profiler.Profiler.restore", lambda: profiler.Profiler.restore(PROFILER_STATE)),
    (
        "policy.CompiledPolicy.hint_for",
        lambda: _uninitialised(policy.CompiledPolicy).hint_for("boolean"),
    ),
    (
        "policy.compile_policy",
        lambda: policy.compile_policy(_uninitialised(policy.PolicyFile)),
    ),
    ("policy.load_policy", lambda: policy.load_policy(Path("policy.yaml"))),
    (
        "arbiter.control_law",
        lambda: arbiter.control_law(FEATURES, HINT, cold=False, ceiling_ms=2600),
    ),
    ("arbiter.Arbiter.__init__", lambda: arbiter.Arbiter(capabilities=CAPS)),
    (
        "arbiter.Arbiter.decide",
        lambda: _uninitialised(arbiter.Arbiter).decide(ARBITER_INPUT),
    ),
    (
        "arbiter.Arbiter.should_force_endpoint",
        lambda: _uninitialised(arbiter.Arbiter).should_force_endpoint(
            ARBITER_INPUT, 900
        ),
    ),
    (
        "arbiter.Arbiter.note_error",
        lambda: _uninitialised(arbiter.Arbiter).note_error(RuntimeError("x")),
    ),
    ("arbiter.Arbiter.state", lambda: _uninitialised(arbiter.Arbiter).state),
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
