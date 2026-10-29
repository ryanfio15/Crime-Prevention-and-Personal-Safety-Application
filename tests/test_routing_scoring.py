"""Route activity scoring (safety/routing/scoring.py) on synthetic cells.

No database and no providers: the routes are straight lines and hand-built
Route objects, the cell rows are dictionaries, and the city averages are
numbers, so every expected figure can be worked out by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import h3
import pytest

from safety.routing import detours, scoring
from safety.routing.schema import Point, Route, TransitLeg, TransitStop, haversine_m

FIXTURES = Path(__file__).parent / "fixtures" / "routing"

# Centre City Philadelphia, west to east along Market Street, ~1.3 km.
WEST = (39.9524, -75.1720)
EAST = (39.9520, -75.1560)


def straight(a=WEST, b=EAST, n=2):
    return [(a[0] + (b[0] - a[0]) * i / (n - 1), a[1] + (b[1] - a[1]) * i / (n - 1)) for i in range(n)]


def route(mode="walk", duration=900.0, geometry=None, distance=None):
    geometry = geometry or straight()
    if distance is None:
        distance = sum(haversine_m(geometry[i - 1], geometry[i]) for i in range(1, len(geometry)))
    return Route(mode=mode, duration_s=duration, distance_m=distance, geometry=geometry)


def all_ratios(runs, value):
    return {i: value for i in range(len(runs))}


# ---------------------------------------------------------------- sampling


def test_sampling_covers_every_cell_the_line_crosses():
    runs = scoring.sample_runs(straight(), 20.0)
    crossed = {r.cell for r in runs}
    start = h3.latlng_to_cell(*WEST, scoring.SCORE_RES)
    end = h3.latlng_to_cell(*EAST, scoring.SCORE_RES)
    # grid_path_cells is a straight line through cell centres, not the
    # geographic line, so it can differ by a cell at the edges; every cell
    # on the path should still be within one ring of a sampled cell.
    for cell in h3.grid_path_cells(start, end):
        assert cell in crossed or any(n in crossed for n in h3.grid_disk(cell, 1))
    assert runs[0].cell == start and runs[-1].cell == end


def test_run_lengths_add_up_to_the_line():
    line = straight(n=7)
    runs = scoring.sample_runs(line, 20.0)
    total = sum(haversine_m(line[i - 1], line[i]) for i in range(1, len(line)))
    assert sum(r.length_m for r in runs) == pytest.approx(total, rel=1e-6)
    # Consecutive runs never repeat a cell: they are merged.
    assert all(a.cell != b.cell for a, b in zip(runs, runs[1:]))


def test_run_lines_join_without_gaps():
    runs = scoring.sample_runs(straight(), 20.0)
    for a, b in zip(runs, runs[1:]):
        assert a.points[-1] == b.points[0]


def test_time_is_spread_by_distance():
    runs = scoring.sample_runs(straight(), 20.0, duration_s=1000.0, t0_s=60.0)
    assert runs[0].t_offset_s == pytest.approx(60.0)
    assert all(a.t_offset_s < b.t_offset_s for a, b in zip(runs, runs[1:]))
    assert runs[-1].t_offset_s < 1060.0


def test_degenerate_lines_have_no_runs():
    assert scoring.sample_runs([WEST], 20.0) == []
    assert scoring.sample_runs([WEST, WEST], 20.0) == []


def test_transit_scores_walks_and_stops_not_the_ride():
    walk_to = TransitLeg(
        mode="WALK", transit=False, start_time="2026-10-29T14:00:00Z",
        end_time="2026-10-29T14:05:00Z", duration_s=300, distance_m=300,
        origin=TransitStop(None, Point(*WEST)), destination=TransitStop("A", Point(39.9524, -75.1685)),
        geometry=[WEST, (39.9524, -75.1685)],
    )  # fmt: skip
    ride = TransitLeg(
        mode="SUBWAY", transit=True, start_time="2026-10-29T14:06:00Z",
        end_time="2026-10-29T14:10:00Z", duration_s=240, distance_m=900,
        origin=TransitStop("A", Point(39.9524, -75.1685)), destination=TransitStop("B", Point(*EAST)),
        geometry=[(39.9524, -75.1685), EAST],
    )  # fmt: skip
    r = Route(
        mode="transit", duration_s=600, distance_m=1200, geometry=[WEST, EAST],
        legs=[walk_to, ride], start_time="2026-10-29T14:00:00Z",
    )  # fmt: skip
    runs = scoring.route_runs(r)
    walk_len = sum(x.length_m for x in runs if x.points)
    stops = [x for x in runs if not x.points]
    assert walk_len == pytest.approx(haversine_m(WEST, (39.9524, -75.1685)), rel=1e-6)
    assert len(stops) == 2 and all(s.length_m == scoring.STOP_M for s in stops)
    # The boarding stop is scored when the ride leaves, six minutes in.
    assert stops[0].t_offset_s == pytest.approx(360)
    assert stops[1].t_offset_s == pytest.approx(600)


# ---------------------------------------------------------------- ratio


@pytest.mark.parametrize(
    "case", json.loads((FIXTURES / "ratio_cases.json").read_text())["cases"], ids=lambda c: c["name"]
)
def test_cell_ratio_shared_cases(case):
    hour = case["hour"]
    base = scoring.Baseline(case["overall"], {hour: case["own"]} if hour is not None and case["own"] else {})
    if hour is None:
        base = scoring.Baseline(case["own"])
    got = scoring.cell_ratio(case["cell"], base, hour)
    if case["expected"] is None:
        assert got is None
    else:
        assert got == pytest.approx(case["expected"])


def test_domain_average_is_per_person_over_painted_cells():
    rows = [
        {"sm": 0.2, "tier": 2, "exposure": 1000},
        {"sm": 0.1, "tier": 1, "exposure": 3000},
        {"sm": 9.9, "tier": 0, "exposure": 50},  # nothing reported: not in the average
    ]
    assert scoring.domain_average(rows, "sm", "tier") == pytest.approx((200 + 300) / 4000)


def test_domain_average_without_people_is_a_plain_mean():
    rows = [{"sm": 0.2, "tier": 2}, {"sm": 0.4, "tier": 1}]
    assert scoring.domain_average(rows, "sm", "tier") == pytest.approx(0.3)
    assert scoring.domain_average([], "sm", "tier") is None
    assert scoring.average_from_sums(0, 0, 0, 0) is None


# ---------------------------------------------------------------- metrics


def test_track_activity_uniform_route():
    runs = scoring.sample_runs(straight(), 20.0)
    a = scoring.track_activity(runs, all_ratios(runs, 1.5))
    assert a["comparable"] and a["mean_ratio"] == pytest.approx(1.5)
    assert a["peak_ratio"] == pytest.approx(1.5)
    assert a["share_above_2x"] == 0
    assert a["rated_share"] == 1.0
    assert a["sense"] == "higher"
    # One colour along the whole line: one segment.
    assert len(a["segments"]) == 1 and a["segments"][0]["ratio"] == 1.5


def test_track_activity_mixed_route():
    runs = scoring.sample_runs(straight(), 20.0)
    ratios = {i: (3.0 if i == 0 else 0.5) for i in range(len(runs))}
    a = scoring.track_activity(runs, ratios)
    total = sum(r.length_m for r in runs)
    expected = (runs[0].length_m * 3.0 + (total - runs[0].length_m) * 0.5) / total
    assert a["mean_ratio"] == pytest.approx(expected, abs=1e-3)
    assert a["share_above_2x"] == pytest.approx(runs[0].length_m / total, abs=1e-3)
    assert a["peak_ratio"] > a["mean_ratio"]


def test_too_little_rated_claims_no_comparison():
    runs = scoring.sample_runs(straight(), 20.0)
    ratios = {i: (1.0 if i == 0 else None) for i in range(len(runs))}
    a = scoring.track_activity(runs, ratios)
    assert a["rated_share"] < scoring.MIN_RATED_SHARE
    assert not a["comparable"]
    assert a["mean_ratio"] is None and a["sense"] is None and a["peak_ratio"] is None


@pytest.mark.parametrize("ratio,word", [(1.06, "higher"), (1.05, "about"), (0.95, "about"), (0.94, "lower")])
def test_sense_deadband(ratio, word):
    assert scoring.sense(ratio) == word


# ---------------------------------------------------------------- pick


def act(mean, comparable=True):
    return {"comparable": comparable, "mean_ratio": mean if comparable else None}


def test_pick_takes_the_lowest_qualifying_route():
    routes = [route(duration=1000), route(duration=1100), route(duration=1150)]
    p = scoring.pick("walk", routes, [act(1.5), act(1.2), act(1.1)])
    assert p["lower"] == 2 and p["extra_s"] == 150 and p["why"] is None


def test_pick_needs_both_drops():
    routes = [route(duration=1000), route(duration=1050)]
    # 0.95 vs 1.0: 5% lower and 0.05 lower -- neither threshold.
    assert scoring.pick("walk", routes, [act(1.0), act(0.95)])["why"] == "not_lower"
    # 0.25 vs 0.3: 17% lower but only 0.05 lower -- fails the absolute test.
    assert scoring.pick("walk", routes, [act(0.3), act(0.25)])["why"] == "not_lower"
    # 2.8 vs 3.0: 0.2 lower but under 10% -- fails the relative test.
    assert scoring.pick("walk", routes, [act(3.0), act(2.8)])["why"] == "not_lower"
    assert scoring.pick("walk", routes, [act(1.0), act(0.8)])["lower"] == 1


def test_pick_time_allowance():
    # 20% of 30 min is 6 min, under the 10 min walking cap.
    assert scoring.extra_allowance_s("walk", 1800) == 360
    # Never under three minutes...
    assert scoring.extra_allowance_s("drive", 300) == 180
    # ...nor over the mode's cap.
    assert scoring.extra_allowance_s("drive", 7200) == 480
    assert scoring.extra_allowance_s("transit", 7200) == 900
    routes = [route(duration=1800), route(duration=1800 + 361)]
    assert scoring.pick("walk", routes, [act(1.5), act(0.5)])["why"] == "too_slow"


def test_pick_single_route_and_unrated():
    assert scoring.pick("drive", [route()], [act(1.0)])["why"] == "single_route"
    routes = [route(duration=1000), route(duration=1050)]
    assert scoring.pick("walk", routes, [act(None, False), act(0.5)])["why"] == "not_rated"
    # An unrated alternative is never picked.
    assert scoring.pick("walk", routes, [act(1.5), act(None, False)])["lower"] is None


def test_distinct_routes_drops_overlapping_ones():
    a = route(duration=1000)
    b = route(duration=1010)  # same line
    c = route(duration=1200, geometry=straight((39.9580, -75.1720), (39.9576, -75.1560)))
    runs = [scoring.route_runs(r) for r in (a, b, c)]
    assert scoring.distinct_routes([a, b, c], runs) == [0, 2]


# ---------------------------------------------------------------- detours


def test_side_offsets_are_either_side_of_the_midpoint():
    a, b = Point(*WEST), Point(*EAST)
    left, right = detours.side_offsets(a, b)
    straight_m = haversine_m(WEST, EAST)
    mid = ((WEST[0] + EAST[0]) / 2, (WEST[1] + EAST[1]) / 2)
    for p in (left, right):
        d = haversine_m(mid, (p.lat, p.lng))
        assert d == pytest.approx(min(max(0.25 * straight_m, 150), 1200), rel=0.01)
    # An east-west trip is offset north and south.
    assert (left.lat - mid[0]) * (right.lat - mid[0]) < 0
    assert detours.side_offsets(a, a) == []


def test_detour_length_bound():
    assert detours.within_detour(1000, 1300)
    assert not detours.within_detour(1000, 1301)
