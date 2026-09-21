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
            "ClipObservation: drop the parallel-length validator",
            "        if self.emitted_silence_start_ms and len(self.emitted_silence_start_ms) != len(",
            "        if False and len(self.emitted_silence_start_ms) != len(",
        ),
    )


CATALOGUE: Final[dict[str, tuple[Mutation, ...]]] = {
    "profiler": _profiler_mutations(),
    "arbiter": _arbiter_mutations(),
    "policy": _policy_mutations(),
    "proxy": _proxy_mutations(),
    "replay": _replay_mutations(),
    "metrics": _metrics_mutations(),
    "transcribe": _transcribe_mutations(),
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


def _run_tests(tests: Sequence[str]) -> bool:
    """Run `tests` in a subprocess. Returns True when they failed (mutation killed)."""
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
            "-x",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode != 0


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


if __name__ == "__main__":
    raise SystemExit(main())
