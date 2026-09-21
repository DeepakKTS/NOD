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
2026-09-15 · Status: accepted — **agreement claim scoped 2026-09-18 at Phase 2 Gate 2**
Context: pause quantiles update per word and the decision path is budgeted at 5 ms p99.
Decision: P² estimator as primary, exact ring-buffer behind `NOD_EXACT_QUANTILES` for
validation; the bench asserts they agree within 5 %.
Consequence: `O(1)` per sample and constant memory, at the cost of an approximation that
must be re-validated whenever the corpora change.

**Amendment, 2026-09-18 — what the 5 % is a claim about.** As written, "they agree within
5 %" reads as a property of the estimator. It is not one, and Gate 2 measured that rather
than reasoning about it. **P² has no a priori error bound.** Its value error depends on the
sample density near the target quantile: where the density is low, a one-position marker
move crosses a large value gap, and the error in milliseconds is large with nothing wrong
in the algorithm. Any relative tolerance is therefore an empirical statement about a
population.

Restated as a `hypothesis` property over arbitrary gap streams, the claim is **false**: at
`n >= 256`, **58 of 300 drawn streams broke `max(1 ms, 5 %)`, worst case 13.73 %** — a p50
estimate of 1969 ms against an exact 2282 ms. Rank error, the density-independent metric
(what fraction of samples actually fall below the estimate), is no better: **45.6 percentage
points at `n = 9`**, and still 0.45 on a 95/5 spiky stream at `n = 256`. The error also
depends strongly on `n`, which the original claim does not mention at all — on the
digit-reading shape at `q = 0.90`, median relative error runs 74.5 % at `n = 8`, 36.9 % at
16, 17.9 % at 24, 11.3 % at 32 and 1.4 % at 256 (full table in ADR-022).

So the claim is scoped: **the 5 % holds for the median over realistic pause shapes at
sufficient `n`, on the corpora — not as a general property and not as a worst case.** That
is what `test_p2_holds_adr_002s_tolerance_on_realistic_pause_streams` asserts, on the median
with the tail reported rather than bounded, and the structural properties that *are*
distribution-free are asserted separately: the estimate never leaves the observed range,
the two agree exactly below five samples, a constant stream is reported exactly, and an
empty estimator raises rather than returning `0.0`.

One consequence worth taking seriously rather than filing: a bound that only holds at large
`n` is a bound that does not hold where the controller starts. ADR-022 is that consequence.

**Second amendment — the ring-capacity caveat this ADR's own instruction walks into.**
"The bench asserts they agree" is unqualified, and a differential test longer than
`GAP_RING_CAPACITY` **tests nothing**. `ExactQuantile` retains the last 256 samples while
P² summarises every sample it has seen, so past the ring the two are estimating **different
populations** and a disagreement is correct behaviour rather than drift. A comparison run
over a long call would therefore report growing "error" that is entirely the ring forgetting,
and a real regression could hide inside it. Every differential assertion must either stay
inside the ring or give the exact estimator capacity for the whole stream;
`test_the_two_estimators_stop_being_comparable_past_the_ring` pins the distinction, and
`seen` exists on both estimators to separate ingested from retained.

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

**Six instances now, and the pattern is worth naming.** The first three were each two
internally coherent documents specifying incompatible things, and each surfaced only when
something had to consume both at once — never from re-reading either one.
1. BENCH_SPEC §4 ("CI runs the fake; the published table is generated from live runs")
   against ROADMAP's Phase 1 exit ("offline, on a clean clone"). Found when Gate C asked
   what `FakeAssemblyAI` concretely had to produce. Resolved above.
2. CONTROL_SPEC §9's "both axes must move" property against ADR-011's single-axis law.
   Found when the property test was written; §9 item 9 replaced it.
3. BENCH_SPEC §2's `correct` illustration, which splices words ("change my, no, cancel
   my"), against `perturb.apply`'s contract that no perturbation changes speech content,
   only its timing. Found when Gate A implemented `correct` and had to satisfy both. The
   contract governs, as in (1): `correct` is a restart built from the clip's own audio,
   and the divergence from the illustration is recorded at the implementation.
4. CONTROL_SPEC §3's policy YAML against `policy.PolicyFile`. Found at Phase 2 Gate 1,
   reading the spec in order to write the loader. **This one is a variant, and the
   difference is the point.** The first three were two documents each coherent alone; §3's
   example was not coherent alone — its `answers:` key had been lost in an edit, leaving
   eight classes indented under nothing, so the block is not valid YAML and `PolicyFile`
   would have rejected it under `extra="forbid"` even if it parsed. Nobody noticed for the
   same reason as the other three: a YAML block in a spec is never executed, so *reading*
   it cannot fail. Restored per reading (a), the key was lost; the multipliers are
   unchanged.
5. CONTROL_SPEC §5's hysteresis guard against §5's asymmetric decay guard — 12 % cannot
   clear 15 %, so one guard cancelled the other and narrowing was unreachable. Found at
   Phase 2 Gate 1 writing §9 property 6. Resolved by ADR-020. Note this pair is *within one
   section of one document*, which the first four were not: coherence is not a property a
   document has, it is a property of every pair of lines in it.
6. CONTROL_SPEC §4's invariant repair against §4's latency ceiling, applied in that order,
   so a low `ceiling_ms` returns `max_ms < min_ms + 200`. Found in the same sitting, from
   the same cause. Resolved by ADR-021.
The lesson is procedural rather than editorial — a spec review would not have caught any of
them. For the first three, because each document is right on its own; for the fourth,
because prose review reads a code block for intent and not for syntax; for the fifth and
sixth, because the arithmetic of two constants is invisible until something has to satisfy
both at once. They are found by building the consumer. A spec fragment that nothing parses
is untested code that happens to live in a document.

**Used forwards for the first time, at pre-Gate-2 on 18 Sep — and this is not a seventh
instance.** The question asked was whether CONTROL_SPEC §5 and §9 failing to cite ADR-020
and ADR-021 is itself an instance. It is not, and the distinction is worth keeping sharp:
every one of the six above is a **realised** defect, already in the repository, found
because something had to consume two things at once. The missing cross-references are a
defect that had not happened yet — no wrong code had been built from them, because Gate 3
had not run. Counting a risk alongside six materialised defects would inflate the list and
blur what the pattern is for.

What it is instead is the first time the pattern was applied as a **forward check** rather
than a post-mortem: §5's guards table and §9 now carry pointers to both ADRs, phrased to
name the question each ADR answers rather than to restate the answer. That phrasing is
deliberate. §5 restating ADR-020's ordering would create two copies that can drift, which
is instance one through six all over again; a pointer keeps one source of truth and still
stops a Gate 3 session meeting the silence that produced the conflict. Instance 2 is the
precedent that made this worth doing — a CONTROL_SPEC property contradicting an ADR is a
shape this repository has already paid for once.

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

**The overhead is not a scalar, and the bound above inherits that.** Calibration measured
the implied overhead per cell as 206, 175, 204, 172 and 217 ms — a 45 ms spread, which is
comparable to the service's own per-cell repeat spread (24–39 ms) and so is as likely to be
measurement noise as structure. Two consequences:
- `make bench` must report `ENDPOINT_OVERHEAD_MS` as **a value and a spread**, not a single
  number. A scalar would assert a constancy the measurement does not show, and the control
  law subtracts this from its ceiling (CONTROL_SPEC §4), so a spread that is real is a
  spread the ceiling has to absorb.
- The ±40 ms per-point bound must then be **revisited against that measured spread**, not
  inherited from this ADR. It was derived from repeat spread before the residual structure
  was visible.
One constant currently explains all five points to within 23.0 ms, leaving **17 ms of
headroom** under the 40 ms bound. That is why a 35 ms uniform bias cannot be isolated by
these five points: it trips the per-point bound as well as the signed one, so the
demonstration that the signed bound catches what the absolute bound misses has to be run at
16 ms. A tighter per-point bound, or more calibration cells, would widen that window.

Without this the simulator is an assertion; with it, it is falsifiable. The other three are console replay mode
(PRD F-10, EC-45), transport fixtures under INV-7, and realistic input-side material.
Consequence: the fake produces the *shape* — a tradeoff curve, a Pareto chart, CI
determinism — and never a published number. A simulated chart presented as measured is the
marketing number CLAUDE §7 and §8 forbid, and the filename is what stops that happening by
accident.

**Two scopes, one headline.** `all-gaps` is the reported scope. `certain_only` is a
*disclosed divergence*, not a parallel result, and the two must never be presented as
alternative readings a reader may pick between. The reason is statistical rather than
editorial: only `repeat` produces certainly-labelled gaps, so on the declared sweeps
`certain_only` covers roughly three of every twenty-seven generated clips. It therefore
supports a materially coarser effect size than the headline — on the order of Δ = 0.10
where the full scope reaches Δ = 0.05. **Agreement between the two is consequently not
corroboration.** The restricted scope is too underpowered to contradict the headline, so it
agreeing means very little and it disagreeing means a great deal. Report both with their
denominators, and read a divergence as a signal while reading agreement as silence.

**What the simulated Pareto chart is allowed to claim.** That the tradeoff between
premature cutoff and latency has *this shape under our model of the endpointer* — not that
these three arms differ, and not by how much. The separation of the arms on the TTL axis is
close to arithmetic: the simulator is deterministic, its spread is zero by construction,
and the arms are configured 240 to 2320 ms apart, so they cannot overlap. Demonstrating
that they do not overlap demonstrates subtraction. The PCR axis carries whatever real
information the chart has, because it depends on the corpus and on the regime labelling
rather than on the configured gate. Any claim that the arms *differ* belongs to the live
run at Phase 4 (BENCH_SPEC §4, INV-9).

## ADR-018 — The truth sidecar must describe silences it did not create
2026-09-17 · Status: accepted
Context: `corpus.build` recorded only the gaps the *generator* inserted. Source speech
contains its own inter-word pauses — up to 250 ms in the seed segments — and those were
absent from the sidecar, so `regime_at` fell through to its `complete` fallback and scored
them as post-complete. A pause in the middle of an utterance was therefore governed by
`min_turn_silence` rather than `max_turn_silence`, and on `aggressive` (minimum 160 ms) it
fired a boundary that PCR then counted as a premature cutoff of an utterance the speaker
was still inside. Every PCR and FRAG figure in the run was wrong.

| arm | PCR before | PCR after | FRAG before | FRAG after | certain-only PCR |
|---|---|---|---|---|---|
| `aggressive` | 0.825 | **0.642** | 2.408 | 1.792 | 0.583 → **0.000** |
| `balanced` | 0.550 | **0.317** | 1.633 | 1.317 | 0.167 → **0.000** |
| `conservative` | 0.150 | **0.000** | 1.150 | 1.000 | 0.000 → 0.000 |

*(Corrected 2026-09-17 at Gate E. The figures first published here were computed while a
stale `.pyc` was supplying `DEFAULT_VAD = 0.5` instead of the documented 0.4 — the same
cache hazard recorded in CLAUDE.md §5. A higher VAD threshold raises the silence floor, so
silence accumulated sooner, boundaries fired earlier, and PCR came out high in every row.
The whole table is recomputed at 0.4. The correction moved PCR by 3 to 6 points and did not
change the conclusion.)*

Decision: `corpus.build` detects silences already present in the source and records them as
`origin="source_intrinsic"`, `preceding="fragment"`, `certainty="ambiguous"` — fragment
because the speaker is by construction mid-utterance, ambiguous for the same reason `pause`
is, since a natural pause can fall where a clause ends and the detector reads level, not
meaning. The `complete` fallback in `regime_at` stays, because a silence genuinely outside
any described gap is the end of the clip; what changes is that far fewer silences are now
undescribed.
Consequence: PCR fell by 18.3, 23.3 and 15.0 points. **The error ran in the flattering
direction**: the headline `balanced` baseline was overstated by 23.3 points, so the room a
controller has to improve on it looked half again as large as it is. `certain_only`
collapsed from 0.583/0.194 to 0.000 across all three arms, confirming ADR-017's warning
that agreement in that scope is not corroboration — at n=15 it discriminates nothing.

**Third flattering-direction error this phase**, which is a pattern rather than a
coincidence and is recorded as one:
1. A search summary attributed the `conservative` quick-start triple (0.4 / 800 / 3600) to
   the `aggressive` arm. An `aggressive` baseline waiting 800/3600 ms would have inflated
   its own premature-cutoff rate and made every adaptive result look better by comparison
   (BENCH_SPEC §3).
2. The default percentile definition (linear interpolation) reports p90 as 82 ms where
   nearest-rank reports 90 ms on the same nine samples — understating the tail that the
   latency claim is about (BENCH_SPEC §5).
3. This one.
None was deliberate and each had an ordinary cause. What they share is direction: every
one, uncaught, would have made Nod look better. That asymmetry is the signal — errors with
no stake in the outcome scatter, and these did not. Treat a convenient result as
provisional until the mechanism behind it has been checked, and give a number that favours
the project more scrutiny than one that does not.

