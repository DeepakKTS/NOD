"""The context axis, connected to the reference agent (ADR-059).

The multipliers, the policy and `ContextSource` have existed and been guarded
since Phase 2. What never existed was anything on the server path *declaring* a
class, so a caller asked to spell their name got the same window as one
answering yes or no. These tests cover the half that was missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nod_core.policy import load_policy
from nod_server.context import (
    ANSWER_CUES,
    INTAKE_SCRIPT,
    DeclaredContext,
    classify_prompt,
)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Can you spell your surname for me?", "spelling"),
        ("And what is your member number?", "entity_id"),
        ("What is your date of birth?", "entity_date"),
        ("What is your postcode?", "entity_address"),
        ("How many medications are you taking?", "entity_list"),
        ("What is the best contact number for you?", "number"),
        ("Is that correct?", "boolean"),
        ("Lovely weather we're having.", None),
        ("", None),
    ],
)
def test_the_classifier_reads_the_question(question: str, expected: str | None) -> None:
    """Each shape an intake call produces maps to the class it invites."""
    assert classify_prompt(question) == expected


def test_punctuation_and_case_cannot_hide_a_cue() -> None:
    """ "MEMBER NUMBER?" and "member number" are the same question.

    Worth a test rather than a comment: the agent's wording comes from a model
    and will not match the table's casing or spacing, so a matcher that only
    worked on the literal cue would fire almost never and the axis would look
    implemented and be inert — which is precisely its history.
    """
    assert classify_prompt("MEMBER  NUMBER, please?") == "entity_id"
    assert classify_prompt("...spell-it-out for me") == "spelling"


def test_the_most_specific_cue_wins() -> None:
    """ "Spell your postcode" is spelling, not an address.

    Ordering carries meaning here, so it is asserted. A question containing
    cues for two classes must resolve to the one that describes the *answer* —
    the caller is about to say letters, and letters need the wider window.
    """
    assert classify_prompt("Could you spell your postcode for me?") == "spelling"


def test_a_declared_class_applies_to_exactly_one_turn() -> None:
    """EC-13: a multiplier scales the turn it was declared for and is released.

    The caller asked to spell their name is not still spelling on the next
    turn. Consuming on read is the whole mechanism, so a second call returning
    the same class would silently widen every turn after an identifier
    question for the rest of the call.
    """
    ctx = DeclaredContext()
    ctx.declare("spelling")
    assert ctx(1) == "spelling"
    assert ctx(2) is None
    assert ctx(3) is None


def test_an_undeclared_turn_is_neutral_not_a_guess() -> None:
    """`None` yields the policy default, which applies no multiplier at all."""
    ctx = DeclaredContext(load_policy(Path("config/policy.yaml")))
    hint = ctx.hint_for(None)
    assert hint.min_mult == 1.0
    assert hint.max_mult == 1.0


def test_spelling_widens_and_boolean_narrows() -> None:
    """The axis must move the window in both directions, or it is a widener.

    `boolean` is the only class below 1.0 and it is the one that proves the
    policy is a *policy* rather than a bias — asserted against the shipped
    file, so editing `policy.yaml` to make every class generous fails here.
    """
    ctx = DeclaredContext(load_policy(Path("config/policy.yaml")))
    assert ctx.hint_for("spelling").max_mult > 2.0
    assert ctx.hint_for("boolean").max_mult < 1.0
    assert ctx.hint_for("entity_id").max_mult > ctx.hint_for("free").max_mult


def test_every_cue_class_exists_in_the_shipped_policy() -> None:
    """A cue for a class the policy does not define would be a silent no-op.

    `hint_for` falls back to the default for an unknown class, so a typo in
    `ANSWER_CUES` would classify correctly, apply nothing, and look like the
    axis simply having no effect — the failure mode this whole module exists
    to end.
    """
    policy = load_policy(Path("config/policy.yaml"))
    for answer, _cues in ANSWER_CUES:
        hint = policy.hint_for(answer)
        assert (hint.min_mult, hint.max_mult) != (1.0, 1.0), (
            f"{answer} is cued but carries no multiplier in config/policy.yaml"
        )


def test_every_scripted_question_classifies() -> None:
    """A script line that cues nothing silently skips a turn.

    The agent asks, the classifier shrugs, the window stays where the speaker
    axis put it, and the demo shows the axis doing nothing on that turn for no
    visible reason. "Is that everything you needed today?" did exactly that
    until `is that everything` was added to the boolean cues — found by taking
    the call rather than by any test, which is why this one exists.
    """
    unmatched = [q for q in INTAKE_SCRIPT if classify_prompt(q) is None]
    assert not unmatched, f"scripted questions that cue no class: {unmatched}"


def test_the_script_exercises_both_directions_of_the_axis() -> None:
    """The five questions must span a widening class and the narrowing one.

    A script of five identifier questions would demonstrate only that the
    window gets bigger, which is the half a viewer already expects. `boolean`
    is the one class below 1.0 and the script has to reach it, or the demo
    cannot show the axis choosing to be *less* patient.
    """
    classes = {classify_prompt(q) for q in INTAKE_SCRIPT}
    assert "boolean" in classes
    assert classes & {"entity_id", "spelling", "entity_date", "number"}
    assert len(classes) >= 4, f"only {len(classes)} distinct classes: {classes}"
