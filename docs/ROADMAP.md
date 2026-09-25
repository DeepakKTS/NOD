# Nod — Build plan

Window: 15 September → 30 September 2026. Submission closes 30 Sep, 11:00 EDT.

> **The 26 Sep feature freeze is declared LAPSED as of 23 Sep, and here is what changed.**
> It was written to protect the last four days for audio devices, deployment and video.
> Two of those three are not merely unfinished, they are blocked on things a freeze cannot
> help: **deploy** is blocked on a locked macOS keychain that stops `git push`, so no
> platform can build from the repo, and there is no local container runtime to build an
> image instead. **Audio** is blocked on a reader who has not been booked.
>
> The freeze is not moved to a later date, because moving it would assert a second date on
> the same evidence as the first. It is declared lapsed, which is the honest description
> of what happened: the date passed the work rather than the work meeting the date.
>
> **What replaces it is a scope decision, not a schedule.** ADR-054 established that the
> incomplete-utterance regime — the thing the controller exists to serve — has not been
> shown to be reachable on this service. That changes what the remaining work is *for*:
> the video and the deck must be written against "three static arms measured live, the
> mechanism not yet exercised, and here is the evidence it may not be exercisable", which
> is a different and more defensible artifact than the one the freeze was protecting.
> **Code is frozen from here: bug fixes, docs and the two cheap checks ADR-054 names.**
> No new features, which is what the freeze was for.

**Feature freeze: 26 September — LAPSED, see above.** The last four days are for the
things that always go wrong: audio devices, deployment, and video.

Each phase has exit criteria. Do not start the next phase until they are all true. If a
phase slips, cut from the Cut List (§3), never from the harness.

That rule stands as written and is not softened by what follows. **One deviation from it
has been taken, deliberately and once**: Phase 2 started on 17 Sep with Phase 0's Track C
bullet still open. It is recorded at Phase 2 below, with its reason and its cost. A
deviation that is written down as a deviation is a different thing from a rule that has
been relaxed — the next session must justify its own, not inherit this one.

---

## Where this stands — 24 Sep, Gate 4h-prep

**The software has been watched working, for the first time in twelve gates.** The
owner drove a live session end to end: **35 turns observed, profiler warm, 3 patches**,
and `max_turn_silence` widened **1280 → 1839 ms** on a human voice with a reason
attached. That is the project's thesis running live. No figure from it enters a
published table — it is one operator, one room, unscripted speech — but the mechanism
is no longer only argued for.

**Nobody had seen it because of four stacked defects between browser and server**
(ADR-058): a permanent `429` lockout from a session registry that never evicted, a
client that read `session_id` off an error body, a console route that accepted unknown
ids, and `send_bytes` delivering JSON as a `Blob` that `JSON.parse` throws on. A test
had asserted the lockout as correct behaviour. All four fixed.

**Two instruments lied in opposite directions while diagnosing it.** A Python probe on
the identical endpoint received every frame — `json.loads` accepts bytes — and declared
the server sound, which was true and useless. A headless reproduction using
`--virtual-time-budget` had exited before the audio arrived and would have called the
page broken however well it worked. Neither was checked against a known-good case first.

**`configure_logging` is implemented** — it was the `NotImplementedError` stub ADR-050
recorded, which is why server-side diagnostics written to chase this produced nothing.
**Four of seven counters are now wired**; `nod_cuts_total` and `nod_decide_seconds` are
not, and `cut.detected` is never published, so the console's Cuts tile cannot leave
zero. All three stated in DEPLOYMENT §3 rather than quietly left.

**The demo screen is now usable for the video** and `VIDEO_SCRIPT.md` carries it as an
optional shot, with the terminal-and-SVG list kept as the fallback that needs no UI.

---

## Where this stands — 23 Sep, Gate 4f

**Submission assets ship.** `docs/nod-deck.pdf` (7 slides, `make deck` from
`docs/deck.html`), `docs/VIDEO_SCRIPT.md` (90 s, shot by shot, every on-screen number
traced to an artifact, runs with no deployed URL), `docs/SUBMISSION.md` (title, short,
long, tags, and the list of claims deliberately kept out). `LICENSE` and
`CONTRIBUTING.md` added — both were on DEPLOYMENT §9's checklist and neither existed.

