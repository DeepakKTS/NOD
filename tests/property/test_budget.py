"""The INV-2 budget test, at the path ARCHITECTURE.md §4 names.

ARCHITECTURE.md §4 states the hard budgets and says they are enforced by this file
in CI: "`decide()` p99 < 5 ms, mean < 200 µs."

The measurement is specified here rather than left to whoever implements the law,
because a latency number is only as good as the harness that produced it and the
three choices below are the ones that decide whether it means anything.

**The clock.** `time.perf_counter_ns()`. Monotonic, so a clock adjustment mid-run
cannot produce a negative sample; the highest resolution the platform offers; and
integer nanoseconds, so nothing is lost to float rounding at the microsecond scale
the mean budget lives at. Not `time.time()`, which is wall clock, coarse, and not
monotonic — CLAUDE.md §6 bans it from the controller, and it is no better for
measuring the controller. Not `time.process_time()`, which excludes time the
process was descheduled: the caller waits through that too, so excluding it would
report a number nobody experiences.

Each call is timed individually rather than in a batch. A batch divided by its
count gives a mean and no tail at all, and the binding half of INV-2 is the p99.
Two `perf_counter_ns()` calls cost on the order of 100 ns, which is 0.05 % of the
200 µs mean budget and less against the p99 — and it is charged *to* `decide`, so
the bias is conservative.

**The harness's resolution floor, measured rather than assumed.** Run against a
`decide` that returns immediately, 11 of the 100 000 samples read exactly 0.0 ms.
A call cannot take zero time, so that is the clock: `perf_counter_ns` resolves to
about 40 ns on this platform, and two reads around a near-empty function sometimes
land in one tick. It is recorded because a zero is the kind of impossible value
that should be explained rather than ignored (CLAUDE.md §5), and the explanation
is benign here — 40 ns is 0.02 % of the mean budget, so a regression large enough
to matter is thousands of ticks wide and cannot hide in the quantisation. What the
floor does mean is that this harness cannot distinguish "fast" from "faster" down
near the clock, and it was never asked to: the assertion is a budget, not a
benchmark.

**The warmup.** `WARMUP_STATES` (1 000) decisions, timed and discarded. They pay
the one-off costs that are not what INV-2 is about: first-call branch prediction,
CPU frequency ramp, lazily-created internal buffers, and the interpreter's own
specialisation of the bytecode, which CPython 3.12 applies only after a function
has been called enough times. Measuring cold would report the first call's cost as
though it were every call's.

The warmup deliberately runs *before* the measurement states are generated. Until
`decide()` exists it raises on the first warmup call, so `make check` pays for
1 000 states rather than 100 000 and this file stays cheap through Gates 1 and 2.

**The states.** `MEASURED_STATES` (100 000) `ArbiterInput`s, generated from a
seeded `random.Random` and fully materialised before the clock starts, so
generation cost is outside every sample. Seeded, so a regression is reproducible.

Not hypothesis: it is a falsification tool, not a timing one, and its draw cost
would sit inside the region being measured. Not a small pool cycled many times
either, tempting as it is for memory — the whole point of a p99 over 100 000
samples is a tail, and a thousand states replayed a hundred times each lets the
branch predictor and the data caches learn them. That would report a number the
real workload never sees, in the flattering direction.

A fresh `Arbiter` every `MAX_PATCHES` states, for the same reason. The §5 rate cap
retires an arbiter after 24 patches and the freeze detector can park the speaker
axis for ten turns; both are early exits, and an arbiter left running for 100 000
turns would spend nearly all of them in one. That measures the cheap path and
calls it the budget.

Quantiles come from `nod_bench.metrics.quantile`, which is nearest-rank inclusive
rather than `numpy.percentile`'s linear interpolation. Reused rather than
reimplemented so this number is computed the same way as every published latency
figure (BENCH_SPEC §5), and because ADR-018 records interpolation understating the
tail as one of Phase 1's four flattering-direction errors.

`decide()` is a stub today, so this is `xfail(strict=True)`: the moment P5
implements it, the marker turns the pass into a loud failure and whoever landed
the law has to read the measured numbers rather than discover this file later.
"""

from __future__ import annotations

import random
import time
from typing import Final

import pytest

from nod_bench.metrics import DECIDE_BUDGET_MS, quantile
from nod_core.arbiter import (
    DEFAULT_CEILING_MS,
    MAX_MS_CEIL,
    MAX_MS_FLOOR,
    MAX_PATCHES,
    MIN_MS_CEIL,
    MIN_MS_FLOOR,
    Arbiter,
    ArbiterInput,
)
from nod_core.profiler import GAP_CLAMP_MAX_MS, MIN_GAPS_FOR_WARM
from nod_core.types import SpeakerFeatures, TurnConfig, WindowHint
from tests.property.strategies import (
    ALL_LIVE_CAPABILITIES,
    EXPECTED_ANSWERS,
    SPEECH_RATE_MAX,
)

DECIDE_P99_BUDGET_MS: Final = DECIDE_BUDGET_MS
"""ARCHITECTURE.md §4: `decide()` p99 < 5 ms. INV-2.

Taken from `nod_bench.metrics.DECIDE_BUDGET_MS` rather than written again. The
bench reports the same budget as the `DEC` metric, and two copies of one threshold
is the shape CLAUDE.md §5 warns about — they would agree by meaning and not by
code, and nothing would notice when one moved.
"""

DECIDE_MEAN_BUDGET_MS: Final = 0.2
"""ARCHITECTURE.md §4: `decide()` mean < 200 µs. Milliseconds.

No constant exists for this one; the bench's `DEC` metric is the p99 only.
"""

