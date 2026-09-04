"""Runtime configuration, read from the environment / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
CROSSWALK_DIR = REPO_ROOT / "reference" / "crosswalk"
WEB_DIR = REPO_ROOT / "web"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_db: str = "safety"
    postgres_user: str = "safety"
    postgres_password: str = "safety_dev_pw"
    postgres_host: str = "localhost"
    postgres_port: int = 55432

    # Local stand-in for the S3-compatible bronze bucket (design doc S9.2).
    bronze_root: Path = REPO_ROOT / "data" / "bronze"

    # Trailing window the Phase 1 backfill loads (design doc S15).
    backfill_months: int = 24

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # HTTP behaviour for source adapters.
    http_timeout_seconds: float = 120.0
    http_max_retries: int = 4

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