**Addendum: the corpus cannot be made sound by making it bigger.** Recorded here rather
than in ADR-017 because it is a statement about ground truth, not about the simulator.
Reaching the power target needs 20–28 source clips against today's 4, and `corpus.build`
scales to that without redesign (16 ms per clip, so 840 clips is about 13 seconds). The
`say` pipeline does not. Every clip would come from one voice, Samantha at 160 wpm, so
**every inter-word pause in the corpus is drawn from a single TTS prosody model**. PCR is a
rate over exactly those pauses. Scaling the pipeline therefore multiplies statistical `n`
while adding none of the variation the metric depends on: 840 clips from one voice are not
840 independent observations of how people pause, and the power calculation assumes they
are. **It moves the p-values and not the validity.** A significance number computed over
them is a statement about one synthesiser's prosody, not about speakers.

Priority order, so this is not re-derived later:
1. **Multiple voices.** `VOICE_CANDIDATES` already lists five. Near-zero cost, and the only
   change here that adds prosodic variation rather than repetition.
2. **More segments**, 20–28. Buys the statistical floor and nothing else; worth doing, but
   worth doing second and worth describing as what it is.
3. **Real speech.** The only fix for independence. Track C, or a licensed corpus for Track
   A. Everything above is mitigation.

Until (3), no PCR figure from this corpus should be reported with a significance claim
attached.

## ADR-019 — Error bars are bootstrapped over clips, never over repeats
2026-09-17 · Status: accepted
Context: BENCH_SPEC §4 says every (clip, arm) pair runs `N = 5` and reports median and IQR.
That is right for a live upstream, which is non-deterministic. Against `FakeAssemblyAI` it
is not: the simulator is deterministic, so five repeats are byte-identical and their IQR is
exactly zero. A chart drawn that way would show zero-width bars, and a zero-width bar reads
as precision. It is the same defect class as the D1 p-values, which were computed over a
deterministic monotone model and reported significance that measured only determinism.
Decision: on the simulated path, repeats are **1**, and the interval is a **non-parametric
bootstrap over clips**: resample the clip set with replacement, `B = 10000` replicates,
report the **2.5th and 97.5th percentiles** of the replicate distribution. Clips are
resampled **jointly across arms** within a replicate, so the paired structure survives and
an arm-to-arm difference can be bootstrapped the same way. Seeded, so the chart is
reproducible.
The chart must say what the bars are **in its own label**, not in a caption a screenshot
loses: they are sampling uncertainty over which clips the generator happened to produce.
Consequence: the interval answers "if the generator had produced a different 120 clips,
how much would this move?" — which is a real question with a real answer. It does **not**
answer "how repeatable is this measurement", because that variance is zero by construction,
and it does not cover the uncertainty that dominates: every clip comes from one synthetic
voice, so the bootstrap resamples 120 draws from one prosody model and cannot see that
limitation at all (ADR-018). The bars are therefore a **lower bound on total uncertainty**
and must be read as one. Widening them is not the fix; real speech is.

## ADR-020 — Hysteresis gates the target, asymmetric decay bounds the step
2026-09-18 · Status: accepted
Context: CONTROL_SPEC §5 states two guards and never states their order. Hysteresis emits
"only if any field moves more than `HYST = 15 %` of its current value". Asymmetric decay
says "narrowing applies at most `NARROW_STEP = 12 %` per turn". **Twelve is less than
fifteen.** Read as two filters applied in series to the emitted value, every narrowing step
a turn is permitted to take is smaller than the threshold that would let it out, so
narrowing is unreachable and nothing else in §5 can reach it either. Found at Phase 2
Gate 1 while writing §9 property 6, not from re-reading §5 — the arithmetic is only
visible once something has to satisfy both guards at once (ADR-016's pattern, fifth
instance).
Decision: **reading (b).** Hysteresis gates on the **law's target**; asymmetric decay
limits the **step actually emitted**. Concretely, per turn, where `reference` is the config
this arbiter last emitted in this session and `state.current` before it has emitted any:

1. `target = control_law(...)`.
2. Hysteresis: emit nothing unless some field's `|target - reference|` exceeds
   `HYST × reference` for that field.
3. Decay, per field: widening takes `target` immediately; narrowing takes
   `max(target, reference × (1 - NARROW_STEP))`.
4. Re-apply §4's invariant repair, then §4's clamps, to the decayed result.

**No constant moves.** `HYST` stays 0.15 and `NARROW_STEP` stays 0.12.

Reasoning: the two guards have distinct stated purposes and §5 gives each its own reason.
Hysteresis suppresses churn from noise — it asks whether the law wants a *materially
different* window, and its enemy is socket chatter. Decay bounds rate of change — it asks
how far this turn may travel towards a window the law already wants, and its enemy is one
stumble making the agent permanently slow, or one crisp answer re-exposing the caller to
cutting. Reading (a) collapses them into a single guard in which one cancels the other:
`NARROW_STEP` becomes a constant with no reachable effect, and the controller is a **one-way
ratchet**. Over a long call it only ever widens, drifting to `MAX_MS_CEIL`.

That is the project's premise inverted, which is the argument that settles it. Nod exists
because one static threshold cannot serve one caller whose rhythm changes. A ratchet serves
a caller who *becomes* fluent mid-call **worse than the static `balanced` arm would** — the
arm would at least have held 1280 ms, where the ratchet has by then parked at 4000. A
control law that is beaten by its own baseline on a case the baseline handles by doing
nothing is not a control law.

Second, independent evidence: **§9 property 6 is a live constraint only under (b).** Under
(a) no narrowing is ever emitted, so the property's bound is never reached and it passes
vacuously — a test that cannot go red, which CLAUDE.md §5 treats as worse than no test.
§9 was written against a law in which §9.6 does work, so (b) is what §9 assumed. §9.5
points the same way and pins the reference: it requires a second decision on an unchanged
state to emit nothing, which is only possible if hysteresis compares against the last
*emitted* config rather than against `state.current`, since the caller passes the same
`current` both times.

**What changes in code.** Nothing yet — `decide()` is still a stub. This ADR fixes the
order Gate 3 implements, so the ordering is not re-derived from §5's silence:
- `Arbiter` carries the last-emitted `TurnConfig` as session state; hysteresis and decay
  both measure against it, not against `ArbiterInput.current`. `current` remains the
  session's starting reference and the host-override input.
- Step 4 is not optional and is easy to miss. Decay applied per field can break §4's
  invariant even when the target satisfied it: a `reference` at `max = min + 200` narrowed
  12 % on both fields gives a gap of 176 ms. §9 properties 1 and 2 assert on `decide()`'s
  output as well as on `control_law()`'s, so repair and clamps run again after decay.
- Widening stays unbounded, so step 4 can only raise `max_ms`, and cannot reintroduce a
  narrowing larger than `NARROW_STEP`.

**What does not change.** `HYST_FRACTION`, `NARROW_STEP` and every other §5 constant.
CONTROL_SPEC §4's law, which is upstream of all of this and untouched. `control_law()`'s
signature and purity — the reference config is `Arbiter` state, and passing it into the
pure law would make the law stateful for no gain. Nothing about widening.
Consequence: §5's table remains silent on the ordering and this ADR is the authority until
someone amends it, which is a documentation debt recorded here rather than a decision left
open. The reference config is new session state in `Arbiter`, which is `O(1)` and does not
touch INV-3. And narrowing is slow on purpose: at 12 % a turn, crossing
1280→400 ms takes **10 turns** and 4000→400 takes **19**. That is §5's stated intent —
one crisp answer must not immediately re-expose the caller to cutting — but it also bounds
how much the ratchet of reading (a) would have cost even if it were later fixed, since a
session parked at `MAX_MS_CEIL` needs 19 turns to recover and most calls do not have 19
turns left.

## ADR-021 — The ceiling is validated at configuration, not repaired per turn
2026-09-18 · Status: accepted
Context: CONTROL_SPEC §4 orders invariant repair before the latency ceiling:

    max_ms = max(max_ms, min_ms + INVARIANT_GAP_MS)      # repair
    max_ms = min(max_ms, ceiling_ms - ENDPOINT_OVERHEAD_MS)   # ceiling

so the ceiling can undo the repair. With `ceiling_ms` at 500 and `min_ms` clamped to its
900 ms maximum, the law returns `max_ms = 500`, below `min_ms + 200 = 1100`, and §9
property 2 — "`max_ms >= min_ms + 200` always holds" — is false for a reason in the spec's
own ordering rather than in any implementation of it. Found at Phase 2 Gate 1, from the same
cause as ADR-020: a property test had to satisfy two §4 lines at once.
Decision: **reading (b), by validating at the boundary.** `ceiling_ms` below
`MIN_MS_CEIL + INVARIANT_GAP_MS` — **1100 ms** today — is not a valid configuration.
§4's ordering is left exactly as written and repair is **not** re-applied after the ceiling.

Because the ceiling arrives from two places, INV-8's own dev/call split governs which
failure it gets, and this is the rule Gate 3 implements rather than chooses:
- **Process configuration** (`NOD_CEILING_MS` via `Settings`): **reject.** A validation
  error at startup, before any call exists. An operator who typed 600 has to learn that,
  and a silent clamp to 1100 is exactly the kind of accommodation nobody discovers.
- **Per-connection override** (`ceiling_ms` on `SessionProxy`, and `Voice.pacing_hint_ms`
  feeding it on a mid-session voice switch): **clamp to 1100 and emit once.** Rejecting
  here would drop a live call to enforce a latency preference, which INV-8 forbids
  outright — fail loud in dev, fail soft in a call.

Reasoning: the alternative is re-applying repair after the ceiling, and it defeats the
guard it is meant to rescue. The ceiling exists so a fluent caller is not made to wait; a
law that applies it and then knowingly raises `max_ms` back above it returns a config that
violates the ceiling on purpose, every turn, silently. That trades a loud impossibility for
a quiet wrong answer. Validating at the boundary keeps **both** guarantees intact — the
invariant always holds and the ceiling is never exceeded — by refusing the one input under
which they cannot both hold. It also fails once, at configuration, instead of on every turn
of every call.

`DEFAULT_CEILING_MS` is 2600, more than double the floor, so **no deployment is affected
and this costs nothing today** — which is precisely why now is the time to settle it. The
same decision taken after a deployment has configured 800 ms would be a migration.

Recorded because it is load-bearing for Gate 1's tests: `tests/property/strategies.py`'s
`CEILING_FLOOR_MS` restricts the drawn domain to `ceiling_ms >= 1100`, and that restriction
is now **spec-backed rather than a convenience**. It was written as a deliberate narrowing
with both readings noted, on the grounds that a property failing on an unreachable state
reports a bug that does not exist. Under this ADR the state is unreachable by construction,
so the domain is the whole of the valid domain and the docstring's "widen this bound once
that is settled by ADR" is answered: it does not widen.

**What changes in code.**
- `nod_core.config.Settings` grows a lower-bound validator on the ceiling, deriving
  `1100` from `MIN_MS_CEIL + INVARIANT_GAP_MS` rather than writing the literal, so the
  floor follows the clamps it comes from.
- `SessionProxy.__init__` clamps its `ceiling_ms` argument and emits a
  `capability_degraded`-style event once per session when it does.
- `tests/property/strategies.py`'s `CEILING_FLOOR_MS` docstring loses its open question and
  cites this ADR.

**What does not change.** CONTROL_SPEC §4's arithmetic and the order of its two lines,
which stay verbatim — this ADR constrains the input, not the law. `control_law()`, which
keeps trusting its `ceiling_ms` argument and stays pure; validation belongs at the boundary
and putting it in the law would put a raise inside the 5 ms budget. `MIN_MS_CEIL`,
`INVARIANT_GAP_MS`, `MAX_MS_FLOOR`, `MAX_MS_CEIL` and `DEFAULT_CEILING_MS`, none of which
move. `ENDPOINT_OVERHEAD_MS`, still 0 and still owed by `make bench` (INV-9); note the floor
is stated against the ceiling *before* the overhead is subtracted, so landing a measured
overhead of ~200 ms narrows the usable headroom and this floor is worth re-deriving then —
ADR-017's warning about inheriting a bound unexamined applies here too.
Consequence: a deployment can no longer express "never wait more than one second", and gets
a startup error rather than a controller that quietly violates its own invariant. That is
the intended trade. §9 property 2 becomes true unconditionally over the valid domain, and
§9 property 3 stays skipped and vacuous on its own terms until the overhead is measured.

## ADR-022 — The warm threshold rises to 24; widening is capped like narrowing
2026-09-18 · Status: accepted
Context: CONTROL_SPEC §2.1 declares the profiler warm at `n_gaps >= 8` and §5 makes
widening immediate while capping narrowing at `NARROW_STEP = 12 %` per turn. Gate 2 measured
what P² actually knows at that threshold, on the digit-reading pause shape Nod exists to
serve, `q = 0.90`, 4 000 trials per row:

| `n` | median rel. error | p75 rel. error | median abs. error | median error in `max_ms` |
|---|---|---|---|---|
| 8 | 74.5 % | 83.3 % | 1107 ms | **1772 ms** |
| 16 | 36.9 % | 54.1 % | 492 ms | **788 ms** |
| 20 | 19.2 % | 42.8 % | 211 ms | 338 ms |
| 24 | 17.9 % | 35.4 % | 226 ms | 361 ms |
| 32 | 11.3 % | 24.7 % | 153 ms | 244 ms |
| 40 | 6.6 % | 15.5 % | 94 ms | 151 ms |

