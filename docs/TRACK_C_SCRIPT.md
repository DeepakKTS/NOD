# Track C — the recording script

Five intake scripts, each recorded twice (fluent and hesitant), for ten calls. This
satisfies Phase 0's Track C bullet in `ROADMAP.md` §0 and BENCH_SPEC §2's Track C.

## 0. What the numbers in this document are

**Every figure here is a design target for a recording that does not exist yet.** None
of them is a measurement and none belongs in the README, in `RESULTS.md` or in a slide.
INV-9 governs published numbers; this is a plan, and the distinction is the whole reason
this section is first.

After the session, every word count below must be replaced by the count from the actual
transcript, and the crossing turns re-derived from it. §8 lists exactly what to re-derive
and what would invalidate the plan. A target that survives contact with a real reader is
a lucky guess until it has been checked.

## 1. The arithmetic, and why the tension resolves

CONTROL_SPEC §2.1 computes gaps "only over finalised words within a turn", and
`profiler._ingest_words` reconsiders an unfinalised word on the next event rather than
dropping it, so a turn of `w` finalised words contributes **exactly** `w - 1` gaps. No
gap spans a turn boundary.

Summed over a call, that collapses to one expression:

```
gaps in a call = total words spoken by the caller - total turns
```

So the call needs **24 more caller words than caller turns**. That is the whole budget.
Two consequences follow, and they point in opposite directions:

- **The total is easy.** A ten-prompt call needs 34 words across all ten answers, an
  average of 3.4. Almost any script clears it.
- **The total is not the requirement.** ROADMAP §0 asks for the controller to be warm
  "for most of the recording rather than only at the end". Ten turns of four words reaches
  24 gaps on turn 8 of 10 — it clears the threshold and measures nothing, because the
  controller adapts for two turns and the call ends. **The binding target is the crossing
  turn, not the total**, and every script here crosses by turn 3 of 10–12.

**Each extra turn boundary costs exactly one gap.** If the endpointer splits a 22-word
answer into three turns, the call yields 19 gaps from it instead of 21. Margin is therefore
naturally measured in *turn boundaries tolerated*, and §7 reports it that way per call.

### The tension the ROADMAP names, and how it is resolved

> declared `expected_answer` classes push toward short answers, and short answers
> contribute `w - 1` gaps each

It resolves in three moves, none of which is a workaround:

**1. ~~The identifier classes are the gap-richest turns in the script, not the poorest.~~
MEASURED FALSE AT GATE 8. They are the poorest.** (ADR-039.)

> The original argument ran: "A spoken digit is a word. A member number read aloud as
> 'W seven four one nine two eight eight zero three' is eleven words and ten gaps. A
> surname spelled out is one word per letter." Every sentence of that is true about the
> *speaker* and false about the *profiler*, which counts `words[]` from the transcript.
>
> Measured on the dry run, that exact member number came back as **`w741928803` — one
> token, zero gaps.** The service normalises spoken numerals to digits and re-assembles
> spelled letters into words. Per class, one sample each:
>
> | class | ratio | | class | ratio |
> |---|---|---|---|---|
> | `free` | **1.00** | | `number` | 0.78 |
> | `entity_list` | **1.00** | | `entity_address` | 0.57 |
> | `boolean` | **1.00** | | `entity_date` | 0.50 |
> | | | | `spelling` | 0.36 |
> | | | | `entity_id` | **0.10** |
>
> So the classes that "sound most like short answers" *are* short answers once transcribed,
> and the two the argument named as richest are the two poorest.

**The corrected rule: only `free`, `entity_list` and `boolean` carry the gap budget.** The
five identifier classes are budgeted at **zero gaps**, exactly as `boolean` already was in
§2, and for the same reason — the plan must hold without them. They keep their place in
every script because they are the entire input to the *context axis*, which is unaffected:
the class is declared by the script, not inferred from what came back.

`MIN_GAPS_FOR_WARM` stays at 24. Lowering it to rescue a script would be tuning a
control-law constant against a corpus defect (CLAUDE.md §7, ADR-026).

**2. Prefer prompts whose natural answer is sentence-shaped.** A read-back that invites a
correction, an open "where should this go", a "what else is on the statement" — each draws
a clause rather than a token, and each still classifies. This is authoring discipline, not
a trick: an intake call that asks only for tokens is a form, and a caller filling in a form
does not pause the way a caller explaining something does.

**3. `boolean` is the one place the tension does not resolve, so it is spent sparingly and
late.** See §2.

## 2. How a class is assigned — the rule, and the trap it avoids

**The class is judged from the prompt, never from the answer.** CONTROL_SPEC §3 declares
`expected_answer` as what the *host* expects of the dialogue state. A prompt earns
`boolean` only if it genuinely expects yes or no.

