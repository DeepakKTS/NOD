# Decision log

Long-term memory. `CLAUDE.md` is the standing contract; this file records why the code
looks the way it does, so a future session does not re-litigate a settled question.

Format, five lines maximum per entry:

```
## ADR-NNN — <title>
Date · Status: accepted | superseded by ADR-NNN
Context: what forced a choice
Decision: what was chosen
Consequence: what this now costs or rules out
```

---

## ADR-000 — Stack
2026-09-15 · Status: accepted
Context: fifteen days, one owner, a controller plus a harness plus a console.
Decision: Python 3.12 / FastAPI for core and bench, Next.js 15 for the console, SQLite and
JSONL for storage, no Redis or Postgres in v1.
Consequence: one language across the two parts that share logic; horizontal scaling needs
sticky routing by session id, which is accepted until measurements say otherwise.

## ADR-001 — Model and capability baseline
Status: **pending one clean full matrix** (harness landed 2026-09-15; two of four
knobs measured at N=3 on 2026-09-16)

Settled at N=3 on `universal-streaming-english`, independent of the regime defect
fixed on 2026-09-16:
- `min_turn_silence` is **LIVE**: connect 315 / 2163 ms, mid-stream 303 / 2160 ms,
  spreads 10-29 ms. Mid-stream lands where connect-time landed.
- `end_of_turn_confidence_threshold` is **INERT**: arms at the documented
  endpoints 0.0 and 1.0 gave 365 / 386 ms where the docs predict 2800 ms apart.

Outstanding, because the first full matrix built every clip from the wrong lead
segment and ran three arms in the complete-utterance regime: `max_turn_silence`,
`vad_threshold`, `ForceEndpoint`, and the `universal-3-5-pro` arm. Re-run the full
matrix and write the measured answer here.
Context: the controller needs `end_of_turn_confidence` and mid-stream updates; Universal-3
Pro Streaming uses punctuation-based turn detection rather than a confidence score.
Decision: record here after `python -m nod_bench.probe` (not `nod_core.probe`, see ADR-007)
— the per-knob verdict, whether confidence varies across partials, whether ForceEndpoint moved
a boundary.
Consequence: determines whether the confidence axis is live or the controller runs on the
silence axis alone; `KnobVerdict.STATIC_ONLY` or `INERT` on a silence knob falsifies
CONTROL_SPEC §0 fact 2 and the control law is rewritten rather than tuned.

## ADR-002 — Streaming quantiles
2026-09-15 · Status: accepted
Context: pause quantiles update per word and the decision path is budgeted at 5 ms p99.
Decision: P² estimator as primary, exact ring-buffer behind `NOD_EXACT_QUANTILES` for
validation; the bench asserts they agree within 5 %.
Consequence: `O(1)` per sample and constant memory, at the cost of an approximation that
must be re-validated whenever the corpora change.

## ADR-003 — Realtime STT path, not the Voice Agent API
2026-09-15 · Status: accepted
Context: the Voice Agent API ships its own closed adaptive pacing and exposes no knobs.
Decision: Nod runs on the Realtime STT path; the Voice Agent API may appear only as a
clearly labelled comparison arm in the benchmark.
Consequence: Nod owns orchestration, LLM and TTS, which is more work but is the only way
the control loop can exist at all.

## ADR-004 — Packaging and lockfile
2026-09-15 · Status: accepted
Context: ARCHITECTURE §10 requires dependencies pinned with hashes, which version pins in
`pyproject.toml` alone cannot express, and CLAUDE §3 names no packaging tool.
Decision: `uv` with a committed `uv.lock`; `make install` and CI both run `uv sync
--frozen`, and `uv python install 3.12` supplies the interpreter the repo pins.
Consequence: lockfile drift fails the build instead of silently resolving something new;
contributors need `uv` on PATH, and `requires-python = ">=3.12,<3.13"` rejects a 3.13 box.

## ADR-005 — Paired significance test
2026-09-16 · Status: accepted
Context: BENCH_SPEC §9 requires a paired Wilcoxon signed-rank test; CLAUDE §3's numerics
row names only numpy, and scipy was arriving undeclared as a librosa transitive.
Decision: use `scipy.stats.wilcoxon` in `nod_bench.metrics`, with `scipy` declared
explicitly in the `bench` extra rather than inherited.
Consequence: scipy is a stated bench-only dependency excluded from the runtime image per
DEPLOYMENT §3; a librosa change can no longer silently remove code we import.

