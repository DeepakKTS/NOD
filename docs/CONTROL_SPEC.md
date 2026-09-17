# Nod — Control specification

This is the authoritative description of the control law. Implementation must match it
line for line, and any change to a constant here is an ADR, not a commit.

## 0. The two facts the whole design rests on

1. **Configuration can be changed mid-session.** `UpdateConfiguration` applies without
   reconnecting. Measured at P1: `min_turn_silence` and `max_turn_silence` take effect
   mid-stream, each landing where the same value set at connect time landed.
   `end_of_turn_confidence_threshold` is accepted and ignored, and `vad_threshold` is
   unproven. The server answers a successful update with silence, so acceptance is never
   evidence: only a measured change in boundary timing counts (ADR-001).
2. **Silence is the only axis, and which silence knob binds depends on the utterance.**
   Measured at P1 on `universal-streaming-english` (ADR-011, ADR-001):
   `end_of_turn_confidence_threshold` is inert — arms at the documented endpoints 0.0 and
   1.0 produced boundaries 13 ms apart where the documentation predicts 2800 ms, and turns
   end at confidence 0.308 against a threshold of 0.95. Endpointing runs on two silence
   gates:

   - after a **semantically complete** utterance the model's own gate fires and
     `min_turn_silence` decides when;
   - after an **incomplete** one it keeps waiting, and `max_turn_silence` is the only
     thing that ends the turn.

   The second is the case Nod exists for: a caller pausing mid-sentence has produced an
   incomplete utterance. `max_turn_silence` is therefore the primary control surface.
   Every implementation of this spec must have a test that would fail if
   `max_turn_silence` were not moved when an incomplete utterance needs tolerating (§9).

## 1. Inputs

Per partial and final `Turn` event:

| Field | Use |
|---|---|
| `turn_order` | ordering, cut detection |
| `end_of_turn` | boundary marker |
| `end_of_turn_confidence` | logged feature only, weight 0 (§2.4). Never an output target |
| `words[].start`, `words[].end` | pause profile, speech rate (stream-relative ms) |
| `words[].confidence` | disfluency proxy |
| `words[].word_is_final` | which words are safe to process; unfinalised last word is skipped |
| `transcript` | token-level disfluency features only |

From the host application (context axis, mode B and C only):

| Field | Use |
|---|---|
| `expected_answer` | one of `free`, `boolean`, `entity_id`, `entity_date`, `entity_address`, `entity_list`, `spelling`, `number` |
| `prompt_id` | policy lookup and trace correlation |

## 2. Features

All updated incrementally, all `O(1)` per word or per turn.

### 2.1 Pause profile
Inter-word gap `g_i = words[i].start - words[i-1].end`, computed only over finalised
words within a turn. Gaps are clamped to `[0, 6000]` ms before ingestion to stop one
pathological silence from poisoning the estimator.

Maintain P² estimators for `g_p50` and `g_p90`. Require `n_gaps >= 8` before either is
consulted; below that, the profiler reports `cold`.

### 2.2 Speech rate
`rate = finalised_words / voiced_ms * 1000`, where `voiced_ms = Σ(word.end - word.start)`.
EWMA with `α = 0.2`. Used only for the sanity clamp in §4.

### 2.3 Disfluency density
Per turn, count:
- adjacent repeated normalised tokens (`the the`, `I I`),
- tokens in the filler set (`um, uh, er, like, you know, hmm`) — configurable per locale,
- duration outliers: `(word.end - word.start) > 2.5 × median_duration_for_length(len(text))`
  where the median table is a fixed five-bucket lookup by character length.

`disfluency = clamp(count / max(finalised_words, 1), 0, 1)`, EWMA `α = 0.3`.

### 2.4 Confidence jitter — retained, weight 0
Welford running variance over the partial-sequence `end_of_turn_confidence`. Output
`jitter ∈ [0,1]` by normalising against a constant, updated `O(1)` per partial.

**It carries no control authority.** §2.4 was written assuming the field measures speaker
uncertainty. The P1 probe falsified that: the value sits near zero throughout an utterance
(0.00001–0.004 across partials) and spikes only on the frame that *is* the boundary, so it
reports turn-completion probability, not hesitation. Its variance across partials is as
likely to be transcription instability as anything about the speaker.

It is computed and logged so the bench can evaluate it, and weighted 0 in §4. It earns
control authority only if `make bench` shows it helps, which is an ADR (ADR-011).

### 2.5 Cut detection (the unsupervised label)

A **cut** is recorded when all of the following hold:

1. Turn `n` ended (`end_of_turn = true`), and
2. Turn `n+1` begins with `first_word.start - last_word_end(n) < RESUME_MS` (default 1200), and
3. The agent had **not** produced audio for turn `n` before turn `n+1` started, or had
   produced less than `AGENT_GRACE_MS` (default 300) of audio, and
