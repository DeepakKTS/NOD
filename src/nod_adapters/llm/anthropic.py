"""The reference intake agent's brain: one Claude call, no streaming.

**Thin on purpose, per ADR-035 clause 1.** The ROADMAP's Phase 3 bullet asks
for streaming, a filler on a slow LLM and false-barge recovery; ADR-035 cut all
three and named the cut. What is left is the smallest thing that makes a call a
call: caller turn in, one sentence out.

CLAUDE.md §7 is the constraint that shapes this file: **no LLM call is on the
turn-timing decision path.** The arbiter never waits on this, never sees its
output, and a total outage here degrades the agent's *words* while leaving the
controller's *timing* untouched. That is why `reply` returns a fallback string
rather than raising.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Final

from nod_adapters.protocols import Message

MODEL: Final = "claude-sonnet-5"
"""The completion model. Latest Sonnet; the reply is one short sentence."""

MAX_TOKENS: Final = 96
"""Cap on the reply. An intake agent that monologues is a demo that drags."""

TIMEOUT_MS: Final = 4000
"""Give up and fall back. Milliseconds.

There is no filler (ADR-035), so a slow call is dead air rather than "let me
just check that" — which is exactly why the budget is tight and the fallback
is a real sentence instead of an error.
"""

SYSTEM: Final = (
    "You are a health-plan member services agent taking an intake call. "
    "Reply with ONE short sentence acknowledging what the caller just said "
    "and, if the script calls for it, asking the next question. Never ask the "
    "caller to repeat themselves. Never mention that you are an AI."
)

FALLBACK: Final = "Got it — thank you."
"""What the agent says when the model is slow, absent or unconfigured.

A real sentence, not an error string: CLAUDE.md §7 keeps the LLM off the
timing path, and the corollary is that an LLM failure must not stop the call
the controller is being demonstrated on.
"""


class AnthropicClient:
    """One-shot Claude completions for the reference agent.

    Satisfies `LlmClient` structurally. `stream` yields the whole reply as a
    single chunk: the console speaks with the browser's own synthesiser
    (ADR-035), which takes a finished string, so streaming would buy nothing
    and cost a partial-utterance cancellation path.
    """

    __slots__ = ("_cancelled", "_client", "id", "model")

    def __init__(self, api_key: str, *, model: str = MODEL) -> None:
        """Wire a client.

        Args:
            api_key: Server-side credential. Never reaches the browser (INV-5).
            model: Completion model id.
        """
        self.id = "anthropic"
        self.model = model
        self._cancelled = False
        self._client: object | None = None
        if api_key:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=api_key)

    async def reply(self, messages: Sequence[Message]) -> str:
        """One completion, or `FALLBACK`. `O(1)` calls.

        Args:
            messages: The conversation so far, oldest first.

        Returns:
            One sentence. Never raises: a broken brain must not drop a call
            (INV-8's spirit, applied to the agent rather than the controller).
        """
        if self._client is None:
            return FALLBACK
        try:
            async with asyncio.timeout(TIMEOUT_MS / 1000):
                response = await self._client.messages.create(  # type: ignore[attr-defined]
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM,
                    messages=[{"role": m.role, "content": m.content} for m in messages],
                )
        except Exception:
            return FALLBACK
        parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        return " ".join(parts).strip() or FALLBACK

    async def stream(
        self, messages: Sequence[Message], *, timeout_ms: int = TIMEOUT_MS
    ) -> AsyncIterator[str]:
        """Yield the reply as one chunk, for `LlmClient` conformance.

        Args:
            messages: The conversation so far.
            timeout_ms: Accepted for the protocol; `TIMEOUT_MS` governs.

        Yields:
            The whole reply, once.
        """
        yield await self.reply(messages)

    def cancel(self) -> None:
        """Abandon the in-flight completion. Barge-in cancels the *speech*.

        The browser stops speaking immediately via `speechSynthesis.cancel()`;
        this only marks the completion abandoned so a late reply is not spoken
        over the caller.
        """
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        """Whether the last completion was abandoned."""
        return self._cancelled