The trap: it is tempting to write "I have you on Marlborough Street — is that right, **or
has it changed?**", declare it `boolean`, and collect the twelve gaps of the sentence the
caller actually says. That prompt does not expect a boolean. Declaring one to harvest gaps
would be the script supplying its own input to the feature it then measures, which ADR-028
and ADR-017 refuse for exactly this reason — and it would be worse than useless, because
`boolean`'s hint is `min_mult 0.7 / max_mult 0.7` (CONTROL_SPEC §3). Narrowing the window
by 30 % on a turn engineered to draw a thirteen-word sentence manufactures premature
cutoffs, and PCR on those turns would report the script's authoring rather than the
controller's behaviour. A flattering-direction error and a published number: the exact
combination CLAUDE.md §5 puts in the first coverage tier.

So, throughout:

- **Either/or prompts are `free`**, not `boolean`. "Move all of them, or just this one?"
  expects neither yes nor no.
- **Genuine yes/no prompts are `boolean`**, and the caller elaborating on one is normal
  and is not the script's doing. "Did anyone tell you it needed renewing?" may well draw
  "Nobody said anything about that when I booked it" — that is a real caller, not a
  manufactured one.
- **Booleans are budgeted at zero gaps** regardless of the target answer, and §7 reports a
  conservative recount on that basis. If the caller says "yes", the plan must still hold.

### The draft this replaces

For the record, so it is not re-proposed. The obvious intake script is confirm/deny:

| Turn | Prompt | Class | w | +gaps |
|---|---|---|---|---|
| 1 | "Can I get your member number?" | `entity_id` | 10 | 9 |
| 2 | "Is that right?" | `boolean` | 1 | 0 |
| 3 | "And your date of birth?" | `entity_date` | 3 | 2 |
| 4 | "Did I get that right?" | `boolean` | 1 | 0 |
| 5 | "What's your zip?" | `number` | 5 | 4 |
| 6 | "Correct?" | `boolean` | 1 | 0 |
| 7 | "Is this about a claim?" | `boolean` | 1 | 0 |
| 8 | "Claim number?" | `entity_id` | 9 | 8 |
| 9 | "Is that the full number?" | `boolean` | 1 | 0 |
| 10 | "Anything else?" | `free` | 3 | 2 |

Thirty-five words, ten turns, **25 gaps — and it crosses 24 on turn 10, the last turn of
the call.** It satisfies the bullet as literally written and measures nothing at all: the
controller warms as the caller hangs up. Half its turns contribute zero gaps. This is the
shape to avoid, and it is the shape an intake script falls into by default.

## 3. Domain, and why it was chosen

Health-plan member services. The choice is doing real work: insurance intake is the one
everyday domain where `entity_id`, `spelling`, `number`, `entity_date`, `entity_address`
and `entity_list` **all** arise without contrivance, because the caller is reading
identifiers off a card and a letter while explaining a problem. In a restaurant booking,
`spelling` and `entity_list` would have to be forced. The class coverage in §7 is a
property of this domain choice, not of clever prompt-writing.

## 4. The five scripts

Notation per row: the agent's prompt, the declared class, the **target** answer, its word
count `w`, its contribution `w - 1`, and the running gap total. **Σ ≥ 24** marks the
crossing turn.

Answers are written the way they are *spoken* — digits as words, letters as words —
because that is what the profiler counts.

### Script A — Lost card, replacement to a new address

| # | Agent prompt | Class | Target answer | w | +g | Σ |
|---|---|---|---|---|---|---|
| 1 | "Thanks for calling member services. Before I pull anything up — tell me in your own words what you're calling about today." | `free` | "I lost my insurance card somewhere between the pharmacy and my car and I need a replacement before my appointment next Thursday" | 22 | 21 | 21 |
| 2 | "I can do that. Read me the member number off your benefits letter — take it slowly, I'll read it back." | `entity_id` | "W seven four one nine two eight eight zero three" | 10 | 9 | **30** |
| 3 | "I have W-seven-four-one-nine-two-eight-eight-zero-three. Is that the whole number, or is there a suffix after it?" | `free` | "That's the whole number there's no suffix on the letter" | 10 | 9 | 39 |
| 4 | "And your date of birth, the way you'd say it out loud?" | `entity_date` | "March the fourteenth nineteen fifty two" | 6 | 5 | 44 |
| 5 | "Where should the new card go? Give me the whole address, including the apartment and the zip." | `entity_address` | "Forty two Marlborough Street apartment three B Boston Massachusetts oh two one one six" | 14 | 13 | 57 |
| 6 | "I want to be sure I have the street right. Could you spell Marlborough for me?" | `spelling` | "M A R L B O R O U G H" | 11 | 10 | 67 |
| 7 | "Who else is on the plan with you? First names are fine." | `entity_list` | "My wife Elena my son Daniel and my daughter Rosa who's still on it until June" | 16 | 15 | 82 |
| 8 | "The appointment you mentioned — what's the copay on the letter you're holding?" | `number` | "It says thirty five dollars for a specialist visit" | 9 | 8 | 90 |
| 9 | "Standard mail takes about a week. Is that soon enough for Thursday?" | `boolean` | "No I'd rather pay for the faster one" | 8 | 7 | 97 |
| 10 | "Anything else I should note on the account while I have you?" | `free` | "No that's everything thank you" | 5 | 4 | 101 |

