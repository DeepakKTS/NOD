"""Settings via pydantic-settings. All deployment tunables live here.

ARCHITECTURE.md §2: this module never hides a constant.

Scope note: `Settings` carries the deployment surface of DEPLOYMENT.md §2 only.
The control-law constants of CONTROL_SPEC.md §4 and §5 are module-level `Final`
values in `arbiter.py` and `profiler.py`, because CONTROL_SPEC.md §0 says moving
one is an ADR rather than an environment change, and `nod tune` emits a preset
file rather than environment variables.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from nod_core.arbiter import CEILING_FLOOR_MS, DEFAULT_CEILING_MS
from nod_core.types import NodMode


def _split_csv(value: object) -> object:
    """Parse the comma-separated list form DEPLOYMENT.md §2 documents.

    pydantic-settings JSON-decodes complex fields before validation, so without
    `NoDecode` the documented `TTS_PROVIDERS=browser,elevenlabs` form raises
    rather than parsing. An empty value yields an empty tuple.

    Args:
        value: The raw environment value, or an already-parsed sequence.

    Returns:
        A tuple of stripped, non-empty entries when given a string; otherwise
        the value unchanged, so programmatic construction still works.
    """
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return value


type CsvTuple = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_split_csv)]
"""A comma-separated environment list, as DEPLOYMENT.md §2 documents them."""


def _blank_to_none(value: object) -> object:
    """Treat an empty or whitespace-only credential as absent.

    `.env.example` ships every credential blank, because none of them has a
    default, so `cp .env.example .env` — the documented first-run path — yields
    `ASSEMBLYAI_API_KEY=`. Pydantic reads that as `SecretStr("")`, which is not
    `None`, so an `is None` check passes it through and the caller fails later
    with an empty key instead of a missing one.

    Args:
        value: The raw environment value.

    Returns:
        `None` for a blank string, otherwise the value unchanged.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


type OptionalSecret = Annotated[SecretStr | None, BeforeValidator(_blank_to_none)]
"""A credential that is absent when blank, not present-and-empty."""

type OptionalText = Annotated[str | None, BeforeValidator(_blank_to_none)]
"""A non-secret optional setting, blank-normalised the same way."""


UPSTREAM_CONCURRENCY_LIMIT: Final = 5
"""Concurrent streaming sessions the AssemblyAI account permits. Sessions.

Measured at Gate 4a, not documented: a ramp of concurrent sessions was refused at
the sixth with `error_code 1008`, "Unauthorized Connection: Too many concurrent
sessions". The refusal arrives as an `Error` frame *after* a successful WebSocket
upgrade, so a caller that only checks the handshake sees a healthy socket.

Re-measure on an account or plan change. `scripts/probe_concurrency.py` is the ramp.
"""

UPSTREAM_SOCKETS_PER_SESSION_PEAK: Final = 2
"""Upstream sockets one Nod session can hold at once. Sockets.

`SessionProxy.rotate` opens the replacement socket *before* closing the one it
replaces (EC-03) — it has to, because the profiler state and the current config
are carried across and a gap would reset the caller's window mid-call. So a
rotating session briefly counts twice against `UPSTREAM_CONCURRENCY_LIMIT`.
"""

MAX_CONCURRENT_SESSIONS: Final = (
    UPSTREAM_CONCURRENCY_LIMIT // UPSTREAM_SOCKETS_PER_SESSION_PEAK
)
"""Default `NOD_MAX_SESSIONS`. 2 sessions (ADR-042).

The pessimistic reading, deliberately: it assumes every live session rotates at
the same instant. Sessions started apart rotate apart, so 5 would usually work —
but the failure mode of guessing high is a rotation refused mid-call on a live
demo, and the failure mode of guessing low is a second browser tab getting a 429.
"""


