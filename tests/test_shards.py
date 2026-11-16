"""Cities split across databases: parsing, the per-database runner, API routing."""

from __future__ import annotations

import sys
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from safety import bootstrap, joblock, migrate, ops, shards
from safety.api import main
from safety.config import settings
from safety.etl import run as etl_run

SPEC = "safety-db1-rw=lax,dc;safety-db2-rw=aus,phl;safety-db3-rw=chi,sea"


# --------------------------------------------------------------------------- parse


def test_parse_in_order():
    parsed = shards.parse(SPEC)
    assert [s.host for s in parsed] == ["safety-db1-rw", "safety-db2-rw", "safety-db3-rw"]
    assert parsed[1].cities == ("aus", "phl")
    assert shards.host_of("sea", parsed) == "safety-db3-rw"
    assert shards.host_of("nyc", parsed) is None


def test_parse_empty_and_whitespace():
    assert shards.parse("") == []
    assert shards.parse(" ; ") == []
    assert shards.parse(" a = PHL , chi ;") == [shards.Shard("a", ("phl", "chi"))]


@pytest.mark.parametrize(
    "spec, message",
    [
        ("a=phl;b=phl", "both"),
        ("a=phl;a=chi", "twice"),
        ("a", "host=city"),
        ("=phl", "host=city"),
        ("a=", "no cities"),
        ("a=ph l", "source id"),
    ],
)
def test_parse_rejects(spec, message):
    with pytest.raises(ValueError, match=message):
        shards.parse(spec)


# --------------------------------------------------------------------------- run

PRINT_ENV = [
    sys.executable,
    "-c",
    "import os; print(os.environ.get('POSTGRES_HOST'), os.environ.get('SHARD_CITIES'),"
    " os.environ.get('BOOTSTRAP_CITIES'))",
]


def test_run_without_shards_runs_once_unchanged(capfd, monkeypatch):
    monkeypatch.delenv("SHARD_CITIES", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "only-db")
    monkeypatch.setenv("BOOTSTRAP_CITIES", "phl")
    assert shards.run(PRINT_ENV, []) == 0
    assert capfd.readouterr().out.strip() == "only-db None phl"


def test_run_once_per_shard_with_its_cities(capfd):
    assert shards.run(PRINT_ENV, shards.parse("h1=lax,dc;h2=phl")) == 0
    lines = [line for line in capfd.readouterr().out.splitlines() if not line.startswith("==>")]
    assert lines == ["h1 lax,dc lax,dc", "h2 phl phl"]


def test_run_goes_on_after_a_failure_and_reports_the_first(capfd):
    command = [
        sys.executable,
        "-c",
        "import os, sys; print(os.environ['POSTGRES_HOST']);"
        " sys.exit({'h1': 3, 'h2': 0, 'h3': 5}[os.environ['POSTGRES_HOST']])",
    ]
    assert shards.run(command, shards.parse("h1=a;h2=b;h3=c")) == 3
    out = [line for line in capfd.readouterr().out.splitlines() if not line.startswith("==>")]
    assert out == ["h1", "h2", "h3"]


def test_cli(monkeypatch, capsys):
    monkeypatch.setattr(settings, "db_shards", "h1=lax;h2=phl")
    assert shards.main(["list"]) == 0
    assert capsys.readouterr().out.splitlines() == ["h1\tlax", "h2\tphl"]
    assert shards.main(["run"]) == 2
    assert shards.main(["run", "--"]) == 2
    monkeypatch.setattr(settings, "db_shards", "h1=phl;h2=phl")
    assert shards.main(["list"]) == 2


# --------------------------------------------------------------------------- bootstrap


@pytest.fixture
def boot(monkeypatch):
    log = []
    monkeypatch.setattr(bootstrap, "wait_for_db", lambda: None)
    monkeypatch.setattr(migrate, "main", lambda argv: 0)
    monkeypatch.setattr(etl_run, "main", lambda argv: log.append(tuple(argv)) or 0)
    monkeypatch.setattr(ops, "main", lambda argv: 0)

    @contextmanager
    def lock(wait=None):
        yield None

    monkeypatch.setattr(joblock, "etl_lock", lock)
    monkeypatch.setattr(bootstrap, "enabled_cities", lambda: ["aus", "dc", "lax", "phl"])
    return log