**111 words, 10 turns, 101 gaps. Crosses 24 on turn 2.**

### Script B — Pharmacy, a prescription that did not transfer

| # | Agent prompt | Class | Target answer | w | +g | Σ |
|---|---|---|---|---|---|---|
| 1 | "Pharmacy benefits. What's going on with the prescription?" | `free` | "The refill my doctor sent over on Friday never showed up and I've been out since Sunday" | 17 | 16 | 16 |
| 2 | "Let me look. What's the Rx number on the last bottle you filled?" | `entity_id` | "Four one nine seven two two six" | 7 | 6 | 22 |
| 3 | "And which pharmacy should I be looking at — the name and the cross street will do." | `entity_address` | "It's the Walgreens at Huntington and Gainsborough right across from the station" | 12 | 11 | **33** |
| 4 | "I see the Huntington store on file but not that one. Do you want me to move all your prescriptions over, or just this one?" | `free` | "Just move all of them I'm not going back to the other one" | 13 | 12 | 45 |
| 5 | "I'll need the prescribing doctor's last name. Spell it for me?" | `spelling` | "K R I S H N A M U R T H Y" | 13 | 12 | 57 |
| 6 | "And when did they send it over — the date, if you have it?" | `entity_date` | "Friday the twelfth of September in the afternoon" | 8 | 7 | 64 |
| 7 | "Which medications are we moving? Name them however you know them." | `entity_list` | "The blood pressure one the metformin and the little blue inhaler" | 11 | 10 | 74 |
| 8 | "How many days of the metformin do you have left?" | `number` | "None I took the last one Sunday morning" | 8 | 7 | 81 |
| 9 | "I'll flag it as urgent. Can you confirm your member number for the pharmacy note?" | `entity_id` | "R three three zero eight one four seven" | 8 | 7 | 88 |
| 10 | "They can have it ready in two hours. Does that work for you?" | `boolean` | "Two hours is fine I'll just walk over after work" | 10 | 9 | 97 |
| 11 | "Anything else while I'm in the account?" | `free` | "No that's it thanks" | 4 | 3 | 100 |

**111 words, 11 turns, 100 gaps. Crosses 24 on turn 3.**

### Script C — Claims, a bill that should not have been charged

| # | Agent prompt | Class | Target answer | w | +g | Σ |
|---|---|---|---|---|---|---|
| 1 | "Claims. What's the bill you're looking at?" | `free` | "I got a bill for eight hundred and forty dollars for an MRI that I was told was covered" | 19 | 18 | 18 |
| 2 | "Let me find it. There's a claim number on the top right of that statement — read it to me." | `entity_id` | "C as in Charlie two zero two six zero four four one nine" | 13 | 12 | **30** |
| 3 | "And the date of service on that statement?" | `entity_date` | "August the twenty ninth this year" | 6 | 5 | 35 |
| 4 | "What does it say you were billed, and what does it say the plan paid?" | `number` | "Eight hundred and forty for me and nothing at all from the plan" | 13 | 12 | 47 |
| 5 | "Which facility was it at? The name and town is enough." | `entity_address` | "Brigham imaging on Francis Street in Boston not the Chestnut Hill one" | 12 | 11 | 58 |
| 6 | "I see a prior authorisation on file but it expired. Did anyone tell you it needed renewing before the scan?" | `boolean` | "Nobody said anything about that when I booked it" | 9 | 8 | 66 |
| 7 | "I'll note the ordering physician. Spell the last name as it appears on the order?" | `spelling` | "A C H T E R B E R G" | 10 | 9 | 75 |
| 8 | "What else is on the statement besides the MRI line?" | `entity_list` | "A radiology reading fee a contrast charge and something called facility services" | 12 | 11 | 86 |
| 9 | "And the total at the bottom, including those?" | `number` | "Nine hundred and twelve dollars and sixty cents" | 8 | 7 | 93 |
| 10 | "I'm going to open an appeal on the authorisation. Do I have your permission to do that on your behalf?" | `boolean` | "Yes" | 1 | 0 | 93 |
| 11 | "It'll take about two weeks. Anything you want me to put in the note?" | `free` | "Just that I was told on the phone beforehand that it was covered" | 13 | 12 | 105 |

