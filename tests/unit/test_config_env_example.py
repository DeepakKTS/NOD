"""`.env.example` must work as-is on a first run.

`cp .env.example .env` is the first thing anyone does with this repo — a judge,
a reviewer, the owner on a fresh machine. An empty string is not "unset" to a
typed parser: it is an invalid `int`, `bool`, `Enum` or `Literal`. Nine of the
twenty variables in DEPLOYMENT.md §2 crashed `Settings()` before this file
existed, and the crash surfaced only at first run.

These tests also guard the drift between DEPLOYMENT.md §2, `.env.example` and
`Settings`, which are three copies of one list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from nod_core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DEPLOYMENT_SPEC = REPO_ROOT / "docs" / "DEPLOYMENT.md"

# Credentials have no default and are legitimately blank in the example.
BLANK_BY_DESIGN = frozenset(
    {"assemblyai_api_key", "nod_api_token", "llm_provider", "llm_api_key"},
)

# DEPLOYMENT.md §2 lists this as a wildcard family, not a single variable name.
WILDCARD_FIELDS = frozenset({"tts_api_keys"})


def _declared_keys(text: str) -> set[str]:
    """Return the variables the example actually sets, ignoring commented lines."""
    return {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)}


@pytest.fixture
def env_from_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy `.env.example` to `.env` in a clean directory, as a new user would."""
    target = tmp_path / ".env"
    target.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    # Isolate from whatever the developer or CI runner already exported, so this
    # tests the file rather than the ambient environment.
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    return target


def test_copying_env_example_to_env_constructs_settings(env_from_example: Path) -> None:
    """The first-run path: `cp .env.example .env` then start the app."""
    assert env_from_example.is_file()
    settings = Settings()
    assert isinstance(settings, Settings)


def test_env_example_reproduces_the_documented_defaults(
    env_from_example: Path,
) -> None:
    """Every non-credential value in the example equals the model default.

    The example is documentation, so it must not quietly configure something
    different from what DEPLOYMENT.md §2 says the default is.
    """
    _ = env_from_example
    from_file = Settings()
    for name, field in Settings.model_fields.items():
        if name in BLANK_BY_DESIGN or name in WILDCARD_FIELDS:
            continue
        assert getattr(from_file, name) == field.default, (
            f"{name} in .env.example differs from its documented default"
        )


def test_credentials_are_blank_and_typed_as_secrets(env_from_example: Path) -> None:
    """INV-5: no key is baked into a committed file, and keys are SecretStr."""
    _ = env_from_example
    settings = Settings()
    for name in BLANK_BY_DESIGN:
        value = getattr(settings, name)
        assert value in (None, "") or (
            isinstance(value, SecretStr) and not value.get_secret_value()
        ), f"{name} is not blank in .env.example"


def test_every_settings_field_is_documented_in_env_example() -> None:
    """A new `Settings` field cannot land without an entry in the example."""
    declared = _declared_keys(ENV_EXAMPLE.read_text(encoding="utf-8"))
    missing = {
        name.upper()
        for name in Settings.model_fields
        if name not in WILDCARD_FIELDS and name.upper() not in declared
    }
    assert not missing, f"missing from .env.example: {sorted(missing)}"


def test_every_settings_field_is_documented_in_deployment_spec() -> None:
    """DEPLOYMENT.md §2 is the source of truth; `Settings` must not drift from it."""
    spec = DEPLOYMENT_SPEC.read_text(encoding="utf-8")
    table = spec.split("## 3. Container")[0]
    missing = [
        name.upper()
        for name in Settings.model_fields
        if name not in WILDCARD_FIELDS and name.upper() not in table
    ]
    assert not missing, f"missing from DEPLOYMENT.md §2: {sorted(missing)}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("browser", ("browser",)),
        ("browser,elevenlabs", ("browser", "elevenlabs")),
        ("browser, elevenlabs", ("browser", "elevenlabs")),
        ("", ()),
    ],
)
def test_comma_separated_lists_parse_as_documented(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: tuple[str, ...],
) -> None:
    """DEPLOYMENT.md §2 calls TTS_PROVIDERS a comma-separated chain, in order.

    pydantic-settings JSON-decodes complex fields by default, so the documented
    form raised `SettingsError` until `CsvTuple` opted out of that decoding.
    """
    (tmp_path / ".env").write_text(f"TTS_PROVIDERS={raw}\n", encoding="utf-8")
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    assert Settings().tts_providers == expected


def test_blank_credentials_parse_as_absent_not_as_empty(
    env_from_example: Path,
) -> None:
    """Regression: `cp .env.example .env` left every credential `SecretStr("")`.

    That is not `None`, so an `is None` guard passes it through and the caller
    fails later with an empty key rather than a missing one. In the probe that
    surfaced as exit 2 with no message at all — a silent failure on the exact
    path the documentation tells a new user to take.
    """
    settings = Settings()
    assert settings.assemblyai_api_key is None
    assert settings.nod_api_token is None
    assert settings.llm_api_key is None
    assert settings.llm_provider is None


def test_a_real_credential_still_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank-normalisation must not swallow a key that is actually set."""
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    (tmp_path / ".env").write_text("ASSEMBLYAI_API_KEY=sk-real\n")
    monkeypatch.chdir(tmp_path)

    key = Settings().assemblyai_api_key
    assert isinstance(key, SecretStr)
    assert key.get_secret_value() == "sk-real"


def test_whitespace_only_credentials_are_also_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    (tmp_path / ".env").write_text('ASSEMBLYAI_API_KEY="   "\n')
    monkeypatch.chdir(tmp_path)
    assert Settings().assemblyai_api_key is None