**Correcting a figure from the Gate 2 report**: it gave 35 % median / 118 % max / 1279 ms at
`n = 8`. Those are the `n = 16` numbers. At `n = 8` the error is *worse* — 74.5 % at the
**median**, not the tail, and 1107 ms of median absolute error. The premise this ADR rests
on is therefore stronger than the one it was raised with.

The two guards compound. Widening is immediate and unbounded, so a single spurious estimate
lands `max_ms` wherever the law puts it in one turn — from `BASE_MAX_MS` 1280, an `n = 8`
median error of 1772 ms reaches `MAX_MS_CEIL` at once. Recovery is then capped at 12 % a
turn: **19 turns from 4000 back to 400.** One bad early gap therefore parks the controller
at the ceiling for most of a call. That is ADR-020's ratchet reached by another route, and
the guard §5 built to *bound* a stumble is what makes the stumble persist.

Decision: two changes, both derived below, neither a round number chosen for looking tidy.

**1. `MIN_GAPS_FOR_WARM` 8 → 24.** Criterion: the *median* g_p90 error at the threshold,
multiplied through `MAX_MS_FROM_P90_GAIN`, must not exceed one hysteresis band at the
hesitant operating point — so a typical early estimate cannot even emit a spurious patch.
At `g_p90 = 1500` the law gives `max_ms = 1.6 × 1500 + 250 = 2650`, and one band is
`0.15 × 2650 = 398 ms`. Against the table: `n = 8` gives 1772 ms (fail), `n = 16` gives
788 ms (**fail**), `n = 20` gives 338 ms (pass), `n = 24` gives 361 ms (pass).

**The proposed 16–24 band is therefore half-supported and 16 is not viable**: at 788 ms it
is nearly twice the band. 20 and 24 both clear it; 24 is taken for the margin, and because
neither is distinguishable under §4's "adaptation begins at roughly turn three" — a turn
carries roughly 4 to 14 gaps, so 24 is two to six turns.

**2. `WIDEN_STEP = 0.25`, capping widening as ADR-020 caps narrowing.** No threshold on this
estimator alone is safe, which is why the threshold is not where the fix lives: `n = 256` is
unreachable inside a short call, and even `n = 64` still admits 574 ms of absolute error in
the tail. A cap bounds the damage per turn whatever the estimate says. Derived against three
quantities at once:

| `W` | turns to serve a hesitant caller | turns to undo one spurious turn | turns to reach the ceiling | `W / NARROW_STEP` |
|---|---|---|---|---|
| 0.12 (symmetric) | 7 | 1 | 11 | 1.00 |
| 0.20 | 4 | 2 | 7 | 1.67 |
| **0.25** | **4** | **2** | **6** | **2.08** |
| 0.30 | 3 | 3 | 5 | 2.50 |
| 0.50 | 2 | 4 | 3 | 4.17 |
| today | 1 | 19 | 1 | — |

Symmetric 12 % is **rejected**: seven turns to serve a genuinely hesitant caller means seven
turns of cutting them off mid-sentence, which is the harm the project exists to prevent, and
trading the ratchet for that is not a trade. 0.50 is rejected at the other end: three
consecutive turns to the ceiling is barely a bound. 0.25 serves a real need in four turns,
undoes one spurious turn in two, and needs six consecutive wrong turns to reach the ceiling
— by which point §5's freeze-on-instability and the 24-patch rate cap have both had
several opportunities. It stays 2.08× `NARROW_STEP`, so §5's asymmetry survives as an
asymmetry rather than being flattened into symmetry.

**Record the error direction, because it is the fourth of its kind.** Early spurious
widening inflates pause tolerance: the agent waits longer, so it cuts fewer callers off, so
**PCR improves** — while every caller waits longer and TTL degrades. PCR is the headline
axis of the Pareto chart. So this defect, uncaught, would have made the controller look
better on the metric the claim is about while making the product worse for the person on the
phone. ADR-018 records three flattering-direction errors in Phase 1 and names the asymmetry
as the signal; those were all in the *measurement*, and this one is in the *controller*,
which is a new place for the pattern to live and a worse one.
Consequence: **for Gate 3, and not implemented here.** `MIN_GAPS_FOR_WARM` moves in
`profiler.py`; `WIDEN_STEP` is new in `arbiter.py` and the ADR-020 decay step becomes
two-sided. Two things do **not** change: `NARROW_STEP` stays 0.12, and §4's law is untouched
— this is guard machinery inside `decide()`.
Two debts are recorded rather than left implicit. **CONTROL_SPEC §5 still says "widening
applies immediately" and §2.1 still says 8**, so both contradict this ADR until amended;
that is the ADR-016 shape and it needs the same cross-reference treatment ADR-020 and
ADR-021 got. And `test_a_narrowing_is_reachable_at_all` has a widening twin owed: a capped
widening must still be *reachable*, or `WIDEN_STEP` is a constant that suppresses the thing
it was meant to bound, exactly as 12 % against 15 % was.
A cost, stated plainly: raising the threshold to 24 leaves a hesitant caller on the static
`balanced` window for two to six turns rather than one to two, and they may be cut off
during them. That is accepted because the alternative is adapting on an estimate whose
median is wrong by 74 %, and because a capped widening means the controller reaches them in
four turns once it starts rather than overshooting to the ceiling in one.

## ADR-023 — `features()` repairs an inverted pause profile
2026-09-18 · Status: accepted
Context: `g_p90 < g_p50` cannot be true of any real sample set — they are two quantiles of
one population. CONTROL_SPEC §2.1 says "maintain P² estimators for `g_p50` and `g_p90`",
two estimators, and says nothing about coupling them. Nothing does: they approximate
independently, so their errors are independent and the order can invert. Gate 2 found it by
asking what value the pair could not take, then checking — over 20 000 random gap streams
the inversion occurred **28 times, 0.14 %, worst inversion 146 ms**.
Decision: clamp in `Profiler.features()`, reporting `g_p90 = max(g_p90, g_p50)`.
Rare is not the same as harmless. An inverted profile lets the control law reason from a
state that describes no speaker, and §4's invariant repair does not catch it: that repair
prevents `max_ms < min_ms`, which is a statement about the *outputs*, and says nothing about
whether the *inputs* were coherent. A law fed `g_p90 < g_p50` produces a `max_ms` below the
`min_ms` its own `g_p50` implies, the repair lifts it to `min_ms + 200`, and the result is a
window that looks lawful and was computed from nonsense — with no trace, because the repair
tidied the evidence away.
Clamping is chosen over coupling the estimators for three reasons. It is **monotone-safe**:
`max(g_p90, g_p50)` is non-decreasing in `g_p90`, so §9 property 4's monotonicity survives,
and `g_p50` reaches `max_ms` only through a floor it could already have reached via the
invariant repair. It is **inside the budget** (INV-2): one comparison, `O(1)`, no
allocation. And coupling the two estimators is **a change to ADR-002's design** — it would
mean P² markers that constrain each other across instances, which is not the published
algorithm and would put the exact-ring validation path on a different footing from the
production one, making the differential test compare two things that differ by more than
approximation.
Consequence: three things are now on the record. **§9's properties have never been evaluated
on an inverted state** — `tests/property/strategies.py` excluded it from the domain — so
nothing is known about how the law behaves there beyond the repair's floor. **That
exclusion's stated justification was wrong**: it read "a state violating this is one the
profiler cannot emit", and the profiler can. And **after this repair the justification
becomes true by construction** rather than by assumption, which is the cleanest possible
resolution — the domain restriction stops being a bet about the implementation and starts
being a consequence of it.
For Gate 3, and not implemented here: the clamp in `features()`, and the docstring in
`strategies.speaker_features()` moving from "measured, and this justification is too strong"
to "guaranteed by ADR-023". `test_two_independent_estimators_can_report_p90_below_p50` then
has to change job — it currently asserts the inversion *is* reachable, and after the repair
the estimators can still invert while `features()` can no longer report it, so it becomes a
test that the repair fires rather than a test that it is needed. Its own docstring already
says so.

## ADR-024 — The boolean floor is absolute and exempt from asymmetric decay
2026-09-18 · Status: accepted
Context: CONTROL_SPEC §5 states two guards that cannot both hold. The floor guard says
"on `boolean`, `min_ms` never exceeds 400", reason: "yes/no must stay snappy". Asymmetric
decay caps a narrowing at `NARROW_STEP = 12 %` of the reference per turn. From a reference of
900 ms — `MIN_MS_CEIL`, and exactly where a hesitant caller's profile puts it — reaching the
cap takes `ln(900/400) / -ln(0.88) = 6.35`, so **7 turns**.

Found at Phase 2 Gate 3 by the impossible-value check, not by reading §5: asked what a
`ConfigPatch` could not contain, one of the thirteen named answers was "a boolean turn left
above the cap", and it occurred **6 889 times in 49 233 emitted patches**. §9 property 8
passes throughout, because its domain was restricted at Gate 1 to
`current.min_turn_silence_ms <= 400` with this conflict recorded as the reason.
Decision: **reading (a). The cap is absolute and exempt from decay.** On a `boolean` turn,
`min_turn_silence` is set to at most `BOOLEAN_MIN_MS_CAP` in one turn regardless of how far
that is from the reference. §9 property 6's narrowing bound acquires one **narrow, explicit
exception**, named in the property itself rather than left to be discovered.

Reasoning: §5 gives this guard a purpose, and under decay it does not serve it. Most boolean
turns are answered inside seven turns of the prompt — that is what makes them boolean — so a
guard that needs seven turns to take effect does nothing on the calls it exists for. It is
not a weaker version of the guarantee; it is the absence of one, wearing the guarantee's
name.

Reading (b) — the cap applies to the law's target and decay shapes the emitted value — keeps
the mechanism uniform, and that is a real virtue: one rule for every field is easier to
reason about and harder to get wrong. But it buys that uniformity by making the guard's own
sentence false. "`min_ms` never exceeds 400" would have to be reworded to "the law never
asks for more than 400", which is a claim about an intermediate value nobody experiences.
Between a spec that is internally consistent and says something untrue about the product,
and a spec with one stated exception that says something true, the second is the better
trade.

**The exception has a principle, and it is worth stating because it will be needed again.**
The boolean floor is a **correctness bound**, not a control move. Asymmetric decay exists to
damp *control churn* — to stop the controller's own oscillation from reaching the caller —
and a rate limiter on control output has no business throttling a bound that was never a
control decision in the first place. The same test separates the other §5 guards: the
clamps, the invariant gap and the latency ceiling are all correctness bounds and none of
them is decayed either; hysteresis and the rate cap are churn dampers and both are. §5's
table does not make this distinction, and every future guard added to it should be classified
before it is placed.
Consequence: **for Gate 3's test suite, which lands with this ADR.**
`test_a_boolean_turn_is_capped_even_from_a_slow_reference` converts from `xfail(strict=True)`
to a passing test, and `make mutate` gains a mutation that removes the exemption — a guard
whose exception is untested is the exception silently not existing.
§9 property 6 now reads "narrowing never exceeds `NARROW_STEP` in one turn, except the
boolean floor". An unqualified property with an unwritten carve-out is how a vacuous test
starts, so the carve-out is written: the property skips `min_turn_silence` on a `boolean`
turn and continues to bound `max_turn_silence`, which the floor does not touch.
What this costs, stated plainly: a caller who has earned a 900 ms minimum and is then asked
a yes/no question has it cut to 400 in one turn, which is a 56 % narrowing and more than four
decay steps. If they are still mid-sentence when the prompt changes, they are more exposed to
a cutoff on that one turn than the decay guard would have left them. That is accepted because
the host declared the turn `boolean`, and a host declaring the next answer is one word is
better evidence about that turn than the profile built from previous ones.
`max_turn_silence` is untouched, so the mid-sentence regime ADR-011 cares about keeps its
full width — the exemption is narrow in exactly the place that matters.

## ADR-025 — ADR-023's repair guards the cold path, not the warm one
2026-09-18 · Status: accepted · amends the justification of ADR-023, not its decision
Context: ADR-023 clamps `g_p90` up to `g_p50` in `features()`, justified by a measured
inversion rate of **0.14 %, worst 146 ms**, over gap streams of `n ∈ [8, 400]`. ADR-022
landed in the same sitting and raised `MIN_GAPS_FOR_WARM` from 8 to 24. Re-measured at
Gate 3:

| `n` range | inversion rate | worst inversion |
|---|---|---|
| `[8, 23]` | 0.120 % | 317 ms |
| `[8, 40]` | 0.050 % | 347 ms |
| `[8, 400]` | 0.015 % | 288 ms |
| **`[24, 400]`** | **0 of 20 000** | — |

The inversion is almost entirely a small-`n` phenomenon. So the range ADR-023 measured is
largely the range ADR-022 removed from the warm path, and **ADR-023's stated justification no
longer holds**: the rate it cites is a rate over sample counts at which the quantiles are
now never consulted by §4.
Decision: **the repair stays; its justification is replaced.** The new one: `features()` is
called on **every** turn, including while the profiler is cold, because §4 needs a
`SpeakerFeatures` record in order to take its cold branch. `n_gaps < 24` is precisely the
cold regime, by definition — so the repair guards the path where the inversion actually
occurs at 0.120 %, and the warm path where it is unmeasurable gets it for free.