**116 words, 11 turns, 105 gaps. Crosses 24 on turn 2.**

Turn 10 is the script's one deliberate single-word turn — a genuine consent question, which
is the one place in an intake call where a bare "yes" is the correct and expected answer.
It sits at turn 10, seventy gaps past warm, where it costs nothing.

### Script D — New enrolment, address and dependants

| # | Agent prompt | Class | Target answer | w | +g | Σ |
|---|---|---|---|---|---|---|
| 1 | "Enrolment. What are we setting up today?" | `free` | "I started a new job in August and I need to get myself and my two kids on the plan before the deadline" | 23 | 22 | 22 |
| 2 | "I'll start the record. Spell your last name for me, letter by letter?" | `spelling` | "V A S Q U E Z" | 7 | 6 | **28** |
| 3 | "And your date of birth?" | `entity_date` | "The second of February nineteen eighty eight" | 7 | 6 | 34 |
| 4 | "Where are you living now? Street, unit, town and zip." | `entity_address` | "Sixteen Dorrance Street unit four Providence Rhode Island oh two nine oh three" | 13 | 12 | 46 |
| 5 | "Is that the same address your employer has on file?" | `boolean` | "No I moved in September so they still have the old one" | 12 | 11 | 57 |
| 6 | "Tell me about the children going on the plan — names and ages." | `entity_list` | "Mateo is nine and Sofia turns six in November" | 9 | 8 | 65 |
| 7 | "Your employer gave you a group number on the onboarding packet. Read it to me?" | `entity_id` | "Six six two dash zero one four" | 7 | 6 | 71 |
| 8 | "And I want the older child's name spelled the way it's on the birth certificate." | `spelling` | "M A T E O no accent on the E" | 10 | 9 | 80 |
| 9 | "How much is coming out of each paycheck on the option you picked?" | `number` | "A hundred and eighteen dollars twice a month" | 8 | 7 | 87 |
| 10 | "When does your coverage need to start?" | `entity_date` | "The first of October if that's still possible" | 8 | 7 | 94 |
| 11 | "It does. Do you want dental on it as well, or medical only for now?" | `free` | "Add the dental my daughter needs braces next year" | 9 | 8 | 102 |
| 12 | "You're all set. Anything you want to ask before I send the confirmation?" | `free` | "How long before the cards actually arrive in the mail" | 10 | 9 | 111 |

**123 words, 12 turns, 111 gaps. Crosses 24 on turn 2.**

This is the script that exercises the context axis hardest: `spelling` (2.4), `entity_list`
(2.2) and `entity_address` (2.0) are the three widest `max_mult` rows in CONTROL_SPEC §3,
and all three appear before turn 7.

### Script E — Urgent care abroad, a reimbursement

| # | Agent prompt | Class | Target answer | w | +g | Σ |
|---|---|---|---|---|---|---|
| 1 | "Member services. What can I help with?" | `free` | "I had to go to an urgent care in Lisbon while I was travelling and I paid for the whole thing myself" | 22 | 21 | 21 |
| 2 | "I can start a reimbursement. What date did you go in?" | `entity_date` | "The nineteenth of July around nine in the evening" | 9 | 8 | **29** |
| 3 | "How much did you pay, and in what currency?" | `number` | "A hundred and forty euros on my credit card" | 9 | 8 | 37 |
| 4 | "Where was the clinic? Whatever's printed on the receipt is fine." | `entity_address` | "Clinica Sao Joao on Rua Castilho in Lisbon Portugal" | 9 | 8 | 45 |
| 5 | "I'll need that spelled for the claim. Spell Castilho?" | `spelling` | "C A S T I L H O" | 8 | 7 | 52 |
| 6 | "What did they actually do for you? List it however the receipt has it." | `entity_list` | "A consultation an ankle scan and a bandage they put on" | 11 | 10 | 62 |
| 7 | "Was any of it paid by a travel insurer?" | `boolean` | "This is the first one nobody else has paid anything" | 10 | 9 | 71 |
| 8 | "I'll attach it to your record. Member number, when you're ready." | `entity_id` | "T eight eight four one zero six six" | 8 | 7 | 78 |
| 9 | "And in your own words, what were you treated for? I have to put a reason on the claim." | `free` | "I rolled my ankle on a cobbled street and it swelled up so badly I couldn't put weight on it" | 20 | 19 | 97 |
| 10 | "Last one — what's the exchange rate line on your card statement, if it shows one?" | `number` | "It came through as a hundred and fifty two dollars" | 10 | 9 | 106 |
| 11 | "That's everything I need. Do you want the reimbursement to the card you paid with, or to the account on file?" | `free` | "Put it back on the same card please" | 8 | 7 | 113 |

