# Demo video — shot list

**90 seconds. No architecture before 0:30.** Every on-screen number is traced to a
committed artifact in the right-hand column; nothing is typed into a slide by hand.

Runs entirely locally. **No deployed URL is required** — the two shots that would use
one are marked optional and the video ships without them.

## Before recording — one command

Everything the video shows is already committed. This regenerates the two timeline
images from the committed observations, so the picture on screen is the picture of the
run in the repository rather than of a fresh run that happens to agree:

```sh
uv run --extra bench python scripts/pilot_ladder.py svg --label continuation
uv run --extra bench python scripts/pilot_ladder.py svg --label sweep
```

Open `bench/runs/ladder_continuation_1500.timeline.live.svg` and
`bench/runs/ladder_sweep_3500.timeline.live.svg` in a browser at 1280 px wide, dark
background. Terminal: dark, 16 pt, ~100 cols.

**No `--label say`.** That renders the retired 532 ms frame; see the shot list below
for why it is not used.

---

## 0:00–0:14 — Cold open. The behaviour, no words about architecture

| | |
|---|---|
| **Screen** | `ladder_continuation_1500.timeline.live.svg`, full width. Four rows, `min400` → `min2400`. Same waveform on each. Blue band = the caller's pause. |
| **Action** | Nothing moves for 3 s. Then highlight the right column: **2 / 2 / 1 / 1 turns**, and the red marks vanishing on the bottom two rows. |
| **VO** | "Same sentence. Same pause in the middle of it. One setting changed. The top two cut the caller off while they were still mid-sentence. The bottom two waited, and took the rest of the sentence as the same turn." |
| **Artifact** | `bench/runs/pilot_ladder.continuation.live.json`. Turn counts **2 / 2 / 1 / 1**, predicted in `7df3a4f` **before** the run (ADR-056). First boundary 2988 ms and 3407 ms — both **inside** the 1532 ms pause. |

> The pause is **1532 ms**, after *"I need to reschedule my appointment **to**"* — a
> prefix English cannot end on. Drawn in the image caption, so it survives a screenshot.
>
> **Do not use the 532 ms frame for this shot.** `aggressive` does emit two turns there,
> but its split falls **mid-prefix**, not at the pause — the image would invite a claim
> about the pause that the artifact does not support (ADR-056).

## 0:14–0:30 — The dial

| | |
|---|---|
| **Screen** | Cut to `ladder_sweep_3500.timeline.live.svg`. Four rows: `min400` → `min2400`. |
| **Action** | Reveal rows top to bottom, ~1 s apart. The red line walks right; the blue band does not move. |
| **VO** | "One knob. `min_turn_silence`. At the vendor default the service gives a hesitating caller 777 milliseconds before it decides they're done. Turn it up and you get 2,574. It's continuous, and it's the whole range we could find." |
| **On screen** | `777 → 1170 → 1765 → 2574 ms` |
| **Artifact** | `bench/runs/pilot_ladder.sweep.live.json`. |

> **Say "three-point-two times", never "it keeps the sentence together".** It does not:
> all four rows read **2 turns**, because the pause here is 3,532 ms and even 2,574 ms of
> patience runs out before the caller resumes. The image shows that honestly and the VO
> must match it.

## 0:30–0:44 — What this is, first mention of method

| | |
|---|---|
| **Screen** | Terminal. Run it live; it is 12 seconds of output from committed files, no network: |

```sh
uv run --extra bench python scripts/pilot_ladder.py predict \
  --label demo --holds 3500 --sweep \
  --prefix data/corpus/ladder/say_prefix.wav \
  --continuation data/corpus/ladder/say_continuation.wav
```

| | |
|---|---|
| **VO** | "Nod is a benchmark that measures those knobs on the live service, and a controller that moves them mid-call. The predictions go in first — the tool refuses to run until they're written down." |
| **After** | It writes `bench/runs/pilot_ladder.demo.live.predictions.json`. Delete it; the label `demo` exists so the recording cannot overwrite the committed `sweep` predictions, which are the provenance for the numbers in the shot before this one. |
| **On screen** | The four-column prediction table it prints. |
| **Artifact** | `bench/runs/pilot_ladder.say.live.predictions.json`, committed in `f3e7838`; the results it predicts arrive in the **next** commit, `b7c3ccc`. |

