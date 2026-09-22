"""Generate the Track C script and answer files from TRACK_C_SCRIPT.md §4 + §4a.

Gate 4a Step 2. Scripts B-E existed only as prose tables; ADR-039's amendment
(one sentence-shaped turn inserted as the new turn 2) existed only as a table in
§4a. This transcribes both into the `CallScript` schema.

It is a generator rather than thirteen hand-written files because §7 publishes
four figures per script — turns, words, `Σ available`, crossing turn — and those
are checkable. Hand-writing the JSON would have made them assertions about what
someone believed; computing them makes the run fail if the transcription drifts
from the document. Every figure below is asserted, not printed.

    uv run python scripts/build_trackc_scripts.py --check
    uv run python scripts/build_trackc_scripts.py --write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final, NamedTuple

OUT: Final = Path("data/trackC/scripts")

GAP_BEARING: Final = frozenset({"free", "entity_list", "boolean"})
"""Classes that contribute gaps (ADR-039 §1).

The five identifier classes are budgeted at **zero** regardless of how many words
were spoken: the service renders spoken numerals as digits and re-assembles spelled
letters into words, so "W seven four one nine two eight eight zero three" comes back
as one token. They keep their place in the scripts because they are the whole input
to the context axis, which is declared by the script and not inferred.
"""

MIN_GAPS_FOR_WARM: Final = 24
"""`profiler.MIN_GAPS_FOR_WARM` (ADR-022). Restated, not imported.

Asserting the relation rather than deriving it: if the control-law constant moves,
these scripts should fail loudly and make someone decide, not silently re-plan the
recording session around a new threshold.
"""

CONDITIONS: Final = ("fluent", "hesitant")
"""§5: each script is recorded twice by the same speaker with the same words."""


class Turn(NamedTuple):
    """One scripted prompt, its declared class, and the target answer."""

    prompt: str
    cls: str
    answer: str


class Expected(NamedTuple):
    """§7's published figures for one script. The generator asserts all four."""

    turns: int
    words: int
    sigma_available: int
    crosses_at: int
    sigma_there: int


# --- §4 + §4a, transcribed --------------------------------------------------
# Answers are written the way they are *spoken* — digits as words, letters as
# words — because that is what the profiler counts (§4's preamble).

