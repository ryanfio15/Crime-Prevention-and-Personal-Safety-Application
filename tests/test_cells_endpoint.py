"""/api/v1/cells serves cached gzip bytes without recompressing them (F9), and
serves the last year and nothing else: no other window, no date range, no
category, no resolution 10 (migration 024)."""

from __future__ import annotations

import gzip
import json

import pytest
from fastapi.testclient import TestClient

from safety.api import main

URL = "/api/v1/cells?city=phl&res=8"

WINDOWS = [
    {
        "id": "last_1y", "label": "Last 12 months", "hourly": True, "safety": True,
        "start": "2024-07-01", "end": "2025-06-30",
    }
]
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
    # Straight past the cache, so the cache assertions below count layers only.
    monkeypatch.setattr(main, "city_windows", lambda conn, source_id: WINDOWS)
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


# ---------------------------------------------------------------- cache scope (F17)


def test_city_without_a_stamp_is_never_cached(calls, monkeypatch):
    monkeypatch.setattr(main, "_refresh_stamp", lambda conn, source_id=None: None)
    client = TestClient(main.app)
    client.get(URL)
    client.get(URL)
    assert len(calls) == 2
    assert not main._cache


def test_plain_layer_is_still_cached(calls):
    client = TestClient(main.app)
    client.get(URL)
    client.get(URL)
    assert len(calls) == 1
    assert main._cache_stats["hits"] == 1


# ------------------------------------------------------- one year only (024)


def test_the_last_year_is_served(calls):
    r = TestClient(main.app).get(URL)
    assert r.status_code == 200
    assert calls[0]["time_window"] == "last_1y"


@pytest.mark.parametrize(
    "extra",
    [
        "&window=last_2y",
        "&window=last_3m",
        "&from=2020-01-01&to=2020-12-31",
        "&category=violent",
        "&min_count=5",
        "&bbox=-75.2,39.9,-75.1,40.0",
        "&measure=activity",
    ],
)
def test_other_data_cannot_be_asked_for(calls, extra):
    """Anything that used to choose other data is ignored: the same layer, from
    the same cache entry, as the plain request."""
    client = TestClient(main.app)
    plain = client.get(URL)
    other = client.get(URL + extra)
    assert other.status_code == 200
    assert other.content == plain.content
    assert len(calls) == 1
    assert set(calls[0]) == {"source_id", "h3_res", "time_window", "hour", "day"}


def test_res10_is_not_served(calls):
    r = TestClient(main.app).get("/api/v1/cells?city=phl&res=10")
    assert r.status_code == 400
    assert calls == []


def test_hour_and_day_are_served(calls):
    client = TestClient(main.app)
    assert client.get(URL + "&hour=3&day=5").status_code == 200
    assert calls[0]["hour"] == 3 and calls[0]["day"] == 5


def test_hour_out_of_range_is_rejected(calls):
    assert TestClient(main.app).get(URL + "&hour=24").status_code == 422


def test_city_with_no_window_yet_still_answers(calls, monkeypatch):
    monkeypatch.setattr(main, "city_windows", lambda conn, source_id: [])
    r = TestClient(main.app).get("/api/v1/cells?city=nowhere&res=8")
    assert r.status_code == 200
    assert calls[0]["time_window"] == "last_1y"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/summary?city=phl",
        "/api/v1/hours?city=phl",
        "/api/v1/categories?city=phl",
        "/api/v1/cells/ring?h3=882a13d0b1fffff",
        "/api/v1/cells/lookup?lat=39.95&lng=-75.16",
    ],
)
def test_retired_endpoints_are_gone(calls, path):
    # /cells/ring and /cells/lookup now fall to /cells/{h3_index}, which refuses
    # "ring" and "lookup" as cell ids.
    assert TestClient(main.app).get(path).status_code in (400, 404)
