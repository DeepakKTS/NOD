"""The probe finds its credential the way the documentation says it is configured.

`.env.example` tells a new user to `cp .env.example .env`, and `config.py`
already sets `env_file=".env"`. A CLI that reads `os.environ` directly makes
that documented path silently fail, which is a trap precisely for the person
following the instructions rather than improvising.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from nod_bench.probe import _resolve_api_key, main
from nod_core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Strip the ambient environment and the settings cache, then work in tmp."""
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


def test_dotenv_alone_satisfies_the_precondition(tmp_path: Path) -> None:
    """The load-bearing case: a key in `.env`, nothing in `os.environ`."""
    (tmp_path / ".env").write_text("ASSEMBLYAI_API_KEY=sk-from-dotenv\n")

    import os

    assert "ASSEMBLYAI_API_KEY" not in os.environ
    assert _resolve_api_key(io.StringIO()) == "sk-from-dotenv"


def test_environment_variable_still_works_without_a_dotenv() -> None:
    """The inline form, `ASSEMBLYAI_API_KEY=... python -m nod_bench.probe`."""
    import os

    os.environ["ASSEMBLYAI_API_KEY"] = "sk-from-env"
    try:
        get_settings.cache_clear()
        assert _resolve_api_key(io.StringIO()) == "sk-from-env"
    finally:
        del os.environ["ASSEMBLYAI_API_KEY"]


def test_an_exported_key_overrides_a_committed_dotenv(tmp_path: Path) -> None:
    """Precedence, so a one-off run does not need the file edited."""
    (tmp_path / ".env").write_text("ASSEMBLYAI_API_KEY=sk-from-dotenv\n")

    import os

    os.environ["ASSEMBLYAI_API_KEY"] = "sk-from-env"
    try:
        get_settings.cache_clear()
        assert _resolve_api_key(io.StringIO()) == "sk-from-env"
    finally:
        del os.environ["ASSEMBLYAI_API_KEY"]


def test_no_credential_anywhere_explains_both_routes() -> None:
    buffer = io.StringIO()
    assert _resolve_api_key(buffer) == ""
    printed = buffer.getvalue()
    assert "ASSEMBLYAI_API_KEY=..." in printed
    assert "cp .env.example .env" in printed
    assert "--fake" in printed


def test_an_unknown_dotenv_key_is_reported_not_swallowed(tmp_path: Path) -> None:
    """`Settings` forbids extras, so say so rather than dying opaquely."""
    (tmp_path / ".env").write_text(
        "ASSEMBLYAI_API_KEY=sk-x\nSOMETHING_UNDOCUMENTED=1\n"
    )
    buffer = io.StringIO()
    assert _resolve_api_key(buffer) == ""
    assert "DEPLOYMENT.md §2" in buffer.getvalue()


def test_cli_refuses_and_never_opens_a_socket_without_a_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 2
    assert "ASSEMBLYAI_API_KEY" in capsys.readouterr().out


def test_cli_accepts_a_dotenv_key_then_stops_on_the_missing_seed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The credential check passes from `.env`; the run stops on the next gate."""
    (tmp_path / ".env").write_text("ASSEMBLYAI_API_KEY=sk-from-dotenv\n")
    assert main([]) == 2
    printed = capsys.readouterr().out
    assert "--seed-wav is required" in printed
    assert "ASSEMBLYAI_API_KEY" not in printed


def test_the_documented_first_run_path_explains_itself(tmp_path: Path) -> None:
    """`cp .env.example .env` with the key left blank must not fail silently."""
    example = Path(__file__).resolve().parents[2] / ".env.example"
    (tmp_path / ".env").write_text(example.read_text(encoding="utf-8"))

    buffer = io.StringIO()
    assert _resolve_api_key(buffer) == ""
    printed = buffer.getvalue()
    assert printed, "a blank credential must produce guidance, not silence"
    assert "cp .env.example .env" in printed


def test_a_blank_key_in_the_environment_is_also_absent() -> None:
    import os

    os.environ["ASSEMBLYAI_API_KEY"] = ""
    try:
        get_settings.cache_clear()
        buffer = io.StringIO()
        assert _resolve_api_key(buffer) == ""
        assert buffer.getvalue()
    finally:
        del os.environ["ASSEMBLYAI_API_KEY"]
