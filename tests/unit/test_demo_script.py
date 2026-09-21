"""The demo call script's gap budget.

ADR-035's clause 2 is "the Floor Meter visibly grows on a hesitant caller", and
it only grows once the profiler warms. Warming needs `MIN_GAPS_FOR_WARM` gaps
by `gaps = words - turns` (TRACK_C_SCRIPT §1), so the demo call is budgeted the
same way a Track C call is — **before** the camera is on, because the budget is
not recoverable from a recording afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

from nod_core.policy import load_policy
from nod_core.profiler import MIN_GAPS_FOR_WARM

SCRIPT = Path("data/trackC/scripts/demo.json")
ANSWERS = Path("data/trackC/scripts/demo-answers.json")

TRANSCRIPTION_RATIO = 0.76
"""Finalised words per scripted word, measured on the Gate 8 dry run.

84 words returned against 111 scripted (TRACK_C_SCRIPT §9). Applied as a
haircut rather than trusted away: a script that only warms at its nominal word
count warms nowhere near the turn it claims. Not a published number — it sizes
a demo, and nothing is reported from it.
"""

CROSS_BY_TURN = 4
"""The demo must be warm with turns to spare, not on its last one.

TRACK_C_SCRIPT §1 records the failure mode: a script that crosses on turn 10 of
10 satisfies the threshold and measures nothing, because the controller adapts
as the caller hangs up. On camera that is a Floor Meter that never moves.
"""


def _turns() -> list[tuple[str, int]]:
    script = json.loads(SCRIPT.read_text())
    answers = json.loads(ANSWERS.read_text())
    assert len(script["turns"]) == len(answers), "a prompt with no answer"
    return [
        (t["expected_answer"], len(a["answer"].split()))
        for t, a in zip(script["turns"], answers, strict=True)
    ]


def test_the_demo_script_warms_the_profiler_with_turns_to_spare() -> None:
    """At the measured transcription ratio, not at the nominal word count."""
    running, crossed_at = 0, None
    for index, (_, words) in enumerate(_turns(), start=1):
        running += max(0, round(words * TRANSCRIPTION_RATIO) - 1)
        if crossed_at is None and running >= MIN_GAPS_FOR_WARM:
            crossed_at = index
    assert crossed_at is not None, f"never reaches {MIN_GAPS_FOR_WARM} gaps"
    assert crossed_at <= CROSS_BY_TURN, f"crosses on turn {crossed_at}"
    assert len(_turns()) - crossed_at >= 4, "too few warm turns to show anything"


def test_every_demo_turn_declares_a_class_the_policy_knows() -> None:
    """An undeclared class silently takes the default and narrows nothing."""
    policy = load_policy(Path("config/policy.yaml"))
    default = policy.hint_for(None)
    declared = [c for c, _ in _turns()]
    assert None not in declared
    named = [c for c in declared if policy.hint_for(c) != default]  # type: ignore[arg-type]
    assert len(named) >= 5, "the context axis barely moves on this script"


def test_the_demo_script_does_not_open_with_a_boolean() -> None:
    """`boolean` is the one gap-poor class and narrows the window 30 %.

    ADR-029: spending it early both starves the profiler and manufactures a
    cutoff on the turn the demo opens with.
    """
    classes = [c for c, _ in _turns()]
    assert classes[0] != "boolean"
    assert classes.index("boolean") >= 4 if "boolean" in classes else True
