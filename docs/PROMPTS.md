# Nod — Claude Code prompt sequence

Copy these in order. One prompt per session where possible; start a fresh session at each
phase boundary so context stays clean. Every prompt assumes `CLAUDE.md` is in the repo
root and will be read automatically.

Two habits that matter more than the prompts themselves:

- End every session with: *"Update `docs/DECISIONS.md` with any decision a future session
  would otherwise re-litigate, five lines maximum per entry."*
- Start every session with: *"Read CLAUDE.md, then `docs/ROADMAP.md`, tell me the current
  phase and its exit criteria, then run `make check` and report the baseline."*

---

## P0 — Bootstrap

```
Read CLAUDE.md and every file in docs/ before writing any code. Then scaffold the
repository exactly as CLAUDE.md §3 describes.

Deliver:
- pyproject.toml with the pinned stack, ruff and mypy configured strict for src/nod_core
- the src/ package layout with __init__ files and empty typed module stubs
- tests/ with unit, integration, property and fixtures directories
- Makefile with: install, check, test, lint, types, run, bench, bench-live, metrics,
  report, fmt, clean
- .github/workflows/ci.yml running lint, types, pytest with a 85% coverage gate on
  src/nod_core, and pip-audit
- .env.example listing every variable in docs/DEPLOYMENT.md §2
- docs/DECISIONS.md with an ADR template and ADR-000 recording the stack choice
- a docker-compose.yml for local dev

Do not implement any logic yet. Every stub raises NotImplementedError.

Exit: `make check` passes on the empty project and CI is green.
```

## P1 — Instrument before building

```
Phase 0 of docs/ROADMAP.md.

Build src/nod_core/probe.py plus a CLI `python -m nod_core.probe`:

1. Open a Universal-Streaming session with model universal-streaming-english.
2. Stream a wav file from disk in paced real-time 50 ms PCM16 frames using a monotonic
   deadline schedule (never sleep(0.05) in a loop).
3. Log EVERY frame received, partials included, to data/traces/{session}.jsonl with a
   versioned line schema.
4. Probe capabilities behaviourally. The server answers a successful UpdateConfiguration
   with silence, so acceptance proves nothing: drive each knob in four cells (connect-time
   low and high, mid-stream low and high) and require the turn boundary to move. Each
   silence knob binds in one regime only, so the stimulus must supply it (EC-50).
5. Print a capability report.

Constraints: no controller logic, no config decisions. This tool only observes and
probes. Follow INV-5 and INV-6 from CLAUDE.md.

Exit: a real trace file exists and the capability report prints. Write the result as
ADR-001 in docs/DECISIONS.md.
```

## P2 — Perturbation generator

```
Phase 1, part one. Read docs/BENCH_SPEC.md §2 first.

Build src/nod_bench/corpus.py and src/nod_bench/perturb.py:
- load CC-licensed source clips listed in a manifest with license fields
- implement pause, repeat, prolong, correct, burst and noise exactly as specified
- prolong uses time-stretch without pitch shift
- emit the .truth.json sidecar schema from the spec verbatim
- seeded and deterministic: same seed and generator_version produce byte-identical audio,
  asserted by a test that hashes the output
- CLI: `python -m nod_bench.corpus build --seed 7 --out data/corpus/trackA`

Tests: determinism, truth-boundary correctness (the inserted pause is exactly where the
sidecar says), and that no perturbation changes total speech content.
```

## P3 — Feeder, fake upstream and metrics

```
Phase 1, part two. Read docs/BENCH_SPEC.md §4 and §5.

Build:
1. src/nod_bench/feeder.py — paced real-time PCM16 feeder, monotonic deadline schedule,
   records actual send timestamps, aborts above 25 ms cumulative drift (EC-37).
2. tests/fixtures/fake_assemblyai.py — a local WebSocket server that replays recorded
   trace JSONL with original inter-event timing and accepts UpdateConfiguration and
   ForceEndpoint, recording them. This is what makes INV-7 possible.
3. src/nod_bench/metrics.py — PCR, TTL p50/p90/p99, FRAG, TCT, RES, PATCH, DEC exactly as
   defined in the spec. Negative latencies are recorded, never clipped.
4. src/nod_bench/run.py — run a (corpus, arm) matrix with N repeats, write
   bench/runs/{run_id}/manifest.json with git sha, seeds, hashes, model, host.
5. Result caching keyed on sha256(audio, config, code_version) under .nodcache/, atomic
   writes only (EC-44).

Then run the three static arms over Track A and produce docs/RESULTS.md with the table
and a hand-rolled SVG Pareto chart. `make bench` must do all of this offline against the
fake server.

Exit: the static tradeoff curve is visible in docs/RESULTS.md on a clean clone with no
API key.
```

## P4 — Profiler