**124 words, 11 turns, 113 gaps. Crosses 24 on turn 2.**

## 4a. The turn added at Gate 8 (ADR-039)

The five tables above are left exactly as written, including their `w` and `Σ` columns,
because they are the design targets §0 says to re-derive against a real transcript — and
`Σ` there is computed on the falsified assumption that identifier turns contribute gaps.
**Read §4's `Σ` as historical. The live budget is this section plus §7.**

Under the corrected rule only `free`, `entity_list` and `boolean` carry gaps. Scripts B, C,
D and E then reach 24 too late — turns 4, 6, 5 and 6 — so each gains **one sentence-shaped
turn, inserted as the new turn 2**. Script A already crosses on sentence-shaped turns alone
and is unchanged.

An open question is the right instrument here for a second reason: it is the one prompt
shape the transcriber returns verbatim, so it is the only place the budget can be made
robust without touching what the call is about.

| Script | New turn 2 prompt | Class | Target answer | w | +g |
|---|---|---|---|---|---|
| B | "Before I look that up — what happens if you go a few days without it?" | `free` | "I get headaches by the second day and my doctor said I should not skip it at all" | 18 | 17 |
| C | "Before I pull the claim — what were you told when you booked the scan?" | `free` | "The woman on the phone said it was fully covered and that I would owe nothing at all" | 18 | 17 |
| D | "Before I start the record — what cover did you have before this job?" | `free` | "I was on my previous employer's plan until the end of July and then nothing for a month" | 18 | 17 |
| E | "Before the paperwork — what actually happened out there?" | `free` | "I stepped off a kerb badly and by the time I got back to the hotel I could not walk" | 20 | 19 |

Script A: unchanged, 10 turns.

## 5. The two conditions

Each of the five scripts is recorded **twice by the same speaker, with the same words**:
once fluent, once deliberately hesitant. Ten calls.

Paired rather than ten unrelated scripts, for one reason: the claim Nod makes is that the
same static configuration serves two speakers differently. Holding the words fixed and
varying only the delivery is the only design in which a difference in the measured numbers
can be attributed to the pause structure rather than to the content. Ten unrelated calls
would confound the two.

**Word counts, classes and crossing turns above are identical across both conditions** —
the same words are spoken either way. Only gap *durations* differ, which is the point.

## 6. What the hesitant condition asks the reader to do

Not "read slowly". Slowing the whole delivery changes `speech_rate` (CONTROL_SPEC §2.2),
which carries no weight in the §4 law, and leaves the pause quantiles roughly where they
were. Five specific instructions instead:

1. **Pause inside clauses, not between them.** This is the one that matters most. A pause
   at a clause boundary leaves a *semantically complete* utterance, where the model's own
   gate fires and `min_turn_silence` decides (CONTROL_SPEC §0.2). Nod's case is the
   *incomplete* utterance, where `max_turn_silence` is the only thing that ends the turn —
   so the pause has to land mid-sentence, before a content word: "I need a replacement
   before my … appointment next Thursday." A hesitant read that pauses only at the commas
   measures the wrong gate, and it will look like a successful recording.
2. **Hold each pause for roughly one to two and a half seconds.** That band is chosen
   because it is where the three static arms disagree: above `aggressive`'s 400 ms gate,
   straddling `balanced`'s 1280 ms, below `conservative`'s 3600 ms (BENCH_SPEC §3). Shorter
   and every arm waits; longer and every arm has already cut. Two unhurried beats is a
   usable instruction for a reader; a visible metronome is better.
3. **Break the digit and letter runs into groups, with a pause between groups.** "W seven
   four one … nine two … eight eight zero three." This is the digit-reading shape ADR-022
   derived `MIN_GAPS_FOR_WARM` against, and it is the most characteristic real case in the
   whole corpus.
4. **Use real disfluency where it falls naturally** — a repeated word, a filler, an
   abandoned restart. Note honestly that on the simulated bench path this registers as
   nothing at all (ADR-028): tokens are reconstructed, so repeats, fillers and duration
   outliers all score 0. It is recorded for the Phase 4 live runs, where it is the only
   place §2.3 is exercised on real material.
