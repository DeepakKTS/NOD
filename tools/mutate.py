"""The committed mutation harness. `make mutate`.

CLAUDE.md §5 requires that a test be seen red before it is trusted, and names
four mechanical guards that a mutation run has to carry. All four were rebuilt
from scratch into a scratch directory at each of Phase 2's first four gates,
which is both how the anchor defect arose and why fixing it did not stay fixed —
the fix was deleted with the job. This file is that requirement given somewhere
to live.

The four guards, each present because it has already caught something:

1. **Source-hash assertion.** A patch that fails to apply must be a loud error
   rather than a green run. Noticing by suspicion does not scale.
2. **`__pycache__` purge, on write *and* restore.** CPython validates a `.pyc`
   on `(source mtime, source size)`. A mutation preserving byte length — `0.4` to
   `0.5` — restored inside the one-second mtime granularity leaves both fields
   matching the restored source while the cached bytecode is still the mutant.
   Observed, not theorised: it made a correct file fail and it corrupted every
   separability figure measured in the same window.
3. **`PYTHONDONTWRITEBYTECODE` for the subprocess**, so the run cannot leave a
   stale cache behind for the next one.
4. **The anchor check.** An anchor matching zero or several times means the
   mutation never ran. Reporting that as a survivor is the inverse failure: it
   **manufactures** a defect instead of hiding one, and a survivor list with
   phantoms in it trains you to distrust the real entries — which are the entire
   output of the exercise. Harness errors are therefore reported in their own
   section and dominate the exit code, because a run containing one is a run you
   cannot conclude anything from.

Exit codes are the interface, so that chaining a commit after this is safe:

- `0` — every mutation applied and every mutation was killed.
- `1` — every mutation applied; at least one survived. Real test gaps.
- `2` — at least one mutation could not be applied, or the restore failed. The
  run is incomplete and its survivor list is not trustworthy.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parent.parent

PROFILER_TESTS: Final = (
    "tests/unit/test_profiler.py",
    "tests/property/test_profiler_quantiles.py",
)
CONTROL_TESTS: Final = (
    "tests/unit/test_arbiter.py",
    "tests/property/test_control_law.py",
    "tests/property/test_budget.py",
)
POLICY_TESTS: Final = ("tests/unit/test_policy.py",)
PROXY_TESTS: Final = ("tests/unit/test_proxy.py",)
WORDS_TESTS: Final = ("tests/unit/test_replay_words.py",)
METRICS_TESTS: Final = ("tests/unit/test_metrics.py",)
TRACKC_TESTS: Final = ("tests/unit/test_trackc.py",)
HEALTH_TESTS: Final = ("tests/integration/test_server_health.py",)
SCRIPT_TESTS: Final = ("tests/unit/test_trackc.py",)
LIVE_TESTS: Final = ("tests/integration/test_live_path.py",)
WIRE_TESTS: Final = ("tests/unit/test_live_path_wire.py",)
REPORT_TESTS: Final = ("tests/unit/test_replay_report.py",)
SMOKE_TESTS: Final = ("tests/integration/test_bench_smoke.py",)
CONTEXT_TESTS: Final = (
    "tests/unit/test_context_axis.py",
    "tests/integration/test_console_loop.py",
)
LADDER_TESTS: Final = ("tests/unit/test_ladder.py",)


@dataclass(frozen=True, slots=True)
class Mutation:
    """One edit to make, and the tests that must notice it."""

    label: str
    target: str
    old: str
    new: str
    tests: tuple[str, ...]


def _profiler_mutations() -> tuple[Mutation, ...]:
    """The `profiler.py` set. `g_p90` reaches `max_turn_silence`, so first tier."""
    src = "src/nod_core/profiler.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, PROFILER_TESTS)

    return (
        # P2Quantile.update
        mutation(
            "update: skip the buffered-init sort",
            "            self._values.sort()",
            "            pass",
        ),
        mutation(
            "update: count positions from the wrong cell",
            "        for index in range(cell + 1, P2_MARKERS):",
            "        for index in range(cell, P2_MARKERS):",
        ),
        mutation(
            "update: never advance desired positions",
            "            self._desired[index] += self._increment[index]",
            "            pass",
        ),
        mutation(
            "update: drop the marker adjustment entirely",
            "        self._adjust()",
            "        pass",
        ),
        mutation(
            "update: off-by-one on the init threshold",
            "        if self._seen <= P2_MARKERS:",
            "        if self._seen < P2_MARKERS:",
        ),
        # marker arithmetic
        mutation(
            "_adjust: step always positive",
            "            step = 1.0 if offset > 0.0 else -1.0",
            "            step = 1.0",
        ),
        mutation(
            "_adjust: accept the parabola unconditionally",
            "            if not self._values[index - 1] < candidate < self._values[index + 1]:",
            "            if False:",
        ),
        mutation(
            "_adjust: move the position without the value",
            "            self._values[index] = candidate",
            "            pass",
        ),
        mutation(
            "_parabolic: sign flip on the step",
            "        return values[index] + step / span * (",
            "        return values[index] - step / span * (",
        ),
        mutation(
            "_linear: wrong neighbour",
            "        neighbour = index + int(step)",
            "        neighbour = index - int(step)",
        ),
        mutation(
            "_locate: do not extend the minimum",
            "            self._values[0] = x\n            return 0",
            "            return 0",
        ),
        mutation(
            "_locate: do not extend the maximum",
            "            self._values[P2_MARKERS - 1] = x\n            return P2_MARKERS - 2",
            "            return P2_MARKERS - 2",
        ),
        # P2Quantile.value
        mutation(
            "value: report the wrong marker",
            "        return self._values[P2_MARKERS // 2]",
            "        return self._values[P2_MARKERS // 2 + 1]",
        ),
        mutation(
            "value: P2 returns 0.0 instead of raising on empty",
            "        if self._seen == 0:\n"
            '            msg = "no samples: a quantile of nothing is not 0.0"\n'
            "            raise ValueError(msg)",
            "        if self._seen == 0:\n            return 0.0",
        ),
        mutation(
            "value: Exact returns 0.0 instead of raising on empty",
            "        if not self._ring:\n"
            '            msg = "no samples: a quantile of nothing is not 0.0"\n'
            "            raise ValueError(msg)",
            "        if not self._ring:\n            return 0.0",
        ),
        mutation(
            "value: use interpolation-style rank",
            "    rank = max(1, math.ceil(q * len(ordered)))",
            "    rank = max(1, int(q * len(ordered)))",
        ),
        # observe_turn gap extraction
        mutation(
            "gaps: measure end-to-end instead of end-to-start",
            "                    float(word.start_ms - self._turn.last_end_ms),",
            "                    float(word.end_ms - self._turn.last_end_ms),",
        ),
        mutation(
            "gaps: drop the upper clamp",
            "                    float(GAP_CLAMP_MAX_MS),",
            "                    float('inf'),",
        ),
        mutation(
            "gaps: drop the lower clamp",
            "                    0.0,\n                    float(GAP_CLAMP_MAX_MS),",
            "                    -float('inf'),\n                    float(GAP_CLAMP_MAX_MS),",
        ),
        mutation(
            "gaps: do not count them",
            "                self._n_gaps += 1",
            "                pass",
        ),
        mutation(
            "gaps: feed only one estimator",
            "                self._g_p90.update(gap)",
            "                pass",
        ),
        mutation(
            "gaps: let them span the turn boundary",
            "            self._turn = _TurnAccumulator(turn_order=turn.turn_order)",
            "            self._turn.turn_order = turn.turn_order",
        ),
        mutation(
            "gaps: ingest unfinalised words too (EC-21)",
            "        while index < len(words) and words[index].is_final:",
            "        while index < len(words):",
        ),
        mutation(
            "gaps: reprocess every word each partial",
            "        index = self._turn.words_done",
            "        index = 0",
        ),
        mutation(
            "warm: off-by-one on the threshold",
            "            cold=self._n_gaps < MIN_GAPS_FOR_WARM,",
            "            cold=self._n_gaps <= MIN_GAPS_FOR_WARM,",
        ),
        # ADR-023 inversion repair
        mutation(
            "ADR-023: drop the g_p90 inversion repair",
            "        g_p90 = max(g_p90, g_p50)",
            "        pass",
        ),
        # snapshot / restore, one dropped field at a time (EC-03)
        mutation(
            "restore: drop g_p90",
            "            profiler._g_p90 = factory(0.90, state.g_p90_ms, seen=state.n_gaps)",
            "            pass",
        ),
        mutation(
            "restore: drop n_gaps",
            "        profiler._n_gaps = state.n_gaps",
            "        pass",
        ),
        mutation(
            "restore: drop speech_rate",
            "        profiler._speech_rate = state.speech_rate",
            "        pass",
        ),
        mutation(
            "restore: drop disfluency",
            "        profiler._disfluency = state.disfluency",
            "        pass",
        ),
        mutation(
            "restore: drop jitter",
            "        profiler._jitter = state.jitter",
            "        pass",
        ),
        mutation(
            "restore: drop last_turn_order",
            "        profiler._last_turn_order = state.last_turn_order",
            "        pass",
        ),
        mutation(
            "restore: drop the cut ring",
            "        for _ in range(cuts):\n            profiler._cut_ring.append(True)",
            "        pass",
        ),
        mutation(
            "snapshot: swap p50 and p90",
            "            g_p50_ms=current.g_p50_ms,\n            g_p90_ms=current.g_p90_ms,",
            "            g_p50_ms=current.g_p90_ms,\n            g_p90_ms=current.g_p50_ms,",
        ),
    )


def _self_test_mutations() -> tuple[Mutation, ...]:
    """Two deliberate failures, so the harness's own two failure modes are testable.

    CLAUDE.md §5 says to confirm a check can fail by making it fail, once, on
    purpose — and that applies to the harness as much as to a test. Committed
    rather than demonstrated by hand, because a demonstration is exactly the kind
    of fix that gets deleted with the session it happened in.
    """
    src = "src/nod_core/profiler.py"
    return (
        # A comment edit changes no behaviour, so nothing can catch it. Expect
        # SURVIVED, which is the harness reporting a real (here, harmless) gap.
        Mutation(
            "SELFTEST expect SURVIVED: edit a comment, which no test can notice",
            src,
            "P2_MARKERS: Final = 5",
            "P2_MARKERS: Final = 5  # selftest marker",
            PROFILER_TESTS,
        ),
        # An anchor that cannot match. Expect HARNESS ERROR, not a survivor.
        Mutation(
            "SELFTEST expect HARNESS ERROR: an anchor that appears zero times",
            src,
            "this string does not occur anywhere in the profiler",
            "irrelevant",
            PROFILER_TESTS,
        ),
    )


def _arbiter_mutations() -> tuple[Mutation, ...]:
    """The `arbiter.py` set. Both gates' decision logic — CLAUDE.md §5's first tier.

    `max_turn_silence` governs the incomplete-utterance regime and is the only knob
    that tolerates a mid-sentence pause; `min_turn_silence` governs responsiveness
    after a complete one. A silent regression in either is the project failing at
    the thing it exists to do, so every term of both gates, every §5 guard, and
    each of ADR-020's four ordered steps gets a mutation.
    """
    src = "src/nod_core/arbiter.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, CONTROL_TESTS)

    return (
        # §4, the max_turn_silence gate (the primary surface, ADR-011)
        mutation(
            "law/max: read p50 instead of p90",
            "MAX_MS_FROM_P90_GAIN * features.g_p90_ms + MAX_MS_OFFSET",
            "MAX_MS_FROM_P90_GAIN * features.g_p50_ms + MAX_MS_OFFSET",
        ),
        mutation(
            "law/max: drop the disfluency term",
            "            + MAX_MS_DISFLUENCY_GAIN * features.disfluency",
            "            + 0.0 * features.disfluency",
        ),
        mutation(
            "law/max: drop the recent_cuts term",
            "            + MAX_MS_RECENT_CUTS_GAIN * features.recent_cuts",
            "            + 0.0 * features.recent_cuts",
        ),
        mutation(
            "law/max: sign flip on the disfluency term",
            "            + MAX_MS_DISFLUENCY_GAIN * features.disfluency",
            "            - MAX_MS_DISFLUENCY_GAIN * features.disfluency",
        ),
        mutation(
            "law/max: ignore the context axis",
            "    max_ms *= hint.max_mult",
            "    pass",
        ),
        mutation(
            "law/max: leak jitter into the law (ADR-011)",
            "            + MAX_MS_RECENT_CUTS_GAIN * features.recent_cuts",
            "            + MAX_MS_RECENT_CUTS_GAIN * features.recent_cuts\n            + 0.5 * features.jitter",
        ),
        # §4, the min_turn_silence gate
        mutation(
            "law/min: drop the p50 term",
            "        min_ms = MIN_MS_FROM_P50_GAIN * features.g_p50_ms + MIN_MS_OFFSET",
            "        min_ms = MIN_MS_OFFSET + 0.0",
        ),
        mutation(
            "law/min: read p90 instead of p50",
            "        min_ms = MIN_MS_FROM_P50_GAIN * features.g_p50_ms + MIN_MS_OFFSET",
            "        min_ms = MIN_MS_FROM_P50_GAIN * features.g_p90_ms + MIN_MS_OFFSET",
        ),
        mutation(
            "law/min: ignore the context axis",
            "    min_ms *= hint.min_mult",
            "    pass",
        ),
        # §4, cold path, clamps, invariant, ceiling
        mutation("law: ignore the cold branch", "    if cold:", "    if False:"),
        mutation(
            "law: drop the min clamp",
            "    min_ms = min(max(min_ms, MIN_MS_FLOOR), MIN_MS_CEIL)",
            "    pass",
        ),
        mutation(
            "law: drop the max clamp",
            "    max_ms = min(max(max_ms, MAX_MS_FLOOR), MAX_MS_CEIL)",
            "    pass",
        ),
        mutation(
            "law: drop the invariant repair",
            "    max_ms = max(max_ms, min_ms + INVARIANT_GAP_MS)\n    max_ms = min(max_ms, ceiling_ms - ENDPOINT_OVERHEAD_MS)",
            "    max_ms = min(max_ms, ceiling_ms - ENDPOINT_OVERHEAD_MS)",
        ),
        # §4, the ceiling clamp. Both of these were unkillable while
        # `ENDPOINT_OVERHEAD_MS` was 0 (ADR-040): §9 property 3 reduced to
        # `max_ms <= ceiling_ms`, which the max clamp already gave, so the
        # property was skipped and nothing else asserted the clamp existed. They
        # are here because the constant moved, and they are the reason it had to.
        mutation(
            "law/ceiling: drop the clamp entirely",
            "    max_ms = min(max_ms, ceiling_ms - ENDPOINT_OVERHEAD_MS)",
            "    pass",
        ),
        mutation(
            "law/ceiling: ignore the measured overhead",
            "    max_ms = min(max_ms, ceiling_ms - ENDPOINT_OVERHEAD_MS)",
            "    max_ms = min(max_ms, ceiling_ms)",
        ),
        # ADR-020 step 4: repair after decay
        mutation(
            "repair: drop the post-decay invariant",
            "    max_ms = max(max_ms, min_ms + INVARIANT_GAP_MS)\n    return replace(",
            "    return replace(",
        ),
        mutation(
            "repair: drop the post-decay min clamp",
            "    min_ms = min(max(config.min_turn_silence_ms, MIN_MS_FLOOR), MIN_MS_CEIL)",
            "    min_ms = config.min_turn_silence_ms",
        ),
        mutation(
            "decide: skip the repair entirely",
            "        proposed = _repair(proposed)",
            "        pass",
        ),
        # §5 hysteresis, on the target (ADR-020)
        mutation(
            "hysteresis: emit unconditionally",
            "        if not overdue and not self._moved_enough(target, reference, sendable):\n            return None",
            "        pass",
        ),
        mutation(
            "hysteresis: measure against state.current, not the last emitted",
            "        reference = self._emitted if self._emitted is not None else state.current",
            "        reference = state.current",
        ),
        mutation(
            "hysteresis: >= instead of >",
            "            if abs(now - was) > HYST_FRACTION * was:",
            "            if abs(now - was) >= HYST_FRACTION * was:",
        ),
        # §5 asymmetric decay (ADR-020 ordering, ADR-022 widening cap)
        mutation(
            "decay: no widening cap (pre-ADR-022 behaviour)",
            "                stepped = min(wanted, was * (1.0 + WIDEN_STEP))",
            "                stepped = float(wanted)",
        ),
        mutation(
            "decay: no narrowing cap",
            "                stepped = max(wanted, was * (1.0 - NARROW_STEP))",
            "                stepped = float(wanted)",
        ),
        mutation(
            "decay: widening capped at NARROW_STEP (symmetric)",
            "                stepped = min(wanted, was * (1.0 + WIDEN_STEP))",
            "                stepped = min(wanted, was * (1.0 + NARROW_STEP))",
        ),
        mutation(
            "decay: swap the two directions",
            "            if wanted > was:",
            "            if wanted < was:",
        ),
        # §5 rate cap, both halves
        mutation(
            "rate cap: drop the per-turn half",
            "        if state.turn_order <= self._patched_turn:\n            return None",
            "        pass",
        ),
        mutation(
            "rate cap: drop the per-session half",
            "        if state.patches_sent >= MAX_PATCHES:\n            return None",
            "        pass",
        ),
        # §5 capability gate and host override
        mutation(
            "capability gate: send every field regardless of verdict",
            "        sendable = self._capabilities.updatable_fields - state.host_override_fields",
            "        sendable = frozenset(wire for wire, _ in WIRE_FIELDS)",
        ),
        mutation(
            "host override: ignore the fields the host set",
            "        sendable = self._capabilities.updatable_fields - state.host_override_fields",
            "        sendable = self._capabilities.updatable_fields",
        ),
        # §5 boolean floor
        mutation(
            "boolean floor: drop the cap",
            '        capped = state.expected_answer == "boolean"',
            "        capped = False",
        ),
        # ADR-024's two exemptions. A guard whose exception is untested is the
        # exception silently not existing.
        mutation(
            "ADR-024: re-subject the floor to hysteresis",
            '        overdue = (\n            capped\n            and "min_turn_silence" in sendable\n            and reference.min_turn_silence_ms > BOOLEAN_MIN_MS_CAP\n        )',
            "        overdue = False",
        ),
        mutation(
            "ADR-024: drop the post-decay floor",
            "            proposed = _boolean_floor(proposed)",
            "            pass",
        ),
        # §6 state machine
        mutation(
            "SAFE: keep patching after a controller error (INV-8)",
            "        if self._state is ControllerState.SAFE:\n            self._state = ControllerState.COLD\n            return None",
            "        pass",
        ),
        mutation(
            "FROZEN: let the speaker axis through while frozen",
            "            cold=state.features.cold or frozen,",
            "            cold=state.features.cold,",
        ),
        mutation(
            "note_error: do not enter SAFE",
            "        self._state = ControllerState.SAFE",
            "        pass",
        ),
        # ADR-021
        mutation(
            "ADR-021: accept a ceiling below the floor at construction",
            "        if ceiling_ms < CEILING_FLOOR_MS:",
            "        if False:",
        ),
        mutation(
            "ADR-021: do not clamp the per-turn ceiling",
            "        ceiling_ms = max(state.ceiling_ms, CEILING_FLOOR_MS)",
            "        ceiling_ms = state.ceiling_ms",
        ),
    )


def _policy_mutations() -> tuple[Mutation, ...]:
    """All of `policy.py`. First tier because `min_mult` reaches published TTL."""
    src = "src/nod_core/policy.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, POLICY_TESTS)

    return (
        mutation(
            "hint_for: None does not get the policy default",
            "        if expected is None:\n            return self._default",
            "        if expected is None:\n            return WindowHint(min_mult=1.0, max_mult=1.0)",
        ),
        mutation(
            "hint_for: an unnamed class gets neutral, not the policy default",
            "        return self._hints.get(expected, self._default)",
            "        return self._hints.get(expected, WindowHint(min_mult=1.0, max_mult=1.0))",
        ),
        mutation(
            "compile: swap min_mult and max_mult",
            "            answer: WindowHint(min_mult=row.min_mult, max_mult=row.max_mult)",
            "            answer: WindowHint(min_mult=row.max_mult, max_mult=row.min_mult)",
        ),
        mutation(
            "compile: every class collapses to the default",
            "            answer: WindowHint(min_mult=row.min_mult, max_mult=row.max_mult)",
            "            answer: WindowHint(min_mult=policy.default.min_mult, max_mult=policy.default.max_mult)",
        ),
        mutation(
            "compile: the default is hardcoded neutral",
            "        default=WindowHint(\n            min_mult=policy.default.min_mult, max_mult=policy.default.max_mult\n        ),",
            "        default=WindowHint(min_mult=1.0, max_mult=1.0),",
        ),
        mutation(
            "__init__: alias the source mapping instead of copying it",
            "        self._hints: Mapping[ExpectedAnswer, WindowHint] = dict(hints)",
            "        self._hints: Mapping[ExpectedAnswer, WindowHint] = hints",
        ),
        mutation(
            "load: key the cache on the path, not the content",
            '    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()',
            "    digest = str(source)",
        ),
        mutation(
            "load: unbounded cache (INV-3)",
            "@lru_cache(maxsize=POLICY_CACHE_SIZE)",
            "@lru_cache(maxsize=None)",
        ),
        mutation(
            "load: skip schema validation",
            "    return compile_policy(PolicyFile.model_validate(document))",
            "    return compile_policy(PolicyFile.model_construct(**document))",
        ),
    )


def _proxy_mutations() -> tuple[Mutation, ...]:
    """`proxy.py`'s two first-tier paths, and only those.

    CLAUDE.md §5 puts `send_patch_upstream` and `run_controller`'s `SAFE` path in
    the non-negotiable tier and the rest of the module in the accepted-thinner
    one. The reason is the failure direction: a broken audio pump is a silent
    call and a broken rotation is a dropped session, and nobody ships either by
    accident. These two fail *invisibly and in the flattering direction* — a
    patch computed, traced and never applied, or a controller quietly holding
    last-known-good, both produce a run that looks like `nod` and behaves like
    `balanced`, so the arm gets measured under the wrong label.

    No mutations for `pump_audio_up`, `pump_events_down`, `rotate` or `aclose`.
    That is the tier decision, taken deliberately rather than by omission.
    """
    src = "src/nod_core/proxy.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, PROXY_TESTS)

    return (
        # send_patch_upstream: did the patch reach the socket, with its values?
        mutation(
            "send: never call update_configuration",
            "                await self._upstream.update_configuration(payload)",
            "                pass",
        ),
        mutation(
            "send: send an empty payload",
            "        if not payload:\n            return",
            "        payload = {}",
        ),
        mutation(
            "send: ignore the host override when building the payload (EC-33)",
            "            if field in patch_fields and field not in held",
            "            if field in patch_fields",
        ),
        mutation(
            "send: advance config_in_force before the send is confirmed",
            "            self._patches_sent += 1\n            self._current = pending.config",
            "            self._patches_sent += 1",
        ),
        mutation(
            "send: do not count the patch against the session rate cap",
            "            self._patches_sent += 1",
            "            pass",
        ),
        # EC-32: one retry, then observe.
        mutation(
            "EC-32: no retry at all",
            "        for attempt in (1, 2):",
            "        for attempt in (1,):",
        ),
        mutation(
            "EC-32: retry forever instead of degrading",
            "                if attempt == 2:",
            "                if False:",
        ),
        mutation(
            "EC-32: degrade to off rather than observe",
            "                    self._mode = NodMode.OBSERVE",
            "                    self._mode = NodMode.OFF",
        ),
        mutation(
            "EC-32: a rejection is not traced",
            '                self._trace.emit(\n                    "config_rejected",',
            '                self._trace.emit(\n                    "nothing_happened",',
        ),
        # run_controller's SAFE path: EC-31 and INV-8, and loudly.
        mutation(
            "EC-31: do not enter SAFE on a controller exception",
            "            self._arbiter.note_error(exc)",
            "            pass",
        ),
        mutation(
            "EC-31: entering SAFE is silent",
            '            self._trace.emit(\n                "controller_error",',
            '            self._trace.emit(\n                "nothing_happened",',
        ),
        mutation(
            "EC-31: the proxy does not count the error",
            "            self._controller_errors += 1",
            "            pass",
        ),
        mutation(
            "EC-31: let a transport failure escape send_patch_upstream",
            "            try:\n                await self._upstream.update_configuration(payload)\n            except Exception as exc:",
            "            try:\n                await self._upstream.update_configuration(payload)\n            except KeyError as exc:",
        ),
        # observe mode must decide and trace but send nothing.
        mutation(
            "observe: send patches anyway (ARCHITECTURE §7)",
            "        if patch is None or self._mode is not NodMode.ADAPT:\n            return",
            "        if patch is None:\n            return",
        ),
        # ADR-021's soft side at this boundary.
        mutation(
            "ADR-021: do not clamp a per-connection ceiling",
            "        self._ceiling_ms = max(ceiling_ms, CEILING_FLOOR_MS)",
            "        self._ceiling_ms = ceiling_ms",
        ),
    )


def _replay_mutations() -> tuple[Mutation, ...]:
    """`_turn_from_words` and the closed loop. First tier: it feeds the chart.

    ADR-031 moved the sidecar from reconstructed gaps to replayed words, and
    `run_nod_clip` is what puts the `nod` arms on the Pareto chart. Both were
    entirely untested before Gate 7 — `replay.py` sat at 55 % with the closed
    loop wholly uncovered — which is the second rule of CLAUDE.md §5 exactly:
    anything feeding a published number.
    """
    src = "src/nod_bench/replay.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, WORDS_TESTS)

    return (
        Mutation(
            "scored_utterances: pool every gap into every turn",
            src,
            "                g for g in clip.truth.gaps if bounds[i] <= g.start_ms < bounds[i + 1]",
            "                g for g in clip.truth.gaps",
            TRACKC_TESTS,
        ),
        Mutation(
            "expected_at: return the first turn's class for every turn",
            src,
            "    return covering[-1].expected_answer if covering else None",
            "    return covering[0].expected_answer if covering else None",
            TRACKC_TESTS,
        ),
        mutation(
            "_turn_from_words: drop the wordless guard",
            "    if not clip.truth.words:",
            "    if not clip.truth.words and False:",
        ),
        mutation(
            "_turn_from_words: put placeholder tokens back (ADR-028 regression)",
            "            text=word.text,",
            '            text="w0",',
        ),
        mutation(
            "_turn_from_words: exclude a word ending exactly on the boundary",
            "    taken = [w for w in clip.truth.words[consumed:] if w.end_ms <= until_ms]",
            "    taken = [w for w in clip.truth.words[consumed:] if w.end_ms < until_ms]",
        ),
        mutation(
            "_turn_from_words: never advance the cursor, so words replay forever",
            "        consumed + len(taken),",
            "        consumed,",
        ),
    )


def _transcribe_mutations() -> tuple[Mutation, ...]:
    """`apply_words`. First tier: it writes the ground truth the metrics read."""
    src = "src/nod_bench/transcribe.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, WORDS_TESTS)

    return (
        mutation(
            "apply_words: stop checking the audio hash before rewriting truth",
            "        if digest != clip.sha256:",
            "        if digest != clip.sha256 and False:",
        ),
        mutation(
            "apply_words: accept a corpus with a clip nobody transcribed",
            "        if clip.clip_id not in words_by_clip:",
            "        if clip.clip_id not in words_by_clip and False:",
        ),
    )


def _health_mutations() -> tuple[Mutation, ...]:
    """`/readyz`'s four conditions (ADR-041). Second tier, deliberately.

    CLAUDE.md §5 puts "the rest of the server plumbing" in the accepted-thinner
    list and that has not changed. These four are here for one narrow reason: the
    checks they guard *were* `ready=False` literals, so every test over them
    asserted a constant. Three mutations is enough to establish that the
    replacements are load-bearing in both directions — that a check stuck green
    fails the 503 test and a check stuck red fails the 200 test. Adding more of
    `app.py` would be diluting the first tier, which §5 forbids.
    """
    src = "src/nod_server/app.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, HEALTH_TESTS)

    return (
        mutation(
            "readyz/config: report ready with the credential missing",
            "        ready=not missing,",
            "        ready=True,",
        ),
        mutation(
            "readyz/volume: trust the path without writing to it",
            '        path.mkdir(parents=True, exist_ok=True)\n        probe.write_bytes(b"")',
            "        pass",
        ),
        mutation(
            "readyz/capability: ignore a knob that is not LIVE",
            "        ready=not not_live,",
            "        ready=True,",
        ),
        mutation(
            "readyz: never aggregate to not_ready",
            "    ready = all(check.ready for check in results)",
            "    ready = True",
        ),
        mutation(
            "schema: advertise an unbuilt route in the public OpenAPI schema",
            '@router.get("/voices", include_in_schema=False)',
            '@router.get("/voices")',
        ),
        mutation(
            "schema: let an unbuilt route raise NotImplementedError, so 500 not 501",
            '    _unbuilt("GET /v1/presets")',
            "    raise NotImplementedError",
        ),
    )


def _metrics_mutations() -> tuple[Mutation, ...]:
    """`certain_only`'s per-gap scoping rule (ADR-036). First tier.

    `ProxyDivergence` is how a reader learns how much of a published PCR rests
    on the construction proxy rather than on a semantic fact, so the rule that
    decides what is in that scope feeds a published number directly — CLAUDE.md
    §5's second tier-one rule. ADR-034 makes it load-bearing: the standing
    fragment/ambiguous rule is tolerable *because* this column exists.
    """
    src = "src/nod_bench/metrics.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, METRICS_TESTS)

    return (
        mutation(
            "governing_certain: treat an uncovered boundary as certain",
            '            if not covering or covering[0].certainty != "certain":',
            '            if covering and covering[0].certainty != "certain":',
        ),
        mutation(
            "governing_certain: accept an ambiguous governing gap",
            "                return False",
            "                continue",
        ),
        mutation(
            "governing_certain: look up the gap at the fired time, not the silence start",
            "            covering = [g for g in self.gaps if g.start_ms <= start <= g.end_ms]",
            "            covering = [g for g in self.gaps if g.start_ms <= start < g.start_ms]",
        ),
        mutation(
            "_selected: scope per utterance again, the rule ADR-036 replaced",
            "                if not utterance.governing_certain(starts):",
            '                if not all(g.certainty == "certain" for g in utterance.gaps):',
        ),
        mutation(
            "_selected: attribute every boundary to every utterance",
            "                    for i in _attribute_indices(index, obs)",
            "                    for i in range(len(obs.emitted_silence_start_ms))",
        ),
        mutation(
            "_selected: drop the missing-silence-starts guard",
            "        if certain_only and obs.emitted_end_ms and not obs.emitted_silence_start_ms:",
            "        if False:",
        ),
        mutation(
            "total_utterances: count observations instead of utterances",
            "    return sum(len(obs.utterances) for obs in runs)",
            "    return len(runs)",
        ),
        mutation(
            "ClipObservation: drop the parallel-length validator",
            "        if self.emitted_silence_start_ms and len(self.emitted_silence_start_ms) != len(",
            "        if False and len(self.emitted_silence_start_ms) != len(",
        ),
    )


def _trackc_script_mutations() -> tuple[Mutation, ...]:
    """The committed Track C scripts. First tier, under the ground-truth rule.

    CLAUDE.md §5's second bullet covers "the corpus ground truth", and these are
    the input to a corpus that **cannot be regenerated**: ADR-031 established that
    the gap budget is not recoverable from the audio afterwards, so a script that
    silently stops crossing `MIN_GAPS_FOR_WARM` costs the recording session and
    there is no repair. Every other guarded artifact in this repository can be
    rebuilt by re-running something.

    Two directions, because the failure has two. Shortening a gap-bearing answer
    pushes the crossing later than the plan; the conditions drifting apart breaks
    §5's paired design, under which a measured difference between fluent and
    hesitant would be content rather than rhythm.
    """

    def mutation(label: str, target: str, old: str, new: str) -> Mutation:
        return Mutation(label, target, old, new, SCRIPT_TESTS)

    return (
        mutation(
            "B-answers: gut the inserted sentence turn (ADR-039 §2)",
            "data/trackC/scripts/B-answers.json",
            '"I get headaches by the second day and my doctor said I should not skip it at all"',
            '"It hurts"',
        ),
        mutation(
            "D-answers: shorten the opener below its planned contribution",
            "data/trackC/scripts/D-answers.json",
            '"I started a new job in August and I need to get myself and my two kids on the plan before the deadline"',
            '"New job in August"',
        ),
        mutation(
            "C-hesitant: reword a prompt, breaking §5's paired design",
            "data/trackC/scripts/C-hesitant.json",
            '"prompt": "Claims. What\'s the bill you\'re looking at?"',
            '"prompt": "Claims. Tell me about the charge."',
        ),
        mutation(
            "E-fluent: drop a class the context axis needs",
            "data/trackC/scripts/E-fluent.json",
            '"expected_answer": "spelling"',
            '"expected_answer": "free"',
        ),
    )


def _live_mutations() -> tuple[Mutation, ...]:
    """The live path, which produces every published figure. First tier.

    CLAUDE.md §5's second rule covers "anything that feeds a published number",
    and after Gate 4a that is this code rather than the simulated driver — INV-9
    routes the README table and `docs/RESULTS.md` through `--live`. The two
    proxy mutations are ADR-044's: the context axis reaching `decide` at all is
    what makes `nod` differ from `nod-nocontext`, and an arm measured under the
    wrong label is the failure §5 names for this module.
    """

    def mutation(label: str, target: str, old: str, new: str) -> Mutation:
        return Mutation(label, target, old, new, LIVE_TESTS)

    proxy = "src/nod_core/proxy.py"
    replay = "src/nod_bench/replay.py"

    return (
        # ADR-044: the context axis in the production controller path.
        mutation(
            "proxy/context: never ask the context source (pre-ADR-044)",
            proxy,
            "            declared = (\n                self._context(turn.turn_order) "
            "if self._context is not None else None\n            )",
            "            declared = None",
        ),
        mutation(
            "proxy/context: compute the hint and discard it",
            proxy,
            "            self._hint = (\n                self._context.hint_for(declared)\n"
            "                if self._context is not None\n"
            "                else NEUTRAL_HINT\n            )",
            "            self._hint = NEUTRAL_HINT",
        ),
        # The live scorer: both arrays reach PCR's arithmetic.
        mutation(
            "live scorer: drop the silence starts (ADR-036 cannot scope)",
            replay,
            "        emitted_silence_start_ms=tuple(b.silence_started_ms for b in boundaries),",
            "        emitted_silence_start_ms=(),",
        ),
        mutation(
            "live scorer: time the boundary from the word end, not the frame",
            replay,
            "        emitted_end_ms=tuple(b.fired_at_ms for b in boundaries),",
            "        emitted_end_ms=tuple(b.silence_started_ms for b in boundaries),",
        ),
        # The ablations, which are arm labels as much as behaviour.
        mutation(
            "ClipContext: ignore the nocontext ablation",
            replay,
            "        if not self._enabled:\n            return None\n"
            "        return self._by_turn.get(turn_order, self._fallback)",
            "        return self._by_turn.get(turn_order, self._fallback)",
        ),
        mutation(
            "ClipContext: return a neutral hint even when enabled",
            replay,
            "        if not self._enabled or self._policy is None:\n"
            "            return WindowHint(min_mult=1.0, max_mult=1.0)\n"
            "        return self._policy.hint_for(declared)",
            "        return WindowHint(min_mult=1.0, max_mult=1.0)",
        ),
        mutation(
            "nospeaker: run the ablation warm",
            replay,
            "    profiler = ColdProfiler() if not axes.speaker else Profiler()",
            "    profiler = Profiler()",
        ),
        mutation(
            "patches: report the decision count, not what the socket took",
            replay,
            "        patches=proxy.patches_sent,",
            "        patches=len(controller.samples),",
        ),
    )


def _wire_mutations() -> tuple[Mutation, ...]:
    """The AssemblyAI wire encoding. First tier, and it earned the place.

    §5's second rule is "anything that feeds a published number". This feeds
    *every* one: sent as floats, the connect config is rejected at 3006 and the
    static arms never run, and `send_patch_upstream` has each mid-stream patch
    refused so a `nod` arm degrades to `balanced` under its own label.

    Both directions are mutated. Not coercing is the bug that was found;
    coercing *everything* is the same bug pointing the other way, because
    `vad_threshold=1.0` is integral and is not an integer.
    """
    src = "src/nod_adapters/assemblyai/session.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, WIRE_TESTS)

    return (
        mutation(
            "wire: send every config value as a float (the Gate 4b bug)",
            "    return int(value) if field in INTEGER_FIELDS else float(value)",
            "    return float(value)",
        ),
        mutation(
            "wire: coerce every integral value, including vad_threshold",
            "    return int(value) if field in INTEGER_FIELDS else float(value)",
            "    return int(value) if float(value).is_integer() else float(value)",
        ),
        mutation(
            "wire: skip the coercion on UpdateConfiguration only",
            "        message.update({k: _typed(k, v) for k, v in patch.items()})",
            "        message.update(patch)",
        ),
        mutation(
            "wire: drop max_turn_silence from the integer set",
            'INTEGER_FIELDS: Final = frozenset({"min_turn_silence", "max_turn_silence"})',
            'INTEGER_FIELDS: Final = frozenset({"min_turn_silence"})',
        ),
    )


def _ladder_mutations() -> tuple[Mutation, ...]:
    """`ladder.py`. First tier under §5's second rule: it feeds the headline.

    ADR-054 is the README's lead finding and this module is what will restate
    it, on human speech, with the artifacts committed this time. Every mutation
    below moves a *published* firing time, and each does so silently — the
    output stays a plausible table of milliseconds, which is the failure mode
    §5 reserves the first tier for.

    The anchor mutation is the one that was a live defect rather than a
    hypothetical: the first draft of `build_from_halves` measured the hold from
    the file length, and `say` leaves ~32 ms of sub-threshold tail, so every
    prediction sat that much late in the direction that flatters the service.
    """
    src = "src/nod_bench/ladder.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, LADDER_TESTS)

    return (
        mutation(
            "ladder: anchor the hold to the file length, not the acoustic end",
            "    prefix_end = runs[-1].end_ms if runs else file_end",
            "    prefix_end = file_end",
        ),
        mutation(
            "ladder: score the requested hold instead of the delivered one",
            "                    hold_measured_ms=cont_start - prefix_end,",
            "                    hold_measured_ms=hold,",
        ),
        mutation(
            "ladder: let one row report a spread, so a single point is a result",
            "    return max(values) - min(values) if len(values) >= 2 else None",
            "    return max(values) - min(values) if len(values) >= 1 else None",
        ),
        mutation(
            "ladder: count the end-of-clip boundary as an in-hold one",
            "            if self.prefix_end_ms < b.fired_at_ms < hold_end:",
            "            if self.prefix_end_ms < b.fired_at_ms:",
        ),
        mutation(
            "patience: label the VAD-relative wait instead of the caller-relative one",
            "                    f'font-size=\"13\">{held:.0f} ms</text>'",
            '                    f\'font-size="13">'
            "{row.in_hold.fired_at_ms - row.in_hold.silence_started_ms:.0f} ms</text>'",
        ),
        mutation(
            "patience: draw the overlay whether or not it was asked for",
            "        if patience:",
            "        if True:",
        ),
        mutation(
            "patience: label a boundary that never fell inside the pause",
            "            if held is not None and row.in_hold is not None:",
            "            if row.boundaries:",
        ),
        Mutation(
            "override: accept the swept gates and send the preset anyway",
            "src/nod_bench/replay.py",
            "    settings = override if override is not None else STATIC_ARMS[arm]",
            "    settings = STATIC_ARMS[arm]",
            LIVE_TESTS,
        ),
        Mutation(
            "override: apply it to every arm, corrupting the presets",
            "src/nod_bench/replay.py",
            "    settings = override if override is not None else STATIC_ARMS[arm]",
            "    settings = override if override is not None else STATIC_ARMS['aggressive']",
            LIVE_TESTS,
        ),
        mutation(
            "ladder: shrink the tail below conservative's gate",
            "TAIL_SILENCE_MS: Final = 4030",
            "TAIL_SILENCE_MS: Final = 3000",
        ),
        mutation(
            "ladder: collapse the confidence hypothesis onto the min gate",
            "        silence_ms = max(float(min_gate_ms), confidence_ms)",
            "        silence_ms = float(min_gate_ms)",
        ),
        mutation(
            "ladder: salvage a mis-segmented take instead of refusing it",
            "    if len(runs) != wanted:",
            "    if False:",
        ),
        mutation(
            "ladder: score the time before the in-hold call",
            "    if prediction.fires_in_hold != (observed is not None):\n        return False",
            "    if prediction.fires_in_hold != (observed is not None):\n        return bool(\n            row.boundaries\n            and min(\n                abs(b.fired_at_ms - prediction.fired_at_ms) for b in row.boundaries\n            )\n            <= tolerance_ms\n        )",
        ),
        Mutation(
            "clamp: drop the max ceiling, against the committed observations",
            "src/nod_bench/ladder.py",
            "        silence_ms = min(max(float(min_gate_ms), confidence_ms), "
            "float(max_gate_ms))",
            "        silence_ms = max(float(min_gate_ms), confidence_ms)",
            SMOKE_TESTS,
        ),
        mutation(
            "turns: include the overhead, so a split turn reads as whole",
            "        expected_turns=2 if silence_ms < geometry.hold_measured_ms else 1,",
            "        expected_turns=2 if wait_ms < geometry.hold_measured_ms else 1,",
        ),
        mutation(
            "turns: always predict a split",
            "        expected_turns=2 if silence_ms < geometry.hold_measured_ms else 1,",
            "        expected_turns=2,",
        ),
        mutation(
            "svg: draw one turn count for every row",
            "        turns = len(row.boundaries)",
            "        turns = len(rows[0].boundaries)",
        ),
        mutation(
            "svg: drop the provenance caption from the image",
            '        f\'<text x="12" y="22" fill="#7d8b9a" font-size="13">{caption}</text>\',',
            '        "",',
        ),
        mutation(
            "svg: draw absolute amplitude, so the waveform shows recording level",
            "    return tuple(min(1.0, v / peak) for v in vals)",
            "    return tuple(min(1.0, v) for v in vals)",
        ),
        mutation(
            "ladder: drift the silence floor away from the corpus floor",
            "SILENCE_FLOOR_DBFS: Final = -44.0",
            "SILENCE_FLOOR_DBFS: Final = -40.0",
        ),
    )


def _context_mutations() -> tuple[Mutation, ...]:
    """The context axis on the server path. First tier under §5's second rule.

    `hint.min_mult` multiplies `min_ms`, and `min_ms` is the gate ADR-055
    measured as the only lever that moves a live boundary. A wrong multiplier is
    therefore a wrong window on a real call, arriving with no symptom — every
    value in the strip stays plausible. The axis was also inert for two phases
    while looking implemented, which is the failure these guard against
    returning.
    """
    src = "src/nod_server/context.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, CONTEXT_TESTS)

    return (
        mutation(
            "context: never release the class, so it widens every later turn",
            "        pending, self._pending = self._pending, None",
            "        pending = self._pending",
        ),
        mutation(
            "context: declare nothing, so the axis is inert again",
            "        self._pending = answer",
            "        self._pending = None",
        ),
        mutation(
            "context: return the first cue that matches any class order",
            "    for answer, cues in ANSWER_CUES:",
            "    for answer, cues in reversed(ANSWER_CUES):",
        ),
        mutation(
            "context: match the raw cue, so casing and punctuation defeat it",
            '            if _WORD_BOUNDARY.sub(" ", cue).strip() in hay:',
            "            if cue in text:",
        ),
        Mutation(
            "context: enable the axis by default, defeating the demo guard",
            "src/nod_server/app.py",
            '    want_context = bool(body.get("context", False))',
            '    want_context = bool(body.get("context", True))',
            CONTEXT_TESTS,
        ),
        Mutation(
            "context: classify the caller's words instead of the agent's",
            "src/nod_server/app.py",
            "        declared = classify_prompt(text)",
            "        declared = classify_prompt(transcript)",
            CONTEXT_TESTS,
        ),
    )


def _flush_mutations() -> tuple[Mutation, ...]:
    """`is_caller_turn`. First tier: it decides what FRAG and TTL are computed over.

    Counting the `Terminate` flush put FRAG at exactly 2.000 on every arm and
    made TTL p90 measure the flush. Not counting it is right and moves both the
    flattering way, so both directions are guarded.
    """
    src = "src/nod_bench/replay.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, LIVE_TESTS)

    return (
        # Defect 2 of ADR-047: the abort path's catch breadth. `ConnectionClosed`
        # is not an `OSError`, so the first fix caught nothing and a 1008 escaped
        # as a raw traceback. Without this mutation the fix is "fixed" but has
        # never been seen red.
        mutation(
            "abort: narrow the static catch back to OSError",
            "    except Exception as exc:",
            "    except OSError as exc:",
        ),
        mutation(
            "abort: swallow feeder drift on the controlled arm (EC-37)",
            "        except TimeoutError:\n            collector.cancel()\n            report = None",
            "        except (TimeoutError, FeederDriftError):\n            collector.cancel()\n            report = None",
        ),
        mutation(
            "abort: treat 1008 as an ordinary clip abort, not a sweep abort",
            "    if code == CONCURRENCY_REFUSED_CODE:",
            "    if False:",
        ),
        mutation(
            "flush: score the wordless Terminate turn as a caller turn",
            "    return bool(turn.words)",
            "    return True",
        ),
        mutation(
            "flush: drop every turn, not just the wordless ones",
            "    return bool(turn.words)",
            "    return False",
        ),
        mutation(
            "flush: stop counting what was dropped",
            "        flush_turns=len(dropped),\n    )\n\n\nasync def _run_live_controlled(",
            "        flush_turns=0,\n    )\n\n\nasync def _run_live_controlled(",
        ),
    )


def _report_mutations() -> tuple[Mutation, ...]:
    """The repeat axis and the two required warnings. First tier.

    §5: "the metrics, the quantile definition, the corpus ground truth, the arm
    configurations, the provenance labelling". `interval_kind` *is* provenance
    labelling — it says what the error bars mean — and EC-38's `inconclusive`
    is the one thing standing between a noisy result and a headline claim.
    """

    def mutation(label: str, target: str, old: str, new: str) -> Mutation:
        return Mutation(label, target, old, new, REPORT_TESTS)

    src = "src/nod_bench/report.py"

    _pcr = (
        mutation(
            "pcr-caveat: drop it from every run",
            src,
            '    return ["", PCR_ANCHOR_CAVEAT] if live else []',
            "    return []",
        ),
        mutation(
            "pcr-caveat: emit it on simulated runs too, so it stops being read",
            src,
            '    return ["", PCR_ANCHOR_CAVEAT] if live else []',
            '    return ["", PCR_ANCHOR_CAVEAT]',
        ),
        mutation(
            "pcr-caveat: keep the warning, drop the measured size of the error",
            src,
            "    \"one clip it puts the prefix's end **560 ms late** and the continuation's \"",
            "    \"one clip it puts the prefix's end late and the continuation's \"",
        ),
        mutation(
            "pcr-caveat: unpin the footer call site",
            src,
            "    lines.extend(_pcr_anchor_caveat(live))",
            "    lines.extend([])",
        ),
    )

    return (
        *_pcr,
        mutation(
            "EC-38: never call a comparison inconclusive",
            src,
            "        if width > difference:",
            "        if False:",
        ),
        mutation(
            "EC-38: compare against the wrong side of the interval",
            src,
            "        width = point.ttl_p90_ci_ms[1] - point.ttl_p90_ci_ms[0]",
            "        width = 0.0",
        ),
        mutation(
            "EC-41: miss a direction flip between tracks",
            src,
            "        if a == 0.0 or c == 0.0 or (a > 0) == (c > 0):",
            "        if True:",
        ),
        mutation(
            "repeat axis: slice the clip axis instead of the repeat axis",
            src,
            "        passes = [observations[r::repeats] for r in range(repeats)]",
            "        passes = [\n            observations[r * repeats : (r + 1) * repeats]\n"
            "            for r in range(repeats)\n        ]",
        ),
        # ADR-049's cluster bootstrap: the published live interval.
        mutation(
            "cluster: resample rows, not whole clips (narrows the interval)",
            src,
            "            picked = [obs for cid in picked_ids for obs in groups[arm][cid]]",
            "            picked = [groups[arm][cid][0] for cid in picked_ids]",
        ),
        mutation(
            "cluster: accept uneven clusters",
            src,
            "        if sizes != {repeats}:",
            "        if False:",
        ),
        mutation(
            "cluster: label the interval as covering one source",
            src,
            '                interval_kind="cluster-bootstrap-over-clips",',
            '                interval_kind="iqr-over-repeats",',
        ),
        mutation(
            "cluster: drop the pairing check across arms",
            src,
            '        if [o.clip_id for o in by_arm[arm]] != [o.clip_id for o in by_arm[arms[0]]]:\n            msg = f"arm {arm!r} was not run over the same clips, so pairing is lost"\n            raise ValueError(msg)\n\n    order: list[str] = []',
            "        pass\n\n    order: list[str] = []",
        ),
        mutation(
            "repeat axis: label a live interval as a clip bootstrap",
            src,
            '                interval_kind="iqr-over-repeats",',
            '                interval_kind="bootstrap-ci-over-clips",',
        ),
        mutation(
            "ADR-019: let the simulator report a repeat IQR",
            src,
            "    if simulated and repeats > 1:",
            "    if False:",
        ),
        mutation(
            "INV-9: publish into the README without checking the markers",
            src,
            "        if found != 1:",
            "        if False:",
        ),
    )


def _shipped_policy_mutations() -> tuple[Mutation, ...]:
    """`config/policy.yaml`. First tier: it multiplies `min_ms`, so it sets TTL.

    CLAUDE.md §5 puts `policy.py` in the first tier under the published-number
    rule rather than the controller rule: `0.7` where `1.4` belongs reads as a
    working controller that is simply faster than it should be. The *file* is
    the other half of that and had no guard until Gate 8.
    """
    src = "config/policy.yaml"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, POLICY_TESTS)

    return (
        mutation(
            "policy.yaml: spelling narrows instead of widening",
            "  spelling:       {min_mult: 1.5, max_mult: 2.4}",
            "  spelling:       {min_mult: 0.7, max_mult: 0.7}",
        ),
        mutation(
            "policy.yaml: boolean stops being the only narrowing class",
            "  number:         {min_mult: 1.2, max_mult: 1.6}",
            "  number:         {min_mult: 0.9, max_mult: 0.9}",
        ),
        mutation(
            "policy.yaml: drop a class, so it silently takes the default",
            "  entity_list:    {min_mult: 1.4, max_mult: 2.2}",
            "",
        ),
        mutation(
            "policy.yaml: entity_id's min drifts one step",
            "  entity_id:      {min_mult: 1.3, max_mult: 2.0}",
            "  entity_id:      {min_mult: 1.2, max_mult: 2.0}",
        ),
    )


def _trackc_mutations() -> tuple[Mutation, ...]:
    """The Track C ingest. First tier: it manufactures a scored corpus.

    Everything here feeds a published number in the second sense of CLAUDE.md
    §5 — the corpus ground truth, the seam that decides the turn count, and the
    regime labels PCR scores. ADR-029 notes that hand-authored ground truth has
    no mutation harness at all; this is the part of it that *is* code, so it
    gets one.
    """
    src = "src/nod_bench/trackc.py"

    def mutation(label: str, old: str, new: str) -> Mutation:
        return Mutation(label, src, old, new, TRACKC_TESTS)

    return (
        mutation(
            "check_seams: accept a seam one frame below the floor",
            "    seams = tuple((s, e) for s, e in interior if e - s >= MIN_SEAM_MS)",
            "    seams = tuple((s, e) for s, e in interior if e - s >= MIN_SEAM_MS - 50)",
        ),
        mutation(
            "check_seams: count the trailing silence as a seam",
            "        (start, end) for start, end in silent_runs(audio, sr) if end <= final_ms",
            "        (start, end) for start, end in silent_runs(audio, sr)",
        ),
        mutation(
            "check_seams: pass when too many pauses reached seam length",
            "    if len(seams) < wanted:",
            "    if len(seams) != wanted:",
        ),
        mutation(
            "MIN_SEAM_MS: shrink below conservative's 3600 ms gate",
            "MIN_SEAM_MS: Final = 4500",
            "MIN_SEAM_MS: Final = 3400",
        ),
        mutation(
            "SEAM_FLOOR_DBFS: drift away from the simulator's floor",
            "SEAM_FLOOR_DBFS: Final = -44.0",
            "SEAM_FLOOR_DBFS: Final = -40.0",
        ),
        mutation(
            "promote_word_gaps: mark a transcript gap certain (ADR-034 breach)",
            '                certainty="ambiguous",',
            '                certainty="certain",',
        ),
        mutation(
            "promote_word_gaps: label a transcript gap complete, not fragment",
            '                preceding="fragment",',
            '                preceding="complete",',
        ),
        mutation(
            "promote_word_gaps: drop the dedup, shadowing the seam label",
            "        if any(start <= g.end_ms and g.start_ms <= end for g in described):",
            "        if False:",
        ),
        mutation(
            "promote_word_gaps: dedup on containment of the start, not overlap",
            "        if any(start <= g.end_ms and g.start_ms <= end for g in described):",
            "        if any(g.start_ms <= start <= g.end_ms for g in described):",
        ),
        mutation(
            "build: accept a single-utterance call",
            "        if len(clip.utterances) < 2:",
            "        if False:",
        ),
        mutation(
            "build_clip: give every turn the first turn's declared class",
            "            expected_answer=turn.expected_answer,",
            "            expected_answer=script.turns[0].expected_answer,",
        ),
    )


CATALOGUE: Final[dict[str, tuple[Mutation, ...]]] = {
    "profiler": _profiler_mutations(),
    "arbiter": _arbiter_mutations(),
    "policy": _policy_mutations(),
    "proxy": _proxy_mutations(),
    "replay": _replay_mutations(),
    "metrics": _metrics_mutations(),
    "trackc": _trackc_mutations(),
    "policyfile": _shipped_policy_mutations(),
    "trackcscripts": _trackc_script_mutations(),
    "transcribe": _transcribe_mutations(),
    "health": _health_mutations(),
    "live": _live_mutations(),
    "report": _report_mutations(),
    "wire": _wire_mutations(),
    "flush": _flush_mutations(),
    "ladder": _ladder_mutations(),
    "context": _context_mutations(),
    "selftest": _self_test_mutations(),
}
"""Mutations per module, first tier only (CLAUDE.md §5)."""


class HarnessError(Exception):
    """The mutation could not be applied, so the run proves nothing about tests."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _purge() -> None:
    """Delete every `__pycache__` under the source and test trees."""
    for root in ("src", "tests", "tools"):
        for cache in (REPO / root).rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)