Read the other way round, the two ADRs turn out to be complementary rather than redundant:
ADR-022 stops the *law* consuming an unreliable estimate, and ADR-023 stops the *profiler*
reporting an incoherent one. They act on the same measurement at different points, and
neither makes the other unnecessary.

Three things are recorded because they would otherwise be re-derived:
- **0 of 20 000 is an upper bound, not a proof.** It bounds the rate at roughly 1.5 × 10⁻⁴ at
  95 % confidence, not at zero, and the estimators remain structurally uncoupled (ADR-002).
- **The repair became unfalsifiable before it became better justified.** Its property test
  draws realistic turn streams, and after ADR-022 the condition never arose in that domain,
  so the test passed with the repair removed — CLAUDE.md §5's test that cannot go red, and
  the only symptom either ADR produced. It now has a deterministic sibling that hands
  `restore` an inverted `ProfilerState`.
- **This is a new justification for unchanged code**, which is a thing an ADR log has to be
  able to say. The alternative — silently keeping code whose stated reason has expired — is
  how a codebase accumulates guards nobody can defend and nobody dares delete.
Consequence: no code changes. ADR-023's decision stands; its Context paragraph should be read
with this one. Cost of keeping the repair now that the warm-path rate is unmeasurable: one
comparison per `features()` call, `O(1)`, which INV-2 does not notice at a measured 4.4 µs
mean for the whole decision path.

## ADR-026 — The simulator does not tune the control law
2026-09-18 · Status: accepted
Context: CONTROL_SPEC §8 says the constants "are tuned by `nod tune`, which sweeps them
against the bench corpus", and CLAUDE.md §7 says "do not hand-tune the control law by ear.
Tune against `make bench`." Both were written before ADR-017 established what `make bench`
actually runs against in Phase 2: `FakeAssemblyAI`, a **simulator**, not a replayer.

Three properties of that simulator make it the wrong instrument for tuning, and each is
recorded in ADR-017 as a deliberate design choice rather than a defect:
1. **Its gate response is 1:1 by construction.** `ENDPOINT_OVERHEAD_MS` defaults to 0, so it
   fires exactly at the configured gate. The real service does not: the P1 matrix measured
   the boundary landing 172–217 ms *after* the gate, a spread comparable to its own repeat
   noise, and `make bench` still owes the measurement (INV-9).
2. **Its timing spread is zero.** Determinism is why it exists (INV-7), and it is also why
   ADR-019 had to replace repeat-IQR error bars with a bootstrap over clips: five repeats are
   byte-identical.
3. **Its regime labelling comes from the truth sidecar**, not from the model. That is correct
   — ADR-001 measured `end_of_turn_confidence_threshold` INERT, so a fake in which it works
   would let a control law be rewarded for exploiting a knob that does not exist — but it
   means the simulator agrees with the corpus by construction on exactly the axis PCR scores.

Tuning is fitting parameters to an environment's response. Fitting to (1), (2) and (3) is
fitting to **our model of the service**, and the closer the fit the more of the model's
1:1-ness and zero spread the constants encode. A constant that is optimal against a gate with
no overhead and no jitter is not thereby optimal against one with 200 ms of overhead and 40 ms
of spread — and worse, the sweep would report the fit as an improvement with no signal that it
came from the model rather than from the world.
Decision: **constants derived from measurement outside the bench are not tuned against
simulated output.** Named, so there is no ambiguity about which:

| constant | derived from | ADR |
|---|---|---|
| `MIN_GAPS_FOR_WARM` = 24 | P² error against exact quantiles on the digit-reading shape | ADR-022 |
| `WIDEN_STEP` = 0.25 | turns-to-serve against turns-to-undo against turns-to-ceiling | ADR-022 |
| `NARROW_STEP` = 0.12 | §5's stated purpose, ordering fixed against hysteresis | ADR-020 |
| `MIN_SEPARATION` floor = 100 ms | measured endpoint overhead, 147–274 ms | ADR-014 |
| `CEILING_FLOOR_MS` = 1100 | `MIN_MS_CEIL + INVARIANT_GAP_MS`, arithmetic | ADR-021 |
| `BOOLEAN_MIN_MS_CAP` = 400 | §5's stated purpose, exemption derived | ADR-024 |

Any constant the bench suggests moving is **reported with the simulated evidence and not
adopted in Phase 2.** The live check is deferred to Phase 4's `N = 5` runs against the real
service, which is where INV-9 already says published figures come from. A suggestion is not
discarded — it is a hypothesis with an experiment attached and a date.

This does not make the bench decorative, and the distinction is worth keeping sharp. The
simulator is the right instrument for the things it is deterministic *about*: that the arms
are wired correctly, that the metrics compute, that the report regenerates, that a refactor
did not change behaviour, and that the tradeoff has **this shape under our model** (ADR-017's
own statement of what the simulated chart may claim). It is the wrong instrument for the value
of a constant, and only that.
Consequence: **Phase 2's chart shows the controller's shape under our model, and its
constants are defensible by derivation rather than by fit.** That is a weaker claim than "we
tuned it and it won", and it is the claim the evidence supports. It also has one real
advantage worth stating rather than conceding: a derived constant comes with the argument that
produced it, so a future session can check the arithmetic and know what would change it, where
a fitted constant comes with a number and a corpus that no longer exists.

Two costs, both accepted:
- **The constants are probably not optimal.** Nothing here claims they are. They are
  defensible, which is a different and, at nine days to freeze, more useful property.
- **`nod tune` is cut** (ROADMAP §3), so no sweep exists to be tempted by in Phase 2 anyway.
  This ADR is therefore mostly about Phase 4, where a live sweep *would* be admissible — and
  it is written now, before any number is in hand, because a tuning decision taken after
  seeing a flattering result is not a decision.
CONTROL_SPEC §8 and CLAUDE.md §7 should be read with this: "tune against `make bench`" means
against the **live** bench of BENCH_SPEC §4, never against the simulated path of ADR-016.

## ADR-027 — `MAX_PATCHES` cannot fire, and the decision waits for a live count
2026-09-18 · Status: accepted — deferred resolution, recorded so it is not re-derived
Context: CONTROL_SPEC §5's rate cap is "at most 1 patch per turn, at most
`MAX_PATCHES = 24` per session", reason "bounds cost and blast radius". The per-turn half is
load-bearing and does real work — §9 property 5 depends on it (ADR-020). The session half
has never been observed to bind. Measured at Gate 5, with Gate 4's argument as the
independent second reason:

| workload | warm at | patches over the session | cap |
|---|---|---|---|
| Track A clip, ×120 | never | **0** | 24 |
| synthetic, monotone drift 200→2500 ms, 120 turns | turn 5 | 8 | 24 |
| synthetic, **oscillating** 150↔2500 ms, 120 turns | turn 5 | 7 | 24 |
| synthetic, random 100–3000 ms, 120 turns | turn 5 | 4 | 24 |

**Eight is the maximum observed, one third of the cap, over 120 turns** — far longer than
any real call. The oscillating row is the one that settles it: it is the profile that
*should* be worst for patch count, and it produces fewer patches than the monotone drift.

**Two guards compound to make a third unreachable**, which is the finding worth recording
rather than the number:
- **Hysteresis** (§5, 15 %) suppresses everything once the window has converged on the
  law's target. That is Gate 4's independent reason, reached by argument before this
  measurement existed: over 199 turns of a monotone profile only 4 patches are ever
  emitted, which is why Gate 4's first attempt at a rate-cap test was vacuous.
- **Freeze on instability** (§5, 3 reversals in 5 turns) damps exactly the oscillation that
  would otherwise generate patches fast enough to reach the cap. The guard designed to stop
  thrashing also removes the only workload that could exercise the cap.
Neither guard is wrong and neither was designed with this in mind. The cap is simply
downstream of both, and nothing between them leaves it anything to do.

**This is CLAUDE.md §5's defect class** — a check that cannot fail, here a guard that cannot
fire — and it is the fifth shape that section catalogues: not a test that stays green, not a
vacuous invariant, but a *runtime* guard rendered unreachable by two other guards that each
work correctly. Worth naming because the mechanism is new to this repository: the previous
instances were all about a check's own construction, and this one is about its position in a
chain.
Decision: **nothing changes in Phase 2, and the resolution is deferred to Phase 4's live
patch count.** Three reasons, in order of weight:
1. **The evidence is simulated.** ADR-026 forbids moving a constant on simulated output, and
   this is a constant about cost and blast radius on a real socket — the one kind of
   question a deterministic model with no network is least able to answer.
2. **The failure mode is benign in the direction it fails.** A cap that never binds costs
   nothing; a cap set too low silently stops a controller adapting mid-call, which is the
   product failing at the thing it exists for. Of the two errors, the current one is the
   one to be making while the evidence is thin.
3. **A guard that cannot fire is still documentation** of an intended bound, and deleting it
   would remove the statement along with the dead code. If Phase 4 confirms it cannot fire
   live either, the right move is probably to keep it and say so in §5 rather than to
   delete it — a guard annotated "never observed to bind, kept as a stated bound" is honest,
   where an absent guard says nothing.
What Phase 4 must actually collect, so this is not re-derived: **the patch count per session
across the `N = 5` live runs**, and the count for the longest session in the set. If the
maximum is still well under 24, amend §5 to record the cap as a stated bound rather than an
active guard. If it approaches 24, the cap is live and this ADR is superseded by the
measurement.
Consequence: `MAX_PATCHES` stays 24 and untested against its own boundary — there is no test
asserting the cap binds, because no input reaches it. `tests/unit/test_arbiter.py` tests the
cap by *supplying* `patches_sent` at the boundary, which tests the arbiter's arithmetic and
not the cap's reachability, and its docstring should not be read as more than that.

## ADR-028 — The bench reconstructs turns from gaps, so disfluency is unmeasured
2026-09-18 · Status: **superseded in part by ADR-031 and ADR-033**; restated at Gate 7

> **Restated 2026-09-19, measured on the regenerated corpus.** The cause this ADR names —
> reconstructed placeholder tokens `w0, w1, …` — **is fixed.** ADR-031 put the service's own
> token text in the sidecar and `replay._turn_from_words` replays it. What that changed, and
> what it did not:
>
> | §2.3 feature | this ADR said | measured at Gate 7, 120 clips |
> |---|---|---|
> | duration outliers | 0 on every clip | **43 across the corpus, 41/120 clips nonzero**, `disfluency` max 0.222 |
> | adjacent repeats | 0 on every clip | **1** — and the 1 is the finding, see ADR-033 |
> | filler set | 0 on every clip | **0, and this is correct behaviour** |
> | `recent_cuts` (§2.5) | 0.0 on every clip | **0.0, unchanged** |
>
> - **Duration outliers register.** `disfluency` has come off the floor for the first time
>   since the corpus existed, and essentially all of the movement is this one feature.
> - **Adjacent repeats stand at 1**, against a corpus with twelve clips of a perturbation
>   built to produce exactly this signal. The placeholder tokens were masking a *second*
>   cause, in the generator rather than the replayer. **ADR-033** records it.
> - **Fillers at 0 is not a defect and must not be filed as one.** Track A is `say` reading a
>   clean script; a synthetic voice never utters "um". A corpus of clean read speech scoring
>   zero on a filler detector is the detector being right.
> - **`recent_cuts` stays 0.0** for the reason this ADR already gives: cut detection needs
>   agent audio and the bench has none. Nothing in ADR-031 touches that.
>
> **The conclusion narrows rather than drops.** This ADR ends by saying only Phase 4's live
> runs exercise the disfluency term at all. Corrected, three ways at once:
> - the **duration-outlier path is exercised offline**, from Gate 7 onward;
> - the **repeat path is not**, and will not be until the perturbation is aligned to word
>   boundaries (ADR-033);
> - the **filler path cannot be** on this corpus at all, at any point, because the material
>   contains no fillers to find. That one is not deferred work; it is out of Track A's reach
>   by construction, and only Track C or a live run can reach it.
>
> Everything below is left as written, including the consequence that simulated figures
> understate the controller — that still holds, since `disfluency` reaching 0.222 on some
> clips rather than 0.0 can only *widen* `max_turn_silence` (§4 coefficient 0.45, positive).

Context: `FakeAssemblyAI` emits boundaries, not word timings, and the profiler's only input
is timings — `g_i = words[i].start - words[i-1].end` (CONTROL_SPEC §2.1). So
`nod_bench.replay._turn_from_gaps` synthesises the `Turn` stream the upstream would have
sent, from the truth sidecar: a `Gap` runs from the end of one word to the start of the
next, so a run of gaps *is* a word sequence with words between them.

That reconstruction is faithful on exactly one axis and silent on the rest, and the silence
is total rather than partial:

