"""/api/v1/cells serves cached gzip bytes without recompressing them (F9),
resolves the requested window against the city's own list (migration 018), and
serves any two calendar dates (migration 020)."""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from safety.api import main

# last_12m on purpose: the previous release's name, which must keep working as
# an alias for last_1y.
URL = "/api/v1/cells?city=phl&res=8&window=last_12m"


def _window(name, start, hourly=False, res10=False):
    return {
        "id": name, "label": name, "hourly": hourly, "res10": res10, "safety": True,
        "start": start, "end": "2025-06-30",
    }


WINDOWS = [
    _window("last_3m", "2025-04-01"),
    _window("last_1y", "2024-07-01", hourly=True, res10=True),
    _window("last_2y", "2023-07-01", res10=True),
]
CITY = {
    "source_id": "phl",
    "selectable_start": date(2006, 1, 1),
    "coverage_end": date(2025, 6, 30),
}
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
    monkeypatch.setattr(main.repo, "get_city", lambda conn, source_id: dict(CITY))
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


def test_non_finite_bbox_is_rejected(calls):
    r = TestClient(main.app).get(URL + "&bbox=nan,0,1,1")
    assert r.status_code == 400
    assert calls == []


def test_bbox_requests_are_served_fresh(calls):
    client = TestClient(main.app)
    client.get(URL + "&bbox=-75.2,39.9,-75.1,40.0")
    client.get(URL + "&bbox=-75.2,39.9,-75.1,40.0")
    assert len(calls) == 2
    assert not main._cache


def test_min_count_requests_are_served_fresh(calls):
    client = TestClient(main.app)
    client.get(URL + "&min_count=1")
    client.get(URL + "&min_count=1")
    assert len(calls) == 2
    assert not main._cache


def test_plain_layer_is_still_cached(calls):
    client = TestClient(main.app)
    client.get(URL)
    client.get(URL)
    assert len(calls) == 1
    assert main._cache_stats["hits"] == 1


# ---------------------------------------------------------------- windows (018)


def test_legacy_name_is_served_as_the_new_window(calls):
    r = TestClient(main.app).get(URL)
    assert r.status_code == 200
    assert calls[0]["time_window"] == "last_1y"
    (key,) = [k for k in main._cache if k[0] == "cells"]
    assert "last_1y" in key and "last_12m" not in key


def test_alias_and_new_name_share_one_cache_entry(calls):
    client = TestClient(main.app)
    client.get(URL)
    client.get(URL.replace("last_12m", "last_1y"))
    assert len(calls) == 1


def test_window_the_city_does_not_have_is_rejected(calls):
    r = TestClient(main.app).get(URL.replace("last_12m", "last_10y"))
    assert r.status_code == 400
    assert "last_10y" in r.json()["detail"]
    assert calls == []


def test_malformed_window_is_rejected(calls):
    r = TestClient(main.app).get(URL.replace("last_12m", "last_7m"))
    assert r.status_code == 400
    assert calls == []


def test_hour_needs_the_hourly_window(calls):
    client = TestClient(main.app)
    assert client.get(URL + "&hour=3").status_code == 200
    assert client.get(URL.replace("last_12m", "last_2y") + "&hour=3").status_code == 400


def test_res10_needs_a_res10_window(calls):
    client = TestClient(main.app)
    base = "/api/v1/cells?city=phl&res=10&window="
    assert client.get(base + "last_2y").status_code == 200
    assert client.get(base + "last_3m").status_code == 400


def test_city_with_no_windows_checks_the_pattern_only(calls, monkeypatch):
    monkeypatch.setattr(main, "city_windows", lambda conn, source_id: [])
    r = TestClient(main.app).get("/api/v1/cells?city=nowhere&res=8&window=last_7y")
    assert r.status_code == 200
    assert calls[0]["time_window"] == "last_7y"


def test_retired_30_day_link_opens_on_3_months(calls):
    r = TestClient(main.app).get(URL.replace("last_12m", "last_30d"))
    assert r.status_code == 200
    assert calls[0]["time_window"] == "last_3m"


# ------------------------------------------------------------ date ranges (020)

RANGE = "/api/v1/cells?city=phl&res=8"


def test_custom_range_is_ranked_from_the_dates(calls):
    r = TestClient(main.app).get(RANGE + "&from=2025-01-01&to=2025-03-31")
    assert r.status_code == 200
    assert calls[0]["date_range"] == main.repo.DateRange(date(2025, 1, 1), date(2025, 3, 31))
    (key,) = [k for k in main._cache if k[0] == "cells"]
    assert ("range", "2025-01-01", "2025-03-31") in key


def test_a_single_day_is_a_range(calls):
    r = TestClient(main.app).get(RANGE + "&from=2025-02-03&to=2025-02-03")
    assert r.status_code == 200
    assert calls[0]["date_range"].start == calls[0]["date_range"].end == date(2025, 2, 3)


def test_end_past_the_data_is_clamped_to_one_cache_entry(calls):
    client = TestClient(main.app)
    today = date.today().isoformat()
    client.get(RANGE + f"&from=2025-01-01&to={today}")
    client.get(RANGE + "&from=2025-01-01&to=2025-06-30")
    assert len(calls) == 1
    assert calls[0]["date_range"].end == date(2025, 6, 30)


def test_range_equal_to_a_window_is_served_as_that_window(calls):
    client = TestClient(main.app)
    r = client.get(RANGE + "&from=2024-07-01&to=2025-06-30")
    assert r.status_code == 200
    assert calls[0]["date_range"] is None
    assert calls[0]["time_window"] == "last_1y"
    # ...including the hourly layer the stored window carries.
    assert client.get(RANGE + f"&from=2024-07-01&to={date.today()}&hour=3").status_code == 200
    # And it shares the window's cache entry.
    client.get(RANGE + "&window=last_1y")
    assert len(calls) == 2  # the hour request is its own layer


@pytest.mark.parametrize(
    "query",
    [
        "&from=2025-03-01&to=2025-01-01",  # reversed
        "&from=2005-12-31&to=2025-01-01",  # before the city's oldest day
        f"&from=2025-01-01&to={date.today() + timedelta(days=5)}",  # future
        "&from=2025-01-01",  # half a range
        "&to=2025-01-01",
    ],
)
def test_bad_ranges_are_rejected(calls, query):
    r = TestClient(main.app).get(RANGE + query)
    assert r.status_code == 400
    assert calls == []


def test_range_needs_a_daily_resolution(calls):
    r = TestClient(main.app).get("/api/v1/cells?city=phl&res=10&from=2025-01-01&to=2025-03-31")
    assert r.status_code == 400
    assert "res 10" in r.json()["detail"]


def test_range_has_no_hourly_layer(calls):
    r = TestClient(main.app).get(RANGE + "&from=2025-01-01&to=2025-03-31&hour=3")
    assert r.status_code == 400
    assert "time-of-day" in r.json()["detail"]


def test_city_not_rebuilt_refuses_ranges_but_serves_windows(calls, monkeypatch):
    monkeypatch.setattr(
        main.repo, "get_city", lambda conn, source_id: {**CITY, "selectable_start": None}
    )
    client = TestClient(main.app)
    assert client.get(RANGE + "&from=2025-01-01&to=2025-03-31").status_code == 400
    assert client.get(RANGE + "&window=last_1y").status_code == 200


def test_safety_is_dropped_past_the_configured_limit(calls, monkeypatch):
    monkeypatch.setattr(main.settings, "safety_max_window_years", 1)
    TestClient(main.app).get(RANGE + "&from=2020-01-01&to=2025-03-31")
    assert calls[0]["date_range"].safety is False