def _run_tests(tests: Sequence[str], *, name_failures: bool = False) -> bool:
    """Run `tests` in a subprocess. Returns True when they failed (mutation killed).

    Args:
        tests: Test paths to run.
        name_failures: Collect the *individual* test ids that died into
            `KILLED_TEST_IDS` and drop `-x`, so every test that would notice the
            mutation is seen rather than only the first. Off by default: it
            makes each mutation run the whole file instead of stopping at the
            first failure, which roughly doubles a full run.

    Returns:
        Whether the suite failed, i.e. whether the mutation was killed.
    """
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(REPO),
    }
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            *tests,
            "--no-cov",
            "-q",
            "-p",
            "no:cacheprovider",
            # Without this pytest colours the summary, so a `FAILED ` prefix
            # match sees an escape sequence and silently finds nothing — which
            # is what it did on the first run of this audit, reporting 0 of 79
            # while every mutation was being killed.
            "--color=no",
            *([] if name_failures else ["-x"]),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if name_failures:
        for line in completed.stdout.splitlines():
            if line.startswith("FAILED "):
                KILLED_TEST_IDS.add(line.split()[1].split(" - ")[0])
    return completed.returncode != 0


KILLED_TEST_IDS: set[str] = set()
"""Individual test ids observed failing under some mutation, when asked.

**This is the only way to answer "which tests have been seen red".** The
catalogue is file-granular — a kill proves *some* test in the file noticed, not
which — so `149/149 killed` certifies files and not tests (ADR-053).
"""


