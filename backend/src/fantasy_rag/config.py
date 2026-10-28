import os
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # FANTASY_RAG_ENV_FILE points elsewhere (tests use a missing file to prove that demo
    # mode starts with no .env at all).
    model_config = SettingsConfigDict(
        env_file=os.environ.get("FANTASY_RAG_ENV_FILE") or REPO_ROOT / ".env", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://fantasy:fantasy@127.0.0.1:5432/fantasy"
    demo_mode: bool = False
    sleeper_username: str = ""
    # Which provider answers chat: the paid Anthropic API or OpenRouter's free models.
    llm_provider: Literal["anthropic", "openrouter"] = "anthropic"
    anthropic_api_key: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_router_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    openrouter_writer_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    llm_budget_usd: float | None = None  # per-process spending cap for chat; unset = none