> **Say "the tool refuses", not "git proves it".** The ordering is git-provable for the
> *say* ladder only. The `sweep` predictions and the `sweep` results landed in the same
> commit (`b7c3ccc`), so for the headline experiment the guarantee rests on the runner's
> refusal at runtime, not on the history. Cite `f3e7838 → b7c3ccc` on screen if a commit
> hash is shown at all.

## 0:44–1:02 — The credibility beat. We published this wrong

| | |
|---|---|
| **Screen** | Split. Left: the old README headline. Right: the current one. |
| **Action** | Strike through the left. |
| **VO** | "Last week we published the opposite. We tested the knob the controller was built around, found it inert, and concluded the whole regime was unreachable. That was wrong. There are two knobs that bracket the same quantity, and we'd varied one." |
| **On screen** | `ADR-054 → ADR-055` |
| **Artifact** | `docs/DECISIONS.md`, ADR-054 and ADR-055. |

| | |
|---|---|
| **VO cont.** | "The correction also says our own controller points at the wrong one, and caps the right one at a third of its range. Both are still in the code. We're not retuning a control law on one clip of synthetic speech." |
| **Artifact** | `src/nod_core/arbiter.py` — `MIN_MS_CEIL = 900`. |

## 1:02–1:16 — The model

| | |
|---|---|
| **Screen** | One line of text, held: |

```
fire_at = prefix_end + clamp(C, min_turn_silence, max_turn_silence) + overhead
C ≈ 590 ms          overhead ≈ 175 ms
```

| | |
|---|---|
| **VO** | "Two parameters. C is the service's own commit point — it doesn't widen when the pause gets longer and it doesn't care that the sentence is unfinished. Everything in this video falls out of that one line." |
| **Artifact** | ADR-055; both `pilot_ladder.*.live.json`. |

## 1:16–1:30 — Scope, said out loud

| | |
|---|---|
| **Screen** | Four lines, plain, no animation. |
| **On screen** | • synthetic speech, one macOS `say` voice<br>• never run on a human<br>• 769 tests certify lines executed, not behaviour asserted<br>• live PCR attributes to the wrong gap; TTL and FRAG don't |
| **VO** | "What it isn't: this has never met a real caller. The corpus is one synthesised voice whose own manifest says it isn't good enough for a published number. We're reporting it anyway, and saying so." |
| **Artifact** | `data/corpus/source/manifest.json`; README honest-scope; ADR-053; ADR-055. |

**End card:** `github.com/DeepakKTS/NOD` — no tagline, no claim.

---

## The live screen — now usable, and it was not before

Gate 4h-prep was the first time anyone watched the software run. The screen rendered
nothing for four stacked reasons (ADR-058); all four are fixed, and the owner's first
working session produced this:

```
min  400 → 271 ms      max 1280 → 1839 ms      3 patches      state: warm
reason: "turn — waiting up to 1.84 s (speaker+context)"
```

**The controller widened the listening window to 1.84 s on a human voice, with a reason
attached.** That is the thesis of the project, live, and it is the strongest single shot
available — better than any SVG, because it is unarguably real time.

> **This session is a demonstration, not a measurement.** One operator, one room,
> unscripted speech, no ground truth. **The 1839 ms must never be spoken or captioned
> as a result**, and no number from a screen recording enters any table. The VO below
> is written to that constraint — do not improvise around it.

**Optional shot, inserted at 0:30 in place of the terminal beat.** Screen recording of
`http://127.0.0.1:8000/`: the orb tracking the speaker's voice, the Listening window
stepping `400 → 271` and `1280 → 1839`, the Patches tile climbing, the reason line
appearing under the meter.

| | |
|---|---|
| **VO** | "This is it running. One knob, moving, mid-call — and it says why." |
| **Artifact** | The session's trace under `data/traces/s-*.jsonl`: `config_decision` / `config_applied` pairs with `rule_id`, and `controller_closed` with `turns_observed` and `patches_sent`. |

**Two things must not be filmed.** The **Cuts tile can never leave zero** —
`cut.detected` is never published (ADR-058) — so do not let it sit on screen implying a
measured zero. And the transcript is whatever the operator says, so it is a demo of the
*mechanism*, never a measurement: no number from a live screen recording enters a table.

