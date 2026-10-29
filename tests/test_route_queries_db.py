"""repo.route_baseline and repo.route_cells against a real PostGIS.

Three hand-made res-9 cells on source `sea`, in one transaction that ends in
rollback, so the city averages are known exactly and nothing persists. Runs in
CI with the rest of the db-marked tests.
"""

from __future__ import annotations

from datetime import date

import h3
import pytest

from safety.api import repository as repo
from safety.routing import scoring

pytestmark = pytest.mark.db

SOURCE = "sea"
WINDOW = "last_6m"
RES = 9

# (sm, tier, exposure) per cell, violent track. The third has nothing reported
# (tier 0): it stays out of the average but still has a rate to borrow.
CELLS = {
    h3.latlng_to_cell(47.6097, -122.3422, RES): (0.2, 2, 1000.0),
    h3.latlng_to_cell(47.6205, -122.3493, RES): (0.1, 1, 3000.0),
    h3.latlng_to_cell(47.6062, -122.3321, RES): (0.05, 0, 500.0),
}
HOUR = 17


def _seed(conn) -> None:
    scheme = conn.execute(
        "SELECT severity_scheme_version AS v FROM reference.source_registry WHERE source_id = %s",
        (SOURCE,),
    ).fetchone()["v"]
    for table in ("gold.cell_hour_safety", "gold.cell_safety"):
        conn.execute(
            f"DELETE FROM {table} WHERE source_id = %s AND h3_res = %s AND time_window = %s",
            (SOURCE, RES, WINDOW),
        )
    common = {
        "sid": SOURCE, "res": RES, "win": WINDOW, "scheme": scheme,
        "start": date(2026, 4, 1), "end": date(2026, 9, 30),
    }  # fmt: skip
    for cell, (sm, tier, exposure) in CELLS.items():
        conn.execute(
            """
            INSERT INTO gold.cell_safety (
                source_id, h3_index, h3_res, time_window, track, scheme_version,
                window_start, window_end, incident_count, weighted_total, weighted_per_km2,
                smoothed_per_km2, safety_percentile, safety_rank, city_cell_total, safety_tier,
                refreshed_at, exposure
            ) VALUES (%(sid)s, %(cell)s, %(res)s, %(win)s, 'violent', %(scheme)s, %(start)s,
                      %(end)s, 1, 1, 1, %(sm)s, 0.5, 1, 3, %(tier)s, now(), %(exposure)s)
            """,
            {**common, "cell": cell, "sm": sm, "tier": tier, "exposure": exposure},
        )
        # At 17:00 only the first cell has anything reported.
        conn.execute(
            """
            INSERT INTO gold.cell_hour_safety (
                source_id, h3_index, h3_res, time_window, hour_block, track, scheme_version,
                window_start, window_end, incident_count, weighted_total, weighted_per_km2,
                smoothed_per_km2, safety_percentile, safety_rank, city_cell_total, safety_tier,
                refreshed_at
            ) VALUES (%(sid)s, %(cell)s, %(res)s, %(win)s, %(hour)s, 'violent', %(scheme)s,
                      %(start)s, %(end)s, 1, 1, 1, %(hsm)s, 0.5, 1, 3, %(htier)s, now())
            """,
            {
                **common, "cell": cell, "hour": HOUR, "hsm": sm / 24,
                "htier": tier if cell == next(iter(CELLS)) else 0,
            },  # fmt: skip
        )


def test_baseline_mirrors_value_domain(db_conn):
    try:
        _seed(db_conn)
        rows = repo.route_baseline(db_conn, SOURCE, RES, WINDOW, hourly=True)
        by_key = {(r["track"], r["hour_block"]): r for r in rows}
        overall = by_key[("violent", None)]
        got = scoring.average_from_sums(
            overall["weighted"], overall["people"], overall["total"], overall["painted"]
        )
        # Exposure-weighted over the two reported cells only.
        assert got == pytest.approx((0.2 * 1000 + 0.1 * 3000) / 4000)
        hourly = by_key[("violent", HOUR)]
        assert hourly["painted"] == 1
        assert hourly["weighted"] / hourly["people"] == pytest.approx(0.2 / 24)
        # Without hourly, no hour rows.
        assert all(
            r["hour_block"] is None
            for r in repo.route_baseline(db_conn, SOURCE, RES, WINDOW, hourly=False)
        )
    finally:
        db_conn.rollback()


def test_route_cells_carries_both_hours(db_conn):
    try:
        _seed(db_conn)
        cells = list(CELLS) + [h3.latlng_to_cell(47.70, -122.30, RES)]  # one with no row
        rows = repo.route_cells(db_conn, SOURCE, RES, WINDOW, cells, [HOUR])["violent"]
        assert set(rows) == set(CELLS)
        first = rows[next(iter(CELLS))]
        assert first["sm"] == pytest.approx(0.2) and first["tier"] == 2
        assert first[f"hsm.{HOUR}"] == pytest.approx(0.2 / 24)
        assert first[f"htier.{HOUR}"] == 2
        assert repo.route_cells(db_conn, SOURCE, RES, WINDOW, [], [HOUR]) == {
            "violent": {},
            "non_violent": {},
        }
    finally:
        db_conn.rollback()