```
Phase 2, part one. Read docs/CONTROL_SPEC.md §1 and §2, and docs/ARCHITECTURE.md §4.

Build src/nod_core/profiler.py:
- P² streaming quantile estimator for g_p50 and g_p90, plus an exact ring-buffer
  implementation behind NOD_EXACT_QUANTILES for validation
- gaps clamped to [0, 6000] before ingestion (EC-14)
- speech rate EWMA, disfluency density with the five-bucket duration table, Welford
  jitter over the partial confidence sequence (weight 0 in the law, computed for the
  bench only — ADR-011)
- cut detection with the FOUR conditions from §2.5, no shortcuts. Condition 5 was
  dropped deliberately (ADR-011): P1 measured 89 % of real boundaries below the old
  CUT_CONF_MAX, so it discriminated nothing
- COLD until n_gaps >= 8
- skip unfinalised trailing words (EC-21), ignore empty turns (EC-16), ignore
  out-of-order turn_order (EC-07)

Every public method documents its complexity bound. No allocation per word beyond
scalars. Tests: hand-labelled fixtures for each cut condition including the four false
positives the conditions exist to prevent; a property test that one 6 s outlier shifts
g_p90 by a bounded amount; a memory test that 10 000 turns does not grow resident size.
```

## P5 — Policy and arbiter

```
Phase 2, part two. Read docs/CONTROL_SPEC.md §3 to §6 in full.

Build src/nod_core/policy.py and src/nod_core/arbiter.py.

policy.py: parse the YAML with yaml.safe_load into a closed pydantic model, compile to a
dict once, cache by content hash. Hints apply for exactly one turn. There is no
`conf_delta`: the context axis lands on `min_mult` and `max_mult` only (ADR-011).

arbiter.py: implement the control law verbatim including clamps, invariant repair, the
ceiling (which subtracts `ENDPOINT_OVERHEAD_MS`, a measured constant from `make bench`,
never hand-written — EC-49, INV-9), and every guard in §5 — hysteresis, rate cap, asymmetric decay, boolean floor,
freeze on instability, host override, capability gate. Implement the four-state machine
in §6 and the degradation matrix in §7.

decide() must be pure and synchronous, perform no I/O, and allocate only the returned
frozen dataclass. Every emitted patch carries a ConfigDecision with trigger, inputs, old,
new and rule id (INV-4).

Write all eight property tests from §9 with hypothesis, plus the budget test asserting
decide() p99 under 5 ms over 100 000 synthetic states.

Critical: read CONTROL_SPEC §0 fact 2 before writing a line. The confidence axis does not
exist in the law — P1 measured `end_of_turn_confidence_threshold` inert on
`universal-streaming-english` (ADR-001, ADR-011), and the field is never sent. Do not
reintroduce it from an older reading of §4.

Include §9 test 9: given a warm profile whose `g_p90` implies a pause longer than
`base_max`, `decide()` must widen `max_turn_silence`. That knob governs the
incomplete-utterance regime, which is the mid-sentence pause Nod exists for, and the test
fails if a change quietly stops moving it. Include §9 test 10 as well: two states
differing only in `jitter` must produce an equal patch, so restoring that weight is a
deliberate change with a failing test rather than a silent one.
```

## P6 — Session proxy

```
Phase 2, part three. Read docs/ARCHITECTURE.md §1 to §3 and docs/EDGE_CASES.md §1.

Build src/nod_core/proxy.py and src/nod_server/app.py:
- WS /v1/stream mirroring the upstream contract plus nod_preset, nod_mode, nod_ceiling_ms
- four tasks per session with the exact backpressure policies in ARCHITECTURE §3, every
  queue bounded with a drop counter
- audio forwarded verbatim, never awaiting the controller (INV-1)
- UpdateConfiguration injection on the same socket, host-override merge (EC-33)
- proactive session rotation 30 s before expires_at carrying profiler state (EC-03)
- reconnect with jittered exponential backoff (EC-04)
- observe mode sends no patches
- clean task cancellation with a leak test (EC-05)
- trace sink: append-only JSONL, bounded flush buffer, redaction on by default (EC-42),
  ENOSPC tolerated (EC-43)
- /healthz, /readyz, /metrics with the counters from ARCHITECTURE §9

Integration tests run against fake_assemblyai and cover EC-01 to EC-10 and EC-30 to
EC-36, each test named after its EC id.

Then add the nod, nod-nocontext and nod-nospeaker arms to the bench and regenerate
docs/RESULTS.md.
```

## P7 — Agent, TTS registry, voice switching

