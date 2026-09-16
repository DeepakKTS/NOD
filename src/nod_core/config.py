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
from typing import Annotated, Literal

from pydantic import BeforeValidator, SecretStr
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    nod_auth: Literal["required", "off"] = "required"

    nod_allowed_origins: CsvTuple = ()
    """CORS allow-list. Never `*` (ARCHITECTURE.md §7)."""

    nod_max_sessions: int = 64
    """Sessions per worker."""

    nod_ceiling_ms: int = 2600
    """Default latency ceiling, milliseconds."""

    nod_preset: str = "balanced"

    nod_trace_raw: bool = False
    """`True` disables redaction; requires a documented reason (INV-6)."""

    nod_trace_dir: Path = Path("/data/traces")
    nod_cache_dir: Path = Path("/data/.nodcache")
    nod_db_path: Path = Path("/data/nod.db")
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