SCRIPTS: Final[dict[str, list[Turn]]] = {
    "A": [
        Turn(
            "Thanks for calling member services. Before I pull anything up, tell me"
            " in your own words what you're calling about today.",
            "free",
            "I lost my insurance card somewhere between the pharmacy and my car and"
            " I need a replacement before my appointment next Thursday",
        ),
        Turn(
            "I can do that. Read me the member number off your benefits letter.",
            "entity_id",
            "W seven four one nine two eight eight zero three",
        ),
        Turn(
            "Is that the whole number, or is there a suffix after it?",
            "free",
            "That's the whole number there's no suffix on the letter",
        ),
        Turn(
            "And your date of birth, the way you'd say it out loud?",
            "entity_date",
            "March the fourteenth nineteen fifty two",
        ),
        Turn(
            "Where should the new card go? Give me the whole address.",
            "entity_address",
            "Forty two Marlborough Street apartment three B Boston Massachusetts oh"
            " two one one six",
        ),
        Turn(
            "Could you spell Marlborough for me?",
            "spelling",
            "M A R L B O R O U G H",
        ),
        Turn(
            "Who else is on the plan with you? First names are fine.",
            "entity_list",
            "My wife Elena my son Daniel and my daughter Rosa who's still on it until"
            " June",
        ),
        Turn(
            "What's the copay on the letter you're holding?",
            "number",
            "It says thirty five dollars for a specialist visit",
        ),
        Turn(
            "Standard mail takes about a week. Is that soon enough for Thursday?",
            "boolean",
            "No I'd rather pay for the faster one",
        ),
        Turn(
            "Anything else I should note on the account while I have you?",
            "free",
            "No that's everything thank you",
        ),
    ],
    "B": [
        Turn(
            "Pharmacy benefits. What's going on with the prescription?",
            "free",
            "The refill my doctor sent over on Friday never showed up and I've been"
            " out since Sunday",
        ),
        # §4a, ADR-039: the inserted sentence-shaped turn.
        Turn(
            "Before I look that up, what happens if you go a few days without it?",
            "free",
            "I get headaches by the second day and my doctor said I should not skip"
            " it at all",
        ),
        Turn(
            "Let me look. What's the Rx number on the last bottle you filled?",
            "entity_id",
            "Four one nine seven two two six",
        ),
        Turn(
            "And which pharmacy should I be looking at? The name and the cross street"
            " will do.",
            "entity_address",
            "It's the Walgreens at Huntington and Gainsborough right across from the"
            " station",
        ),
        Turn(
            "I see the Huntington store on file but not that one. Do you want me to"
            " move all your prescriptions over, or just this one?",
            "free",
            "Just move all of them I'm not going back to the other one",
        ),
        Turn(
            "I'll need the prescribing doctor's last name. Spell it for me?",
            "spelling",
            "K R I S H N A M U R T H Y",
        ),
        Turn(
            "And when did they send it over, the date, if you have it?",
            "entity_date",
            "Friday the twelfth of September in the afternoon",
        ),
        Turn(
            "Which medications are we moving? Name them however you know them.",
            "entity_list",
            "The blood pressure one the metformin and the little blue inhaler",
        ),
        Turn(
            "How many days of the metformin do you have left?",
            "number",
            "None I took the last one Sunday morning",
        ),
        Turn(
            "I'll flag it as urgent. Can you confirm your member number for the"
            " pharmacy note?",
            "entity_id",
            "R three three zero eight one four seven",
        ),
        Turn(
            "They can have it ready in two hours. Does that work for you?",
            "boolean",
            "Two hours is fine I'll just walk over after work",
        ),
        Turn(
            "Anything else while I'm in the account?",
            "free",
            "No that's it thanks",
        ),
    ],
    "C": [
        Turn(
            "Claims. What's the bill you're looking at?",
            "free",
            "I got a bill for eight hundred and forty dollars for an MRI that I was"
            " told was covered",
        ),
        Turn(
            "Before I pull the claim, what were you told when you booked the scan?",
            "free",
            "The woman on the phone said it was fully covered and that I would owe"
            " nothing at all",
        ),
        Turn(
            "Let me find it. There's a claim number on the top right of that"
            " statement. Read it to me.",
            "entity_id",
            "C as in Charlie two zero two six zero four four one nine",
        ),
        Turn(
            "And the date of service on that statement?",
            "entity_date",
            "August the twenty ninth this year",
        ),
        Turn(
            "What does it say you were billed, and what does it say the plan paid?",
            "number",
            "Eight hundred and forty for me and nothing at all from the plan",
        ),
        Turn(
            "Which facility was it at? The name and town is enough.",
            "entity_address",
            "Brigham imaging on Francis Street in Boston not the Chestnut Hill one",
        ),
        Turn(
            "I see a prior authorisation on file but it expired. Did anyone tell you"
            " it needed renewing before the scan?",
            "boolean",
            "Nobody said anything about that when I booked it",
        ),
        Turn(
            "I'll note the ordering physician. Spell the last name as it appears on"
            " the order?",
            "spelling",
            "A C H T E R B E R G",
        ),
        Turn(
            "What else is on the statement besides the MRI line?",
            "entity_list",
            "A radiology reading fee a contrast charge and something called facility"
            " services",
        ),
        Turn(
            "And the total at the bottom, including those?",
            "number",
            "Nine hundred and twelve dollars and sixty cents",
        ),
        # §4's note: the one deliberate single-word turn, a genuine consent
        # question, sitting seventy gaps past warm where it costs nothing.
        Turn(
            "I'm going to open an appeal on the authorisation. Do I have your"
            " permission to do that on your behalf?",
            "boolean",
            "Yes",
        ),
        Turn(
            "It'll take about two weeks. Anything you want me to put in the note?",
            "free",
            "Just that I was told on the phone beforehand that it was covered",
        ),
    ],
    "D": [
        Turn(
            "Enrolment. What are we setting up today?",
            "free",
            "I started a new job in August and I need to get myself and my two kids"
            " on the plan before the deadline",
        ),
        Turn(
            "Before I start the record, what cover did you have before this job?",
            "free",
            "I was on my previous employer's plan until the end of July and then"
            " nothing for a month",
        ),
        Turn(
            "I'll start the record. Spell your last name for me, letter by letter?",
            "spelling",
            "V A S Q U E Z",
        ),
        Turn(
            "And your date of birth?",
            "entity_date",
            "The second of February nineteen eighty eight",
        ),
        Turn(
            "Where are you living now? Street, unit, town and zip.",
            "entity_address",
            "Sixteen Dorrance Street unit four Providence Rhode Island oh two nine oh"
            " three",
        ),
        Turn(
            "Is that the same address your employer has on file?",
            "boolean",
            "No I moved in September so they still have the old one",
        ),
        Turn(
            "Tell me about the children going on the plan, names and ages.",
            "entity_list",
            "Mateo is nine and Sofia turns six in November",
        ),
        Turn(
            "Your employer gave you a group number on the onboarding packet. Read it"
            " to me?",
            "entity_id",
            "Six six two dash zero one four",
        ),
        Turn(
            "And I want the older child's name spelled the way it's on the birth"
            " certificate.",
            "spelling",
            "M A T E O no accent on the E",
        ),
        Turn(
            "How much is coming out of each paycheck on the option you picked?",
            "number",
            "A hundred and eighteen dollars twice a month",
        ),
        Turn(
            "When does your coverage need to start?",
            "entity_date",
            "The first of October if that's still possible",
        ),
        Turn(
            "It does. Do you want dental on it as well, or medical only for now?",
            "free",
            "Add the dental my daughter needs braces next year",
        ),
        Turn(
            "You're all set. Anything you want to ask before I send the confirmation?",
            "free",
            "How long before the cards actually arrive in the mail",
        ),
    ],
    "E": [
        Turn(
            "Member services. What can I help with?",
            "free",
            "I had to go to an urgent care in Lisbon while I was travelling and I paid"
            " for the whole thing myself",
        ),
        Turn(
            "Before the paperwork, what actually happened out there?",
            "free",
            "I stepped off a kerb badly and by the time I got back to the hotel I"
            " could not walk",
        ),
        Turn(
            "I can start a reimbursement. What date did you go in?",
            "entity_date",
            "The nineteenth of July around nine in the evening",
        ),
        Turn(
            "How much did you pay, and in what currency?",
            "number",
            "A hundred and forty euros on my credit card",
        ),
        Turn(
            "Where was the clinic? Whatever's printed on the receipt is fine.",
            "entity_address",
            "Clinica Sao Joao on Rua Castilho in Lisbon Portugal",
        ),
        Turn(
            "I'll need that spelled for the claim. Spell Castilho?",
            "spelling",
            "C A S T I L H O",
        ),
        Turn(
            "What did they actually do for you? List it however the receipt has it.",
            "entity_list",
            "A consultation an ankle scan and a bandage they put on",
        ),
        Turn(
            "Was any of it paid by a travel insurer?",
            "boolean",
            "This is the first one nobody else has paid anything",
        ),
        Turn(
            "I'll attach it to your record. Member number, when you're ready.",
            "entity_id",
            "T eight eight four one zero six six",
        ),
        Turn(
            "And in your own words, what were you treated for? I have to put a reason"
            " on the claim.",
            "free",
            "I rolled my ankle on a cobbled street and it swelled up so badly I"
            " couldn't put weight on it",
        ),
        Turn(
            "Last one, what's the exchange rate line on your card statement, if it"
            " shows one?",
            "number",
            "It came through as a hundred and fifty two dollars",
        ),
        Turn(
            "That's everything I need. Do you want the reimbursement to the card you"
            " paid with, or to the account on file?",
            "free",
            "Put it back on the same card please",
        ),
    ],
}