5. **Do not hesitate on every turn.** A caller who pauses uniformly is a caller with one
   rhythm, and the profiler will fit it in three turns. Vary it: fluent on the narrative
   turns, hesitant on the identifiers, which is also what real callers do.

The fluent condition is the same words with none of the above — ordinary, unhurried,
no mid-clause pauses.

## 7. Summary

### Crossing turns and margin

**Recomputed at Gate 8 under ADR-039's zero-gap budget for identifier classes.** The
previous version of this table assumed a spoken digit was a word and is superseded; margin
is still measured in **turn boundaries tolerated**, since each unplanned endpointer split
inside the turns before crossing costs exactly one gap.

`Σ available` counts gaps from `free`, `entity_list` and `boolean` turns only. Word counts
are nominal, from §4 plus §4a.

| Script | Turns | Words | Σ available | Crosses 24 at | Σ there | Margin | Turns warm |
|---|---|---|---|---|---|---|---|
| A | 10 | 111 | 56 | **turn 3** | 30 | 6 | 7 of 10 |
| B | 12 | 129 | 67 | **turn 2** | 33 | 9 | 10 of 12 |
| C | 12 | 134 | 66 | **turn 2** | 35 | 11 | 10 of 12 |
| D | 13 | 141 | 75 | **turn 2** | 39 | 15 | 11 of 13 |
| E | 12 | 144 | 85 | **turn 2** | 40 | 16 | 10 of 12 |

Every script now crosses on **turn 2 or 3** and is warm for **seven turns or more**, which
is what ROADMAP §0 asks for — "warm for most of the recording rather than only at the end".

**This table is the conservative one and there is no separate conservative recount.** The
old §7 carried a second column re-running the arithmetic with `boolean` floored to one
word; that is now the standing assumption for five more classes as well, so the figures
above already are the floor. Anything the identifier turns actually return is margin on
top of them.

### Class coverage

55 turns across the five scripts.

| Class | `min_mult` / `max_mult` | Turns | Natural in intake? |
|---|---|---|---|
| `free` | 1.0 / 1.0 | 14 | yes — opener, closer, either/or |
| `number` | 1.2 / 1.6 | 7 | yes — amounts, counts, rates |
| `entity_id` | 1.3 / 2.0 | 6 | yes — member, claim, Rx, group numbers |
| `entity_date` | 1.2 / 1.8 | 6 | yes — birth, service, effective dates |
| `spelling` | 1.5 / 2.4 | 6 | yes — surnames, street names |
| `boolean` | 0.7 / 0.7 | 6 | yes — genuine yes/no and consent |
| `entity_address` | 1.3 / 2.0 | 5 | yes — mailing, facility, pharmacy |
| `entity_list` | 1.4 / 2.2 | 5 | yes — dependants, medications, line items |

All eight, every one of them natural. §3 explains why: the domain was chosen for it.

## 8. What must be re-derived after the session, and what would invalidate this

Per §0. Run these before any number from Track C is reported anywhere.

1. **Transcribe each recording and recount.** Replace `w` per turn with the finalised word
   count from the real transcript, recompute `Σ`, and re-report the crossing turn. The
   plan is confirmed only if every call still crosses by turn 3.
2. **Count the turns the endpointer actually produced, per arm.** `gaps = words - turns`,
   so a call that split into more turns than planned yields fewer gaps. If the turn count
   differs *between arms*, the gap count differs between arms too, and the arms are no
   longer comparable on this corpus — see the inter-turn silence requirement below.
3. **Report `n_gaps` at the turn the first patch is emitted**, per call. This is the
   figure that says whether the recording did its job, and it is the one to put in the
   Gate report.
4. **Check the pause durations landed in the 1000–2500 ms band** on the hesitant reads.
   A hesitant condition whose pauses all landed under 400 ms is a fluent recording with a
   different label, and every arm will agree on it.

### The open risk this script cannot fix

**There is no Track C ingestion path yet, and the obvious one loses most of these gaps.**
On the simulated path `replay._turn_from_gaps` reconstructs words from the truth sidecar —
one word per sidecar gap — and `corpus._intrinsic_gaps` records a source silence only if
it is at least `MIN_INTRINSIC_GAP_MS = 100` ms, detected acoustically at 50 ms frame
resolution. Ordinary inter-word gaps in connected fluent speech are mostly shorter than
that and many are acoustically absent altogether. So a 22-word fluent turn that contributes
21 gaps on the live path may contribute a handful through the sidecar, and **the fluent
condition — the control — is the one at risk of never warming offline.** The hesitant
condition is largely safe, because a deliberate pause is by construction well over 100 ms.