**The framing is (b), the instrument story.** The deck is built around the ADR-054 →
ADR-055 correction rather than closing with it; the controller is section 4, as what was
built to exercise the measurement.

**A Gate 4e claim was false and is corrected.** "Then took the continuation as the same
turn" — it did not. All four swept arms emitted **two** turns; even 2574 ms of patience
expires 958 ms before the caller resumes on a 3532 ms pause. The sweep shows the knob
buys time continuously, not that this setting buys enough. Corrected in ADR-055 and the
README before it reached a slide.

**`LICENSE` is scoped, not blanket.** Code is MIT; the audio under `data/` is macOS
`say` output that the corpus manifest calls "not redistributable speech" and is not ours
to relicense. A blanket MIT would have been a false claim about the audio.

**Git history is clean for going public.** All refs plus every unreachable object — 966
blobs — scanned for the literal key, for secret-shaped assignments and for bare
`sk-`/`AKIA`/32-hex tokens. Nothing. `.env` was never tracked. The scanner was verified
against a planted decoy in both directions.

**The published table now carries the PCR anchor caveat**, generated into the
machine-owned region rather than typed beside it, with the provenance guard seen red on
a one-digit hand edit and green again on restore.

**Still blocked, both external:** the repository is **private** and the submission needs
it public; and `scripts/deploy_aws.sh` has not been run, so there is no Application URL.
Neither blocks the deck or the video.

---

## Where this stands — 23 Sep, Gate 4e

