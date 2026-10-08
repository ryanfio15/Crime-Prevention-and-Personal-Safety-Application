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
    # No default (F20): a well-known password in code ends up in a deployment
    # sooner or later. Required in the instance .env; `dsn` refuses to build
    # without it, at connect time, so imports and DB-free tests still work.
    postgres_password: str = ""
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

    # Serving-layer cache bounds (safety/api/main.py), the Redis stand-in from
    # S9.5. Settings rather than constants because the right size depends on how
    # many cities are enabled and how large the largest one is, which is
    # deployment configuration, not a property of the code.
    #
    # A count alone is not a bound on memory: one whole-city layer at resolution
    # 10 is ~14 MB of JSON for Philadelphia and several times that for Los
    # Angeles. The byte budget is what actually protects the container; the
    # entry count just stops a long tail of small layers accumulating. /cells
    # layers are stored gzip-compressed (about ten to one), so the budget is
    # counted in compressed bytes for them.
    cache_max_entries: int = 192
    cache_max_bytes: int = 256 * 1024 * 1024

    # Bounds on the API's database work (safety/api/main.py). The largest
    # legitimate uncached query measured on dev is ~4 s (resolution-10 cells for
    # Chicago and Los Angeles); nginx gives up at 60 s. A query past the
    # statement timeout is cancelled and answered 503, as is a request that
    # waits longer than the pool timeout for a free connection -- before, pool
    # exhaustion hung every endpoint, /health included, for 30 s and then 500ed.
    # API pool only: the ETL and migrate (safety/db.py) legitimately run for
    # minutes and never get a statement timeout.
    api_statement_timeout_ms: int = 15000
    api_pool_timeout_seconds: float = 10.0

    # Strict-Transport-Security max-age (safety/api/security.py), sent only on
    # https. One day to start (user decision 2026-10-07): long enough to matter,
    # short enough that a certificate problem does not lock browsers out for
    # long. Raise it once the headers have been stable for a while.
    hsts_max_age_seconds: int = 86400

    # HTTP behaviour for source adapters.
    http_timeout_seconds: float = 120.0
    http_max_retries: int = 4

    # Optional Socrata application token, shared by the four Socrata sources
    # (Chicago, Seattle, Los Angeles, Austin). Not credentials -- it identifies
    # the caller so requests are counted against a per-token quota rather than a
    # shared anonymous per-IP one. Everything works without it; a 24-month
    # backfill across four cities is where the anonymous throttle starts to bite.
    # Register one at https://evergreen.data.socrata.com/signup
    socrata_app_token: str = ""

    # --- ops convergence (safety/ops.py) ---------------------------------
    #
    # Settings rather than flags so a scheduler that runs a fixed
    # `python -m safety.ops` command line can still be steered: anything an
    # operator needs to vary is reachable as an environment variable. The CLI
    # flags are the interactive equivalent and override these.
    #
    # One city instead of every enabled one.
    ops_city: str = ""
    # Re-run a step the ledger says was attempted recently. The escape hatch for
    # "I know it failed, the portal is back up now, go".
    ops_force: bool = False
    # Print the plan and exit without doing any of it.
    ops_dry_run: bool = False
    # How long a failed step is left alone before it is retried. This exists
    # because the ops service redeploys on every push to `main`: without a
    # cooldown, a city whose portal is down gets a fresh 24-month backfill
    # attempt on every unrelated code change. Six hours is below the ETL's own
    # six-hourly tick, so a genuine outage is still retried promptly.
    ops_retry_cooldown_hours: float = 6.0
    # How long one `safety.ops` run spends loading city history (the `history`
    # step) before it stops starting new slices. Keeps the ETL lock short enough
    # that the six-hourly pull and deploys are never held up for long; the
    # hourly safety-ops@.timer picks up where the last run stopped.
    ops_history_minutes: float = 25.0

    # Bound on a single connection attempt. Without it libpq waits out the OS
    # TCP timeout, which only matters when a host accepts the packets and never
    # answers -- a wrong hostname on a platform network, say, rather than one
    # that refuses outright. wait_for_db() reads as a 60-second ceiling (30
    # attempts, 2s apart), but with an unbounded connect each attempt can cost
    # minutes: a misconfigured deployment took 66 minutes to fail, printing one
    # line. Ten seconds keeps a genuinely slow start working while making a dead
    # address fail in minutes with visible progress.
    connect_timeout_seconds: int = 10

    # How long `python -m safety.migrate` waits for another migrate to finish
    # before giving up (safety/migrate.py migration_lock, F16). Deploys are
    # serialised anyway; this bounds a manual run racing one.
    migrate_lock_wait_seconds: float = 600.0

    # Records withdrawn upstream (safety/etl/withdrawn.py, F13; docs/DEPLOY.md
    # "Operating the data"): off | report | delete. An incremental compares its
    # revision window with silver; `report` records what is missing as a
    # `withdrawn_upstream` validation issue, `delete` also removes it behind an
    # outage guard, archiving each row. Anything else means report (with a
    # warning), so a typo can neither break the import nor enable deletion.
    # The default stays report (user decision 2026-10-07); enable deletion per
    # instance in its .env.
    withdrawn_reconcile: str = "report"
    # How long deleted rows stay in etl.withdrawn_incident for exact recovery.
    withdrawn_retention_days: int = 90

    # Gold time windows (safety/etl/gold.py, migration 018). While on, every
    # refresh also writes last_3m / last_1y / last_2y under the names the
    # previous release reads (last_90d / last_12m / last_24m), so rolling back
    # still finds a map. Turned off, and the copies deleted, by the contract
    # migration once no deployed release reads the old names.
    gold_legacy_windows: bool = True
    # Fallback if the long windows do not fit: windows longer than this many
    # years keep their incident counts but are built without the safety
    # ranking. Unset builds the ranking for every window.
    safety_max_window_years: int | None = None

    @property
    def dsn(self) -> str:
        if not self.postgres_password:
            raise RuntimeError(
                "POSTGRES_PASSWORD is not set; put it in the instance .env (see .env.example)"
            )
        # connect_timeout rides on the DSN so it applies to both plain
        # connections (safety/db.py) and the API's pool (safety/api/main.py).
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            f"?connect_timeout={self.connect_timeout_seconds}"
        )


settings = Settings()
