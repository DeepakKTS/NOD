"""Append-only JSONL traces with redaction and a bounded flush buffer.

ARCHITECTURE.md §2: this module never blocks the loop. `emit` is synchronous and
non-blocking; `drain` is the low-priority task that actually writes. Tracing is
never load-bearing for a call (EC-43).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from nod_core.types import JsonValue

FLUSH_BUFFER_BYTES: Final = 64 * 1024
"""`B` in the complexity table of ARCHITECTURE.md §4. Bytes."""

ROTATE_BYTES: Final = 64 * 1024 * 1024
"""Traces rotate into `.1`, `.2` at this size (ARCHITECTURE.md §6). Bytes."""

TRACE_QUEUE_MAXSIZE: Final = 1024
"""Bounded queue for the `drain_trace` task; drop-oldest (ARCHITECTURE.md §3)."""

DIGIT_RUN_MIN: Final = 4
"""Digit runs of this length or longer are masked (INV-6, EC-42)."""


def redact(text: str) -> str:
    """Mask personally identifying shapes in transcript text. `O(len(text))`.

    Masks digit runs of `DIGIT_RUN_MIN` or more, date patterns, emails and
    phone-like strings. On by default; disabled only when `NOD_TRACE_RAW=1` is
    set explicitly (INV-6, ARCHITECTURE.md §10, EC-42).

    Args:
        text: The raw transcript fragment.

    Returns:
        The redacted fragment.
    """
    raise NotImplementedError


class TraceSink:
    """One trace file per session, written off the hot path.

    `raw` defaults to `False`, so redaction is the default value rather than a
    convention to remember (INV-6).
    """

    def __init__(
        self,
        session_id: str,
        *,
        directory: Path,
        raw: bool = False,
        maxsize: int = TRACE_QUEUE_MAXSIZE,
    ) -> None:
        """Open a sink for one session.

        Args:
            session_id: Names the file, `{session_id}.jsonl`.
            directory: `NOD_TRACE_DIR`.
            raw: Disable redaction. Requires a documented reason (INV-6).
            maxsize: Bounded queue depth; overflow drops the oldest entry.
        """
        raise NotImplementedError

    def emit(self, kind: str, payload: Mapping[str, JsonValue], *, t_ms: int) -> None:
        """Queue one trace line. Non-blocking, `O(1)`.

        Overflow policy is drop-oldest with a `trace_dropped` counter. Dropping
        telemetry is always preferable to delaying audio (ARCHITECTURE.md §3).

        Args:
            kind: The event name, one JSON object per line.
            payload: The event body.
            t_ms: Stream-relative milliseconds (CLAUDE.md §6).
        """
        raise NotImplementedError

    async def drain(self) -> None:
        """Run the `drain_trace` task: buffer, write, rotate.

        `O(1)` amortised per line against a `FLUSH_BUFFER_BYTES` buffer. On
        `ENOSPC` this disables tracing for the session, emits a metric, and lets
        the call continue (EC-43).
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        """Flush the buffer and close the file."""
        raise NotImplementedError