**The regime is reachable and the controller is aimed at the wrong knob.** Sixteen live
sessions, predictions committed before the sockets opened. `min_turn_silence` swept at
400/900/1600/2400 ms held the turn open for 777/1170/1765/**2574** ms after a
mid-sentence stop, 4/4 inside tolerance. `max_turn_silence` binds only *below* the
service's ~590 ms commit point, where binding cuts sooner rather than waiting longer, and
at end of stream (16/16). ADR-011 makes `max` the primary lever; that is wrong, and
`MIN_MS_CEIL = 900` caps the lever that works at a third of its range. **Neither constant
is changed inside the freeze** — one clip of synthetic speech is not grounds to retune a
control law (§7) — and both are the first thing to fix after it (ADR-055, superseding
ADR-054).

**ADR-054's observation stands, its conclusion does not.** Hold-invariance reproduced
exactly (spreads 14/39/42 ms). The FAIL was a real measurement read against a criterion
with a blind spot: `aggressive` was firing *at* its max gate the whole time, which is the
pilot's own PASS condition.

**The pilot is now a committed procedure** (`nod_bench.ladder`, `scripts/pilot_ladder.py`,
`ladder` mutation catalogue, 9/9 killed). Gate 4d's runner lived in a scratch directory
and was deleted with the job; its numbers existed only as prose in one ADR. `run` refuses
to start unless `predict` has written its file first.

**A live-table defect this found:** `silence_started_ms` is not a usable silence anchor —
560 ms late on one turn and 190-250 ms early on the next in the same session — and `pcr`
attributes live boundaries to gaps with it. Every live PCR figure is suspect. Named, not
fixed.

**Deploy was blocked; it no longer is, and both halves of that sentence have since
changed.** As written at Gate 4e: the six commits were pushed, AWS was authenticated and
the ECR repository existed, creating the build role and source bucket was refused by that
session's permission layer, and the GitHub repo was private.

**Resolved at Gate 6, and corrected here rather than left to read as current status.**
The repository is **public**. The AWS path was abandoned entirely: App Runner refuses
WebSocket upgrades at its ingress, which is the whole product (ADR-064). Nod is deployed
on Fly and verified live at <https://nod-turn-timing.fly.dev> — both sockets upgrade,
one full call returns turns in `observe` mode.

**The human ladder recording has not been supplied**, so the `say`-to-human
generalisation is still open. It is now a smaller question — where `C` sits on a human
voice — and the tooling takes it with one flag.

---

## Where this stands — 23 Sep, Gate 4c

**The live table measures the static arms' `min_turn_silence`, and nothing else.**
`max_turn_silence` — the gate ADR-011 makes Nod's primary lever — **never binds on Track
A**, and it is not a corpus defect: the pauses reach 2200 ms, 2.75x `conservative`'s min
gate, and the service judges those points complete anyway (ADR-051). The run also does
**not** establish that the controller acted: the patch census is **unmeasured, not zero**,
because the trace sink was the only recorder and it writes nothing when there is nothing
to write (ADR-050, corrected).

**The published table cannot be re-rendered** under ADR-049's estimator — only per-arm
aggregates were committed, so the intervals keep the warning that they understate
(ADR-052). Later runs are re-analysable.

**Deploy has not happened and is blocked twice over**: the repo cannot be pushed (locked
keychain) so no platform can build from it, and there is no container runtime here to
build an image instead. `docs/DEPLOYMENT.md` now opens with what has and has not executed.

**`docs/PILOT_REGIME.md` is the next thing to run** — 30 s of audio, before any recording
session, to find where the incomplete-utterance regime begins on human speech. A FAIL
there stops a three-hour session that would otherwise produce ten calls of the same null.

---

## Where this stands — 22 Sep, Gate 4b

**The live path ran against AssemblyAI for the first time, and it could not have run
before today.** The first clip failed on its first frame: every config value was sent as
a float and two fields are parsed with `int()`. The same bug sat in
`proxy.send_patch_upstream`, so every mid-stream patch would have been refused and each
`nod` arm would have degraded to `balanced` under its own label. Two more followed — a
rejected config surfacing as a bare transport error, and the `Terminate` flush being
scored as a caller turn, which put FRAG at exactly 2.000 on all six arms (ADR-047).

**Track A `N = 5` does not fit.** The account allows one new session roughly every 15 s
once a closed session's slot is counted, so 3,600 sessions is ~16 hours, not the 2.3 h
estimated at Gate 4a from a concurrency figure that assumed slots free on close. The
published table comes from a **stratified 12-clip subsample at `N = 5`**, labelled as such
everywhere it appears (ADR-048).

**Not done, and not started:** the Track C recording, the pilot gate, any Track C live
sweep, the video and the deck. **Deploy is blocked on tooling, not on code** — there is no
container runtime or platform CLI on this machine; the `Dockerfile` is written and the
three health endpoints are green locally against a real volume.

---

## Where this stands — 21 Sep, end of day

Pointers, not restatements. One source of truth; do not copy this elsewhere.

**Phase 0** closed. **Phase 1** exit open on **Track C recording only** — the ingestion path
now exists. **Phase 2** implemented; its exit is no longer blocked on tooling. **Phase 3**
restated by ADR-035 and largely built.

**Gate 8 cleared the Phase 2 blocker.** The controller had never emitted a patch outside a
synthetic driver because Track A is per-utterance. `nod_bench.trackc` ingests a multi-turn
call, and on a `say`-synthesised script-A dry run the profiler **warms on turn 3** and the
arms emit **8 / 4 / 8** patches. The three controlled arms differ from each other and from
`balanced` for the first time. That is a dry run on synthetic audio, not a measurement:
TRACK_C_SCRIPT §9 states why warming there is necessary and not sufficient.

**What landed at Gate 8.** ADR-034 (transcript gaps promoted to `Gap`s under the standing
rule), ADR-035 (Phase 3's exit restated against the cut list), ADR-036 (`certain_only`
scopes per gap), ADR-037 (the gate check moved into a pre-commit hook after a red gate
reached a commit a third time), ADR-038 (the one demo screen is static, not Next.js).
Track A sidecars regenerated with promotion — every figure unchanged, as predicted. The
context axis is wired into `make bench` for the first time.

**Phase 3, per ADR-035.** Clause 2 holds at full strength and its server path is tested end
to end. Clause 1 is thin: browser TTS, one Claude call, no filler, no false-barge recovery.
Clause 3 is dropped. Clause 4 is **not built** and is downstream of a recorded call.

**What Phase 4 inherits is listed at Phase 4 below. Freeze 26 Sep.**

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
- Record Track C audio: 10 calls, fluent and deliberately hesitant. **Two requirements
  added 18 Sep from what Gate 5 measured, and neither is optional** — the bullet as
  originally written would be satisfied by ten hesitant monologues, which would satisfy
  nothing else:
  - **Enough turns per call to accumulate 24 inter-word gaps**, the warm threshold
    (ADR-022). Gaps never span a turn boundary (§2.1), so a turn of `w` words contributes
    `w - 1` gaps: measured, six words a turn warms the profiler on **turn 5** at 25 gaps.
    Call it **five turns of six words**, and note that a turn of two or three words
    contributes almost nothing — a short-answer script needs proportionally more turns.
    A call of one long utterance carries 1–9 gaps and leaves the profiler cold for
    its whole duration, which is exactly what Track A does today. Prefer calls that run
    well past five turns, so the controller is warm for most of the recording rather than
    only at the end.
  - **Declared `expected_answer` classes**, one per prompt, from CONTROL_SPEC §1's eight.
    The context axis has no other input: `hint_for(None)` returns the policy default, so a
    recording without declared classes leaves `nod-nospeaker` measuring nothing and
    `nod`'s context axis contributing nothing. An intake script asking for a member number,
    a date and a yes/no answer supplies three of the eight for free — which is the point of
    scripting the calls rather than recording free conversation.
  Both are properties of the **recording script**, so they cost nothing if decided before
  the session and cannot be added afterwards.

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

**Started 17 Sep with Phase 1 not exited. This is a deliberate deviation from §0's "do not
start the next phase until they are all true", recorded rather than quietly taken.**

What is still open is Phase 1 exit item 1, Track C audio — 10 recorded calls, fluent and
deliberately hesitant, which Phase 0's fourth bullet never produced. Three reasons to
proceed anyway:

1. **Nine days to freeze.** Feature freeze is the 26th and Phases 2 to 4 are unchanged
   behind it. Track C is a recording session whose cost is wall-clock time and a
   participant's availability, neither of which is bought by delaying the controller.
2. **Phase 2 is the critical path.** The controller is one of the four things §3 says must
   ship, and Phases 3 and 4 both consume it: the console renders its decisions and the
   live bench run measures them. Nothing in Phase 3 or 4 is unblocked by Track C alone.
3. **The controller needs no audio to be built or tested.** `decide()` is pure and
   synchronous over a feature vector (INV-2); the profiler consumes `Turn` events, not
   samples; INV-7 puts every test against `FakeAssemblyAI`. Track C is corpus material for
   the *measurement*, and the ROADMAP already routes published numbers through the live run
   at Phase 4 (ADR-016).

**What this deviation does not buy — rewritten 18 Sep, after Gate 5 measured it.**

This paragraph used to say Track C was needed for the chart's *coverage*: that measuring
only fluent synthetic speech would test the controller everywhere except where it matters,
and would run in the flattering direction (ADR-018) by omitting the clips most likely to
expose a cut. All of that is still true and all of it understated the problem by a wide
margin.

**Gate 5 ran the arms. Track C is needed for the chart to have any controlled arm on it at
all.** Measured, not argued:

- Nod is a **per-session** adapter. Track A is a corpus of **per-utterance** clips: 120
  clips of 7.7–12.3 seconds, each producing one or two turns.
- Inter-word gaps per clip: **min 1, median 3, max 9**. The warm threshold is **24**
  (ADR-022). **0 of 120 clips reach it** — and at the pre-ADR-022 threshold of 8, 1 of 120
  did, so this is the corpus and not the constant.
- So the profiler never warms, §4 returns its base values, hysteresis suppresses every
  patch, and **all three nod arms are byte-identical to `balanced`** — 0 patches on every
  clip, PCR 0.317, TTL p90 426 ms, FRAG 1.317, the same numbers to the last digit.
- The context axis is degenerate for a second, independent reason: Track A carries no
  declared `expected_answer`, so `hint_for(None)` returns the policy default and
  `nod-nospeaker` — context axis only — has no input whatsoever.

The identity with `balanced` is the evidence the wiring is *correct*; the corpus is the
reason there is nothing to show. A per-session adapter measured on single-utterance clips
has nothing to adapt from.

**So Phase 2's exit is blocked on multi-turn material, not on coverage breadth.** It is not
that the chart would be narrow without Track C — it is that the chart has three arms on it
that are the baseline under another name. Either Track C lands with the shape §0's bullet
now requires, or the corpus gains multi-turn sessions with declared answer classes, which
would be a new Track and a new ADR rather than a tweak.

One piece of good news from the same measurement: on a synthetic multi-turn session the
profiler warms at **turn 5** — six words a turn gives five gaps a turn, since gaps never
span a turn boundary (§2.1), so `5 × 5 = 25 ≥ 24`. The threshold is comfortably reachable
inside a real call. A ten-second clip simply is not a call.

**Track C must land before Phase 2's exit, and Gate 1's and Gate 2's tests did not need
it** — which is what the deviation bought and all it bought.

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
  metric tiles; ~~replay view with two panes and a shared scrubber~~; ~~benchmark view~~.
  **Superseded by ADR-035 clause 4 and ADR-038**: the two-pane replay and its shared
  scrubber are cut to one pane, the benchmark view is cut in favour of the static report
  card, and the screen is static HTML rather than Next.js. Left as struck-through rather
  than deleted because this is the Phase 3 plan of record; the second half of the bullet
  outlived the decision by a day and was missed when the Phase 4 video bullet was fixed.
- Presets and `nod tune`.

**Exit:** a full call runs end to end in the browser. The Floor Meter visibly grows on a
hesitant caller. Voice switches mid-call without dropping. Replay mode runs from a
committed trace with no API key.

---

## Phase 4 — Freeze and finish (Sep 26–28)

**Inherited from Phase 3, explicitly, so none of it is discovered on the 27th:**

1. **No Track C recording exists.** The ingestion path, the seam check and the pilot gate
   do (TRACK_C_SCRIPT §9). A session has to be booked, and the pilot run *first* — room
   tone against the −44 dBFS floors is the one risk the dry run cannot bound.
2. **Replay mode (ADR-035 clause 4) is unbuilt and needs a committed Nod trace**, which
   needs one real call first. 118 committed traces carry zero `config_decision` records.
3. **No live bench run.** `--live` still exits 2. INV-9 routes every published figure
   through it, so `docs/RESULTS.md` and the README table cannot be written until it runs.
4. **ADR-032's patch census** and ADR-027's per-session count, collected together.
5. ~~**`ENDPOINT_OVERHEAD_MS` is still 0**, so CONTROL_SPEC §9 property 3 stays skipped and
   every simulated TTL is optimistic by the overhead.~~ **Closed at Gate 4a (ADR-040).** Set
   to **217**, the top of ADR-017's spread, because the constant is subtracted from the
   ceiling and the smaller value is the permissive one. Property 3 is live and has been seen
   red against the vacuous form. It moved `CEILING_FLOOR_MS` to 1317 as well — ADR-021's
   floor was derived at an overhead of 0 and took §9 property 1 red at 1100. Phase 4's live
   run still owes its own spread.
6. ~~**`/metrics`, `configure_logging` and auth are stubs.** Only `/metrics` is on
   DEPLOYMENT.md §4's path.~~ **Resolved at Gate 4a.** `/metrics` is implemented — the
   registry was already built, so it was one `generate_latest` call. `/readyz`'s four
   checks became real conditions (ADR-041); they were hardcoded `ready=False`, so it
   returned 503 for any input and no test over it could pass *or* fail. **Auth stays cut**
   and `configure_logging` stays a stub, both on purpose (ADR-042): `NOD_AUTH` now defaults
   to `off` instead of declaring a guarantee no route honoured, and the exposure it was
   nominally covering — a public URL spending the upstream key — is bounded by
   `NOD_MAX_SESSIONS`, now **2**, derived from a measured account limit of 5 concurrent
   streams and the 2 sockets a rotating session holds.

- **26 Sep: feature freeze.** Only bug fixes, docs and polish after this point.
- Full bench run, live, `N = 5`. Regenerate `docs/RESULTS.md` and the README table.
- Report card HTML.
- README with the prior-art and honest-scope section.
- Deploy per DEPLOYMENT.md. Health checks green. Smoke test against the deployed URL.
- Demo video: 90 seconds. Cold open on the **one-pane** replay and the counter. No
  architecture slides in the first 30 seconds. (Was "two-pane"; ADR-035 clause 4 cut the
  stock-vs-Nod split view and its shared scrubber, against §3's "one screen that shows the
  loop closing". Corrected at Gate 4a — the bullet had outlived the decision.)
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