def test_bootstrap_on_a_shard_disables_the_other_cities(boot, monkeypatch):
    monkeypatch.setattr(settings, "shard_cities", "lax,dc")
    assert bootstrap.main(["--cities", "lax,dc", "--skip-converge"]) == 0
    assert boot == [
        ("enable", "--city", "lax"),
        ("enable", "--city", "dc"),
        ("enable", "--city", "aus", "--off"),
        ("enable", "--city", "phl", "--off"),
    ]


def test_bootstrap_without_shards_disables_nothing(boot, monkeypatch):
    monkeypatch.setattr(settings, "shard_cities", "")
    bootstrap.main(["--cities", "lax", "--skip-converge"])
    assert all("--off" not in call for call in boot)


def test_joblock_uses_its_own_host(monkeypatch):
    seen = []
    monkeypatch.setattr(settings, "postgres_password", "pw")
    monkeypatch.setattr(settings, "postgres_host", "safety-db2-rw")
    monkeypatch.setattr(joblock.psycopg, "connect", lambda dsn, **kw: seen.append(dsn))
    monkeypatch.setattr(settings, "joblock_host", "safety-db1-rw")
    joblock._connect()
    monkeypatch.setattr(settings, "joblock_host", "")
    joblock._connect()
    assert "@safety-db1-rw:" in seen[0]
    assert "@safety-db2-rw:" in seen[1]


# --------------------------------------------------------------------------- API


class Conn:
    def __init__(self, host):
        self.host = host


class Pool:
    def __init__(self, host, up=True):
        self.host, self.up, self.borrowed = host, up, 0

    @contextmanager
    def connection(self):
        if not self.up:
            raise main.PoolTimeout(f"{self.host} is down")
        self.borrowed += 1
        yield Conn(self.host)


CITY_OF = {"h1": ["lax", "dc"], "h2": ["aus", "phl"], "h3": ["chi", "sea"]}
CELL_CITY = {("h3", "8828d542c9fffff"): "sea"}


@pytest.fixture
def sharded(monkeypatch):
    p = {host: Pool(host) for host in CITY_OF}
    monkeypatch.setattr(main, "pools", p)
    monkeypatch.setattr(
        main, "_city_hosts", {c: h for h, cities in CITY_OF.items() for c in cities}
    )
    monkeypatch.setattr(main, "pool", p["h1"])
    monkeypatch.setattr(main, "_cell_city", {})

    def serving_version(conn, source_id=None):
        cities = CITY_OF[conn.host] if source_id is None else [source_id]
        return {
            "last_refreshed_at": f"2026-11-0{list(CITY_OF).index(conn.host) + 1}",
            "data_as_of": "2026-11-01",
            "incident_count": 100 * len(cities),
            "cities": len(cities),
            "host": conn.host,
        }

    monkeypatch.setattr(main.repo, "serving_version", serving_version)
    monkeypatch.setattr(
        main.repo, "cell_source", lambda conn, cell: CELL_CITY.get((conn.host, cell))
    )
    return p


def test_pool_for(sharded):
    assert main.pool_for("sea") is sharded["h3"]
    assert main.pool_for("phl") is sharded["h2"]
    assert main.pool_for("nyc") is sharded["h1"]
    assert main.pool_for(None) is sharded["h1"]


def test_a_city_request_goes_to_its_database(sharded):
    body = TestClient(main.app).get("/api/v1/version", params={"city": "chi"}).json()
    assert body["host"] == "h3"


def test_version_without_a_city_spans_every_database(sharded):
    body = TestClient(main.app).get("/api/v1/version").json()
    assert body["cities"] == 6
    assert body["incident_count"] == 600
    assert body["last_refreshed_at"] == "2026-11-03"


def test_health_degrades_rather_than_fails_with_one_database_down(sharded):
    # h2 holds Philadelphia, the default city: health must not depend on it.
    sharded["h2"].up = False
    response = TestClient(main.app).get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["databases"] == {"h1": "ok", "h2": "unavailable", "h3": "ok"}
    assert body["incidents"] == 400
    assert TestClient(main.app).get("/api/v1/version").json()["cities"] == 4


def test_health_fails_when_every_database_is_down(sharded):
    for each in sharded.values():
        each.up = False
    assert TestClient(main.app).get("/api/v1/health").status_code == 503


