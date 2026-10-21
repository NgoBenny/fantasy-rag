import os
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # FANTASY_RAG_ENV_FILE points elsewhere (tests use a missing file to prove that demo
    # mode starts with no .env at all).
    model_config = SettingsConfigDict(
        env_file=os.environ.get("FANTASY_RAG_ENV_FILE") or REPO_ROOT / ".env", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy"
    demo_mode: bool = False
    sleeper_username: str = ""
    anthropic_api_key: SecretStr = SecretStr("")
    llm_budget_usd: float | None = None  # per-process spending cap for chat; unset = none