4. Turn `n+1`'s first token is not an affirmation or a new-topic marker
   (`yes`, `no`, `correct`, `wait`, `sorry`) — those indicate a genuine new turn or a
   correction.

**Condition 5 was dropped, deliberately and with its reason recorded.** It admitted a
candidate cut when turn `n`'s `end_of_turn_confidence` was below `CUT_CONF_MAX` (0.85).
The 69-session P1 matrix measured 184 real boundaries: **144 of them (78 %) fall below
0.85**, median 0.443. A condition that admits four turns in five is not discriminating, and
leaving it in would suggest the label is better guarded than it is. Conditions 1 to 4 carry
the whole discrimination.

(These figures supersede the 67-of-75 at median 0.352 quoted before the clean matrix. The
sample is larger and spans both regimes; the conclusion is unchanged.)

Conditions 3 and 4 exist because the naive rule produces false positives every time a
caller answers quickly or corrects themselves. Every condition here has a regression
test with a hand-labelled fixture.

`recent_cuts` = count of cuts in the last `CUT_WINDOW` (default 5) turns, normalised to
`[0,1]` by dividing by 3 and clamping.

## 3. The context axis

A policy file compiles to `expected_answer → WindowHint`:

```yaml
version: 1
default: {min_mult: 1.0, max_mult: 1.0}

  boolean:        {min_mult: 0.7, max_mult: 0.7}
  free:           {min_mult: 1.0, max_mult: 1.0}
  number:         {min_mult: 1.2, max_mult: 1.6}
  entity_id:      {min_mult: 1.3, max_mult: 2.0}
  entity_date:    {min_mult: 1.2, max_mult: 1.8}
  entity_address: {min_mult: 1.3, max_mult: 2.0}
  entity_list:    {min_mult: 1.4, max_mult: 2.2}
  spelling:       {min_mult: 1.5, max_mult: 2.4}
```

Applied for exactly one turn, then released. A context hint never persists into the
speaker profile; the two axes are combined at decision time and stored separately.

Rationale for widening on identifiers: a caller reading a member number pauses between
groups of digits, and those pauses are structural rather than personal.

## 4. The control law

```
base_min   = 400     # ms, balanced starting point
base_max   = 1280    # ms

# speaker axis — max_turn_silence is the primary surface (ADR-011).
# disfluency and recent_cuts route here because a mid-sentence pause is an
# incomplete utterance, and that is the regime max_turn_silence governs.
min_ms  = 0.6 * g_p50 + 120
max_ms  = (1.6 * g_p90 + 250) * (1 + 0.45 * disfluency + 0.15 * recent_cuts)

# jitter is computed and logged but weighted 0 until the bench earns it (ADR-011).

# context axis — lands on min_ms, which governs responsiveness after a
# complete utterance. Wide-answer classes widen max_ms as well, because a
# caller reading an id pauses mid-utterance.
min_ms  *= hint.min_mult
max_ms  *= hint.max_mult

# clamps (hard, absolute)
min_ms  = clamp(min_ms, 160, 900)
max_ms  = clamp(max_ms, 400, 4000)

# invariant repair
max_ms  = max(max_ms, min_ms + 200)

# latency ceiling. ENDPOINT_OVERHEAD_MS is measured by `make bench`, never
# hand-written (INV-9): the boundary lands that long after the configured gate,
# so a ceiling applied to the knob alone is overshot on every turn.
max_ms  = min(max_ms, ceiling_ms - ENDPOINT_OVERHEAD_MS)
```

`end_of_turn_confidence_threshold` is never emitted. The capability gate in §5 still
consults it, so a model that honours the field can be re-enabled by a future ADR without
re-litigating this law (ADR-011).

If the profiler is `cold` (`n_gaps < 8`), the speaker axis is skipped entirely and only
the context axis applies to the base values. Adaptation begins at roughly turn three.

### Confident early endpoint
When the profiler is warm, the caller's trailing gap already exceeds
`max(max_ms, g_p90 * 1.8)`, and the last word is finalised, the controller may send
`ForceEndpoint` rather than waiting out the silence. Rate-limited to once per turn and
disabled entirely when `disfluency > 0.35`, because a disfluent speaker is exactly the
person whose long gap is not a finished turn.

## 5. Guards

