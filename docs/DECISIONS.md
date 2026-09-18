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
2026-09-17 · Status: **accepted** — measured on the clean 69-session matrix, N=3
Context: the controller needs to know which turn-detection fields this model actually
honours mid-stream, and whether `end_of_turn_confidence` is usable as a feature. Verdicts
below are per `KnobVerdict` under ADR-014's two paths. Figures come from
`data/traces-p0-final` (the 69-session matrix) except `ForceEndpoint`, which comes from
`data/traces-force` — the matrix's force cells were void, see below.
Decision: record the measured answer. Four states are distinguished and must not be
collapsed into each other.

**`universal-streaming-english`, N=3, connect-time and mid-stream** (`data/traces-p0-final`)

| field | verdict | evidence |
|---|---|---|
| `min_turn_silence` | **LIVE**, continuous | 306 → 2175 ms at connect, 304 → 2172 ms mid-stream; worst arm spread 29 ms; mid lands within 2–3 ms of its connect twin |
| `vad_threshold` | **LIVE**, continuous (ADR-014) | 404 ms separation at connect, 415 ms mid; worst arm spread 42 ms; inverted direction as predicted; mid within 7 and 18 ms of its connect twin. Clears the noise test ~5× and the 100 ms floor ~4×. The superseded `0.6 × expected_shift_ms` gate demanded 480 ms and called this INERT |
| `max_turn_silence` | **LIVE**, categorical | the 600 ms arm ends the turn at 817 ms, 3/3, at connect and mid; the 3000 ms arm produces **no boundary inside the gap at all**, 3/3, in both. No floor applies to this path |
| `ForceEndpoint` | **LIVE**, categorical (`data/traces-force`) | `force_test` ends at 424 ms, 3/3, spread 1 ms; `force_control` on the identical clip produces no boundary, 3/3. Send-to-boundary latency 22 ms, spread 1 ms |
| `end_of_turn_confidence_threshold` | **INERT** | arms at the documented endpoints 0.0 and 1.0 landed 353 and 347 ms — 6 ms apart, the high arm *earlier* — against a documented 2800 ms. The control arm ended turns at confidence 0.251 with the threshold pinned at 0.40, so the field does not gate endpointing even when it is set |
| `end_of_turn_confidence` | **present and varying** | 69/69 sessions carry samples, 13–52 per session, varying across partials. Trajectory is near zero across an utterance and spikes only on the boundary frame, so it reports turn-completion probability, not speaker hesitation (§2.4, ADR-011) |
| word timings | **present** | `words[].start` / `.end` on every session |

INERT is used exactly once above, and only where boundaries occurred and did not move with
the knob. A separation too small to resolve is UNPROVEN, never INERT (ADR-014).

**`universal-3-5-pro`, N=2, connect-time only** (`data/traces-p0-final`)

A different model is a different measurement and is **not merged** into the verdicts above.

| field | result | why |
|---|---|---|
| `end_of_turn_confidence_threshold` | **no gating observed — UNPROVEN** | arms at 0.0 and 1.0 gave 3532 and 3422 ms, 110 ms apart, which is smaller than the high arm's own 143 ms spread. But both arms landed at `max_turn_silence` (pinned 3000), so that gate bound first and the threshold never got an opportunity to act |
| `min_turn_silence` | **no gating observed — UNPROVEN** | 3431 vs 3364 ms, 67 ms apart against spreads of 420 and 100 ms. Same cause: every boundary landed at the pinned `max_turn_silence` of 3000 ms, so the minimum was never the binding constraint |
| `max_turn_silence` | **connect-time effect observed** | the 600 ms arm ends the turn at 902 ms; the 3000 ms arm produces no boundary. Mid-stream was not run on this model, so this is not a claim about `UpdateConfiguration` there |

**None of the pro rows is INERT**, and as of Gate A the classifier agrees. It used to
return `inert` for the first two, which asserts the model ignores a field on an experiment
that never ran. `verdict_for` now takes `other_gate_ms` and returns UNPROVEN when every
boundary in both arms lands at or beyond another pinned silence gate — here
`max_turn_silence` at 3000 ms, which ended every pro turn. Re-classifying
`data/traces-p0-final` reproduces both rows above exactly.

One artifact remains and is **not** the recorded answer: `max_turn_silence` on pro
classifies `static_only`, because pro is planned connect-time only and `verdict_for`
reads the absence of mid-stream cells as a failure to demonstrate a mid-stream loop. That
is by design of the matrix, not a finding about the model. Distinguishing "mid-stream was
not run" from "mid-stream did not work" needs the plan, not the observations, and is left
alone deliberately.

