"""/api/v1/cells serves cached gzip bytes without recompressing them (F9)."""

from __future__ import annotations

import gzip
import json

import pytest
from fastapi.testclient import TestClient

from safety.api import main

URL = "/api/v1/cells?city=phl&res=8&window=last_12m"
FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"h3": f"cell-{i}", "count": i}, "geometry": None}
        for i in range(60)  # comfortably over GZipMiddleware's 1 KB floor
    ],
}


def _dummy_conn():
    yield object()


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_geojson(conn, **kwargs):
        seen.append(kwargs)
        return FIXTURE

    main.app.dependency_overrides[main.get_conn] = _dummy_conn
    monkeypatch.setattr(main.repo, "cells_geojson", fake_geojson)
    monkeypatch.setattr(main, "_refresh_stamp", lambda conn, source_id=None: "s1")
    return seen


def test_gzip_client_gets_the_cached_gzip(calls):
    r = TestClient(main.app).get(URL, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers["vary"].lower()
    assert r.json() == FIXTURE


def test_identity_client_gets_plain_json(calls):
    r = TestClient(main.app).get(URL, headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.json() == FIXTURE


def test_second_request_is_a_cache_hit(calls):
    client = TestClient(main.app)
    client.get(URL, headers={"Accept-Encoding": "gzip"})
    client.get(URL, headers={"Accept-Encoding": "identity"})
    assert len(calls) == 1


def test_cache_holds_gzip_bytes(calls):
    TestClient(main.app).get(URL, headers={"Accept-Encoding": "gzip"})
    (value,) = [v[1] for k, v in main._cache.items() if k[0] == "cells"]
    assert value[:2] == b"\x1f\x8b"


def test_body_is_gzipped_exactly_once(calls):
    client = TestClient(main.app)
    with client.stream("GET", URL, headers={"Accept-Encoding": "gzip"}) as r:
        raw = b"".join(r.iter_raw())
        assert int(r.headers["content-length"]) == len(raw)
    (stored,) = [v[1] for k, v in main._cache.items() if k[0] == "cells"]
    assert raw == stored
    assert json.loads(gzip.decompress(raw)) == FIXTURE