| Guard | Rule | Reason |
|---|---|---|
| **Hysteresis** | emit only if any field moves more than `HYST = 15 %` of its current value | prevents socket chatter |
| **Rate cap** | at most 1 patch per turn, at most `MAX_PATCHES = 24` per session | bounds cost and blast radius |
| **Asymmetric decay** | widening applies immediately; narrowing applies at most `NARROW_STEP = 12 %` per turn | one stumble must not make the agent permanently slow, and one crisp answer must not immediately re-expose the caller to cutting |
| **Ceiling** | `max_ms` never exceeds `ceiling_ms - ENDPOINT_OVERHEAD_MS` | **Unenforced pending a measured overhead.** `ENDPOINT_OVERHEAD_MS` is still 0, so the subtraction does nothing and the guard holds only arithmetically: the boundary arrives some way *after* the configured gate, so a `max_ms` clamped exactly to `ceiling_ms` overshoots the ceiling on every turn. The P1 matrix measured that lag as consistently positive and well outside the noise across every plain silence-gate cell. Until `make bench` supplies the value (INV-9 — it is not written here), do not rely on this guard to keep a fluent caller from waiting |
| **Floor on boolean turns** | on `boolean`, `min_ms` never exceeds 400 | yes/no must stay snappy |
| **Freeze on instability** | if 3 patches in 5 turns all reverse direction, freeze the speaker axis for 10 turns and emit `controller_frozen` | detects oscillation instead of thrashing |
| **Host override** | if the host application sent its own `UpdateConfiguration` in the last 5 s, Nod does not touch the fields the host set | the host owns its own decisions |
| **Capability gate** | only a field the probe marked `LIVE` is ever sent | `STATIC_ONLY`, `INERT` and `UNPROVEN` all fail closed; `end_of_turn_confidence_threshold` is currently `INERT` (ADR-001) |

## 6. State machine

```
COLD ──(n_gaps ≥ 8)──► WARM ──(3 reversals in 5 turns)──► FROZEN ──(10 turns)──► WARM
  │                      │
  └──────────────────────┴──(controller_error)──► SAFE ──(next turn)──► COLD
```

- `COLD`: base config plus context axis only.
- `WARM`: full law.
- `FROZEN`: speaker axis held, context axis still applies.
- `SAFE`: last known good config, no patches, error counted. Entered on any exception.
  Per INV-8 the call continues.

State transitions are logged and appear in the trace and on the console strip.

## 7. Degradation matrix

| Missing capability | Behaviour |
|---|---|
| No `end_of_turn_confidence` in events | jitter feature disabled. No effect on any output: jitter is already weight 0 (ADR-011) |
| `end_of_turn_confidence_threshold` not updatable | none. The field is never sent, so this is the expected state on `universal-streaming-english`; emit `capability_degraded` once for the record |
| `max_turn_silence` not updatable | the primary surface is gone. Fall to `observe` mode and surface it: the incomplete-utterance regime cannot be controlled at all |
| `min_turn_silence` not updatable | context axis has no output; speaker axis continues on `max_turn_silence` alone |
| No word timings | profiler disabled entirely; controller runs context axis only; emit a loud warning |
| `UpdateConfiguration` rejected | one retry, then fall to `observe` mode for the session and surface it on the console |
| `ForceEndpoint` unsupported | early-endpoint path disabled |

## 8. Tuning

The constants above are the starting point, not the answer. They are tuned by
`nod tune`, which sweeps them against the bench corpus and reports the Pareto frontier.
Hand-tuning by listening is forbidden (see CLAUDE.md §7). Any constant change lands with
the bench delta in the commit message.

## 9. Property tests (must exist)

Written with `hypothesis`, over arbitrary feature vectors:

1. Output is always within the hard clamps.
2. `max_ms >= min_ms + 200` always holds.
3. `max_ms <= ceiling_ms - ENDPOINT_OVERHEAD_MS` always holds. Note this is currently
   vacuous: with `ENDPOINT_OVERHEAD_MS` at 0 it reduces to `max_ms <= ceiling_ms`, which
   the clamps already give. It becomes a real constraint only once the bench measures
   the overhead (§5, Ceiling).
4. Monotonicity: increasing `disfluency` with everything else fixed never decreases
   `max_ms`.
5. Idempotence: `decide()` on the same state twice returns an equal patch and the second
   emits nothing after hysteresis.
6. Narrowing never exceeds `NARROW_STEP` in one turn.
7. `decide()` performs no I/O — asserted by monkeypatching `socket` and `open` to raise.
8. On `boolean` context, `min_ms <= 400`.
9. **The incomplete-utterance test.** Given a warm profile whose `g_p90` implies a pause
   longer than `base_max`, `decide()` must widen `max_turn_silence`. This replaces the
   test that used to require both axes to move, which is now vacuous because there is only
   one axis. It is the test that fails if a future change quietly stops moving
   `max_turn_silence`, and with it the only regime in which a mid-sentence pause can be
   tolerated (§0 fact 2, ADR-011).
10. `jitter` has no effect on any output: two states differing only in `jitter` produce an
    equal patch. This pins ADR-011's weight-0 decision so that restoring the weight is a
    deliberate change with a failing test, not a silent one.