| §2 feature | reconstructed? | why |
|---|---|---|
| `g_p50`, `g_p90` (§2.1) | **yes, at true duration** | every generator-inserted gap reaches the estimator exactly |
| `speech_rate` (§2.2) | partially | word durations are synthetic, so the rate is an artifact of the reconstruction |
| adjacent repeats (§2.3) | **no** | tokens are `w0, w1, w2…`, never equal to their predecessor |
| filler set (§2.3) | **no** | no token is ever in `FILLER_TOKENS` |
| duration outliers (§2.3) | **no** | every word is the same synthetic length |
| `recent_cuts` (§2.5) | **no** | cut detection needs agent audio, which the bench has none of |

So `disfluency` and `recent_cuts` are **0.0 on every clip of every run**, and both are terms
in §4's `max_turn_silence` expression — the primary control surface (ADR-011). The bench
therefore exercises the **pause axis alone**, and would do so even on a corpus where the
profiler warms.
Decision: record this as a **floor on what the controller can demonstrate on the bench**,
not as a defect to fix inside the harness. Reconstructing plausible disfluent tokens would
mean the harness inventing the feature it then measures, which is precisely what ADR-017
refuses for the regime labelling and refuses for the same reason: a benchmark that supplies
its own input to a feature measures the supply.

**This persists on Track C unless the recordings are transcribed rather than
reconstructed.** That is the operative consequence and it is easy to miss, because Track C
fixes the *other* two problems — it is multi-turn, so the profiler warms, and it is scripted,
so the context axis has input (ROADMAP §0). It does not fix this one. Real audio through the
real service returns real `words[].text`, and §2.3's three features then work; real audio
replayed through the *simulator* still arrives as gaps and still scores 0. So a Track C run
on the simulated path measures the pause axis alone, and only the Phase 4 live runs exercise
the disfluency term at all.
Consequence: three things follow and are recorded so a later reader does not infer more from
the bench than it shows.
- **Any simulated figure understates the controller**, in the one direction that matters:
  `disfluency` and `recent_cuts` only ever *widen* `max_turn_silence` (§4 coefficients 0.45
  and 0.15, both positive), so a run with them pinned at 0 gives the controller less room
  than the law would. This is the rare case of a measurement error that runs *against* the
  project rather than for it, which is worth stating plainly given ADR-018's tally of four
  errors that ran the other way.
- **The ablations are narrower than their names.** `nod-nocontext` is described as "speaker
  axis only"; on the simulated path it is "pause quantiles only", which is a strict subset.
  BENCH_SPEC §3's names are kept, and the report must say which.
- **§9 and the unit tests are unaffected.** They drive `decide` and `observe_turn` directly
  with constructed features and real tokens, so the disfluency path is fully covered there
  (`tests/unit/test_profiler.py`). What is untested is the *composition* of real disfluency
  features with a real corpus, and that needs the live path.

## ADR-029 — `expected_answer` is judged from the prompt, never from the answer
2026-09-18 · Status: accepted
Context: writing the Track C script (`docs/TRACK_C_SCRIPT.md`) ran straight into ROADMAP §0's
stated tension — declared answer classes push toward short answers, and a turn of `w` words
contributes `w - 1` gaps, so short answers starve the profiler. `boolean` is where it bites:
a yes/no answer is one word and zero gaps.

The obvious escape is to write a prompt that offers an alternative — "I have you on
Marlborough Street, is that right, **or has it changed?**" — declare it `boolean`, and collect
the twelve gaps of the sentence the caller actually says. It is natural dialogue, it reads as
a yes/no question, and it is wrong.

CONTROL_SPEC §3 gives `boolean` a hint of `min_mult 0.7 / max_mult 0.7`. The rationale for
that number is that a yes/no answer is short and completes quickly. So the escape **narrows
the window by 30 % on a turn deliberately engineered to draw a long answer**, and the arm
under test then cuts the caller off mid-sentence at a rate the script chose. PCR on those
turns would report the script's authoring and print it as the controller's behaviour.

**This is a flattering-direction error in the corpus itself**, and that is the part worth
recording rather than the rule. ADR-018 tallies four such errors in the metrics; ADR-022
records one in the control law. Ground truth is a third place for the pattern and it is the
hardest of the three to catch:
- a wrong metric is caught by `make mutate` — the code computing it is guarded;
- a wrong constant is caught by a property test or by the arithmetic in its ADR;
- **hand-authored ground truth has no mutation harness at all.** There is no source to
  mutate, no test that goes red, and no assertion to make vacuous. A mislabelled
  `expected_answer` is one word in a markdown table, it is plausible, it survives every gate
  in the repository, and it moves a published number.
CLAUDE.md §5's "ask what would have to change for this to fail" has no purchase here, because
nothing fails. The only available check is the rule, applied when the corpus is written.
Decision: **the class describes the prompt's expectation, not the answer's shape.** Three
rules, for this script and for any corpus work that declares a dialogue state:
1. **Either/or prompts are `free`.** "Move all of them, or just this one?" expects neither
   yes nor no, so it is not a `boolean` however much it reads like one.
2. **Genuine yes/no prompts are `boolean`**, and are budgeted at zero gaps regardless of the
   answer written next to them. `docs/TRACK_C_SCRIPT.md` §7 carries a conservative recount on
   exactly that basis, and no crossing turn depends on a boolean.
3. **A caller elaborating on a real yes/no is a real caller.** "Did anyone tell you it needed
   renewing?" drawing "Nobody said anything about that when I booked it" is not the script's
   doing and needs no correction. The defect is a prompt that *cannot* expect the class it
   declares, not an answer that exceeds it.
The general form: a corpus may not supply its own input to the feature it then measures.
ADR-017 refuses this for regime labelling and ADR-028 refuses it for disfluency tokens; this
is the same refusal on the context axis.
Consequence: `boolean` stays gap-poor and the script works around it by spending it late
rather than by relabelling it — all six boolean turns in the five scripts sit at turn 5 or
later, past a warm threshold crossed on turn 2 or 3. The cost is real and accepted: the
context axis gets six `boolean` turns instead of a dozen, and they contribute almost nothing
to warming. That is the correct cost of not fabricating the input.

## ADR-030 — The inter-turn seam is 4500 ms, above `conservative`'s gate
2026-09-18 · Status: accepted
Context: a Track C call is recorded as caller audio only — the agent's prompts are read
off-mic or played from TTS and cut out, because the bench has no agent audio (ADR-028) and
anything left in the clip is read as caller speech by the VAD. What remains where each prompt
was is a silent **seam**, and that seam is what ends the turn: there is no other boundary
signal in a caller-only multi-turn clip.

So the seam length decides the turn count, and the turn count is not a cosmetic property.
`docs/TRACK_C_SCRIPT.md` §1 establishes that no gap spans a turn boundary, so

```
gaps in a call = total words - total turns
```

If a seam is shorter than an arm's `max_turn_silence`, that arm does not end the turn there
and sees one turn where a wider-gated arm sees two. `conservative` runs at 3600 ms and
`aggressive` at 400 ms (BENCH_SPEC §3), so a 2000 ms seam gives `aggressive` more turns than
`conservative` — and therefore **fewer gaps**, a different `n_gaps` trajectory, a different
warm turn, and a different speaker profile. The arms would be compared on corpora that differ
in the one quantity the controller reads. The headline delta would be partly an artifact of
the edit.