Also recorded: `SpeechStarted`, an undocumented frame type, 22 occurrences, `pro` only,
never on `universal-streaming-english` (see `KNOWN_FRAME_TYPES`).

Consequence: the confidence axis is dead on the chosen model and the controller runs on the
silence axis alone, which is ADR-011, now confirmed rather than provisional. Both silence
knobs are sendable mid-stream and the capability gate will pass them. `vad_threshold` is
live but is not a control surface in CONTROL_SPEC §4 and nothing sends it. `ForceEndpoint`
is available for §4's confident-early-endpoint path at ~22 ms.

Caveat carried from the seed: every figure above was measured against **synthesized speech**
(macOS `say`, voice Samantha, 160 wpm). That is adequate for capability probing, which asks
only whether a boundary moves. It is not adequate for Track C, disfluency features, cut
detection, or any published number — those come from `make bench` over real audio (INV-9).


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
2026-09-16 · Status: **accepted** — confirmed on the clean 69-session matrix at N=3, 2026-09-16
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

Confirmed 2026-09-16 on the 69-session matrix, N=3, `universal-streaming-english`, every
cell in its own regime and no rejected field:
- `end_of_turn_confidence_threshold` **inert**: arms at 0.0 and 1.0 gave medians 353 and
  347 ms — 6 ms apart against a documented 2800 ms — with spreads of 15-25 ms. The control
  arm ended turns at confidence 0.251 with the threshold pinned at 0.40, so the field does
  not gate endpointing even when it is set.
- `max_turn_silence` **live** and categorical in the fragment regime: 817 ms on the 600 ms
  arm against no boundary at all on the 3000 ms arm, 3/3 in both connect-time and
  mid-stream cells.
- `min_turn_silence` **live** in the complete-utterance regime: 306 to 2175 ms connect,
  304 to 2172 ms mid-stream, the two landing within 3 ms of each other.
- The confidence trajectory is the shape §2.4 now assumes: near zero across an utterance,
  spiking only on the frame that is the boundary. `jitter` stays at weight 0.
`universal-3-5-pro` showed no gating either, but on weaker evidence: every pro boundary
landed at `max_turn_silence`, so the threshold never got an opportunity there and reads
unproven rather than inert. A different model is a different measurement and is not merged
into the verdicts above.

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

## ADR-014 — A separation is real if it clears the noise and 100 ms
2026-09-17 · Status: accepted
Context: `_separated` required `gap >= MIN_SEPARATION_FRACTION * expected_shift_ms` on top
of the IQR test. That fraction is well calibrated where `expected_shift_ms` is a
prediction — `min_turn_silence` predicted 1900 ms and delivered 1840 — and wrong where it
is an upper bound. `vad_threshold` declares 800 ms because that is the accumulation window
it acts inside, not a distance the boundary is expected to travel, so the derived 480 ms
floor demanded more movement than the mechanism can produce and a knob that plainly moved
the boundary classified INERT. Worse, `verdict_for` routes a failed `connect_moved`
straight to INERT, which is a positive claim that the model ignores the field — the same
inference the file already refuses ten lines earlier for the no-boundary case.
Decision: there are **two admissible paths to a real separation**, not one rule.
1. **Categorical** — one arm produces no boundary inside the gap and the other fires on
   every repeat. The arms are separated by construction, no statistics apply, and **no
   floor is applied**. This is `max_turn_silence` and `ForceEndpoint`.
