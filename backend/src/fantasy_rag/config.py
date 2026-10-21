from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy"
    demo_mode: bool = False
    sleeper_username: str = ""
    anthropic_api_key: SecretStr = SecretStr("")