```
Phase 3, part one. Read docs/ARCHITECTURE.md §8 and docs/EDGE_CASES.md §3.

Build src/nod_adapters/{llm,tts}/ behind the Protocols in protocols.py:
- LlmClient with streaming and a hard timeout; on timeout emit a filler from a fixed
  phrase set after 1200 ms and label those turns in the trace so they are excluded from
  latency metrics (EC-26)
- TtsEngine with an immediate cancel() for barge-in; providers: browser (Web Speech,
  always registered, no key) plus one cloud provider
- voice registry returning provider, voice_id, latency_class, cost_class, sample_rate,
  pacing_hint_ms
- TTS audio cache keyed on sha256(provider, voice_id, text, speed), 64 in memory, 256 MiB
  on disk, LRU, atomic writes, hit/miss counters
- mid-session voice switch: cancel, swap, resynthesise only the unspoken remainder, apply
  pacing_hint_ms to the arbiter ceiling, never drop the session (EC-28)
- fallback chain on synthesis failure, never silence (EC-27)
- barge-in: cancel immediately, truncate the recorded spoken text to what was actually
  emitted, and do not count it as a cut (EC-23); false-barge resume from position (EC-24);
  backchannels do not interrupt (EC-25)

Then the reference intake agent as a declarative script that emits expected_answer for
each prompt so the context axis has real input.
```

## P8 — Console

```
Phase 3, part two. Read docs/DESIGN_SYSTEM.md in full before writing any CSS. The brief
is fixed: Apple iOS liquid glass. Follow the tokens and the material definitions exactly.

Next.js 15 App Router, React 19, TypeScript strict, Tailwind v4.

Screens: Live call, Replay, Benchmark, Settings sheet.

Non-negotiable from the design spec:
- the Floor Meter is the hero and the only self-animating element
- tabular-nums on every live-changing number
- maximum six blurred layers; never animate backdrop-filter or a blurred element's size
- the waveform is a canvas outside the blur stack
- telemetry coalesced through a rAF-batched store, maximum 20 UI updates per second
- prefers-reduced-transparency falls back to solid, prefers-reduced-motion removes the
  spring (EC-47)
- copy follows §6: sentences about people and time, raw parameters one tap away

WS /v1/console is server-to-client only, per-session subscription, and disconnects a slow
client rather than buffering.

Replay mode must run entirely from a committed trace with no API key (EC-45).
```

## P9 — Presets and auto-tune

```
Phase 3, part three.

- Presets: healthcare-intake, drive-thru, field-ops, elderly-outreach, id-capture. Each
  is a base config plus a context policy map, stored as YAML, loaded through the same
  closed pydantic schema.
- `nod tune --domain <name> --corpus <dir>`: sweep the control-law constants against the
  bench corpus, emit the Pareto frontier and a recommended preset with the supporting
  data. Use the bench result cache so a sweep re-runs only what changed.
- POST /v1/presets saves a tuned preset; the console Settings sheet selects one.

This is the feature that lets the product configure itself for a deployment instead of
being hand-tuned. It must show its work: the recommendation ships with the chart.
```

## P10 — Hardening

```
Freeze day minus one. No new features.

Work through docs/EDGE_CASES.md end to end. For every EC id without a test named after
it, write the test. For every one that fails, fix it. Report a table of EC id, test name,
status.

Then:
- soak test: 4 hours simulated, assert flat resident memory (EC-09)
- load test: MAX_SESSIONS concurrent, assert no live session degrades and the 503 path
  works (EC-10)
- task-leak test across 500 session open/close cycles
- pip-audit clean, dependencies pinned with hashes
- confirm no secret reaches the client bundle: grep the built output for key patterns
- confirm INV-9: every number in README and docs/RESULTS.md is regenerated by make bench
```

## P11 — Ship

```
Read docs/DEPLOYMENT.md.

- multi-stage Dockerfile, non-root, healthcheck, under 400 MB
- deploy config, secrets, health checks, log drain
- run `make bench-live` with N=5, regenerate docs/RESULTS.md and the README table, commit
  the raw traces under bench/traces/
- `make report` for the HTML report card
- README: what it is, the two facts the design rests on, the loop diagram, quickstart,
  the results table, and the "Prior art and honest scope" section from CLAUDE.md §8
  verbatim in substance
- smoke test the deployed URL from a phone on mobile data

Do not soften the honest-scope section. It is the reason the numbers are believable.
```

---

## Prompts to reuse mid-phase

**When something is ambiguous**
```
Before implementing, list every line in the spec you found ambiguous, give two readings
of each, and say which you would pick and why. Do not write code yet.
```

**When a change touches the control law**
```
This changes a constant in docs/CONTROL_SPEC.md. Run make bench before and after and put
the delta in the commit message. If the delta is within run-to-run variance, say so and
revert.
```

**When tempted to tune by ear**
```
Do not adjust this by listening. Add the case to the bench corpus, measure it, and tune
against the measurement.
```

**Review pass**
```
Review the last change as a staff engineer who did not write it. Check it against the
invariants in CLAUDE.md §2 one by one, name any that are at risk, and check the tests
actually fail if the behaviour regresses — delete an assertion, confirm red, restore.
```