## ADR-006 — Untyped third-party imports (pending)
2026-09-16 · Status: **pending P2 and P7**
Context: mypy runs strict repo-wide, but librosa, soundfile, scipy and assemblyai ship no
inline types; declaring `ignore_missing_imports` overrides before anything imports them
trips `warn_unused_configs` and leaves dead config in pyproject.toml.
Decision: add each `[[tool.mypy.overrides]]` block in the phase that first imports the
package — librosa, soundfile and scipy at P2, assemblyai at P1 and P7 — never pre-emptively.
Consequence: the first import of each package fails `make types` until its override lands,
which is the intended prompt; revisit if any of the four ships a `py.typed` marker.

## ADR-007 — Capability probe lives in `nod_bench`, not `nod_core`
2026-09-15 · Status: accepted
Context: PROMPTS.md P1 specifies `src/nod_core/probe.py`, but the live probe needs the paced
feeder and the concrete AssemblyAI adapter, both of which `tests/unit/test_boundaries.py`
forbids `nod_core` from importing at any nesting depth.
Decision: the live orchestrator is `nod_bench/probe.py` (`python -m nod_bench.probe`); the
pure verdict model and classifier stay in `nod_core/capabilities.py`, driven through
`SttSession` with the feeder and clock injected.
Consequence: the boundary test and the `--cov=nod_core` gate stay untouched and live-only
network code stays out of the runtime image; P1's literal module path no longer applies.

## ADR-008 — "Cumulative drift" means max lag, not the sum
2026-09-15 · Status: accepted
Context: BENCH_SPEC §4 and EC-37 say "abort above 25 ms cumulative drift", but under the
absolute schedule those same lines mandate (`t0 + n * FRAME_S`) the sum of per-frame drifts
is near zero by construction, so the literal guard would never fire.
Decision: enforce `max(actual_n - deadline_n)`, the furthest the feeder ever fell behind
schedule; record the sum alongside it so the literal reading stays available in the trace.
Consequence: the guard bites on a real stall, which is what invalidates latency numbers; a
future reader of EC-37 must read it as max lag, and `FeedReport` carries both.

## ADR-009 — `Capabilities` carries verdicts, not booleans
2026-09-15 · Status: accepted
Context: the streaming API answers a successful `UpdateConfiguration` with silence, so
acceptance and silent-drop are indistinguishable; a `frozenset[str]` of "updatable fields"
cannot express *accepted but unproven*, nor *works at connect time but not mid-stream*.
Decision: `Capabilities.knobs` holds a `KnobVerdict` per knob
(`LIVE`/`STATIC_ONLY`/`INERT`/`REJECTED`/`UNPROVEN`), with `updatable_fields` and
`has_end_of_turn_confidence` derived so the arbiter's capability gate is unchanged.
Consequence: every verdict short of `LIVE` fails closed and is never sent; `STATIC_ONLY`,
the outcome that would falsify the closed-loop thesis, is now expressible and is covered by
a test before it is ever needed.

## ADR-010 — Probe tolerances scale a millisecond shift, not the arm delta
2026-09-15 · Status: accepted
Context: the classifier scaled its separation and agreement tolerances by the difference
between the two arm values, which is milliseconds for the silence knobs but dimensionless
for the two thresholds — giving `end_of_turn_confidence_threshold` a 0.3 ms agreement
tolerance and reporting a working knob as `STATIC_ONLY`.
Decision: each `KnobStimulus` declares `expected_shift_ms`, the boundary movement the
stimulus is designed to produce, stated per knob rather than derived from the arm values;
for a categorical stimulus that is the gap length.
Consequence: tolerances are always in the unit of the observable; `AGREEMENT_FRACTION`
stays below 0.5 so a mid-stream arm can never agree with the opposite connect-time arm.

