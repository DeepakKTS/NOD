"""Bearer auth for mutating routes, and short-lived per-session tokens."""

from __future__ import annotations

from typing import Final

from fastapi import Request

from nod_core.config import Settings

SESSION_TOKEN_TTL_S: Final = 300
"""Default lifetime of a per-session browser token. Seconds."""


async def require_bearer(request: Request, settings: Settings) -> None:
    """Require `Authorization: Bearer $NOD_API_TOKEN` when `NOD_AUTH=required`.

    Comparison is constant time (ARCHITECTURE.md §7).

    Args:
        request: The incoming request.
        settings: Process settings.

    Raises:
        NotImplementedError: scaffold only.
    """
    raise NotImplementedError


def mint_session_token(session_id: str, *, ttl_s: int = SESSION_TOKEN_TTL_S) -> str:
    """Mint a short-lived token scoped to one session id.

    The browser receives this, never an API key (INV-5, ARCHITECTURE.md §10).

    Args:
        session_id: The session the token is scoped to.
        ttl_s: Lifetime in seconds.

    Returns:
        The opaque token.
    """
    raise NotImplementedError