**The terminal-and-SVG shot list below remains the fallback** and still needs no UI, no
microphone and no deploy. If the screen misbehaves on the day, cut to it and lose
nothing that carries a claim.

## Final shot list — numbered takes, nothing decided with a camera running

Every element below was driven and read out of a real browser at Gate 4i before this
list was written. Each take names the exact action; none requires a judgement call.

**Do not film these, in any take.** They are dead or misleading on the current build:

| Do not film | Why |
|---|---|
| The **Cuts** tile | `cut.detected` is never published, `nod_cuts_total` is unwired. It cannot leave zero, so filming it implies a measured zero (ADR-058). |
| `/metrics` in a browser | `nod_cuts_total`, `nod_decide_seconds` and `nod_queue_dropped_total` still read `0.0` unconditionally. |
| Any **specific number** as a promise | Your session will not reproduce 271/1271/1839. The numbers depend on your voice. Caption the *movement*, never the value. |

### Setup, once

```sh
make run                       # http://127.0.0.1:8000
```

Headphones on. Mute every other audio source. **One** tab, hard-reloaded (⇧⌘R).
Window 1280 wide. DevTools closed.

### Take 1 — cold open, the screen at rest (6 s)

**Action:** load the page, do not click. **Frame:** whole window.
**On screen:** pill `ready`, orb idle, `state cold`, `Patches 0`, Connection showing
Session ✓ and Events subscribed ✓ with the three audio rows grey.
**VO:** "This is a voice agent's turn-timing, before anyone speaks."

### Take 2 — the handshake (6 s)

**Action:** click **Start call**, grant the mic. **Frame:** Connection card, tight.
**On screen:** all five rows go green in sequence, the fifth counting frames.
**VO:** "Every step is verified, not assumed."
*Why it is in the list: this is the panel ADR-058 exists because of.*

### Take 3 — the orb and the transcript (14 s)

**Action:** read Part 1 of the speaking script (the ~35-word paragraph).
**Frame:** orb and transcript together.
**On screen:** orb scales with your voice and turns amber; transcript fills line by line.
**VO:** "It is listening, and transcribing through AssemblyAI."

### Take 4 — the controller moves. **The shot the film is for.** (14 s)

**Action:** keep talking until `state` flips **cold → warm**, then stop.
**Frame:** Listening window card and the reason line under the meter, both visible.
**On screen:** `state` → `warm`, `min` and `max` change, **Patches** climbs off 0, the
reason line appears reading `turn — waiting up to N s (speaker+context)`, and the meter
capsule springs to its new width.

**VO — say all four sentences, in this order:**

> "It has learned this caller's rhythm, and changed the listening window mid-call —
> and it tells you why.
> Where it lands depends on the voice; yours will be a different number from mine.
> The movement is the claim, not the number.
> This is a demonstration, not a measurement."

The last two sentences are **mandatory and must be audible.** The third is what stops
a viewer reconciling the on-screen value against the deck's; the fourth is the label
that appears beside the number in every written asset.

> **Why the caption says "depends on the voice", and why no number is spoken.**
> The same controller on the same config has landed `min_turn_silence` at **1839 ms**
> on one voice and **271 ms** on another — the owner's live session and the Gate 4i
> verification run. That is a 4.7x spread, and it is per-caller adaptation visible in
> two points: precisely the thing the controller exists to do.
>
> **It is deliberately not spoken.** At n = 2 it is two anecdotes, and a voice-over is
> the least qualifiable medium in the submission — the deck can set a red box beside a
> number, speech cannot. "4.7x" would be the most quotable line in the film and the
> least defensible one. So the *reason* the caption is voice-dependent lives here, in
> the repository, where it can carry its own caveat; the film says only that the value
> varies. Smoothing it over as "variance" would have been the dishonest alternative —
> it is not noise, it is the mechanism.