def test_a_cell_is_found_in_its_database_once(sharded):
    assert main._city_of_cell("8828d542c9fffff") == "sea"
    borrowed = sum(p.borrowed for p in sharded.values())
    assert main._city_of_cell("8828d542c9fffff") == "sea"
    assert sum(p.borrowed for p in sharded.values()) == borrowed
    assert main._city_of_cell("not-a-cell") is None


def test_cities_merges_every_database(sharded, monkeypatch):
    names = {"lax": "Los Angeles", "dc": "Washington", "aus": "Austin", "phl": "Philadelphia",
             "chi": "Chicago", "sea": "Seattle"}
    monkeypatch.setattr(
        main.repo,
        "list_cities",
        lambda conn: [{"source_id": c, "city_name": names[c], "host": conn.host}
                      for c in CITY_OF[conn.host]],
    )
    windows_from = {}

    def city_windows(conn, source_id):
        windows_from[source_id] = conn.host
        return []

    monkeypatch.setattr(main.repo, "city_windows", city_windows)
    monkeypatch.setattr(main.repo, "series_caveats", lambda conn, source_id: [])
    body = TestClient(main.app).get("/api/v1/cities").json()
    assert [c["city_name"] for c in body["cities"]] == sorted(names.values())
    # Each city's windows were read from its own database.
    assert windows_from == {c: h for h, cities in CITY_OF.items() for c in cities}
    assert [r["source_id"] for r in main.list_cities_all()] == [
        "aus", "chi", "lax", "phl", "sea", "dc"
    ]


def _request(path_params=None, query=b""):
    from starlette.requests import Request

    return Request(
        {"type": "http", "path_params": path_params or {}, "query_string": query, "headers": []}
    )


def test_request_city_reads_what_the_endpoint_reads(sharded):
    assert main.request_city(_request({"source_id": "dc"})) == "dc"
    assert main.request_city(_request(query=b"city=aus&res=8")) == "aus"
    assert main.request_city(_request({"h3_index": "8828d542c9fffff"})) == "sea"
    assert main.request_city(_request(query=b"h3=8828d542c9fffff&k=1")) == "sea"
    # /cells/lookup: the point's resolution-8 cell (central Seattle).
    assert main.request_city(_request(query=b"lat=47.6062&lng=-122.3321")) == "sea"
    assert main.request_city(_request(query=b"lat=x&lng=1")) == main.DEFAULT_CITY
    # No city named: the endpoints' own default.
    assert main.request_city(_request()) == main.DEFAULT_CITY


def test_cities_and_directions_list_survive_one_database_down(sharded, monkeypatch):
    monkeypatch.setattr(
        main.repo,
        "list_cities",
        lambda conn: [{"source_id": c, "city_name": c} for c in CITY_OF[conn.host]],
    )
    monkeypatch.setattr(main.repo, "city_windows", lambda conn, source_id: [])
    monkeypatch.setattr(main.repo, "series_caveats", lambda conn, source_id: [])
    sharded["h1"].up = False
    response = TestClient(main.app).get("/api/v1/cities")
    assert response.status_code == 200
    assert [c["source_id"] for c in response.json()["cities"]] == ["aus", "chi", "phl", "sea"]
    assert [r["source_id"] for r in main.list_cities_all()] == ["aus", "chi", "phl", "sea"]
    for each in sharded.values():
        each.up = False
    assert TestClient(main.app).get("/api/v1/cities").status_code == 503


# --------------------------------------------------------------------------- egress


def test_routing_proxy_carries_https_only(monkeypatch):
    import asyncio

    from safety.routing import service

    monkeypatch.setattr(settings, "routing_https_proxy", "http://10.20.0.10:8888")
    client = service.new_http_client()
    https = client._transport_for_url(service.httpx.URL("https://photon.komoot.io/api"))
    plain = client._transport_for_url(service.httpx.URL("http://valhalla:8002/route"))
    assert https is not client._transport
    assert plain is client._transport
    assert https._pool._proxy_url.host == b"10.20.0.10"
    asyncio.run(client.aclose())

    monkeypatch.setattr(settings, "routing_https_proxy", "")
    client = service.new_http_client()
    assert client._transport_for_url(service.httpx.URL("https://x.org")) is client._transport
    asyncio.run(client.aclose())