This is not a defect in existing code: `MIN_INTRINSIC_GAP_MS` is correctly derived for the
perturbation corpus, where it sits below `aggressive`'s 160 ms gate so that every silence
that could bind a gate is described. It is the wrong rule for Track C, where the profiler
needs every gap and not only the ones that could fire a boundary.

**Measure it before the session, do not assume it.** Record one pilot turn, transcribe it
through the real service, and compare the transcript's inter-word gap count against the
count `_intrinsic_gaps` returns for the same audio. If they diverge as expected, the Track
C sidecar has to be built from the real transcript's word timings rather than from acoustic
silence detection — a Gate 6 Part B decision and an ADR, not something the script can
resolve.

### Recording requirements that follow from the harness

- **Caller audio only.** The agent's prompts are read by an operator off-mic or played
  from TTS and are **cut from the clip**. The bench has no agent audio (ADR-028: this is
  also why `recent_cuts` scores 0), and any agent speech left in the clip would be read as
  caller speech by the VAD and by the intrinsic-gap detector.
- **Inter-turn silence must exceed `conservative`'s 3600 ms gate**, with margin — call it
  4500 ms. The seam where the agent prompt was removed is what ends the turn, and if it is
  shorter than the widest arm's gate, `conservative` sees fewer turns than `aggressive`
  does. Turn counts would then differ by arm, and by `gaps = words - turns` so would the
  gap counts, so the arms would be measured on different profiles. This is the same shape
  as the tail-silence defect recorded in CLAUDE.md §5: assert against the requirement —
  `conservative`'s 3600 ms — never against whatever constant the tooling happens to hold.

### Session logistics

- **Speakers.** BENCH_SPEC §2 says owner-recorded, so one speaker is the floor and the
  plan works at one. Two or three is materially better and costs only their time: with one
  speaker the per-caller claim is demonstrated on a single caller performing two rhythms,
  which is a weaker thing than it sounds. Whoever reads must consent to MIT-licensed audio
  committed to a public repository.
- **Length.** Roughly 110–125 caller words per call. At an unhurried pace the fluent reads
  run about 45–70 s of speech and the hesitant reads about 90–150 s, plus eleven inter-turn
  seams of 4.5 s each. Call it 2–3 minutes of finished audio per call and 20–30 minutes for
  the set. With setup, retakes and a break, budget **two and a half to three and a half
  hours** of a reader's time. These are estimates and are the only figures in this document
  that do not need re-deriving, because nothing is published from them.
- **What not to claim.** A deliberately hesitant read by a fluent speaker is *performed*
  hesitancy. Track B — the real atypical-speech corpus — is cut (ROADMAP §3). Per CLAUDE.md
  §8, nothing built on this recording may claim Nod was measured on older callers,
  non-native speakers or people who stutter. The honest claim is that the controller
  responds to pause structure, measured on pause structure.

---

## 9. The pilot gate — thirty seconds of audio, before you book anyone

**Run this first.** The ingestion path exists and the dry run passed on synthesised audio,
which is the easy case in the direction that matters: every floor in the path thresholds at
**−44.0 dBFS against digital silence, and a room is not digitally silent.** The gap budget
is not recoverable from a recording afterwards (ADR-031), so a failed session costs the
session.

### Checklist

- [ ] **1. Record about thirty seconds**, with the real mic, in the real room, at the real
      distance. One answer, one seam, one answer:
      - speak any sentence of ten words or so;
      - **stop, and hold silence for a slow count of six** (≥ 4.5 s);
      - speak a second sentence.
      Do not clap, do not talk over it, do not stop the recorder during the seam.
- [ ] **2. Export** mono, 16 000 Hz, PCM16 WAV. Anything else fails on sample rate.
- [ ] **3. Write a two-turn script file** — copy `data/trackC/scripts/A-fluent.json`, keep
      the first two `turns` entries, delete the rest. The check compares the seam count
      against this, so it has to say two.
- [ ] **4. Run the check:**

      make trackc-check AUDIO=pilot.wav SCRIPT=pilot-script.json

      or, without make:

      uv run --extra bench python -m nod_bench.trackc check \
          --audio pilot.wav --script pilot-script.json

- [ ] **5. Confirm the pass.** Exactly this, and exit code 0:

      pilot: 1 seams, all clear

      One seam for two turns. Nothing else is a pass — in particular `0 seams` is the
      room-tone failure below, not a near miss.
- [ ] **6. Only if step 5 passed**, transcribe and ingest, and read off the word count:

      uv run --extra bench python -m nod_bench.trackc build \
          --audio pilot.wav --script pilot-script.json --out /tmp/pilot

      Compare the reported word count against what you actually said. **That ratio is the
      single most useful number the pilot produces** — it scales the whole gap budget, and
      ADR-039 is what happens when it is assumed instead of measured. Per §1, expect ~1.00
      on sentence-shaped speech; if a sentence of ten words comes back as six, stop and
      re-derive §7 before booking.