> ### Take 4b — the context axis — **cut, and here is why**
>
> `?context=on` connects the context axis (ADR-059): the reference agent asks five
> scripted intake questions, and the window is multiplied per question — 2.4x for
> spelling, 2.0x for an identifier, **0.7x** for a yes/no.
>
> **It works.** Driven through a real browser over CDP at Gate 4l: the agent asked
> *"Can I take your member number?"*, the chip read `expects: entity id`, ten patches
> went out and the window moved on every turn.
>
> **It is not filmable as a numbered take**, which is a different question and the one
> this format exists to answer. Two reasons, both correct behaviour rather than bugs:
>
> - The arbiter **rate-limits** each change, so the window *moves toward* a class's
>   target across several turns instead of landing on it. There is no frame where the
>   screen reads "2.4x".
> - Looping audio tripped the **anti-oscillation guard** into `FROZEN`
>   (`FREEZE_REVERSALS`), which holds the speaker axis and lets only the context axis
>   through. Correct, and it means the on-screen values are path-dependent.
>
> So a caption for this take cannot be written in advance — the numbers depend on how
> the call goes — and **a take whose caption is decided with a camera running is exactly
> what this document exists to prevent.** Take 4 needs nothing from this path.
>
> The axis stays in the repository, tested (17 unit, 3 integration) and mutation-guarded
> (6/6), and the deck claims it as built rather than filmed.

> ### The two URLs are not the same agent
>
> Anyone assembling the film needs this. **Without the flag** the reference agent replies
> with one constant line, *"Got it — thank you."*, to everything. **With `?context=on`**
> it asks five different intake questions. That is deliberate — the stub brain asks
> nothing, so the axis could never fire without a script (ADR-059) — but it means audio
> recorded on the two URLs will not cut together. **Film every take on the plain URL.**

### Take 5 — the pause (10 s)

**Action:** say *"I need to move it to"* — **stop for a slow two-count** — *"the following
Tuesday."* **Frame:** meter and transcript.
**On screen:** the amber fill runs left to right during your pause; if it reaches the end
it turns red and reads "Agent takes the floor".
**VO:** "That bar is how long it will wait before deciding you are done."

### Take 6 — end (4 s)

**Action:** click **End call**. **Frame:** whole window.
**On screen:** orb greys to `ended`, pill reads `ended`, **no red banner**.
*If a red banner appears, that is a bug — stop and report it, do not film around it.*

### Take 7 — the evidence (8 s, screen recording of a terminal)

```sh
tail -1 data/traces/s-*.jsonl | python3 -m json.tool
```

**On screen:** the `controller_closed` record — `turns_observed`, `patches_sent`,
`state: warm`. **VO:** "And it wrote down what it did."

---

## Optional shots — only if `scripts/deploy_aws.sh` has run

Insert at 0:44, pushing the credibility beat to 0:50 and trimming the model beat by 6 s.

| | |
|---|---|
| **Screen** | Phone, mobile data off wifi, loading `https://<id>.awsapprunner.com/`. |
| **VO** | "It's deployed." |
| **Artifact** | The URL printed by `scripts/deploy_aws.sh`. |

If there is no URL, **cut the shot and say nothing about deployment.** Do not show
`localhost` and let it read as deployed.

---

## Numbers permitted on screen

Nothing not on this list may appear. Each is regenerable.

| Number | Where it comes from |
|---|---|
| 1532 ms, 3532 ms | measured holds, `pilot_ladder.{continuation,sweep}.live.json` `hold_measured_ms`. 532 ms is the **retired** `say` frame the guide forbids rendering |
| 2 turns / 1 turn | `len(boundaries)` per row, drawn by `timeline_svg` |
| 777 / 1170 / 1765 / 2574 ms | sweep rows, held silence |
| 3.3× | 2574 ÷ 777 |
| C ≈ 590 ms, overhead ≈ 175 ms | ADR-055 |
| 400 / 900 / 1600 / 2400 | the swept `min_turn_silence` values |
| 769 tests, 98.18 % | `make gate` **at 79cd368**, the commit the film was cut from. Pinned, not chased: the slide is rendered and the count moves whenever a test is added (ADR-062) |
| MIN_MS_CEIL = 900 | `src/nod_core/arbiter.py` |

**Forbidden on screen, in VO, or in the description:** any claim that Nod beats the
static tradeoff curve; any latency or cut-rate improvement attributed to the controller;
any number from Track C; live PCR as a headline. None of these has an artifact
(ROADMAP §3, Gate 4e cut list).
