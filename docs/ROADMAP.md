# Nod — Build plan

Window: 15 September → 30 September 2026. Submission closes 30 Sep, 11:00 EDT.
**Feature freeze is 26 September.** The last four days are for the things that always go
wrong: audio devices, deployment, and video.

Each phase has exit criteria. Do not start the next phase until they are all true. If a
phase slips, cut from the Cut List (§3), never from the harness.

---

## Phase 0 — Ground truth (Sep 15, half a day)

Instrument before building anything. If the thesis is not measurable by the end of day
one, the project changes rather than proceeding on faith.

- Repo scaffold, `make check` green on an empty project, CI running.
- Raw WebSocket client that logs **every** frame, partials included, to JSONL.
- Capability probe: confirm which of `end_of_turn_confidence_threshold`,
  `min_turn_silence`, `max_turn_silence`, `vad_threshold` are updatable mid-stream on the
  chosen model, and whether `end_of_turn_confidence` appears in events. Record the answer
  in `docs/DECISIONS.md` as ADR-001.
- Record Track C audio: 10 calls, fluent and deliberately hesitant.

**Exit:** a JSONL trace of a real session exists, and a one-page note states exactly
which knobs moved and which did not.

---

## Phase 1 — Measurement (Sep 16–18)

- `nod_bench`: perturbation generator (`pause`, `repeat`, `prolong`, `correct`, `burst`),
  truth sidecars, seeded and deterministic.
- Paced real-time feeder with deadline scheduling and drift abort.
- Metrics: PCR, TTL p50/p90/p99, FRAG, and the run manifest.
- Baseline sweep: aggressive / balanced / conservative over Track A and Track C.
- `FakeAssemblyAI` replay server built from Phase 0 traces.

**Exit:** `make bench` produces a table and the Pareto chart for the three static arms,
offline, on a clean clone. The tradeoff curve is visible. There is still no controller.

---

## Phase 2 — The controller (Sep 19–22)

- `profiler.py`: P² quantiles, speech rate, disfluency density, Welford jitter (weight 0,
  ADR-011), cut detection with the four surviving conditions.
- `policy.py`: YAML policy compile, the eight `expected_answer` classes.
- `arbiter.py`: the control law, all guards, the four-state machine.
- `proxy.py`: dual-socket proxy, fan-out, `UpdateConfiguration` injection, host-override
  merge, reconnection and session rotation.
- Property tests from CONTROL_SPEC §9. Budget test for INV-2.
- Tune constants against the bench. Never by ear.

**Exit:** the `nod` arm appears on the Pareto chart with error bars, plus both ablation
arms. `make check` green, coverage gate met, `decide()` p99 under 5 ms.

---

## Phase 3 — The agent and the console (Sep 23–25)

- Reference intake agent: LLM adapter, TTS registry with `browser` plus one cloud
  provider, barge-in, false-barge recovery, filler on slow LLM.
- Voice switching mid-session with resynthesis of the unspoken remainder.
- Console: live call view with the Floor Meter, config strip, reason line, transcript,
  metric tiles; replay view with two panes and a shared scrubber; benchmark view.
- Presets and `nod tune`.

**Exit:** a full call runs end to end in the browser. The Floor Meter visibly grows on a
hesitant caller. Voice switches mid-call without dropping. Replay mode runs from a
committed trace with no API key.

---

## Phase 4 — Freeze and finish (Sep 26–28)

- **26 Sep: feature freeze.** Only bug fixes, docs and polish after this point.
- Full bench run, live, `N = 5`. Regenerate `docs/RESULTS.md` and the README table.
- Report card HTML.
- README with the prior-art and honest-scope section.
- Deploy per DEPLOYMENT.md. Health checks green. Smoke test against the deployed URL.
- Demo video: 90 seconds. Cold open on the two-pane replay and the counter. No
  architecture slides in the first 30 seconds.
- Slide deck: problem, the two facts, the loop, the chart, the honest-scope slide.

**Exit:** submission fields are all filled and the deployed URL works from a phone on
mobile data.

---

## Phase 5 — Slack (Sep 29–30)

Reserved. Do not plan work here. Submit on the 29th, not the 30th.

---

## 2. Definition of done, per unit of work

1. Spec read.
2. Types and docstrings with complexity bounds.
3. Tests including at least one edge case from EDGE_CASES.md, referenced by id.
4. `make check` green.
5. Trace or metric emitted where the behaviour should be observable.
6. Doc updated if behaviour diverged from the spec, or an ADR if a decision was made.

## 3. Cut list, in order

Cut from the top when behind:

1. Track B (real atypical-speech corpus).
2. Streaming diarization and per-speaker profiles.
3. `nod tune` sweep, keep hand-picked presets.
4. Cloud TTS providers, keep `browser` only.
5. Benchmark view in the console, keep the static report card.
6. The context axis. **Cut last — it is half the originality.**

Never cut: the harness, the traces, the honest-scope section.

## 4. Post-hackathon backlog

Not in scope before 30 Sep, listed so it stops leaking into the build:

- SIP and telephony adapter.
- Per-tenant policy management and auth.
- Learned endpointing policy (contextual bandit over the same features), with the static
  law as the safety floor and an offline evaluation before any online use.
- Locale packs for filler and backchannel sets.
- Postgres and Redis if concurrency demands it, behind an ADR.
- Public leaderboard for the benchmark.
