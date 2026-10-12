import os

import pytest

from guandan_ai.local_config import load_local_env


def test_private_config_does_not_execute_and_respects_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("# server only\nDEEPSEEK_API_KEY='fake-test-key'\nDEEPSEEK_MODEL=example\nGUANDAN_TEST=$(never execute)\nPATH=unexpected\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_MODEL", "existing")
    monkeypatch.delenv("GUANDAN_TEST", raising=False)
    old_path = os.environ.get("PATH")
    load_local_env(path)
    assert os.environ["DEEPSEEK_API_KEY"] == "fake-test-key"
    assert os.environ["DEEPSEEK_MODEL"] == "existing"
    assert os.environ["GUANDAN_TEST"] == "$(never execute)"
    assert os.environ.get("PATH") == old_path
    monkeypatch.delenv("GUANDAN_TEST")
    monkeypatch.delenv("DEEPSEEK_API_KEY")


def test_config_errors_do_not_expose_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("DEEPSEEK_API_KEY='fake-secret", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_local_env(path)
    assert "fake-secret" not in str(error.value)