2. **Continuous** — both arms produce boundaries. The gap between the medians must clear
   **both** the existing noise test (`IQR_MULTIPLE` × the wider arm's IQR) **and** a
   **100 ms** absolute floor. Conjunctive: clearing one is not enough.
`MIN_SEPARATION_FRACTION * expected_shift_ms` is deleted. That derivation, and nothing
else, is what this ADR removes; the IQR test is untouched.
The floor is the literal **100**. Measured endpoint overhead runs 147–274 ms across the
plain silence-gate cells, so a shift smaller than 100 ms is smaller than the unmodelled
overhead of the very system the knob is meant to steer, and is not actionable even if it
is real. It is deliberately not derived from `expected_shift_ms` or from any documented
figure — those are what produced the bad gate. **Revisit it once `make bench` measures
`ENDPOINT_OVERHEAD_MS`; do not let it be inherited unexamined.**
A separation that clears the noise test and misses the floor is `UNPROVEN`, never `INERT`.
`INERT` stays reserved for boundaries that occurred and did not move with the knob.
`_force_verdict`'s categorical `fired and quiet` test is correct as written and is **not**
changed by this ADR. It reported UNPROVEN only because the force cells ran the
complete-utterance regime, where the control arm ends its own turn; with the fragment lead
restored it returns LIVE unaided.
Consequence: `vad_threshold` resolves **LIVE** on the continuous path — 404 ms separation
at connect and 415 ms mid-stream against a worst arm spread of 42 ms, clearing `2 × IQR`
roughly fivefold and the 100 ms floor fourfold, in the predicted direction, with mid-stream
landing within 7 and 18 ms of its connect-time twin (`data/traces-p0-final`).

## ADR-015 — Word tokens are redacted by a different rule than sentences
2026-09-16 · Status: accepted
Context: ADR-013 closed the `transcript` leak but `words[].text` kept carrying `6`, `61`,
`5`, `55`, `0`, `01`, `4` — the prefixes of a real phone number — in the trace chosen as
the ROADMAP Phase 1 bullet 5 replay fixture, bound for a public repository. A word token is
one to four characters, so every threshold in `redact` is too coarse for it: a streaming
endpointer grows a word character by character and only the completed `617`, `555`, `0142`
ever reach a rule that recognises them.
Decision: split `text` out of `_REDACTED_TEXT_KEYS` into `_REDACTED_WORD_KEYS`, routed
through `redact_word`, which runs the existing shape rules **first** and only then masks
any token that still carries a digit as `[NUM]`. The ordering is the decision: shape-first
preserves mask type, so a fully transcribed `5551234567` still reads `[PHONE]` while `61`,
`01` and `4` become `[NUM]`. Digits-first would flatten both to `[NUM]` and lose the
distinction. `transcript` and `utterance` rules are unchanged.
Consequence: nothing the controller reads is lost — CONTROL_SPEC §1 uses token text for
disfluency features only, and §2.3's three features are adjacent repeats, filler-set
membership and duration outliers, none of which reads a digit's value. `start`, `end`,
`confidence` and `word_is_final` stay untouched, which is what `redact_payload` exists to
protect. The same string now behaves differently in the two places it appears: `"3"` in a
sentence is a quantity and survives, `"3"` as a whole token being read aloud is a digit of
something and masks.
Also widened `_PHONE`'s separator class to include en dash (`–`) and em dash
(`—`), because `universal-3-5-pro` formats digit groups with them and a hyphen-only
class does not see those numbers at all.
Not fixed, and named so it is not mistaken for covered: **hyphen-separated single digits**
in a *sentence* — the `6-1-1` shape `universal-3-5-pro` produces. It defeats `_DIGIT_RUN`,
which needs three consecutive digits and sees none, and `_PHONE`, which needs seven
characters and counts five. Widening the dash class does not reach it, and neither does the
word rule, which only governs `words[].text`. A bare trailing digit (`6—`, and the
`30` left behind when `_WORDY_DATE` matches a date whose year is still being transcribed)
is the same gap. Closing it means masking single digits inside sentences, which contradicts
`test_short_digit_runs_survive`'s standing decision that "I have 3 cats" is not PII, so it
needs its own ADR rather than a threshold nudge.

## ADR-016 — BENCH_SPEC §4 governs; ROADMAP's Phase 1 exit is amended
2026-09-17 · Status: accepted
Context: two documents specified incompatible things and both read as deliberate. BENCH_SPEC
§4: "CI runs the fake; the published table is generated from live runs and the traces are
committed" — the fake exists for determinism, never for published numbers. ROADMAP Phase 1
exit: "`make bench` produces a table and the Pareto chart for the three static arms,
**offline, on a clean clone**." Offline means the fake, so Phase 1 as written required the
fake to produce the headline table that BENCH_SPEC reserves for live runs. Neither was a
typo; each is coherent alone, and the conflict only surfaced when Gate C asked what
`FakeAssemblyAI` actually has to do.
Decision: **BENCH_SPEC §4 governs.** ROADMAP's Phase 1 exit is amended to read that
`make bench` produces the tradeoff curve and Pareto chart offline from the simulator,
**labelled simulated**, and that the published table is regenerated from live runs at
Phase 4 per INV-9. BENCH_SPEC is unchanged.
Consequence: Phase 1 can still exit offline on a clean clone, which was the point of that
criterion — the harness must be runnable by someone without credits. What it may not do is
let a simulated figure become a published one. The labelling is structural rather than
editorial (ADR-017), because a chart leaving the repo has to carry its own provenance.
BENCH_SPEC governs because it is the measurement contract and ROADMAP is a schedule: when a
schedule and a contract disagree about what a number means, the contract wins.

## ADR-017 — `FakeAssemblyAI` is a simulator, not a replayer
2026-09-17 · Status: accepted
Context: a replay of a recorded event stream reproduces the boundaries recorded under the
config in force at capture time. The three-arm sweep varies exactly that config, so replay
holds fixed the thing the sweep varies and yields one arm drawn three times. The committed
trace compounds it: its arm is a probe stimulus (`min_turn_silence` 100 mid-stream, max
3000) rather than aggressive/balanced/conservative, its audio is a spliced probe clip
rather than a corpus clip, and it ships no `.truth.json`, so PCR and FRAG — both defined
over ground-truth utterances — have nothing to score against.
Decision: `FakeAssemblyAI` is the socket-level promotion of `nod_bench.fake_session`, which
is already a parameterised endpointer rather than a canned script. Three changes make it
sweep-grade, each forced by ADR-001:
- **Regime comes from the truth sidecar**, not from `TURN_CONFIDENCE` against a threshold.
  ADR-001 measured `end_of_turn_confidence_threshold` INERT, so a fake in which it works is
  more capable than the service it stands for and would let the bench reward a control law
  exploiting a knob that does not exist. The sidecar already records utterance boundaries
  and the perturbation, so completeness is read from data rather than invented.
- **Endpoint overhead is a named parameter defaulting to 0**, with the optimistic bias
  stated wherever it is reported: a simulator that fires exactly at the configured gate is
  early by the overhead on every turn. The value comes from `make bench` (INV-9) and is
  never hand-written.
- **`end_of_turn_confidence_threshold` is accepted and ignored**, matching what ADR-001
  measured on `universal-streaming-english`.
The simulated/live distinction is **structural, not editorial**: every artifact the
simulator produces carries `simulated` in its filename and as a field in the run manifest.
Prose labelling is not enough for a chart that leaves the repo.
The committed trace keeps four jobs, and the first is load-bearing: **calibration**.

The simulator is deterministic, so its own run-to-run spread is zero and is the wrong
reference class for a tolerance. Calibration is therefore **multi-point**, against five
measured cells rather than one (`data/traces-p0-final`):

| cell | configured | measured boundary |
|---|---|---|
| `min_turn_silence` connect, low | 100 ms | 306 ms |
| `min_turn_silence` connect, high | 2000 ms | 2175 ms |
| `min_turn_silence` mid, low | 100 ms | 304 ms |
| `min_turn_silence` mid, high | 2000 ms | 2172 ms |
| `max_turn_silence` fragment regime | 600 ms | 817 ms |

Two bounds, and the second is the one that matters:
- **Per point: ±40 ms.** Justified from the service's own repeat spread on these cells —
  24 ms worst on the calibrated `min` cell, 29 ms worst across all `min` cells, 39 ms on
  the `max` cell — plus headroom. It is also well inside ADR-014's 100 ms actionability
  floor, so a passing simulator agrees with the service to less than the smallest
  difference the project would act on, and far inside the 160/400/1280/3600 ms spacing of
  the arms, so a simulator that could reorder two arms cannot pass.
- **Mean signed error across the five points: within ±15 ms of zero.** A single point, or
  five points bounded only in absolute value, cannot distinguish *correct* from
  *consistently early*: a simulator firing 35 ms early everywhere passes every per-point
  check and biases every TTL number in the same direction. The signed bound catches the
  bias that the absolute bound is blind to. ±15 ms is roughly a third of the per-point
  tolerance, which is the most slack a systematic offset can take before it starts to
  matter against the 100 ms floor.

The `max_turn_silence` point is included deliberately: the first four share a regime, and
a simulator that modelled only the complete-utterance gate would pass all of them while
being wrong about the regime Nod exists for.

Without this the simulator is an assertion; with it, it is falsifiable. The other three are console replay mode
(PRD F-10, EC-45), transport fixtures under INV-7, and realistic input-side material.
Consequence: the fake produces the *shape* — a tradeoff curve, a Pareto chart, CI
determinism — and never a published number. A simulated chart presented as measured is the
marketing number CLAUDE §7 and §8 forbid, and the filename is what stops that happening by
accident.
