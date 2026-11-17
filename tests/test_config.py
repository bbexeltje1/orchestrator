"""Configuration loading, overrides, validation, secret handling."""

from __future__ import annotations

import pytest

from orchestrator.config import (
    ConfigError,
    load_config,
    load_dotenv,
    resolve_secret,
    upgrade_config_file,
    validate_config,
)


def test_defaults_load(base_dir):
    config = load_config(base_dir=base_dir)
    assert config.project.name == "DRIFTLINE"
    assert config.thx.model == "HAL-X/thx-02:q4_k_m"
    assert config.workflow.dry_run is True
    assert config.server.bind_host == "127.0.0.1"


def test_invalid_confidence_rejected(base_dir):
    config = load_config(base_dir=base_dir)
    config.thx.min_confidence = 1.5
    with pytest.raises(ConfigError):
        validate_config(config)


def test_public_bind_host_rejected(base_dir):
    config = load_config(base_dir=base_dir)
    config.server.bind_host = "0.0.0.0"
    with pytest.raises(ConfigError):
        validate_config(config)


def test_bad_url_rejected(base_dir):
    config = load_config(base_dir=base_dir)
    config.qwen.base_url = "127.0.0.1:8090"
    with pytest.raises(ConfigError):
        validate_config(config)


def test_env_override(base_dir):
    env = {"DRIFTLINE_WORKFLOW_DRY_RUN": "false", "DRIFTLINE_PROJECT_NAME": "OTHER"}
    config = load_config(base_dir=base_dir, env=env)
    assert config.workflow.dry_run is False
    assert config.project.name == "OTHER"


def test_dotenv_does_not_override_env(base_dir, tmp_path):
    (tmp_path / ".env").write_text("DRIFTLINE_PROJECT_NAME=FROMFILE\n", encoding="utf-8")
    env = {"DRIFTLINE_PROJECT_NAME": "FROMENV"}
    config = load_config(base_dir=tmp_path, env=env)
    assert config.project.name == "FROMENV"


def test_dotenv_parser(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\nexport A=1\nB='two'\nC=\"three\"\nBAD LINE\n",
        encoding="utf-8",
    )
    values = load_dotenv(path)
    assert values["A"] == "1"
    assert values["B"] == "two"
    assert values["C"] == "three"
    assert "BAD LINE" not in values


def test_secret_resolution_uses_env_only(base_dir):
    config = load_config(base_dir=base_dir)
    assert resolve_secret(config, env={"ARENA_BRIDGE_TOKEN": "secret"}) == "secret"
    assert resolve_secret(config, env={}) is None


def test_services_section_defaults(base_dir):
    config = load_config(base_dir=base_dir)
    assert config.services.auto_start is True
    assert config.services.health_wait_seconds == 60
    assert config.services.qwen_start_command == ""
    assert config.services.ollama_start_command == ""


def test_services_env_override(base_dir):
    env = {"DRIFTLINE_SERVICES_QWEN_START_COMMAND": "llama-server -m q.gguf --port 8090"}
    config = load_config(base_dir=base_dir, env=env)
    assert config.services.qwen_start_command.startswith("llama-server")


def test_services_bad_wait_rejected(base_dir):
    config = load_config(base_dir=base_dir)
    config.services.health_wait_seconds = 0
    with pytest.raises(ConfigError):
        validate_config(config)


def test_upgrade_appends_missing_services_section(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("project:\n  name: KEEPME\n", encoding="utf-8")
    added = upgrade_config_file(path)
    assert "services" in added
    text = path.read_text(encoding="utf-8")
    assert "KEEPME" in text
    assert "qwen_start_command" in text
    config = load_config(config_path=path, base_dir=tmp_path)
    assert config.project.name == "KEEPME"
    assert config.services.auto_start is True


def test_upgrade_is_noop_when_complete(base_dir):
    path = base_dir / "config" / "config.yaml"
    before = path.read_text(encoding="utf-8")
    assert upgrade_config_file(path) == []
    assert path.read_text(encoding="utf-8") == before


def test_upgrade_preserves_existing_sections_and_comments(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("# my comment\nqwen:\n  enabled: false\n", encoding="utf-8")
    added = upgrade_config_file(path)
    assert "qwen" not in added
    assert "services" in added
    text = path.read_text(encoding="utf-8")
    assert "# my comment" in text
    config = load_config(config_path=path, base_dir=tmp_path)
    assert config.qwen.enabled is False
    assert config.services.health_wait_seconds == 60