class Settings(BaseSettings):  # type: ignore[explicit-any]  # pydantic's own Any
    """Every variable in DEPLOYMENT.md §2, with its documented default.

    Credentials are `SecretStr` so that a trace line or a response model cannot
    serialise one by accident (INV-5).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
        case_sensitive=False,
    )

    nod_env: Literal["dev", "prod"] = "prod"
    """`dev` re-raises controller errors after the call (INV-8)."""

    assemblyai_api_key: OptionalSecret = None
    """Required at runtime, server-side only (INV-5)."""

    nod_model: str = "universal-streaming-english"
    nod_api_token: OptionalSecret = None
    nod_auth: Literal["required", "off"] = "off"
    """Bearer auth on mutating routes. **`off`, and that is a real default.**

    It read `required` while `nod_server.auth.require_bearer` raised
    `NotImplementedError` and was referenced by no route — a setting declaring a
    guarantee the process did not honour, which is worse than an honest `off`
    because a reader configuring a deployment would believe it. Auth stays cut
    (ADR-042); the open-door problem it was nominally covering is closed by
    `nod_max_sessions` instead, which bounds the cost rather than the access.
    """

    nod_allowed_origins: CsvTuple = ()
    """CORS allow-list. Never `*` (ARCHITECTURE.md §7)."""

    nod_max_sessions: int = MAX_CONCURRENT_SESSIONS
    """Concurrent streaming sessions per worker. Refused with 429 above this.

    Derived from the measured upstream limit rather than chosen (ADR-042). It was
    `64`, which was not a measurement of anything and was **twelve times** what
    the account actually permits.
    """

    nod_ceiling_ms: int = Field(default=DEFAULT_CEILING_MS, ge=CEILING_FLOOR_MS)
    """Default latency ceiling, milliseconds.

    **Derived, not copied.** The default was the bare literal `2600` while
    `arbiter.DEFAULT_CEILING_MS` held the same number and every other consumer —
    `nod_server.ws`, the budget test, the property strategies — read the constant.
    Nothing checked the two agreed, so moving the constant would have left this
    default at the old value silently. That is the shape CLAUDE.md §5 records for
    `corpus.INTRINSIC_FLOOR_DBFS` against `fake_assemblyai.DEFAULT_VAD`: two
    constants agreeing by meaning and not by code.

    §5 prescribes *asserting* the relation there and *deriving* it here, and the
    difference is not inconsistency. `INTRINSIC_FLOOR_DBFS` had already been frozen
    into committed corpus sidecars, so deriving would have silently recalibrated
    ground truth to a new threshold and made the disagreement unaskable — it had to
    fail loudly and make someone decide. Nothing is frozen here: this default is
    read at process start and persisted nowhere, so one source of truth is simply
    correct and a second copy buys nothing but drift.

    `ge=CEILING_FLOOR_MS` is ADR-021's lower bound, 1317 ms since ADR-040 folded
    `ENDPOINT_OVERHEAD_MS` into it: a ceiling under that cannot
    satisfy §4's invariant repair, and pydantic raises at startup rather than
    letting every turn of every call quietly violate it. Rejection is right at this
    boundary because configuration is not a call; INV-8's "fail soft in a call"
    governs the per-connection override in `SessionProxy`, which clamps instead.
    """

    nod_preset: str = "balanced"

    nod_trace_raw: bool = False
    """`True` disables redaction; requires a documented reason (INV-6)."""

    # Relative, so a clean clone that copies `.env.example` starts ready. The
    # absolute container paths are set explicitly by the Dockerfile ENV,
    # docker-compose, fly.toml and deploy_aws.sh, so no deployment relies on
    # these. Gate 5: with `/data/traces` as the default, `make run` on a
    # laptop reported `/readyz` 503 — `/data` is read-only on macOS — and the
    # first thing a stranger saw was a not-ready server.
    nod_trace_dir: Path = Path("data/traces")
    nod_db_path: Path = Path("data/nod.db")
    nod_log_level: str = "info"

    llm_provider: OptionalText = None
    llm_api_key: OptionalSecret = None

    tts_providers: CsvTuple = ("browser",)
    """Comma-separated fallback chain, in order."""

    tts_api_keys: dict[str, SecretStr] = {}
    """Per-provider cloud keys, collected from `TTS_API_KEY_*`."""

    nod_exact_quantiles: bool = False
    """Validation only, slower (ADR-002)."""

    nod_mode_default: NodMode = NodMode.ADAPT
    """Default mode for new sessions.

    DEPLOYMENT.md §5 step 1 deploys with `NOD_MODE_DEFAULT=observe`, which is
    genuinely zero-risk: `observe` cannot change a call's behaviour.
    """


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once.

    Cached, so `.env` is parsed once per process rather than once per caller.
    Precedence is pydantic-settings' own: an explicit environment variable beats
    a `.env` entry, which beats the field default. That ordering is what lets a
    one-off `ASSEMBLYAI_API_KEY=... python -m nod_bench.probe` override a
    committed `.env` without editing it.

    Tests that manipulate the environment must call `get_settings.cache_clear()`
    or construct `Settings()` directly.

    Returns:
        The cached `Settings` instance.
    """
    return Settings()
