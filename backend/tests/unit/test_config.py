import pytest

from fantasy_rag.config import Settings


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:1/d")
    monkeypatch.setenv("DEMO_MODE", "true")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.database_url == "postgresql+psycopg://u:p@h:1/d"
    assert s.demo_mode is True


def test_demo_mode_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert Settings(_env_file=None).demo_mode is False  # type: ignore[call-arg]
