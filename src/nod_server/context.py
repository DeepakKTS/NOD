"""The context axis, connected to the reference agent (CONTROL_SPEC §3).

**The axis was built, tested, mutation-guarded and invisible.** `config/policy.yaml`
has carried multipliers since Phase 2 — `spelling` widens the window 2.4x, `boolean`
narrows it to 0.7 — and ADR-044 wired `ContextSource` into `SessionProxy`. The console
path passed no source at all, so a caller asked to spell their name got the same window
as one answering yes or no. This module is the missing half.

**Why a keyword classifier and not the LLM.** The agent's reply already goes through a
model, and asking it for a class would be one prompt change. It is deterministic here
instead, for three reasons that outrank the extra accuracy:

- CLAUDE.md §7 forbids an LLM in the turn-timing decision path. This is off that path —
  it runs after a turn has ended, for the *next* one — but a rule table keeps the
  distance obvious rather than arguable.
- INV-7 says tests never touch the network. A deterministic classifier is testable in
  full; a model-derived one is testable only against a recorded fixture.
- A wrong class costs a caller time on one turn. A classifier that cannot be exercised
  offline is one nobody re-checks after the model changes underneath it.

It is a keyword matcher and it is described as one. It recognises the shapes an intake
call actually produces, and returns `None` — which yields the policy default, no
multiplier — for everything else. That default is the honest failure mode: unknown
means unchanged.
"""

from __future__ import annotations

import re
from typing import Final

from nod_core.policy import CompiledPolicy
from nod_core.types import ExpectedAnswer, WindowHint

ANSWER_CUES: Final[tuple[tuple[ExpectedAnswer, tuple[str, ...]], ...]] = (
    # Ordered most specific first: "spell your postcode" is spelling, not an
    # address, and the first match wins.
    (
        "spelling",
        ("spell", "letter by letter", "how do you spell", "spelt", "spelled"),
    ),
    (
        "entity_id",
        (
            "member number",
            "membership number",
            "account number",
            "reference number",
            "policy number",
            "booking reference",
            "customer id",
            "order number",
            "national insurance",
            "card number",
        ),
    ),
    (
        "entity_date",
        (
            "date of birth",
            "what date",
            "which date",
            "when is",
            "when was",
            "what day",
            "appointment was on",
        ),
    ),
    (
        "entity_address",
        ("address", "postcode", "post code", "zip code", "where do you live"),
    ),
    (
        "entity_list",
        (
            "list ",
            "which ones",
            "what are all",
            "anything else",
            "each of the",
            "medications",
            "symptoms",
        ),
    ),
    (
        "number",
        (
            "how many",
            "how much",
            "what number",
            "phone number",
            "telephone",
            "contact number",
            "how old",
        ),
    ),
    (
        "boolean",
        (
            "is that correct",
            "is that right",
            "is that everything",
            "is that all",
            "anything more",
            "can you confirm",
            "would you like",
            "do you want",
            "shall i",
            "are you",
            "did you",
            "yes or no",
        ),
    ),
)
"""Phrase cues per answer class, most specific first.

Transcribed from the shapes `docs/TRACK_C_SCRIPT.md` actually asks. Not learned,
not tuned against a held-out set, and not claimed to generalise past an intake
call — it is a lookup table for a reference agent, and anything it does not
recognise falls through to `None`.
"""

INTAKE_SCRIPT: Final[tuple[str, ...]] = (
    "Thanks for calling. Can I take your member number?",
    "And your date of birth?",
    "Could you spell your surname for me?",
    "What is the best contact number for you?",
    "Is that everything you needed today?",
)
"""The reference intake agent's questions, in order (ADR-035 clause 1).

**Scripted because the stub brain is a constant.** With no LLM key configured,
`reply()` returns the same sentence every time — "Got it — thank you." — which
asks nothing and therefore cues nothing. The context axis would have been
switched on and still never fired: implemented and inert, which is the exact
state this module was written to end, arriving one layer up.

The five lines walk the axis across its range in a single call —
`entity_id`, `entity_date`, `spelling`, `number`, `boolean` — so the widest and
the *narrowest* multiplier are both exercised by simply taking the call. Used
only on the opted-in path, so the default demo keeps the agent it was recorded
with. A configured LLM takes precedence: this is the floor, not a ceiling.
"""

_WORD_BOUNDARY: Final = re.compile(r"[^a-z0-9]+")


def classify_prompt(text: str) -> ExpectedAnswer | None:
    """The answer class the agent's question invites. Pure. `O(cues)`.

    Matches on a normalised copy so punctuation and case cannot hide a cue —
    "Member Number?" and "member number" are the same question.

    Args:
        text: What the agent is about to say.

    Returns:
        The class, or `None` when nothing matches. `None` is not a failure: it
        yields the policy default, which applies no multiplier at all, so an
        unrecognised question leaves the window exactly where the speaker axis
        put it.
    """
    if not text:
        return None
    hay = " " + _WORD_BOUNDARY.sub(" ", text.lower()).strip() + " "
    for answer, cues in ANSWER_CUES:
        for cue in cues:
            if _WORD_BOUNDARY.sub(" ", cue).strip() in hay:
                return answer
    return None


class DeclaredContext:
    """The class the agent declared, offered to the controller for one turn.

    **One turn, then released** (EC-13). A multiplier scales the window for the
    answer it was declared for and must not leak into the next one: the caller
    who was asked to spell their name is not still spelling on the turn after.
    `SessionProxy` calls this once per turn, so consuming on read is the whole
    mechanism.

    Not thread-safe and does not need to be: one session, one event loop.
    """

    __slots__ = ("_asked", "_pending", "_policy")

    def __init__(self, policy: CompiledPolicy | None = None) -> None:
        """Hold the compiled policy for this session.

        Args:
            policy: The compiled multipliers. `None` leaves every hint neutral,
                which is what an unconfigured deployment gets rather than a
                crash.
        """
        self._pending: ExpectedAnswer | None = None
        self._policy = policy
        self._asked = 0

    def declare(self, answer: ExpectedAnswer | None) -> None:
        """Record what the agent's next question expects. `O(1)`."""
        self._pending = answer

    def __call__(self, turn_order: int) -> ExpectedAnswer | None:
        """The class for this turn, consumed on read. `O(1)`.

        Args:
            turn_order: Which caller turn is being decided. Unused — the agent
                declares against "the next answer", not against an index, and
                pretending otherwise would invent a precision the dialogue
                state does not have.

        Returns:
            The declared class, or `None` once it has been consumed.
        """
        pending, self._pending = self._pending, None
        return pending

    def hint_for(self, declared: ExpectedAnswer | None) -> WindowHint:
        """The multipliers for a class. `O(1)`.

        Neutral without a policy, matching `ClipContext` — the axis degrades to
        "no effect" rather than to an error, because a missing policy file must
        not drop a live call (INV-8's direction).

        Args:
            declared: The class from `__call__`, or `None`.

        Returns:
            The window multipliers to apply for this one turn.
        """
        if self._policy is None:
            return WindowHint(min_mult=1.0, max_mult=1.0)
        return self._policy.hint_for(declared)

    def next_prompt(self) -> str:
        """The scripted agent's next question, advancing the script. `O(1)`.

        Lives here rather than on `SessionRecord` because that record is
        `frozen=True` per CLAUDE.md §6 — every dataclass crossing a module
        boundary is — so the mutable turn counter belongs on the one per-session
        object that is already mutable.

        Returns:
            One question from `INTAKE_SCRIPT`, cycling.
        """
        prompt = INTAKE_SCRIPT[self._asked % len(INTAKE_SCRIPT)]
        self._asked += 1
        return prompt