### Failure modes

| Output | What it means | What to do |
|---|---|---|
| `0 seam(s) of at least 4500 ms … needs 1`, and the sub-threshold list is **empty or tiny** | **Room tone.** The seam never drops below −44 dBFS, so nothing downstream can hear it | The two remedies below |
| `0 seam(s) … needs 1`, sub-threshold list shows something **near 4500 ms** | The pause was just too short | Hold it longer; count six, not four |
| `2 silences of at least 4500 ms … expects 1` + `retake rather than relabel` | You paused mid-sentence for over 4.5 s, so that pause ends the turn on every arm and the call really has three turns | Retake. Do not relabel — `gaps = words − turns` is void either way |
| `expected 16000 Hz, found 44100` | Wrong export | Resample to mono 16 kHz PCM16 |
| `no transcribed words` (at step 6) | Mic was muted, or the file is silent | Check the recording plays |

### The room-tone failure, and the two remedies

This is the one to expect, so decide it here rather than at the mic.

`SEAM_FLOOR_DBFS` is **−44.0 dBFS**, and `test_the_seam_floor_matches_the_simulators`
asserts it equals what the simulator's `Endpointer` hears at the default `vad_threshold`.
ADR-031 measured the direction: over 135 frames of `say` speech only **2** reached that
floor, and a person in a room adds room tone, breath and mic self-noise on top. So a seam
that sounds silent can sit at −38 dBFS and be, to every consumer in this repository,
continuous speech.

**Measured, so the boundary is not guesswork.** A synthetic seam of white noise at a
sweep of levels, through the real `check`:

| seam noise floor | result |
|---|---|
| −52, −48, −46 dBFS | `1 seams, all clear` |
| **−44, −42, −38, −30 dBFS** | **`0 seam(s) … Longest sub-threshold silences: none`** |

The break is clean between −46 and −44, and it is slightly *stricter* than the constant
suggests: a run counts as silence only if **every** 50 ms frame in it is below the floor,
so noise hovering at the threshold breaks one long seam into fragments rather than
shortening it. That is why the failure prints `none` rather than a list of near-misses —
there is no partial credit, and a seam either registers whole or not at all. Aim at
**−48 dBFS or quieter**, not at −44.

**Remedy A — gate the seam to digital zeros at the edit. Prefer this.** In the editor,
select each seam and silence it outright (not "fade", not "noise reduction" — replace the
samples with zeros). Thirty seconds of work per call.

**Remedy B — lower `SEAM_FLOOR_DBFS`.** Available, and worse. Three reasons, in order:

1. **It decouples the corpus from what the arms actually hear.** The floor is pinned to the
   simulator's, and the simulator's is what decides whether a seam ends a turn. Lower only
   the ingest's copy and the check starts certifying seams that no arm detects; the turn
   count the check believed is then not the turn count any arm produces, and by
   `gaps = words − turns` every profile differs from the one that was validated.
2. **It moves a constant two other things are calibrated against.** `DEFAULT_VAD` and
   `corpus.INTRINSIC_FLOOR_DBFS` agree with it by meaning rather than by code — the exact
   coincidence CLAUDE.md §5 records — so changing one silently recalibrates intrinsic-gap
   detection on Track A, a corpus already committed and already behind published figures.
3. **It is global where the problem is local.** One noisy room would loosen the threshold
   for every recording ever ingested, including the quiet ones.

Remedy A changes one call's audio and nothing else. Remedy B changes what silence means
everywhere, to fix one room. **If Remedy B ever looks necessary, it is an ADR and a
re-derivation of ADR-018's floor agreement, not a constant edit.**

### The dry run, recorded (2026-09-21)

Script A, `say`-synthesised, 10 turns, 4600 ms seams, 78.8 s. **Not a recording and not a
published number** — the standing note that `say` is adequate for probing and not for
numbers applies (ADR-031).

| | script A predicts | dry run |
|---|---|---|
| caller words | 111 | 84 |
| turns | 10 | 10 |
| gaps | 101 | 74 |
| crosses `MIN_GAPS_FOR_WARM` (24) | turn 2 | **turn 3** |

Patches emitted: `nod` 8, `nod-nocontext` 4, `nod-nospeaker` 8. The three controlled arms
differ from each other and from `balanced` for the first time in the project.

**Warming in the dry run is necessary and not sufficient.** It shows the pipeline is not
broken. It does not show that a recorded call will warm, because every floor in the path is
calibrated against digital silence and a room is not digitally silent.
