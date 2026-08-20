import os

import pytest

from graphbench.environment import (
    EnvironmentConfigurationError,
    connection_settings,
    load_dotenv,
    sanitize_text,
)


def test_dotenv_does_not_override_process_credentials(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("NEO4J_URI=bolt://from-file\nNEO4J_PASSWORD=test-value\n")
    monkeypatch.setenv("NEO4J_URI", "bolt://from-process")
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    load_dotenv(tmp_path)
    assert os.environ["NEO4J_URI"] == "bolt://from-process"
    assert os.environ["NEO4J_PASSWORD"] == "test-value"


def test_connection_settings_never_includes_values_in_missing_error(monkeypatch) -> None:
    monkeypatch.delenv("TEST_URI", raising=False)
    monkeypatch.delenv("TEST_USER", raising=False)
    monkeypatch.delenv("TEST_PASSWORD", raising=False)
    with pytest.raises(EnvironmentConfigurationError) as error:
        connection_settings("TEST")
    assert "PASSWORD" in str(error.value)


def test_sanitization_redacts_uri_credentials_and_assignments() -> None:
    value = "bolt://alice:secret@example.test password=another-secret"
    sanitized = sanitize_text(value)
    assert "secret" not in sanitized
    assert "another-secret" not in sanitized
