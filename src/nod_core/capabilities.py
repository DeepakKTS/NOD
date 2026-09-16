"""Probe which knobs and fields this model actually exposes.

ARCHITECTURE.md §2: this module never assumes. Universal-3 Pro Streaming uses
punctuation-based turn detection rather than a confidence score, so the presence
of every field below has to be established rather than taken on trust
(DECISIONS.md ADR-001).
"""

from __future__ import annotations

from typing import Final

from nod_adapters.protocols import SttSession
from nod_core.types import Capabilities

UPDATABLE_FIELDS: Final = (
    "end_of_turn_confidence_threshold",
    "min_turn_silence",
    "max_turn_silence",
    "vad_threshold",
)
"""The four knobs `UpdateConfiguration` is documented to cover (CONTROL_SPEC.md §0)."""

PROBE_CACHE_SIZE: Final = 16
"""Capability probes retained, keyed on (model, api_version) (ARCHITECTURE.md §5)."""

PROBE_CACHE_TTL_S: Final = 3600
"""Probe cache time to live. Seconds (ARCHITECTURE.md §5)."""


async def probe(session: SttSession, *, model: str) -> Capabilities:
    """Establish what this session supports, one knob at a time.

    Attempts a mid-stream `UpdateConfiguration` for each of `UPDATABLE_FIELDS`
    separately and records which are accepted; records whether
    `end_of_turn_confidence` is present on `Turn` events and whether
    `ForceEndpoint` is accepted (docs/PROMPTS.md P1).

    Cached on `(model, api_version)`, `PROBE_CACHE_SIZE` entries, TTL
    `PROBE_CACHE_TTL_S`, because a probe per session wastes a round trip.

    Args:
        session: A live upstream session to probe.
        model: The model name, part of the cache key.

    Returns:
        What this model exposes.
    """
    raise NotImplementedError


def degrade(caps: Capabilities) -> frozenset[str]:
    """Map capabilities onto the degradation matrix of CONTROL_SPEC.md §7. `O(1)`.

    Args:
        caps: What the probe found.

    Returns:
        The names of the axes and features that must be disabled. An empty set
        means the full law applies.
    """
    raise NotImplementedError
