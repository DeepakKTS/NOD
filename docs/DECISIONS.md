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

**Four instances now, and the pattern is worth naming.** The first three were each two
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
The lesson is procedural rather than editorial — a spec review would not have caught any of
them. For the first three, because each document is right on its own; for the fourth,
because prose review reads a code block for intent and not for syntax. They are found by
building the consumer. A spec fragment that nothing parses is untested code that happens to
live in a document.

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