MEASURED_STATES: Final = 100_000
"""Decisions timed. docs/PROMPTS.md P5.

Sets the resolution of the tail: at 100 000 samples the p99 is the 99 000th
ordered value and moves on roughly a thousand observations, so it is a property of
the workload rather than of one unlucky scheduling event.
"""

WARMUP_STATES: Final = 1_000
"""Decisions run and discarded before the clock matters. See the module docstring."""

SEED: Final = 7
"""Matches the corpus seed, so "seed 7" means one thing across the repository."""

NS_PER_MS: Final = 1_000_000


def _states(rng: random.Random, count: int) -> tuple[ArbiterInput, ...]:
    """Materialise `count` synthetic decision inputs. No I/O, no clock.

    Drawn over the same domains as `tests/property/strategies.py` and for the same
    reasons, by hand rather than through hypothesis so that nothing in the
    generator runs inside a timed region.

    `cold` is derived from `n_gaps` rather than drawn, matching
    `Profiler.features`; a third of states are cold, which keeps §4's cheap
    cold-path branch from dominating the sample while still covering it.

    Args:
        rng: Seeded generator. The only source of randomness.
        count: How many states to build.

    Returns:
        The states, fully materialised.
    """
    states: list[ArbiterInput] = []
    for index in range(count):
        g_p50 = rng.uniform(0.0, GAP_CLAMP_MAX_MS)
        g_p90 = rng.uniform(g_p50, GAP_CLAMP_MAX_MS)
        n_gaps = (
            rng.randrange(0, MIN_GAPS_FOR_WARM)
            if index % 3 == 0
            else rng.randrange(MIN_GAPS_FOR_WARM, 4096)
        )
        minimum = rng.randrange(MIN_MS_FLOOR, MIN_MS_CEIL + 1)
        states.append(
            ArbiterInput(
                features=SpeakerFeatures(
                    n_gaps=n_gaps,
                    g_p50_ms=g_p50,
                    g_p90_ms=g_p90,
                    speech_rate=rng.uniform(0.0, SPEECH_RATE_MAX),
                    disfluency=rng.random(),
                    jitter=rng.random(),
                    recent_cuts=rng.random(),
                    cold=n_gaps < MIN_GAPS_FOR_WARM,
                ),
                # Spans CONTROL_SPEC §3's own table, 0.7 to 2.4.
                hint=WindowHint(
                    min_mult=rng.uniform(0.7, 2.4),
                    max_mult=rng.uniform(0.7, 2.4),
                ),
                expected_answer=rng.choice(EXPECTED_ANSWERS),
                current=TurnConfig(
                    min_turn_silence_ms=minimum,
                    max_turn_silence_ms=rng.randrange(
                        max(MAX_MS_FLOOR, minimum + 200), MAX_MS_CEIL + 1
                    ),
                    end_of_turn_confidence_threshold=rng.random(),
                    vad_threshold=None,
                ),
                capabilities=ALL_LIVE_CAPABILITIES,
                ceiling_ms=DEFAULT_CEILING_MS,
                turn_order=index + 1,
                t_ms=index * 1_200,
                host_override_fields=frozenset(),
                patches_sent=index % MAX_PATCHES,
            )
        )
    return tuple(states)


@pytest.mark.xfail(
    raises=NotImplementedError,
    strict=True,
    reason="decide() is a P5 stub. The measurement below is the real one; it runs "
    "as written the moment decide() exists.",
)
def test_decide_holds_the_latency_budget() -> None:
    """INV-2: `decide()` p99 under 5 ms, mean under 200 µs.

    Fails on either budget independently. The p99 is the one INV-2 names and the
    one ARCHITECTURE.md §4 alerts on; the mean is what a regression shows up in
    first, because a change that adds work to every decision moves the mean long
    before it moves a tail set by the operating system's scheduler.
    """
    # S311: this is a timing fixture, not a secret. A seeded Mersenne Twister is
    # exactly what is wanted — reproducible, and cheap enough that generation
    # does not dominate the setup it sits in.
    rng = random.Random(SEED)  # noqa: S311

    # Warmup first, so the stub raises before the expensive generation. See the
    # module docstring.
    warmup = _states(rng, WARMUP_STATES)
    engine = Arbiter(capabilities=ALL_LIVE_CAPABILITIES, ceiling_ms=DEFAULT_CEILING_MS)
    for index, state in enumerate(warmup):
        if index % MAX_PATCHES == 0:
            engine = Arbiter(
                capabilities=ALL_LIVE_CAPABILITIES, ceiling_ms=DEFAULT_CEILING_MS
            )
        engine.decide(state)

    measured = _states(rng, MEASURED_STATES)
    samples: list[float] = []
    for index, state in enumerate(measured):
        if index % MAX_PATCHES == 0:
            # Outside the timed region: arbiter construction is not `decide`.
            engine = Arbiter(
                capabilities=ALL_LIVE_CAPABILITIES, ceiling_ms=DEFAULT_CEILING_MS
            )
        start = time.perf_counter_ns()
        engine.decide(state)
        samples.append((time.perf_counter_ns() - start) / NS_PER_MS)

    assert len(samples) == MEASURED_STATES
    p99 = quantile(samples, 0.99)
    mean = sum(samples) / len(samples)
    assert p99 < DECIDE_P99_BUDGET_MS, (
        f"decide() p99 is {p99:.4f} ms, over INV-2's "
        f"{DECIDE_P99_BUDGET_MS} ms budget over {MEASURED_STATES} states"
    )
    assert mean < DECIDE_MEAN_BUDGET_MS, (
        f"decide() mean is {mean * 1000:.1f} µs, over ARCHITECTURE §4's "
        f"{DECIDE_MEAN_BUDGET_MS * 1000:.0f} µs budget over "
        f"{MEASURED_STATES} states"
    )
