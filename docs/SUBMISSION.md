# lablab.ai submission copy

**Hackathon:** AssemblyAI — Voice Agent Hackathon, 1–30 Sep 2026.
**Framing:** (b), the instrument story — a capability probe that measured a production
service, with the controller as what was built to exercise it (Gate 4e, ROADMAP §3).

> ## Every numeric constraint below is **inferred, not stated**.
>
> Checked again at Gate 4h-prep against the event page itself
> (`lablab.ai/ai-hackathons/assemblyai-voice-agent-hackathon`): it states the dates
> ("Sep 1–30, 2026") and the prize ("$10,000 Prize Pool") **and no field constraints at
> all**. `lablab.ai/delivering-your-hackathon-solution` and `/hackathon-rules` render
> their content in JavaScript and return only a page title to a fetch, and the
> submission form is behind a login.
>
> So the limits marked below — 50 characters, 255 characters, 100 words, MP4 under
> 300 MB and 5 minutes, PDF, public repository — come from **search-result summaries of
> lablab's guidance, not from any page this project has read**. They are written as
> `inferred` for that reason. **Check every one against the live form before pasting.**
> The copy is deliberately written short so that a tighter real limit does not require
> a rewrite.

---

## Title — 43 characters (limit 50, *inferred*)

```
Nod — measuring a voice agent's turn timing
```

## Short description — 230 characters (limit 255, *inferred*)

```
We measured AssemblyAI's turn-detection knobs against the live API and published the
transfer function. One knob is inert. One buys a hesitating caller 2.6 seconds. We got
the headline wrong once and the correction is in the repo.
```

## Long description — 354 words (minimum 100, *inferred*)

```
Every voice agent picks one silence threshold and applies it to everyone who calls.
That threshold is a compromise tuned for an average speaker, and it cuts off the people
who pause mid-sentence: older callers, non-native speakers, anyone reading a number off
a card.

Nod is two things. It is a benchmark that measures what AssemblyAI's turn-detection
knobs actually do on the live streaming API, and it is a controller that moves them
mid-call. The benchmark is the part with results.

What we measured, across sixteen live sessions on a pause ladder plus a sixty-nine
session capability matrix:

The end_of_turn_confidence_threshold does not gate endpointing on
universal-streaming-english. Arms at the documented endpoints 0.0 and 1.0 landed six
milliseconds apart, with the high arm firing earlier.

Turns end on a clamp rather than on either gate:
fire_at = clamp(C, min_turn_silence, max_turn_silence) + overhead, with C around 590 ms
and overhead around 175 ms. C is the service's own commit point. It does not widen when
the pause gets longer, and it does not care that the sentence is grammatically
unfinished.

So min_turn_silence is the lever that buys a hesitating caller time, continuously, from
777 ms at the vendor default to 2574 ms at 2400 — a 3.3x range. max_turn_silence only
binds below C, where binding cuts the caller off sooner, or at end of stream.

And the service's word-timing field is not a usable silence anchor: on one clip it puts
a turn's last word 560 ms late and the next turn's 190 to 250 ms early, in the same
session, so it is not a clock offset you can subtract out.

The fifth thing we found is that we had published the opposite. Our previous headline
said the regime the controller exists for was unreachable. The observation behind it
reproduced exactly; the conclusion was wrong, because two knobs bracket the same
quantity and we had varied one. The correction also says our own controller is aimed at
the wrong knob. Both ADRs are in the repository, in order.

The corpus is synthetic speech from one macOS voice. Nod has never been run on a human.
```

**Counts are measured, not estimated.** The three above were written by eye first and
all three were wrong (44/238/341 against an actual 43/230/354). Re-measure after any
edit rather than adjusting the label:

```sh
python3 - <<'EOF'
import re, pathlib
b = re.findall(r"```\n(.*?)\n```", pathlib.Path("docs/SUBMISSION.md").read_text(), re.S)
print("title", len(b[0].strip()), "| short", len(b[1].strip()), "| long", len(b[2].split()))
EOF
```

## Technology tags

```
AssemblyAI · Python · FastAPI · WebSockets · asyncio · pytest · numpy
```

## Category tags

```
Voice Agents · Developer Tools · Benchmarking · Accessibility
```

## Repository

```
https://github.com/DeepakKTS/NOD
```

**Must be public before submitting.** It is private as of this writing.
`gh repo edit DeepakKTS/NOD --visibility public`. History was scanned across all refs
and all unreachable objects at Gate 4f: no credential of any shape, `.env` never
tracked.

## Application URL

Blocked. See `docs/DEPLOYMENT.md` — `scripts/deploy_aws.sh` builds remotely on
CodeBuild and runs on App Runner; it needs credentials permitted to create two IAM
roles and has not been run.

## Cover image

**`docs/nod-cover.png`** — 1280x720, exactly 16:9, `make cover` from `docs/cover.html`.
The continuation frame: four arms on the same waveform and the same 1532 ms pause, turn
counts **2 / 2 / 1 / 1** legible on the right, provenance drawn inside the embedded SVG
so it survives being cropped or rehosted. Not the 532 ms frame — `aggressive` splits
there too, but mid-prefix rather than at the pause (ADR-056).

---

## Claims deliberately absent

Kept here so a later edit does not quietly reintroduce them. None of these has a
traceable artifact.

- That Nod beats, or sits off, the static tradeoff curve. The controlled arms read
  identically to the baseline in the only live sweep, and the patch census is
  **unmeasured, not zero** (ADR-050).
- Any latency or cut-rate improvement attributed to the controller.
- Any number from Track C, or any claim about older, non-native or disfluent speakers
  as measured. Track C is written and unrecorded; Track B is cut.
- Live PCR as a headline figure — it reads the word-timing field in fact four above
  (ADR-055).
- "Real-time adaptive AI that learns each caller." The profiler cannot warm on the only
  corpus that has been run live.
