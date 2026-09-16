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
Status: **pending the live run** (harness landed 2026-09-15)
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
