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
uv run --extra bench python scripts/pilot_ladder.py svg --label say
uv run --extra bench python scripts/pilot_ladder.py svg --label sweep
```

Open `bench/runs/ladder_say_500.timeline.live.svg` and
`bench/runs/ladder_sweep_3500.timeline.live.svg` in a browser at 1280 px wide, dark
background. Terminal: dark, 16 pt, ~100 cols.

---

## 0:00–0:14 — Cold open. The behaviour, no words about architecture

| | |
|---|---|
| **Screen** | `ladder_say_500.timeline.live.svg`, full width. Three rows: `aggressive`, `balanced`, `conservative`. Same waveform on each. Blue band = the caller's pause. |
| **Action** | Nothing moves for 3 s. Then highlight the right-hand column: **2 turns / 1 turn / 1 turn**. |
| **VO** | "Same sentence, same pause, same speaker. Three turn-detection configs. The top one cut the caller off in the middle of it. The other two didn't." |
| **Artifact** | `bench/runs/pilot_ladder.say.live.json`, row `aggressive` hold 500: `[2855, 4587]`. Row `balanced` hold 500: `[5406]`. |

> The pause is **532 ms**, after *"I need to reschedule my appointment **to**"* — a
> prefix English cannot end on. Drawn in the image caption, so it survives a screenshot.

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
| **VO** | "Two parameters. Thirteen live rows. C is the service's own commit point — it doesn't widen when the pause gets longer and it doesn't care that the sentence is unfinished. Everything in this video falls out of that one line." |
| **Artifact** | ADR-055; both `pilot_ladder.*.live.json`. |

## 1:16–1:30 — Scope, said out loud

| | |
|---|---|
| **Screen** | Four lines, plain, no animation. |
| **On screen** | • synthetic speech, one macOS `say` voice<br>• never run on a human<br>• 740 tests certify lines executed, not behaviour asserted<br>• live PCR attributes to the wrong gap; TTL and FRAG don't |
| **VO** | "What it isn't: this has never met a real caller. The corpus is one synthesised voice whose own manifest says it isn't good enough for a published number. We're reporting it anyway, and saying so." |
| **Artifact** | `data/corpus/source/manifest.json`; README honest-scope; ADR-053; ADR-055. |

**End card:** `github.com/DeepakKTS/NOD` — no tagline, no claim.

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
| 532 ms, 3532 ms | measured holds, `pilot_ladder.*.live.json` geometry |
| 2 turns / 1 turn | `len(boundaries)` per row, drawn by `timeline_svg` |
| 777 / 1170 / 1765 / 2574 ms | sweep rows, held silence |
| 3.2× | 2574 ÷ 777 |
| C ≈ 590 ms, overhead ≈ 175 ms | ADR-055 |
| 400 / 900 / 1600 / 2400 | the swept `min_turn_silence` values |
| 740 tests, 98.18 % | `make gate` |
| MIN_MS_CEIL = 900 | `src/nod_core/arbiter.py` |

**Forbidden on screen, in VO, or in the description:** any claim that Nod beats the
static tradeoff curve; any latency or cut-rate improvement attributed to the controller;
any number from Track C; live PCR as a headline. None of these has an artifact
(ROADMAP §3, Gate 4e cut list).
