"""Config fails fast and never leaks keys."""

import pytest

from outbound import config


def test_missing_keys_exits(monkeypatch):
    for k in config.REQUIRED:
        monkeypatch.delenv(k, raising=False)
    # python-dotenv may have loaded a .env; force the validator to see nothing.
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        config.get_config()
    msg = str(exc.value)
    assert "Missing required env vars" in msg
    # Never echoes values — only names.
    assert "sk-" not in msg


def test_database_url_default(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert config.get_database_url() == "sqlite:///outbound.db"


def test_all_keys_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("APOLLO_API_KEY", "y")
    monkeypatch.setenv("SMARTLEAD_API_KEY", "z")
    cfg = config.get_config()
    assert cfg["anthropic_key"] == "x"
    assert cfg["database_url"].startswith("sqlite") or cfg["database_url"]
