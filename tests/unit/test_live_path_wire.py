"""The AssemblyAI wire encoding. Gate 4b.

Small file, one subject: the types the service parses each config field as. It
exists because getting this wrong made the entire live path unrunnable and
nothing offline could see it — `FakeProbeSession` reads config through
`float(...)` and accepts either spelling.
"""

from __future__ import annotations

import json
from typing import Final
from urllib.parse import parse_qs, urlparse

import pytest

from nod_adapters.assemblyai.session import INTEGER_FIELDS, AssemblyAISession

MEASURED_REJECTION: Final = (
    "3006 User Input Validation Error: Invalid 'min_turn_silence': "
    "Value error, invalid literal for int() with base 10: '160.0'"
)
"""What the service answered when these were sent as floats (Gate 4b)."""


def _session(**config: float) -> AssemblyAISession:
    return AssemblyAISession(api_key="k", model="m", config=config)


def test_the_silence_knobs_go_on_the_wire_as_integers() -> None:
    """The service parses them with `int()` and rejects `'160.0'`.

    Asserted on the rendered query string rather than on a helper, because the
    query string is what the socket actually receives.
    """
    url = _session(min_turn_silence=160.0, max_turn_silence=400.0).url
    params = parse_qs(urlparse(url).query)
    assert params["min_turn_silence"] == ["160"]
    assert params["max_turn_silence"] == ["400"]
    assert "160.0" not in url, MEASURED_REJECTION


def test_the_threshold_knobs_are_not_coerced() -> None:
    """The companion direction: `vad_threshold=1.0` is integral, not an integer.

    A fix that coerced every integral value would silently turn a float field
    into `1`, which is the same class of wire-type error pointing the other way.
    """
    url = _session(vad_threshold=1.0, end_of_turn_confidence_threshold=0.4).url
    params = parse_qs(urlparse(url).query)
    assert params["vad_threshold"] == ["1.0"]
    assert params["end_of_turn_confidence_threshold"] == ["0.4"]


def test_the_integer_fields_are_named_not_inferred() -> None:
    """Exactly the two millisecond knobs, so the set cannot drift into floats."""
    assert frozenset({"min_turn_silence", "max_turn_silence"}) == INTEGER_FIELDS


@pytest.mark.asyncio
async def test_update_configuration_sends_integers_too() -> None:
    """**The path that mattered most.** `proxy.send_patch_upstream` uses this.

    Every mid-stream patch went through here as a float. A rejected update is
    retried once and then the session drops to `observe` (EC-32), so a `nod` arm
    would have computed patches, traced them, had them all refused, and behaved
    like `balanced` for the rest of the call — measured under the wrong label,
    which is the failure CLAUDE.md §5 names for that module.
    """
    sent: list[dict[str, object]] = []
    session = _session()
    # Reaching into the adapter on purpose: the point is to observe the exact
    # bytes `_send_json` produces without opening a socket (INV-7).
    session._on_sent = sent.append  # type: ignore[assignment]
    session._socket = _Recorder()  # type: ignore[assignment]

    await session.update_configuration(
        {"min_turn_silence": 520.0, "max_turn_silence": 1640.0}
    )

    assert sent, "nothing was sent"
    message = sent[-1]
    assert message["min_turn_silence"] == 520
    assert message["max_turn_silence"] == 1640
    assert isinstance(message["min_turn_silence"], int)
    encoded = json.dumps(message)
    assert "520.0" not in encoded, MEASURED_REJECTION


class _Recorder:
    """The smallest thing `_send_json` will send to. INV-7: no socket."""

    def __init__(self) -> None:
        self.frames: list[str] = []

    async def send(self, payload: str) -> None:
        """Record one frame."""
        self.frames.append(payload)