## ADR-011 — The control law moves to the silence axis
2026-09-16 · Status: **provisional**, confirmed on the full matrix at N=3
Context: the P1 probe showed `end_of_turn_confidence_threshold` inert on
`universal-streaming-english` — arms at the documented endpoints 0.0 and 1.0 gave
boundaries 13 ms apart against an expected 2800 ms, boundaries fired at confidence 0.308
against a 0.95 threshold in 15 of 17 sessions, and the field never exceeded 0.940 in 929
samples. The two silence knobs each bind in one regime only: after a complete utterance
the semantic gate fires and `min_turn_silence` decides; after a fragment it keeps waiting
and `max_turn_silence` is the only thing that ends the turn.
Decision: `max_turn_silence` is the primary control surface and carries `disfluency` and
`recent_cuts`, because the incomplete-utterance regime is the mid-sentence pause Nod
exists for; `min_turn_silence` carries post-complete responsiveness and receives the
context axis; `end_of_turn_confidence_threshold` is never sent, though the capability gate
stays so a model that honours it can be enabled by a future ADR; `jitter` is retained as a
logged feature at weight 0 until the bench shows it helps.
Consequence: the confidence axis contributes nothing to any output, so §4's `conf` line,
`WindowHint.conf_delta` and `base_conf` are removed rather than frozen; `jitter` keeps its
`O(1)` update cost for no current benefit, which is accepted so the bench can evaluate it;
and the law is now single-axis per regime, so a stimulus that tests a knob in the wrong
regime reads as inert.

## ADR-012 — Feeder drift aborts on sustained lag, not a single frame
2026-09-16 · Status: accepted
Context: ADR-008 read EC-37's "cumulative drift" as max lag behind schedule. A 5-minute
offline soak showed no growth with elapsed time (slope +0.0012 ms per 1000 frames, first
decile 1.130 ms against last decile 1.119 ms) but a heavy tail: p99 1.488 ms and a single
16.055 ms OS scheduling stall in 6000 frames. Max lag scales with sample count, so a
30-minute P3 clip would eventually trip the 25 ms abort on one stall unrelated to the
measurement.
Decision: abort when `DRIFT_SUSTAIN_FRAMES` (4) consecutive frames each exceed
`MAX_LAG_MS`. Four frames is 200 ms of stream time — long enough that a single scheduler
preemption cannot reach it, short enough to catch a feeder that has genuinely fallen
behind within a fifth of a second.
Consequence: a one-frame stall displaces one 50 ms frame out of tens of thousands and no
longer voids a run; sustained lag displaces the whole timeline and still does. The guard is
weaker per-frame, so the mutation test must still go red under the accumulating-schedule
bug, which it does because that bug produces lag on every subsequent frame rather than one.

Addendum 2026-09-16 — which test actually carries that guarantee. `test_sustained_lag_voids_the_run`
does not. Mutating `PacedFeeder.feed` to `await asyncio.sleep(self._frame_s)` instead of
sleeping to `self._t0 + n * self._frame_s` leaves it **green**: it asserts the guard fires,
and the guard fires under the bug and under a legitimate sustained stall alike, so it
cannot tell them apart. It is a runtime safety net for EC-37, not a correctness test for
the schedule. That same mutation turns exactly three tests red —
`test_schedule_absorbs_a_stall_instead_of_carrying_it_forward` (the elapsed-time assertion,
the one that verifies the schedule), `test_one_isolated_stall_does_not_void_the_run` and
`test_the_counter_resets_between_separated_stalls`. Recorded because the sentence above is
true but reads as if the drift-guard test were the one doing the work, and a future session
trusting that would weaken the pacing loop with the guard test still passing.

## ADR-013 — Redaction is specified over partials, not finished utterances
2026-09-16 · Status: accepted
Context: INV-6 masked digit runs of 4+ and phone shapes of 9+ characters, both tuned to a
completed number. A streaming endpointer emits one group at a time, so the P1 traces
carried `617` in 30 records and `617 555` in 44 while the finished number masked correctly.
Decision: `DIGIT_RUN_MIN` 4 → 3 and `_PHONE` admits two groups (`{7,}` → `{5,}`); the
regression test asserts over the real partial sequence, not a synthetic whole utterance.
Consequence: a three-digit quantity is now masked too (`turn 100 of 250` reads
`turn [NUM] of [NUM]`), which is the right direction to err for a persisted trace;
`NOD_TRACE_RAW=1` remains the only escape, and key-scoping to `transcript`/`utterance`/
`text` keeps `clip_sha256` and `session_id` intact under the looser digit rule.