def _apply(mutation: Mutation, original: str, baseline: str) -> None:
    """Write the mutant, or raise `HarnessError` explaining why it could not be."""
    target = REPO / mutation.target
    occurrences = original.count(mutation.old)
    if occurrences != 1:
        msg = (
            f"anchor matched {occurrences} times, expected exactly 1 "
            f"({mutation.target})"
        )
        raise HarnessError(msg)
    target.write_text(original.replace(mutation.old, mutation.new), encoding="utf-8")
    _purge()
    if _digest(target) == baseline:
        msg = f"source hash unchanged after writing the mutant ({mutation.target})"
        raise HarnessError(msg)


def run(mutations: Sequence[Mutation]) -> int:
    """Apply each mutation, run its tests, restore, and report. Returns an exit code."""
    survivors: list[str] = []
    harness_errors: list[str] = []
    killed = 0

    for mutation in mutations:
        target = REPO / mutation.target
        original = target.read_text(encoding="utf-8")
        baseline = _digest(target)
        try:
            _apply(mutation, original, baseline)
        except HarnessError as error:
            print(f"HARNESS ERROR  {mutation.label}\n               {error}")
            harness_errors.append(f"{mutation.label}: {error}")
            continue
        finally:
            target.write_text(original, encoding="utf-8")
            _purge()
            if _digest(target) != baseline:
                msg = f"restore failed for {mutation.target}"
                raise HarnessError(msg)

        # Re-apply for the test run, then restore again. Split from the check
        # above so a failed restore can never be confused with a test result.
        target.write_text(
            original.replace(mutation.old, mutation.new), encoding="utf-8"
        )
        _purge()
        try:
            was_killed = _run_tests(mutation.tests)
        finally:
            target.write_text(original, encoding="utf-8")
            _purge()
            if _digest(target) != baseline:
                msg = f"restore failed for {mutation.target}"
                raise HarnessError(msg)
        if was_killed:
            killed += 1
            print(f"KILLED         {mutation.label}")
        else:
            survivors.append(mutation.label)
            print(f"SURVIVED       {mutation.label}")

    total = len(mutations)
    print(f"\n{killed}/{total} killed")
    if survivors:
        print(
            f"\nSURVIVORS ({len(survivors)}) — real test gaps, add the test that goes red:"
        )
        for label in survivors:
            print(f"  - {label}")
    if harness_errors:
        print(
            f"\nHARNESS ERRORS ({len(harness_errors)}) — these mutations never ran. "
            "The survivor list above is incomplete and cannot be concluded from:"
        )
        for label in harness_errors:
            print(f"  - {label}")
        return 2
    return 1 if survivors else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the selected mutation sets."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "modules",
        nargs="*",
        default=None,
        help=f"one or more of: {', '.join(sorted(CATALOGUE))}. Default: all but selftest.",
    )
    args = parser.parse_args(argv)
    names = args.modules or [name for name in CATALOGUE if name != "selftest"]
    unknown = sorted(set(names) - set(CATALOGUE))
    if unknown:
        parser.error(f"unknown module(s): {', '.join(unknown)}")
    selected: list[Mutation] = []
    for name in names:
        selected.extend(CATALOGUE[name])
    print(f"mutating: {', '.join(names)}  ({len(selected)} mutations)\n")
    return run(selected)


def audit_test_ids(modules: Sequence[str]) -> int:
    """Report which individual tests are seen red by `modules`' mutations.

    A floor, never a ceiling: it names tests that *did* die under at least one
    catalogued mutation. Silence about a test means only that no mutation in
    this catalogue reached it.
    """
    KILLED_TEST_IDS.clear()
    selected = [m for name in modules for m in CATALOGUE[name]]
    files = sorted({t for m in selected for t in m.tests})
    print(f"auditing {len(selected)} mutations over {len(files)} file(s)\n")
    for mutation in selected:
        target = REPO / mutation.target
        original = target.read_text()
        baseline = _digest(target)
        try:
            _apply(mutation, original, baseline)
            _run_tests(mutation.tests, name_failures=True)
        finally:
            _purge()
            target.write_text(original)
            _purge()
    total = sum(
        len(re.findall(r"^\s*(?:async )?def (test_\w+)", (REPO / f).read_text(), re.M))
        for f in files
    )
    print(
        f"individually seen red: {len(KILLED_TEST_IDS)} of {total} defs in those files"
    )
    for tid in sorted(KILLED_TEST_IDS):
        print(f"  {tid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
