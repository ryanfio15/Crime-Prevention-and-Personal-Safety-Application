"""The backfill progress bar (safety/etl/progress.py, GET /api/v1/progress)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from safety.etl import progress

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _row(source_id, *, served=False, phase=None, done=0, total=None, started=None, finished=None):
    return {
        "source_id": source_id,
        "city_name": source_id.upper(),
        "served": served,
        "phase": phase,
        "phase_done": done,
        "phase_total": total,
        "load_started_at": started,
        "phase_started_at": started,
        "updated_at": NOW,
        "finished_at": finished,
        "now": NOW,
    }


# ---------------------------------------------------------------- one city


def test_phases_add_up_in_order():
    assert sum(progress.WEIGHTS.values()) == pytest.approx(1.0)
    assert tuple(progress.WEIGHTS) == progress.PHASES


@pytest.mark.parametrize(
    ("phase", "done", "total", "expected"),
    [
        ("fetch", 0, 12, 0.0),
        ("fetch", 6, 12, 0.30),
        ("promote", 0, None, 0.60),
        ("census", 1, 2, 0.75),
        ("gold", 2, 4, 0.90),
    ],
)
def test_city_fraction_weights_each_phase(phase, done, total, expected):
    row = _row("phl", phase=phase, done=done, total=total, started=NOW)
    assert progress.city_fraction(row) == pytest.approx(expected)


def test_a_phase_never_reads_complete_before_the_next_begins():
    row = _row("phl", phase="fetch", done=15, total=12, started=NOW)
    assert progress.city_fraction(row) < progress.WEIGHTS["fetch"]


def test_an_unsized_fetch_shows_movement_once_it_starts():
    assert progress.city_fraction(_row("dc", phase="fetch", done=0, started=NOW)) == 0.0
    assert progress.city_fraction(_row("dc", phase="fetch", done=3, started=NOW)) == pytest.approx(
        progress.WEIGHTS["fetch"] * progress.INDETERMINATE
    )


def test_finished_and_queued_cities():
    assert progress.city_fraction(_row("phl", served=True, phase="done", finished=NOW)) == 1.0
    assert progress.city_fraction(_row("phl", served=True)) == 1.0
    assert progress.city_fraction(_row("phl")) == 0.0


# ---------------------------------------------------------------- the whole load


def test_nothing_loading_is_inactive():
    rows = [_row("phl", served=True, phase="done", started=NOW - timedelta(days=3),
                 finished=NOW - timedelta(days=3)),
            _row("chi", served=True)]
    body = progress.summarize(rows)
    assert body["active"] is False
    assert body["cities_ready"] == 2 and body["cities_total"] == 2
    assert body["eta_seconds"] is None


def test_a_load_in_progress_reports_percent_and_eta():
    started = NOW - timedelta(minutes=30)
    rows = [
        # Finished during this load: part of the figure, at 100%.
        _row("phl", served=True, phase="done", started=started, finished=NOW - timedelta(minutes=10)),
        # Halfway through its fetch.
        _row("chi", phase="fetch", done=6, total=12, started=NOW - timedelta(minutes=10)),
        # Not started yet.
        _row("sea"),
    ]
    body = progress.summarize(rows)
    assert body["active"] is True
    share = (1.0 + 0.30 + 0.0) / 3
    assert body["percent"] == pytest.approx(round(share * 100, 1))
    elapsed = 30 * 60
    assert body["eta_seconds"] == round(elapsed * (1 - share) / share)
    assert body["cities_ready"] == 1 and body["cities_total"] == 3
    by_id = {c["id"]: c for c in body["cities"]}
    assert by_id["phl"]["ready"] and by_id["phl"]["percent"] == 100.0
    assert not by_id["chi"]["ready"] and by_id["chi"]["phase"] == "fetch"
    assert by_id["sea"]["percent"] == 0.0


def test_cities_served_before_the_load_are_left_out_of_the_figure():
    rows = [
        _row("phl", served=True, phase="done", started=NOW - timedelta(days=9),
             finished=NOW - timedelta(days=9)),
        _row("aus", phase="fetch", done=5, total=10, started=NOW - timedelta(minutes=20)),
    ]
    body = progress.summarize(rows)
    assert body["percent"] == pytest.approx(30.0)
    assert body["cities_ready"] == 1


def test_cities_loaded_one_after_another_are_one_load():
    # dc finished, then phl began a few seconds later on the same database.
    rows = [
        _row("dc", served=True, phase="done", started=NOW - timedelta(minutes=20),
             finished=NOW - timedelta(minutes=12)),
        _row("phl", phase="gold", done=2, total=4, started=NOW - timedelta(minutes=12)),
    ]
    body = progress.summarize(rows)
    assert body["percent"] == pytest.approx((1.0 + 0.9) / 2 * 100, abs=0.1)
    assert body["started_at"] == (NOW - timedelta(minutes=20)).isoformat()


def test_no_estimate_until_enough_is_done():
    rows = [_row("chi", phase="fetch", done=0, total=12, started=NOW - timedelta(minutes=20))]
    assert progress.summarize(rows)["eta_seconds"] is None
    rows = [_row("chi", phase="fetch", done=6, total=12, started=NOW - timedelta(seconds=20))]
    assert progress.summarize(rows)["eta_seconds"] is None


def test_a_failed_load_stays_active():
    rows = [_row("chi", phase="failed", done=4, total=12, started=NOW - timedelta(minutes=5))]
    body = progress.summarize(rows)
    assert body["active"] is True and body["cities"][0]["phase"] == "failed"


def test_writes_never_break_the_load(monkeypatch):
    def boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(progress, "_writer", boom)
    monkeypatch.setattr(progress, "_retry_after", 0.0)
    progress.begin("phl", 12)  # logged, not raised
    progress.step(None, "phl", 1)  # and then quiet for RETRY_SECONDS
    assert progress._retry_after > 0


# ---------------------------------------------------------------- the endpoint


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args):
        pass

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


class _Pool:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows or [], fail

    def connection(self):
        pool = self

        class _Ctx:
            def __enter__(self):
                if pool.fail:
                    raise OSError("database down")
                return _Conn(pool.rows)

            def __exit__(self, *exc):
                return False

        return _Ctx()


def test_endpoint_merges_every_database_and_is_never_cached(monkeypatch):
    from safety.api import main

    shard1 = _Pool([_row("phl", served=True)])
    shard2 = _Pool([_row("chi", phase="gold", done=1, total=4, started=NOW - timedelta(hours=1))])
    down = _Pool(fail=True)
    monkeypatch.setattr(main, "all_pools", lambda: [shard1, shard2, down])
    r = TestClient(main.app).get("/api/v1/progress")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["active"] is True
    assert {c["id"] for c in body["cities"]} == {"phl", "chi"}
    assert body["cities_ready"] == 1


@pytest.mark.db
def test_a_load_is_recorded_phase_by_phase(db_conn):
    conn = db_conn
    source = "aus"
    conn.execute("DELETE FROM etl.load_progress WHERE source_id = %s", (source,))
    conn.commit()
    progress._retry_after = 0.0

    def row():
        conn.rollback()
        return conn.execute(
            "SELECT * FROM etl.load_progress WHERE source_id = %s", (source,)
        ).fetchone()

    # Outside a load, phase/step/finish write nothing.
    progress.phase(conn, source, "gold", total=4)
    assert row() is None

    progress.begin(source, 10)
    assert (row()["phase"], row()["phase_total"]) == ("fetch", 10)
    progress.step(conn, source, 4)
    assert row()["phase_done"] == 4
    progress.phase(conn, source, "census", total=2)
    assert (row()["phase"], row()["phase_done"]) == ("census", 0)
    progress.finish(conn, source)
    done = row()
    assert done["phase"] == "done" and done["finished_at"] is not None
    # A finished load ignores the scheduled refreshes that follow.
    progress.phase(conn, source, "gold", total=4)
    assert row()["phase"] == "done"

    with conn.cursor() as cur:
        cur.execute(progress.STATE_SQL)
        states = {r["source_id"]: r for r in cur.fetchall()}
    conn.rollback()
    if source in states:
        assert states[source]["phase"] == "done"
    conn.execute("DELETE FROM etl.load_progress WHERE source_id = %s", (source,))
    conn.commit()
