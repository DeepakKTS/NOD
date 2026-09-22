"""Bearer auth for mutating routes, and short-lived per-session tokens.

**Both functions are stubs and stay stubs through the freeze** (ADR-042). Auth is
post-hackathon work (ROADMAP §4, "per-tenant policy management and auth") and this
module is referenced by no route. It is kept rather than deleted because the
signatures are the contract ARCHITECTURE §7 and §10 describe, and an empty file
would say nothing about what is missing.

What matters is that nothing now *claims* it is active: `NOD_AUTH` defaults to
`off`, and the exposure a public URL actually has — anyone opening the demo socket
spends the upstream key — is bounded by `NOD_MAX_SESSIONS` in `nod_server.app`,
which is a cost bound and not an access bound. INV-5 is unaffected either way: the
browser receives relative WebSocket paths and no credential, which is why
`mint_session_token` being unimplemented costs nothing today.
"""

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
