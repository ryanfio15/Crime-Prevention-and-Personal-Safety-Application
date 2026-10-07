"""Saturation and runaway queries answer 503 with Retry-After (F7).

TestClient(app) without `with`: the lifespan would open a real pool."""

from __future__ import annotations

from fastapi.testclient import TestClient
from psycopg.errors import QueryCanceled
from psycopg_pool import PoolTimeout

from safety.api import main


def _no_pool():
    raise PoolTimeout("couldn't get a connection after 10.00 sec")
    yield  # pragma: no cover - makes this a generator dependency like get_conn


def _dummy_conn():
    yield object()


def test_pool_timeout_is_503_with_retry_after():
    main.app.dependency_overrides[main.get_conn] = _no_pool
    r = TestClient(main.app).get("/api/v1/cities")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert r.json() == {"detail": "The service is busy; retry shortly."}


def test_statement_timeout_is_503(monkeypatch):
    def cancelled(conn):
        raise QueryCanceled("canceling statement due to statement timeout")

    main.app.dependency_overrides[main.get_conn] = _dummy_conn
    monkeypatch.setattr(main.repo, "list_cities", cancelled)
    r = TestClient(main.app).get("/api/v1/cities")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"


def test_statement_timeout_is_only_on_the_api_pool():
    from safety.config import settings

    assert settings.api_statement_timeout_ms == 15000
    assert settings.api_pool_timeout_seconds == 10.0


def test_lifespan_builds_the_pool_with_both_timeouts(monkeypatch):
    # P4: the settings above are only worth something if the pool is built with
    # them. A fake pool records the ConnectionPool(...) call the lifespan makes.
    from safety.config import settings

    captured = {}

    class FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, conninfo, **kw):
            captured.update(kw, conninfo=conninfo)

        def wait(self, timeout=None):
            pass

        def close(self):
            pass

    monkeypatch.setattr(settings, "postgres_password", "x")  # F20: dsn refuses an empty one
    monkeypatch.setattr(main, "ConnectionPool", FakePool)
    with TestClient(main.app):
        pass
    assert captured["kwargs"]["options"] == f"-c statement_timeout={settings.api_statement_timeout_ms}"
    assert captured["timeout"] == settings.api_pool_timeout_seconds


def test_dsn_requires_a_password(monkeypatch):
    # F20: no default password; the dsn refuses to build without one.
    import pytest

    from safety.config import Settings

    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        Settings(_env_file=None, postgres_password="").dsn
    assert ":x@" in Settings(_env_file=None, postgres_password="x").dsn
