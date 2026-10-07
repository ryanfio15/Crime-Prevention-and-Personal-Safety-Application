"""/quality's silver mixes are served from the refresh-stamped cache (F6)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from safety.api import main

KEYS = ["validation_issues", "recent_pulls", "coordinate_provenance", "offense_mapping_confidence"]


def _dummy_conn():
    yield object()


def _patch(monkeypatch, stamp="s1"):
    calls = {"live": 0, "mix": 0}

    def data_quality(conn, city):
        calls["live"] += 1
        return {"validation_issues": [], "recent_pulls": [{"pull_id": calls["live"]}]}

    def mix(conn, city):
        calls["mix"] += 1
        return {
            "coordinate_provenance": [{"coordinate_source": "published_wgs84", "n": 10}],
            "offense_mapping_confidence": [{"mapping_confidence": "exact", "n": 9}],
        }

    main.app.dependency_overrides[main.get_conn] = _dummy_conn
    monkeypatch.setattr(main.repo, "data_quality", data_quality)
    monkeypatch.setattr(main.repo, "silver_provenance_mix", mix)
    monkeypatch.setattr(main, "_refresh_stamp", lambda conn, source_id=None: stamp)
    return calls


def test_silver_mix_is_cached_and_etl_findings_stay_live(monkeypatch):
    calls = _patch(monkeypatch)
    client = TestClient(main.app)
    first = client.get("/api/v1/quality?city=phl").json()
    second = client.get("/api/v1/quality?city=phl").json()
    assert calls == {"live": 2, "mix": 1}
    assert second["recent_pulls"] == [{"pull_id": 2}]
    assert first["coordinate_provenance"] == second["coordinate_provenance"]


def test_quality_key_order_is_unchanged(monkeypatch):
    _patch(monkeypatch)
    body = TestClient(main.app).get("/api/v1/quality?city=phl").json()
    assert list(body) == KEYS


def test_unknown_city_is_not_cached(monkeypatch):
    calls = _patch(monkeypatch, stamp=None)
    client = TestClient(main.app)
    client.get("/api/v1/quality?city=nowhere")
    client.get("/api/v1/quality?city=nowhere")
    assert calls["mix"] == 2
    assert not main._cache
