"""Runtime configuration, read from the environment / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
CROSSWALK_DIR = REPO_ROOT / "reference" / "crosswalk"
SEVERITY_DIR = REPO_ROOT / "reference" / "severity"
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
    # Literal IPv4, not "localhost". docker-compose.yml publishes the database
    # on 127.0.0.1 only, which is an IPv4 listener, while "localhost" resolves
    # to ::1 first on Windows and on most Linux distributions. A single
    # psycopg.connect() falls back to the IPv4 address on its own, but the
    # pool's background worker does not reliably, and the failure looks like
    # `PoolTimeout: pool initialization incomplete` rather than a refused
    # connection -- a slow thing to debug from that message alone.
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 55432

    # Local stand-in for the S3-compatible bronze bucket (design doc S9.2).
    bronze_root: Path = REPO_ROOT / "data" / "bronze"

    # Trailing window the Phase 1 backfill loads (design doc S15).
    backfill_months: int = 24

    # Swagger UI / ReDoc / openapi.json. On by default: the frontend is served
    # from the same origin and names every endpoint in plain JavaScript
    # (web/app.js), so switching this off buys almost no obscurity -- it is a
    # kill switch for when the interactive docs themselves are the problem
    # (bot traffic, or not wanting the API shape clickable), not a security
    # control. Rate limiting is what protects the expensive endpoints.
    enable_docs: bool = True

    # Per-client request limiting (safety/api/ratelimit.py). On by default,
    # because the API has no authentication and /api/v1/cells builds the whole
    # city layer per uncached request. Turning it off is reasonable for local
    # development and for load testing your own instance; on anything publicly
    # reachable it is the control doing the real work.
    enable_rate_limit: bool = True

    # HTTP behaviour for source adapters.
    http_timeout_seconds: float = 120.0
    http_max_retries: int = 4

    # Bound on a single connection attempt. Without it libpq waits out the OS
    # TCP timeout, which only matters when a host accepts the packets and never
    # answers -- a wrong hostname on a platform network, say, rather than one
    # that refuses outright. wait_for_db() reads as a 60-second ceiling (30
    # attempts, 2s apart), but with an unbounded connect each attempt can cost
    # minutes: a misconfigured deployment took 66 minutes to fail, printing one
    # line. Ten seconds keeps a genuinely slow start working while making a dead
    # address fail in minutes with visible progress.
    connect_timeout_seconds: int = 10

    @property
    def dsn(self) -> str:
        # connect_timeout rides on the DSN so it applies to both plain
        # connections (safety/db.py) and the API's pool (safety/api/main.py).
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            f"?connect_timeout={self.connect_timeout_seconds}"
        )


settings = Settings()