The number is derived against **the requirement — `conservative`'s 3600 ms gate — and not
against any constant the tooling currently holds.** That is deliberate and it is CLAUDE.md
§5's tail-silence lesson applied before the fact rather than after: `test_every_clip_ships_a_
sidecar_and_a_tail` compared a generated tail against `TAIL_SILENCE_MS`, the very constant
that produced it, so it passed for any value and survived a tail shrinking below the widest
arm's gate. Anchoring the seam to a tooling constant would reproduce that defect exactly. The
900 ms of margin covers frame quantisation in the editor and any endpoint overhead above the
configured gate — the P1 matrix measured boundaries landing 172–217 ms late (ADR-026).
Decision: **every inter-turn seam in a Track C clip is at least 4500 ms of silence**, and
this is enforced by **a check on the edited audio before it becomes a corpus**, not by a line
in the recording notes. A convention in prose is a check that cannot fail: nothing reads it,
nothing reports it, and a mis-edited clip enters the corpus looking like every other clip.
The check, when the Track C ingestion path is built: for each clip, detect the silent runs,
assert every run that separates two answers is `>= 4500` ms, and fail the ingest — loudly and
non-zero — naming the clip and the short seam. It asserts against 4500 as a literal tied to
`conservative`'s documented gate in a comment, never against `MIN_INTRINSIC_GAP_MS`,
`TAIL_SILENCE_MS` or any other constant that could drift underneath it.
Consequence: the edit is constrained and slightly unnatural — 4.5 s between answers is longer
than a real agent would take, so the clips are not a realistic rendering of a call's pacing.
Accepted, because the clips exist to be replayed arm-by-arm and not to be listened to. Also:
this makes each call roughly 45–55 s longer than its speech content, which is already in the
length estimate in `docs/TRACK_C_SCRIPT.md` §8. The seams are not gaps and never reach the
profiler — CONTROL_SPEC §2.1 computes gaps only within a turn — provided the turn actually
ends there, which is what the check guarantees.

## ADR-031 — Sidecar word timings come from transcription, not silence detection
2026-09-18 · Status: accepted — implemented at Gate 7 (2026-09-19)

> **Correction, added at Gate 7, and it is the first thing to read.** This ADR moved the
> **profiler's input**. It did **not** move the **scorer's ground truth**, and nothing below
> should be read as claiming it did. PCR is computed from `Gap` records; `_intrinsic_gaps`
> still produces those at `INTRINSIC_FLOOR_DBFS = -44.0`; and **none of the ~16 transcript
> inter-word gaps per clip became a `Gap`.** The sidecars gained one key, `words`, and the
> `gaps` arrays are byte-identical before and after.
>
> **So the incomplete-ground-truth exposure this ADR records is still open. Gate 7 did not
> close it.** The 4-of-49 measurement below still stands as written: the committed Track A
> source still carries undescribed silences up to 720 ms, several above `aggressive`'s
> 400 ms gate, and Phase 4's live runs will still fire boundaries inside them.
>
> **The evidence is that Gate 7's bench table reproduces Gate 5's exactly** — all six arms,
> every cell, PCR, TTL and FRAG. That identity is not a coincidence and not a caching
> artifact: it is what it looks like when the quantity a metric reads has not changed. It
> was verified rather than assumed, per CLAUDE.md §5 — `git` confirms the `gaps` arrays are
> unchanged, and the run demonstrably consumed the new words, because `_turn_from_words`
> raises `MissingWordsError` on a wordless clip and the bench exited 0.
>
> Closing the exposure means promoting transcript gaps to `Gap` records with regime labels,
> which is a change to what the scorer is told and therefore a separate ADR under ADR-017's
> constraint — not an extension of this one.

Context: `docs/TRACK_C_SCRIPT.md` §8 flagged a risk it could not resolve: there is no Track C
ingestion path, and the obvious one built on `corpus._intrinsic_gaps` might lose most of the
script's gaps. The pilot specified there was run before scheduling any recording.

**Measured, on `say`-synthesised connected speech.** One continuous utterance, 22 words,
6811 ms, transcribed live against `universal-streaming-english`:

| | gaps |
|---|---|
| live, from AssemblyAI word timings | **21** (22 words, one turn — exactly `w - 1`) |
| `_intrinsic_gaps`, same audio | **0** |

**`MIN_INTRINSIC_GAP_MS` is not the filter.** Thirteen of the 21 live gaps exceed 100 ms by
word timing, so the duration threshold would have admitted them. `INTRINSIC_FLOOR_DBFS` at
−44.0 is what rejects them: over the 135 speech frames only **2** reach that floor, and
neither run is 100 ms long. Frame energy runs min −47.8, p05 −36.9, median −14.1 dBFS. The
intervals between words in connected speech never get that quiet.

Sweeping the floor shows the two methods agree about *where* the gaps are and disagree only
about the threshold: at −20 dBFS the detector finds **13 runs of 100 ms or more, the same 13**
the word timings give. So the detector is not broken. It is correctly calibrated for what it
was written for — generator-inserted silence, which is digital zeros — and wrong for
connected speech, which is not.

**The Track A extension, which is the more consequential half.** The same comparison was run
against the four committed source segments rather than inferred from the threshold sweep:

| segment | live gaps | sidecar | live ≥ 100 ms | longest live gap |
|---|---|---|---|---|
| `seed_seg0` | 16 | 1 | 6 | 560 ms |
| `seed_seg1` | 8 | 2 | 5 | 720 ms |
| `seed_seg2` | 9 | 1 | 3 | 320 ms |
| `seed_seg3` | 16 | **0** | 9 | 560 ms |
| total | **49** | **4** | 23 | |

**4 of 49.** The committed Track A source carries inter-word silences up to 720 ms that the
sidecar does not describe, several of them above `aggressive`'s 400 ms gate, and `seed_seg3`
contributes none at all. This is the defect CLAUDE.md §5 already records — "source-intrinsic
silences were undescribed in the sidecar, and every PCR figure in the run was wrong" — and
the fix applied then was partial: it caught the 4 and left the 45.

**What is and is not claimed.**

> **Corrected 2026-09-21 at Gate 8, measured. The sentence this paragraph opened with was
> wrong and Phase 4 will read this document, so it is replaced rather than annotated.** It
> read: "the `Endpointer` is driven by the sidecar's gaps, so it cannot fire where the
> sidecar is silent". It is not, and it can.
>
> `Endpointer.feed` detects silence **acoustically**, frame by frame, against
> `SILENCE_FLOOR_DBFS + vad_threshold × VAD_RANGE_DB`. It consults `regime_at` only to
> **select the gate** — `min_turn_silence` for `complete`, `max_turn_silence` for
> `fragment` — and `regime_at` falls back to `complete` where no gap covers the silence.
> So an undescribed silence does not go unmeasured. It is measured **under the wrong
> regime**: `min_turn_silence` judges a pause that `max_turn_silence` should have, on the
> narrower gate of the two on every arm (160 vs 400, 400 vs 1280, 800 vs 3600).
>
> Measured on the committed corpus: **45 boundaries per arm fire at silences no sidecar gap
> covers**, on all three static arms. On Track A all 45 begin within 14 ms of
> `final_word_end_ms` — the utterance-end silence starting one frame before the
> `utterance_end` gap — so the `complete` fallback happens to be the correct label there and
> no Track A figure is affected. That is a property of this corpus, not of the mechanism.
>
> `corpus._intrinsic_gaps`'s own docstring had it right all along and should have been read:
> "`regime_at` falls back to `complete` for every natural inter-word pause — so a pause in
> the middle of an utterance is scored as if the speaker had finished, and
> `min_turn_silence` governs where `max_turn_silence` should."

The simulated path is internally consistent in the narrower sense that remains true: **no
simulated figure is wrong through a disagreement with the audio**, because the same acoustic
floor drives the `Endpointer` and `_intrinsic_gaps`, so on Track A every silence the one can
detect the other has already described. The exposure is that the ground truth is incomplete,
and that Phase 4's live runs will fire boundaries inside silences the scorer does not know
exist. Gate 5's numbers are **not** re-derived here and **no direction is asserted** —
establishing the sign means re-running the regime labelling, which is Gate 7's work, not
this ADR's.

**The consequence for Track C is severe and is the reason ADR-034 exists.** The two floors
coinciding is what makes Track A safe, and Track C breaks it: `_intrinsic_gaps` finds almost
nothing on recorded speech (0 of 21 measured above), so a hesitant pause the `Endpointer`
*does* detect would find no gap covering it, fall back to `complete`, and be judged by
`min_turn_silence`. The gate Nod primarily moves is `max_turn_silence` (ADR-011), so it
would never bind, on any arm.

The stimulus was `say`, per the standing note that it is adequate for probing and not for
numbers. The direction of that limitation is knowable even where the magnitude is not: a
human recorded in a room carries room tone, breath and mic self-noise, all of which push
frame energy further above −44 dBFS. A real recording should score worse than 0 of 21, not
better. That is an argument and not a measurement, and it is the one the decision turns on.
Decision: **the sidecar's word timings are derived from a transcription pass, not from
acoustic silence detection.** Transcribe each corpus clip once at build time and record
`words[].start` and `words[].end` in the sidecar.

**On the ownership split, which is the part that needs arguing rather than assuming.**
ADR-017 refuses to let the harness supply its own input to the feature it then measures, and
the axis it protects is **regime labelling**, because that is the axis PCR scores. Regime
labelling stays generator-owned: a gap's `preceding` and `certainty` come from what the
generator inserted, or from `_intrinsic_gaps`'s standing rule that a source pause is
`fragment`/`ambiguous`. Nothing the service returns decides them. What moves to the service is
*what the words were and when* — a physical property of the audio, which the service measures
better than an energy threshold does, and which is not a question about what the right answer
is. On that reading the split is admissible, and it is accepted.

Two things sharpen it, and both are recorded so the precedent is not read wider than it is:
- **It is narrower than it sounds for Track A and total for Track C.** Generator-inserted
  gaps keep their exact generator-recorded durations; only the *intrinsic* gaps change hands.
  Track C has no generator-inserted gaps at all, so there the whole gap geometry becomes
  service-derived, and the regime labels come from the standing rule rather than from a
  record of an insertion.
- **"The service cannot influence PCR" would be too strong.** A gap's start and end reach
  PCR's arithmetic — whether a boundary fired inside a gap depends on where the gap is — even
  when the *label* on that gap does not come from the service. The defensible claim is
  narrower and is the one to cite: **the service cannot influence what counts as premature,
  only when the silence was.**
**The risk, stated plainly.** If a future change lets service output reach regime labelling —
`end_of_turn`, `end_of_turn_confidence`, or any model judgement about completeness deciding a
`preceding` value — ADR-017 is breached, and **this ADR is the precedent that will be cited to
justify it.** It does not justify it. The line is between measuring the audio and judging the
answer, and it is written here so that a later session has to argue past it rather than
through it.
Consequence: four, and the second is a gain rather than a side effect.

1. **The Track C gap arithmetic survives to the simulated path.** `n_gaps` becomes real words
   minus real turns, so `docs/TRACK_C_SCRIPT.md` §7's crossing turns hold offline as well as
   live, and the fluent condition — the control arm — can warm.
2. **Real token text removes ADR-028's floor.** §2.3's adjacent repeats, filler set and
   duration outliers score 0 on every clip today *because the reconstructed tokens are
   placeholders* `w0, w1, …`. A transcript-derived sidecar carries the words, so those three
   features become measurable offline for the first time, and `disfluency` stops being pinned
   at 0 in §4's `max_turn_silence` expression. **ADR-028 needs restating once this lands** —
   its claim that "only the Phase 4 live runs exercise the disfluency term at all" will no
   longer be true. Not restated here: it is restated against a working implementation, not
   against an intention.
3. **`replay._turn_from_gaps` stops reconstructing and starts replaying.** It exists to invert
   a gap list into a word sequence, one synthetic word per gap, because the simulator emits
   boundaries and the profiler needs timings. With real `Word` records in the sidecar it
   replays them instead, and the inversion — along with the reconstruction caveat in its
   docstring — goes away.
4. **Cost: one transcription pass per corpus clip at build time.** A key is needed to
   *regenerate* the corpus and not to run `make bench`, because the sidecar is committed
   exactly as the `.wav` files already are, so the clean-clone property of Phase 1's exit
   survives unchanged. One further cost worth recording because it feeds the control law:
   the service quantises word timings to **80 ms** — observed across both pilot clips, every
   gap a multiple of 80 — so gap durations, and therefore `g_p50` and `g_p90`, arrive on an
   80 ms grid where the 50 ms acoustic frame was nominally finer. Finer and wrong is worse
   than coarser and right, but the grid is real and a later reader should not be surprised
   by it.

**ADR-022's threshold of 24 was checked against the grid and does not move.** Its table was
derived on a continuous distribution, and P² markers behave differently when samples share
exact values, so the Gate 2 differential was re-run with every gap snapped to `round(g/80)*80`
over the same four shapes and the same `n` values. On the digit-reading shape the quantised
error is uniformly *lower* — 71.6 % against 74.9 % median at `n = 8`, 16.1 % against 17.5 % at
`n = 24` — because snapping concentrates the bulk cluster and P²'s markers settle sooner. The
crossing of ADR-022's criterion (median error in `max_ms` within one 397.5 ms hysteresis band
at the hesitant operating point) stays exactly where it was: fail at 8 and 16, pass at 20 and
24. The harness was validated by reproducing ADR-022's continuous column from a different seed
before the quantised column was believed. **24 stands and needs no re-derivation.**

Implementing this is **Gate 7 and needs approval**. No recording session is scheduled until
it lands: a Track C session run against the current ingestion path would produce a fluent arm
that never warms, and the script's gap budget is not recoverable from the audio afterwards.

## ADR-032 — 80 ms quantisation manufactures patches on the digit-reading shape
2026-09-19 · Status: accepted — measured and recorded, resolution deferred to Phase 4
Context: ADR-031 moved the sidecar's word timings to the service, and the service
quantises them to an 80 ms grid. ADR-022 derived `MIN_GAPS_FOR_WARM` against a continuous
distribution, so two things had to be checked before the change could be trusted: whether
the threshold still holds (it does — recorded in ADR-031, the crossing does not move), and
whether the grid can move the control law on its own. This is the second.

**The arithmetic, over the operating range rather than at one point.** One grid step in
`g_p90` is `1.6 × 80 = 128 ms` in `max_ms`, and the hysteresis band is
`HYST_FRACTION × reference max_ms`. The fraction of a band one step consumes is therefore
inversely proportional to where the controller is sitting:

| `g_p90` | `max_ms` | band | step / band | |
|---|---|---|---|---|
| 60 | 400 | 60.0 | **2.13** | `MAX_MS_FLOOR` |
| 160 | 506 | 75.9 | **1.69** | fluent, pilot-like gaps |
| 231 | 620 | 92.9 | **1.38** | the fluent shape's p90 |
| 377 | 853 | 128.0 | **1.00** | break-even |
| 645 | 1282 | 192.3 | 0.67 | `BASE_MAX_MS` |
| 1500 | 2650 | 397.5 | 0.32 | ADR-022's hesitant operating point |
| 2344 | 4000 | 600.0 | 0.21 | `MAX_MS_CEIL` |

**Break-even is `128 / 0.15 = 853 ms` of `max_ms`, which the law reaches at
`g_p90 = 377 ms`, and the whole fluent regime sits below it.** The intuition that one step
is comfortably inside a band is correct only at the hesitant end, which is where it was
first checked. With a context hint at the widest multiplier — `spelling` at 2.4 — the step
becomes 307 ms and reaches **5.12 × a band** at `MAX_MS_FLOOR`.

**Measured on the real arbiter**, full state machine, 60 turns × 6 words, 3000 streams per
shape, the same stream fed continuous and snapped:

| shape | cont. patches | quant. patches | manufactured | suppressed | per-turn rate |
|---|---|---|---|---|---|
| fluent | 12010 | 12010 | 115 | 115 | **0.064 %** |
| digit_reading | 13587 | 13811 | 1810 | 1586 | **1.006 %** |
| hesitant | 6109 | 6093 | 310 | 326 | 0.172 % |
| mixed | 8742 | 8628 | 1458 | 1572 | 0.810 % |

Manufactured and suppressed counts are close enough that the effect could be re-timing
rather than fabrication, so that was measured too rather than inferred:

| shape | identical patch count | Δ distribution | final `max_ms` identical |
|---|---|---|---|
| fluent | 99.5 % | −1: 8, 0: 2984, +1: 8 | 99.2 % |
| digit_reading | 77.5 % | −1: 178, 0: 2326, **+1: 361** | 58.9 % |
| hesitant | 96.6 % | −1: 54, 0: 2898, +1: 38 | 39.4 % |
| mixed | 75.9 % | −1: 396, 0: 2278, +1: 283 | 19.3 % |

**So it is dominated by re-timing and it is not only re-timing.** 77.5 % of digit-reading
streams end with the same patch count, which is the re-timing half; the delta is skewed
361 to 178 toward *more* patches, a net +1.6 %, which is not. The same arithmetic sits
benign at 0.064 % on `fluent`, where the near-perfect symmetry (115 against 115, 8 against
8) is what a pure timing jitter looks like. The mechanism on `digit_reading` is different
in kind: its 90th percentile sits on the cliff between the 180 ms cluster and the 1500 ms
cluster, so snapping reorders ranks across a discontinuity in the quantile function and the
two estimates land on opposite sides of it — measured pairs like (814, 1297) and
(1025, 591). That is not one grid step, and it is why the effect is largest on the shape
the project exists to serve.

**This is not a regression, and the reason is the whole point.** The live path has always
been quantised. Every one of the 70 inter-word gaps measured across the ADR-031 pilot clip
and the four Track A source segments was a multiple of 80 ms, so the profiler has consumed
grid-quantised input in every live session since P1. What was continuous was the
*simulated* path, because generator gap durations are floats. ADR-031 makes the simulation
match production. The behaviour recorded above is therefore a property of the **production
controller** that was invisible for as long as the bench ran on continuous input — a
measurement newly able to see something, not a change that broke something.
Decision: **record it, change nothing, and do not propose a fix here.** Three reasons, and
the first is ADR-026's: the evidence is simulated, and a constant governing how a real
controller responds to a real service's timing resolution is exactly the kind of question a
deterministic model cannot answer. The second is that the failure direction is mild — a
patch arriving one turn early or late, on a shape where the controller's job is to widen,
with the final `max_ms` identical on 58.9 % of streams and a median absolute difference of
0 ms. The third is that any plausible fix — snapping `g_p90` to the grid before the law
reads it, widening the hysteresis band at low `max_ms`, or quantising the law's output —
is a change to the control law, and CLAUDE.md §7 says the law is not tuned by ear and
ADR-026 says it is not tuned against the simulator either.
**What evidence would justify a fix**, so this is not re-litigated from the same figures:
a patch census from Phase 4's `N = 5` live runs showing either (a) a per-session patch
count materially above what the same sessions produce under a de-quantised counterfactual,
or (b) a measurable PCR or TTL difference attributable to the manufactured patches rather
than to their timing. Neither is obtainable from the simulator. If the live runs show the
net +1.6 % holding on real digit-reading speech, the candidate fix to evaluate first is
widening the band at low `max_ms` — the break-even table above says the exposure is
entirely in the fluent regime, so a floor on the band costs nothing at the hesitant end
where the controller does its work.
Consequence: `HYST_FRACTION` stays 0.15 and nothing in §4 or §5 moves. Phase 4 must collect
the patch census named above alongside ADR-027's per-session count — they are the same
measurement read two ways, and collecting them together costs nothing. Until then the
honest statement about the simulated chart is that its patch *timings* carry an 80 ms
grid's worth of jitter on bimodal pause profiles, and its patch *counts* are within 1.6 %
of what a continuous-timing controller would emit.

## ADR-033 — The `repeat` perturbation does not produce a transcribable repetition
2026-09-19 · Status: accepted — defect recorded, fix deferred past freeze
Context: `repeat` exists to put adjacent word repetition into the corpus, which is the first
of CONTROL_SPEC §2.3's three disfluency features. It has scored zero on every clip since the
corpus was built. ADR-028 attributed that to the harness handing the profiler placeholder
tokens `w0, w1, …`, which no comparison can ever see as a repeat. That cause was real and
ADR-031 fixed it. **The zero survived the fix**, which is how the second cause became
visible.

Measured at Gate 7 on the regenerated corpus, 12 `repeat` clips:

| kind | clips | adjacent repeats | fillers | duration outliers |
|---|---|---|---|---|
| `repeat` | 12 | **1** | 0 | 9 |
| all kinds | 120 | **1** | 0 | 43 |

**The perturbation is at fault, not the detector.** Three pieces of evidence, in the order
they settle it:

1. **The duplicated window is not a word.** `REPEAT_WORD_MS = 320` is a fixed slice taken at
   `at_ms = 0.4 × clip_length`, an offset computed from clip duration and chosen without
   reference to any word boundary — the generator had no word timings when it was written.
   In `seed_seg0_repeat_015` that window is 1950–2270 ms, and the transcript places the
   neighbouring words at **2160–2240 (`nod`)** and **2480–3200 (`capability`)**. So the
   window holds **210 ms of silence and 80 ms of one word.** The docstring's claim that this
   is "how much audio at `at_ms` counts as *the word*" was never true of this corpus.
2. **Duplicating it changes no token.** The three `seed_seg0` repeat clips at `times = 1, 2`
   and `3` transcribe **identically**: "this is a test recording for the nod capability probe
   i am reading at a normal pace". One, two or three copies of a mostly-silent fragment
   produce the same transcript as none.
3. **The detector works.** It caught the corpus's one genuine adjacent repeat,
   `seed_seg1_repeat_017`, token `then` — a clip where the fixed slice happened to land on a
   word. `_count_disfluency`'s token comparison is also driven directly by
   `tests/unit/test_replay_words.py`. A detector that finds the one real instance in 120
   clips is not the broken component.

**One cause masked another, and that is the part to carry forward.** ADR-028 diagnosed the
zero correctly and completely for the evidence available, and was still wrong about the
whole story, because a feature reading zero for one sufficient reason cannot show you a
second sufficient reason sitting behind it. The placeholder tokens guaranteed zero, so no
amount of staring at the zero could reveal that the audio would have produced zero anyway.
**Fixing the first cause is what made the second measurable** — and if Gate 7 had stopped at
"disfluency came off the floor", the repeat path would have been recorded as working.
Generalised: when a fix to a known cause does not move a number as far as expected, the
remaining gap is evidence of another cause, not noise. Ask what the number *should* have
moved to before fixing, so there is something to compare against afterwards.
Decision: **record it, do not fix it.** Seven days to freeze. `disfluency` is a secondary
term in §4's `max_turn_silence` expression (coefficient 0.45, against the pause quantiles'
primary path), the demo and the headline claim rest on the speaker axis, and Track A's
inability to warm the profiler (Gate 7 item 3: 0 of 120 clips reach `MIN_GAPS_FOR_WARM`)
means no `repeat` clip would reach the control law even with the feature working. Fixing it
now would improve a term that nothing currently reads.
**The fix is available and was not before**, which is worth stating so the next session does
not re-derive it: the sidecar now carries `words[].start` and `words[].end`, so `repeat` can
select a real word — take the word spanning `at_ms`, or the nearest one, and duplicate
exactly its span — instead of guessing 320 ms. That is a change to the generator, so it
invalidates the committed corpus and requires a rebuild and a re-transcription pass, which
is the other reason it is not a freeze-week change.
Consequence: `repeat` stays in the sweep and stays labelled `repeat`, contributing 12 clips
whose gaps are real and whose repetition is not. Any report that breaks disfluency down by
perturbation kind must say so rather than showing a 1. ADR-028's restatement records the
narrowed claim: the duration-outlier path is exercised offline, the repeat path is not, and
the filler path cannot be on clean synthetic read speech.

## ADR-034 — Transcript inter-word gaps are promoted to `Gap` records
2026-09-21 · Status: accepted — decided at Gate 8, before the Track C ingestion path
Context: Track C has no generator-inserted gaps at all (ADR-031), so `_intrinsic_gaps` is
the only existing producer of `Gap` records for it — and it is measured not to work on this
material. ADR-031's pilot found **0 of 21** inter-word gaps on connected `say` speech and
**4 of 49** across the four committed Track A source segments, because
`INTRINSIC_FLOOR_DBFS = -44.0` is calibrated for generator-inserted digital silence while
connected speech runs p05 −36.9 dBFS. A human recorded in a room carries room tone, breath
and mic self-noise, all of which push frame energy further above the floor, so the direction
on real audio is worse and not better.

**What an undescribed gap costs, stated correctly — this is the whole basis of the decision
and an earlier framing of it was wrong.** The cost is **not** that nothing fires there, and
it is not a missing measurement. `Endpointer.feed` detects silence acoustically and consults
`regime_at` only to select the gate; `regime_at` falls back to `complete` where no gap covers
the silence. So an undescribed hesitant pause is measured, and measured **under the wrong
regime**: `min_turn_silence` judges a pause that `max_turn_silence` should have. That is the
narrower gate on every arm — 160 against 400, 400 against 1280, 800 against 3600 — so the
endpointer cuts *earlier* than the ground truth says it should, and it does so precisely at
the mid-utterance pauses the project exists to protect. ADR-031's phrasing to the contrary is
corrected in place above; `corpus._intrinsic_gaps`'s docstring had it right from the start.

**On Track A this is harmless and on Track C it is fatal**, for one reason: the `Endpointer`
and `_intrinsic_gaps` share the same −44 dBFS floor and the same 50 ms frames, so on Track A
every silence the one can detect the other has already described, and the fallback is never
reached mid-utterance. Track C breaks the coincidence. The detector finds almost nothing on
recorded speech, the `Endpointer` still detects whatever silence clears the floor, and every
such pause would fall through to `complete`. `max_turn_silence` — the gate Nod primarily
moves (ADR-011) — would never bind on any arm, and the corpus would report a controller that
cannot act because the knob it turns is not the knob being consulted.

The geometry is the easy half and ADR-031 already solved it: the sidecar carries
`words[].start` and `words[].end`. The hard half is the **label**. On Track A a gap's
`preceding` and `certainty` come from a record of what the generator cut. Track C has no
such record, so the label has to come from somewhere, and the three candidates are not
equally admissible.

**Rejected — the script's declared structure.** `docs/TRACK_C_SCRIPT.md` knows which turn is
which and could in principle say where a clause ends. This is hand-authored ground truth
with no source to mutate, which ADR-029 names as the hardest of the three places for a
flattering-direction error to hide: "there is no source to mutate, no test that goes red,
and no assertion to make vacuous. A mislabelled `expected_answer` is one word in a markdown
table, it is plausible, it survives every gate in the repository, and it moves a published
number." A hand-placed `complete` label is the same object, and it would be placed by the
person who wants the controller to look good.

**Rejected — the service's completeness judgement.** `end_of_turn`,
`end_of_turn_confidence`, or any model judgement about whether the caller had finished.
This is exactly what ADR-017 forbids, and ADR-031 anticipated it being proposed here and
refused to license it in advance: "If a future change lets service output reach regime
labelling … ADR-017 is breached, and **this ADR is the precedent that will be cited to
justify it.** It does not justify it." Cited and declined.

**Accepted — the standing rule, with the cost stated rather than minimised.**
`_intrinsic_gaps` already labels every source silence `fragment`/`ambiguous`, on the
reasoning that the speaker is by construction mid-utterance and that a natural pause can
fall where a clause ends without the detector being able to tell. The same rule extends to
a transcript-derived gap for the same reason and with the same blind spot.

Its cost, plainly: **it labels a genuine clause-end pause as a fragment.** That is wrong,
it is wrong uniformly, and the direction is knowable — a `fragment` label selects
`max_turn_silence`, which is the wider gate on every arm (400 vs 160, 1280 vs 400,
3600 vs 800), so a mislabelled clause end makes the endpointer wait *longer* there than it
should. The error therefore inflates TTL and suppresses cutoffs — it runs **against** the
project, in the same rare direction ADR-028 records, and not for it. That is why it is
tolerable where a hand-placed label is not.
Decision: **every transcript inter-word gap becomes a `Gap` record, labelled `fragment` /
`ambiguous` by the standing rule, deduplicated against gaps already described** — the same
`any(g.start_ms <= start <= g.end_ms for g in described)` guard `_intrinsic_gaps` uses, so a
generator-inserted gap keeps its own generator-owned label and is never shadowed.
**Every transcript-derived gap carries `certainty="ambiguous"`**, without exception and
regardless of how the pause reads, because the rule that produced the label cannot tell a
clause end from a fragment and the field exists to carry exactly that doubt.

**Against ADR-017, explicitly, as ADR-031 required.** The split is: **the service supplies
geometry, the rule supplies the label.** `words[].start` and `words[].end` are a physical
property of the audio, measured better by the service than by an energy threshold, and they
answer *when the silence was*. `preceding` and `certainty` answer *which gate governs it*,
and they come from a standing rule written in this repository, applied uniformly, with no
input from the service whatsoever. The defensible claim is ADR-031's, unchanged and now
load-bearing: **the service cannot influence what counts as premature, only when the silence
was.** What would breach it: `preceding` taking any value from `end_of_turn`,
`end_of_turn_confidence`, punctuation, casing, or any other model judgement about whether
the caller had finished; or `certainty` being set to `certain` on a transcript-derived gap
on the strength of the transcript reading like a complete sentence. Both would move
regime labelling into service hands, which is the axis PCR scores and the axis ADR-017
exists to protect. Neither is licensed here, by this ADR or by ADR-031.
Consequence: four, and the second is measured rather than asserted.

1. **This also closes the incomplete-ground-truth exposure on Track A**, which ADR-031's
   Gate 7 correction left open in as many words: "none of the ~16 transcript inter-word gaps
   per clip became a `Gap` … the incomplete-ground-truth exposure this ADR records is still
   open." The same promotion applies to Track A and closes it. Measured on the committed
   corpus: **882 transcript inter-word gaps, 81 already covered by a described gap, 801 newly
   described**, median 240 ms, p90 720 ms; 120 of the 801 exceed `aggressive`'s 400 ms gate
   and 7 exceed `balanced`'s 1280 ms.

2. **The predicted effect on the simulated Track A figures is exactly none, and that
   prediction is recorded before the run** (ADR-033: ask what the number should move to
   before fixing, so there is something to compare against). The reasoning, which also
   corrects a claim made twice:

   Promotion changes the `Endpointer` only where it flips a silence the detector **actually
   sees** from `complete` to `fragment`, moving it from the min gate to the max gate. On
   Track A there is nowhere for that to happen, and the reason is the shared floor: the
   `Endpointer` and `_intrinsic_gaps` both threshold at −44 dBFS over 50 ms frames, so every
   mid-utterance silence the one can detect the other has already described and labelled.
   The 801 newly described gaps sit at intervals below that floor, where no silence run
   accumulates and `regime_at` is never reached.

   The 45 undescribed-silence boundaries are not the counter-example they look like: all 45
   begin within 14 ms of `final_word_end_ms` — the utterance-end silence starting one frame
   before the `utterance_end` gap — so the `complete` fallback is the *correct* label there,
   and transcript gaps lie strictly between words, so promotion does not cover them anyway.
   Both facts are properties of this corpus. Neither generalises to Track C, where the floors
   do not coincide.

   **Predicted: PCR, TTL and FRAG unchanged on all three static arms — `aggressive` 0.642,
   `balanced` 0.317, `conservative` 0.000.** The closure is real and pays at Phase 4, where
   the live service hears a 720 ms inter-word gap that a −44 dBFS threshold cannot; offline
   it is invisible.

   > **Measured at Gate 8, and the prediction was wrong the first time. The failure is
   > worth more than the eventual agreement, so it is recorded rather than overwritten.**
   >
   > First run, promotion applied to Track A in memory: PCR and FRAG unchanged exactly as
   > predicted, and **TTL p90 moved on every arm** — 186 → 396, 426 → 1276, **826 → 3596**.
   > Each delta is almost exactly `max_turn_silence − min_turn_silence`, which says the
   > utterance-end boundary had switched gates on every clip.
   >
   > **Cause: the dedup tested containment of the promoted gap's *start*, and `regime_at`
   > returns the *first* gap covering a time.** In `seed_seg0_pause_000` the transcript's
   > last word runs 5040–5120 while the acoustic `final_word_end_ms` is 5024, so the
   > promoted gap 4720–5040 **overlaps** `utterance_end` (5024–9274) by 16 ms without
   > starting inside it. It was admitted, it sorted first, and every utterance end was
   > relabelled `fragment` — a label that is simply wrong once the speaker has finished.
   > `corpus._intrinsic_gaps` guards the same hazard with `start >= final_ms`; that guard
   > was not carried over.
   >
   > Fixed by deduplicating on **overlap** rather than containment. 801 candidate gaps
   > become **697**; the 104 refused are the ones clipping a described gap's edge.
   > Re-measured: **PCR, TTL p90 and FRAG identical on all three arms**, 369 → 1066 gaps.
   >
   > Two things this cost and one it bought. It cost a prediction stated with more
   > confidence than the code had earned. It also went **undetected by 21 passing tests**,
   > because every one of them used non-overlapping fixtures — the overlap only arises
   > where the acoustic and transcript estimates of the utterance end disagree, which no
   > hand-written fixture had. `test_a_promoted_gap_that_only_clips_a_described_one_is_
   > still_refused` now covers it and a mutation guards it. What it bought is the general
   > lesson, which is ADR-033's read forwards instead of backwards: **the prediction is
   > what made the defect visible.** An unpredicted TTL move on a metric nobody was
   > watching would have shipped.

   **And an identity here proves nothing on its own.** This is the shape that hid the
   `run_nod_clip` defect (CLAUDE.md §5) — a predicted-and-observed match produced equally by
   a correct no-op and by a promotion that never ran. So the promotion must be demonstrated
   separately: the sidecar gap counts must be asserted to have changed, and a test must show
   a silence the Endpointer *can* see flipping gate under promotion and going red without it.

3. **`certain_only` collapses to an empty scope on Track A**, and this is a blocker rather
   than a note. `ScoredUtterance.certain` requires *every* gap in the utterance to be
   `certain`; 15 of 120 clips qualify today; after promotion all 120 carry at least one
   ambiguous gap, so the scope is 0 utterances and `pcr(certain_only=True)` raises
   `ValueError("no utterances in scope: PCR of nothing is not 0.0")` — which `replay.main`
   calls unconditionally. **Settled at this gate by ADR-036**, which scopes `certain_only`
   per gap rather than per utterance, so the column survives promotion and the
   "report with and without" this ADR's argument leans on remains available. Without it this
   ADR would assert that the standing rule's doubt is measurable while shipping a
   measurement that cannot exist.

4. **Track C's gap geometry becomes wholly service-derived**, as ADR-031 predicted it would.
   There is no generator record to fall back on, so every `Gap` in a Track C sidecar is a
   transcript gap under the standing rule, plus the seams and the utterance ends. The whole
   corpus is therefore `ambiguous` by construction, and any Track C figure must be reported
   as such rather than alongside a certain-only column that cannot exist.

## ADR-035 — Phase 3's exit criterion, restated against the cut list
2026-09-21 · Status: accepted — supersedes ROADMAP.md Phase 3's exit bullet as written
Context: ROADMAP Phase 3 exits on four clauses — "a full call runs end to end in the
browser. The Floor Meter visibly grows on a hesitant caller. Voice switches mid-call
without dropping. Replay mode runs from a committed trace with no API key." Freeze is
26 Sep. Measured at Gate 8: `console/` does not exist, `src/nod_adapters/llm/` and
`src/nod_adapters/tts/` contain `__init__.py` and nothing else, and `TelemetryHub.publish`,
`TelemetryHub.subscribe`, `ws.stream_endpoint`, `ws.console_endpoint` and
`app.create_session` all raise `NotImplementedError`. The controller itself is done and
already emits every record the screen needs.

Two of the four clauses are not reachable as written, and one of them contradicts the
standing cut list rather than merely exceeding the time available. Restating the criterion
is better than missing it silently: §3's cut list is the mechanism for this, and a clause
that survives only by being quietly reinterpreted on the 26th is a criterion that cannot
fail.
Decision: the exit criterion is the four clauses below, replacing the ROADMAP's.

1. **A full call runs end to end in the browser — thinner, and the thinning is named.**
   Browser TTS only, one LLM call, **no filler on slow LLM, no false-barge recovery**.
   Barge-in is attempted and is **at risk**; if it goes, the clause still passes without it
   and the demo says so. This is ROADMAP Phase 3's first bullet minus its second half, and
   the deleted half is where the week went.

2. **The Floor Meter visibly grows on a hesitant caller — full strength, not thinned.**
   §3 survivor 3 is "one demo screen … the live call view with the Floor Meter, the config
   strip and the reason line", and this clause is the only evidence the loop closes. It is
   cut last and it is cut after clause 1, not before. One precondition, recorded because it
   is cheap in advance and expensive to discover on camera: the capsule only grows when the
   profiler warms, which needs 24 gaps by `words − turns` (ADR-022), so **the demo call must
   be scripted to the gap budget** exactly as a Track C script is — five turns of six words
   crosses on turn 5.

3. **Voice switching is dropped**, not restated. §3 contingent cut 4 is "Cloud TTS
   providers, keep `browser` only", which leaves no second provider to switch *to*, and
   ARCHITECTURE §8's resynthesis of the unspoken remainder is separate work on top. The
   available restatement — switching between two *browser* voices — was considered and
   rejected: it demonstrates the mechanism while supporting none of the claim, and a demo
   that shows a voice changing proves nothing a viewer cares about. Better to drop a clause
   than to pass it on a technicality. `POST /v1/sessions/{id}/voice` stays a stub and
   `Voice.pacing_hint_ms` stays wired to the arbiter ceiling for the post-freeze path.

4. **Replay mode runs from a committed trace with no API key — thinner, and downstream of
   clause 1 rather than parallel to it.** This is the dependency the ROADMAP had inverted,
   and it is the reason this ADR exists rather than a note in the report. The ROADMAP lists
   the four clauses as independent deliverables, which reads as four things that can be
   built in parallel and cut independently. They cannot: **there is no committed trace to
   replay.** Measured — 118 `.jsonl` traces are committed across `data/traces*` and
   `tests/fixtures/traces`, and **zero contain a `config_decision` record.** The seed
   fixture's frame kinds are `frame` (406), `event` (50), `sent` (2), `meta`, `feed_report`
   and `verdict`; it is an ADR-001 probe stimulus, which ADR-017 already says in as many
   words. A replay view has nothing to render until a live `SessionProxy` call has produced
   a trace carrying `config_decision`, `config_applied` and the turn stream — which is
   clause 1. So clause 4 cannot start before clause 1 finishes, and if clause 1 is cut,
   clause 4 goes with it automatically.
   Thinned to: **one pane replaying one committed Nod trace with no API key.** The two-pane
   stock-vs-Nod view with a shared scrubber is cut — it is a second screen, against §3's
   "one screen that shows the loop closing, not three".
Consequence: three. **The demo video loses the voice switch**, which was never one of §3's
four survivors and costs the pitch little. **Producing the committed trace becomes a named
deliverable of clause 1**, not a by-product — record one scripted hesitant call, commit the
trace, and clause 4 is most of the way done. And **the ordering is now explicit**: 1 → 2 → 4,
with 3 gone, so cutting from the end of that chain on the 26th degrades the demo gracefully
instead of leaving a half-built screen. If the whole chain is at risk, clause 2 is what
survives, because it is the only one of the four that §3 lists among the four that ship.

## ADR-036 — `certain_only` scopes per gap, not per utterance
2026-09-21 · Status: accepted — supersedes the utterance-level rule in ADR-017's reporting
Context: ADR-034 promotes every transcript inter-word gap to a `Gap` and marks all of them
`ambiguous`. `ScoredUtterance.certain` required **every** gap in an utterance to be
`certain`, so promotion empties the scope: 15 of 120 Track A clips qualified before, and
after promotion all 120 carry at least one ambiguous gap. `pcr(certain_only=True)` then
raises `ValueError("no utterances in scope")`, which `replay.main` calls unconditionally, so
`make bench` would have crashed.

**The crash is the smaller problem.** "Report with and without" is load-bearing in ADR-034's
argument, not decoration: the standing rule's uniform mislabelling of clause ends as
fragments is tolerable *because* `Gap.certainty` carries the doubt and a reader can see how
much of a figure rests on it. A column that is empty by construction leaves that argument
unsupported — ADR-034 would be asserting that the doubt is measurable while shipping a
measurement that cannot exist.

**The old rule was correct for the corpus it was written against and stopped meaning
anything.** With one or two gaps per utterance, "every gap is certain" and "the gap that
decided this is certain" are nearly the same question. At a dozen gaps they are not: a
single ambiguous pause four seconds away from any boundary excluded an utterance whose
verdict never consulted it. The rule did not become wrong — the corpus grew out from under
it, which is the same shape as ADR-022 and ADR-023 being derived under constants the other
one changed.
Decision: **an utterance is in the certain scope iff every gap that *governed a boundary
attributed to it* is labelled `certain`.** Three parts, each with a reason:

- **The lookup is at the silence start, not the fired time**, matching
  `fake_assemblyai.regime_at` exactly. A boundary fires at `silence_started + threshold`,
  which is routinely past the end of the gap that governed it, so looking up at the fired
  time would read the wrong gap or none.
- **A boundary no gap covers counts as ambiguous.** Its regime came from `regime_at`'s
  `complete` fallback, which is a rule this repository applies and not a datum the sidecar
  recorded. Track A has 45 of these per arm (ADR-034).
- **An utterance that emitted no boundary is certain.** Nothing was judged, so no label was
  relied upon, and PCR's verdict of "not premature" rests on no proxy.

`ClipObservation` gains `emitted_silence_start_ms`, parallel to `emitted_end_ms` and
validated to the same length. Asking for `certain_only` on an observation that emitted
boundaries without it raises `MissingSilenceStartsError` rather than falling back to the old
rule: a silent fallback would report a number computed under a definition the caller did not
ask for.

**The metric's raise is unchanged.** `pcr` and `frag` still refuse an empty scope — "PCR of
nothing is not 0.0" is their job. The guard lives in `replay.main`, which is the only caller
that knows a missing column is reportable rather than fatal, and it records `None` in
`ProxyDivergence` — not `nan`, not `0.0`, because an empty scope is the absence of a
measurement and a float there would turn a missing record into a confident one.
Consequence: four.

1. **Measured on the current corpus, before promotion: the certain scope moves from 15
   utterances to 23**, `pcr_certain_only` 0.0 and `frag_certain_only` 1.0. The column stays
   informative instead of collapsing, which is the whole point. That 23 is dominated by
   utterances whose only boundary landed in the `certain` `utterance_end` gap, so PCR of 0.0
   over it is expected rather than surprising — a premature cutoff inside a `certain` gap is
   possible (the `repeat` gaps are certain and mid-utterance) and simply does not occur here.

2. **This is a published metric's definition, so it carries first-tier mutation discipline**
   (CLAUDE.md §5, "anything that feeds a published number"). The scoping rule is in
   `tools/mutate.py`'s catalogue for `metrics`.

3. **Three tests were confirmed red under the replaced rule**, by reverting `_selected` to
   `all(g.certainty == "certain" for g in utterance.gaps)` with a source-hash assertion and a
   `__pycache__` purge: `test_an_ambiguous_gap_that_governed_no_boundary_does_not_exclude`
   (0 == 1), `test_an_utterance_whose_boundary_no_gap_covers_is_not_certain` (1 == 0) and
   `test_an_utterance_that_emitted_nothing_is_certain` (0 == 1). They fail in **both**
   directions, which is what distinguishes a definition change from a loosening.

4. **`ProxyDivergence`'s rates and denominators came from different pools. Corrected at
   this gate**, after being recorded here as a deferred decision and then taken.
   `pcr_all` and `frag_all` were computed over all six arms' utterances while
   `utterances_all` reported **120** — one arm's observation count — against a rate whose
   pool was **720**. `utterances_certain_only` had the same split, reporting **23**.

   **The old values are named here so a later reader does not mistake the change for the
   pool having changed.** It did not. The manifest now reads `utterances_all` **720** and
   `utterances_certain_only` **290**, over exactly the pool the rates use. No rate moved:
   `pcr_all` is 0.318 before and after.

   Two reasons it was fixed rather than left. The field **contradicted `pcr`'s own
   documented rule** — "Never report it without its denominator" — by printing a
   denominator that belonged to a different population, which is worse than printing none.
   And **nothing is published yet**: INV-9 routes every published figure through Phase 4's
   live runs, so correcting it now costs a manifest regeneration and correcting it later
   would mean retracting a number.

   Two further defects fell out of the same read and are fixed with it: `utterances_all`
   counted **observations**, not utterances, which is identical only while every clip holds
   exactly one — true of Track A and false of Track C, where a clip holds ten to twelve —
   and `utterances_certain_only` was counted for `arms[0]` alone rather than the pool.
   `total_utterances` now does the counting and carries a mutation.