EXPECTED: Final[dict[str, Expected]] = {
    "A": Expected(
        turns=10, words=111, sigma_available=56, crosses_at=3, sigma_there=30
    ),
    "B": Expected(
        turns=12, words=129, sigma_available=67, crosses_at=2, sigma_there=33
    ),
    "C": Expected(
        turns=12, words=134, sigma_available=66, crosses_at=2, sigma_there=35
    ),
    "D": Expected(
        turns=13, words=141, sigma_available=75, crosses_at=2, sigma_there=39
    ),
    "E": Expected(
        turns=12, words=144, sigma_available=85, crosses_at=2, sigma_there=40
    ),
}
"""§7's table, restated so the transcription above is checked against it."""


def audit(script_id: str, turns: list[Turn]) -> tuple[Expected, list[str]]:
    """Recompute §7's four figures from the turns. Returns (measured, failures)."""
    words = sum(len(t.answer.split()) for t in turns)
    sigma = 0
    crosses_at = 0
    sigma_there = 0
    for index, turn in enumerate(turns, start=1):
        if turn.cls in GAP_BEARING:
            sigma += len(turn.answer.split()) - 1
        if not crosses_at and sigma >= MIN_GAPS_FOR_WARM:
            crosses_at, sigma_there = index, sigma
    measured = Expected(len(turns), words, sigma, crosses_at, sigma_there)
    want = EXPECTED[script_id]
    failures = [
        f"{script_id}.{field}: §7 says {getattr(want, field)}, turns give {got}"
        for field, got in measured._asdict().items()
        if getattr(want, field) != got
    ]
    return measured, failures


def main() -> int:
    """Audit every script against §7, then write the files on `--write`.

    Returns:
        `0` when all five reproduce §7, `1` when any figure disagrees.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []
    for script_id, turns in SCRIPTS.items():
        measured, bad = audit(script_id, turns)
        failures.extend(bad)
        print(
            f"{script_id}: {measured.turns:2d} turns  {measured.words:3d} words  "
            f"Σ={measured.sigma_available:3d}  crosses at turn {measured.crosses_at} "
            f"(Σ={measured.sigma_there})"
        )
    if failures:
        print("\nFAILED against TRACK_C_SCRIPT §7:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("\nall five reproduce §7 exactly")

    if not args.write:
        print("(dry run; pass --write to emit)")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    written = 0
    for script_id, turns in SCRIPTS.items():
        for condition in CONDITIONS:
            payload = {
                "script_id": script_id,
                "condition": condition,
                "turns": [
                    {"order": i, "prompt": t.prompt, "expected_answer": t.cls}
                    for i, t in enumerate(turns, start=1)
                ],
            }
            path = OUT / f"{script_id}-{condition}.json"
            path.write_text(json.dumps(payload, indent=1) + "\n")
            written += 1
        answers = OUT / f"{script_id}-answers.json"
        answers.write_text(json.dumps([t.answer for t in turns], indent=1) + "\n")
        written += 1
    print(f"wrote {written} files to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
