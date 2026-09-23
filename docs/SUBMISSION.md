# lablab.ai submission copy

**Hackathon:** AssemblyAI — Voice Agent Hackathon, 1–30 Sep 2026.
**Framing:** (b), the instrument story — a capability probe that measured a production
service, with the controller as what was built to exercise it (Gate 4e, ROADMAP §3).

> Field constraints below are as understood from lablab's public guidance. The
> submission form itself is behind a login and could not be read directly, so **check
> each limit against the live form before pasting.** Where a limit is uncertain the copy
> is written short.

---

## Title — 43 characters

```
Nod — measuring a voice agent's turn timing
```

## Short description — 230 characters

```
We measured AssemblyAI's turn-detection knobs against the live API and published the
transfer function. One knob is inert. One buys a hesitating caller 2.6 seconds. We got
the headline wrong once and the correction is in the repo.
```

## Long description — 354 words

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
777 ms at the vendor default to 2574 ms at 2400 — a 3.2x range. max_turn_silence only
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

16:9. Use a crop of `bench/runs/ladder_say_500.timeline.live.svg` — three arms on the
same waveform, with the turn counts visible on the right. It carries its own provenance
caption, which is the point.

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
