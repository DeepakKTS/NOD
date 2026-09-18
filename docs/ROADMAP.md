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

## Phase 1 — Measurement (Sep 17–19)

Restated on 17 Sep. Phase 0 ran a day long — the capability matrix had to be re-run twice,
once for a lead-segment defect and once for a redaction defect — so this phase starts on
the 17th rather than the 16th. It does not get three days of slack back: **feature freeze
is the 26th** and Phases 2 to 4 are unchanged behind it. One of the five bullets is already
done, which is where the day comes from.

- ~~Paced real-time feeder with deadline scheduling and drift abort.~~ **Done**
  — `src/nod_bench/feeder.py`, absolute-deadline schedule, sustained-lag abort
  (ADR-008, ADR-012). Shipped during Phase 0 because the probe needed it.
- `nod_bench`: perturbation generator (`pause`, `repeat`, `prolong`, `correct`, `burst`),
  truth sidecars, seeded and deterministic.
- Metrics: PCR, TTL p50/p90/p99, FRAG, and the run manifest.
- Baseline sweep: aggressive / balanced / conservative over Track A and Track C.
- `FakeAssemblyAI` replay server built from Phase 0 traces. The seed trace is committed at
  `tests/fixtures/traces/seed-min-turn-silence-midstream.jsonl`.

**Exit:** `make bench` produces the tradeoff curve and the Pareto chart for the three
static arms offline, on a clean clone, from the simulator — every artifact **labelled
`simulated`** in its filename and run manifest (ADR-016, ADR-017). The tradeoff curve is
visible. The published table is regenerated from live runs at Phase 4 per INV-9; nothing
generated here is a published number. There is still no controller.

**Open, as of 17 Sep — one item. Two of the three are closed.**

1. **Track C audio does not exist.** Phase 0's fourth bullet was never done. The sweep is
   specified over Track A *and* Track C, and Track A does not substitute for it. **Still
   open, and the only thing standing between here and Phase 1's exit.**
2. ~~The Track A corpus is not committed.~~ **Closed.** 120 clips and their sidecars, plus
   the sliced source clips and manifest, committed directly at 37 MB. `.git` went from
   2.5 MB to 19 MB.
3. ~~`make bench` does not run on a clean clone.~~ **Closed, and verified by actually
   cloning**, not by reasoning about it: `git clone` of the pushed branch into a temp
   directory, then `make bench`, which completed in 19 seconds including `uv` building the
   environment from scratch. The artifacts it produced are byte-identical to the working
   tree's.

That clone found a defect nothing else would have. `make bench` failed on
`No module named 'soundfile'`: the audio libraries live in the `bench` extra, and a bare
`uv run` installs base dependencies only. Every local run had worked because the working
tree had the extra installed from `make install`, which hid it completely from the moment
the extra was introduced. Fixed by giving the bench targets their own `RUN_BENCH`. **A
clean-clone criterion is only met by performing the clone** — the working tree is exactly
the environment that cannot detect this class of problem.

(2) and (3) are one problem and close together: **commit the audio** — the Track A corpus
plus the Track C recordings once they exist. There is no cross-platform way to regenerate
it, so committing is the only route to a clean-clone build. Roughly 36 MB for Track A plus
whatever Track C weighs.

**Decided: direct commit, not Git LFS.** 120 files averaging ~300 KB is well inside
GitHub's per-file and repository limits, so LFS buys nothing here. It costs something real:
a clone on a machine without `git-lfs` installed silently receives pointer files instead of
audio, and `make bench` then fails on a wav that is 130 bytes of text. That failure is
obscure and nobody diagnoses it quickly — which defeats the exact criterion the commit
exists to satisfy. A clean clone that works everywhere beats a smaller clone that works
where the tooling happens to be present.

**Before the corpus lands, verify it is the current generation**, not an earlier one. Once
committed it becomes the permanent ground truth behind every figure in the repository,
which places it squarely in CLAUDE.md §5's non-negotiable category. Check three things:
- it rebuilds byte-identically from `seed=7` under current code (`corpus_sha256` matches);
- its sidecars carry `source_intrinsic` gaps, i.e. they are post-ADR-018 — a pre-ADR-018
  corpus scores mid-utterance pauses under the wrong gate and inflates PCR;
- `corpus.INTRINSIC_FLOOR_DBFS` still equals the simulator's floor at
  `fake_assemblyai.DEFAULT_VAD` (both −44.0 dBFS at 0.4 today).

That third one is a latent trap and is written down because nothing enforces it: the two
constants are coupled by *meaning* and not by code, so changing `DEFAULT_VAD` would leave
the corpus's intrinsic-gap detection silently calibrated to the old threshold. That is the
same shape as the stale-bytecode and vacuous-invariant defects — a disagreement no test
currently notices. Assert the equality before committing the audio, or the ground truth
can drift out from under the figures without anything going red.

Verified on 17 Sep: the corpus at `data/corpus/trackA` rebuilds byte-identically, its
sidecars match a fresh build, it carries 125 `source_intrinsic` gaps, and the two floors
agree.

Considered and rejected: **swapping `say` for a cross-platform TTS** so the corpus could be
regenerated anywhere instead of committed. It is real work this close to the 26th — a new
dependency, new determinism guarantees, and every existing figure re-measured against a
different voice. And it does not touch (1) at all, because Track C is recorded human
speech and no synthesiser produces it. It buys reproducibility of the part that is already
reproducible on one machine, at the cost of the days needed for the part that is not.

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

## 3. Cut, and what survives

This is no longer a contingency list. The first three are **cut**. They are not deferred,
not "if time permits", not revisited on the 25th. They are out of scope for this build and
belong in §4.

**Cut:**

1. **Track B**, the real atypical-speech corpus. Track A and Track C carry the measurement.
2. **Streaming diarization and per-speaker profiles.** One speaker per session.
3. **`nod tune`.** Hand-picked presets, tuned against `make bench`, never by ear.

Still contingent, cut from the top if the 26th is at risk:

4. Cloud TTS providers, keep `browser` only.
5. Benchmark view in the console, keep the static report card.
6. The context axis. **Cut last — it is half the originality.**

**The four that survive.** If everything else goes, these ship:

1. **The harness.** `nod_bench` plus the committed traces. The claim is the measurement;
   without it there is no project, only an assertion.
2. **The two-gate controller.** `max_turn_silence` for the incomplete-utterance regime,
   `min_turn_silence` for the complete one — the silence axis, per ADR-011 and ADR-001.
3. **One demo screen.** The live call view with the Floor Meter, the config strip and the
   reason line. One screen that shows the loop closing, not three that show architecture.
4. **The honest-scope section.** Nod is not the first adaptive endpointer. It is an open
   one on the STT path with a published policy and published numbers (CLAUDE.md §8).

## 4. Post-hackathon backlog

Not in scope before 30 Sep, listed so it stops leaking into the build:

- Track B, a real atypical-speech corpus, with the consent and licensing that needs.
- Streaming diarization and per-speaker profiles.
- `nod tune`, an automated sweep over the control-law constants.
- SIP and telephony adapter.
- Per-tenant policy management and auth.
- Learned endpointing policy (contextual bandit over the same features), with the static
  law as the safety floor and an offline evaluation before any online use.
- Locale packs for filler and backchannel sets.
- Postgres and Redis if concurrency demands it, behind an ADR.
- Public leaderboard for the benchmark.
