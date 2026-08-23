"""Env-file discovery and LLM readiness gating."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from oracle_mcp import settings as settings_module
from oracle_mcp.settings import (
    DEFAULT_ENV_FILE,
    Settings,
    env_file_candidates,
    is_placeholder,
    load_env_file,
)


@pytest.mark.parametrize("value", ["", "   ", "...", "change-me", "XXX", "your-key-here"])
def test_placeholder_secrets_are_not_configured(value: str, settings: Settings):
    assert is_placeholder(value)
    assert not settings.model_copy(update={"llm_api_key": SecretStr(value)}).llm_configured


def test_real_key_is_configured(settings: Settings):
    ready = settings.model_copy(update={"llm_api_key": SecretStr("sk-live-abc123")})
    assert ready.llm_configured
    assert ready.llm_status() == "ok"


def test_status_distinguishes_missing_from_placeholder(settings: Settings):
    missing = settings.model_copy(update={"llm_api_key": SecretStr("")})
    assert "is not set" in missing.llm_status()

    placeholder = settings.model_copy(update={"llm_api_key": SecretStr("...")})
    assert "placeholder" in placeholder.llm_status()


def test_env_file_is_anchored_to_the_repository(tmp_path: Path, monkeypatch):
    """Running from an unrelated directory must still find the repo's .env."""
    monkeypatch.chdir(tmp_path)
    assert DEFAULT_ENV_FILE in env_file_candidates()


def test_load_env_file_does_not_override_real_environment(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ONPREM_USER=from_file\nATP_USER=from_file\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "_loaded_env_files", set())
    monkeypatch.setitem(os.environ, "ONPREM_USER", "from_environment")
    monkeypatch.delenv("ATP_USER", raising=False)

    load_env_file(env)

    assert os.environ["ONPREM_USER"] == "from_environment"
    assert os.environ["ATP_USER"] == "from_file"


def test_dq_writer_is_disabled_by_default(settings: Settings):
    assert settings.dq_writer_profile is None


def test_dq_writer_uses_separate_credentials(settings: Settings, monkeypatch):
    monkeypatch.setenv("ONPREM_USER", "CHATBOT_RO")
    monkeypatch.setenv("ONPREM_PASSWORD", "reader-secret")
    monkeypatch.setenv("ONPREM_DSN", "db.example.test/service")
    monkeypatch.setenv("DQ_WRITE_USER", "EIM_DQ_WRITER")
    monkeypatch.setenv("DQ_WRITE_PASSWORD", "writer-secret")
    configured = settings.model_copy(update={"dq_persistence_enabled": True})
    profile = configured.dq_writer_profile
    assert profile is not None
    assert profile.user == "EIM_DQ_WRITER"
    assert profile.password.get_secret_value() == "writer-secret"
    assert profile.user != os.environ["ONPREM_USER"]
    assert "writer-secret" not in repr(profile)
