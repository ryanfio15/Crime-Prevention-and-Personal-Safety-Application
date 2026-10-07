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
