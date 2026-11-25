"""Only this service receives database credentials and the user JWT secret."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    database_url: str = "sqlite:///./.state/lab2.sqlite3"
    jwt_secret: str  # No silent production fallback.
    access_token_expire_minutes: int = 60
    cors_origins: str = "http://localhost:8010,http://127.0.0.1:8010"
    model_config = SettingsConfigDict(extra="ignore")

    @property
    def cors_origins_list(self) -> list[str]:
        return self.cors_origins.split(",")


settings = Settings()
