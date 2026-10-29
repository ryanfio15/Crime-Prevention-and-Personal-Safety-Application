"""Route activity scoring: how much reported activity a route passes through,
against the city average (plan S3; design doc S13).

A route is never given a score of its own invention. Every figure here is the
map's own comparison -- a cell's smoothed severity-weighted rate over the
city's exposure-weighted average, the ratio web/app.js colours by -- averaged
along the route by length. So a route that reads "about the city average"
crosses cells the map paints, on average, the colour of an average cell.

    sample_runs   the route's line as runs of res-9 cells, with length and time
    Baseline      the city averages a ratio divides by (valueDomain's port)
    cell_ratio    one cell's multiple of the average (cellRatio's port)
    track_activity   mean / peak / share-above-2x / rated-share for one track
    pick          the "lower reported activity" alternative, or why there is none

Pure: no database and no FastAPI. The endpoint (safety/api/directions.py)
fetches the cell rows and the baseline and hands them in.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import h3

from safety.routing.schema import Route, haversine_m

# The map's detail resolution: the finest one the safety ranking is built at
# (SAFETY_RESOLUTIONS), ~0.1 km^2 a cell, about two city blocks across.
SCORE_RES = 9

TRACKS = ("violent", "non_violent")

# Sample spacing along the line. A res-9 cell is ~350 m across, so 20 m on foot
# cannot step over a cell corner the route clips for more than a few metres;
# driving is longer and coarser for the same point count.
SAMPLE_M = {"walk": 20.0, "bike": 20.0, "transit": 20.0, "drive": 40.0}

# Below this share of the route's length in rated cells, no comparison is
# claimed: the mean would describe the part of the route we can see, and the
# rest could be anything.
MIN_RATED_SHARE = 0.6

# The window `peak_ratio` is taken over: about four blocks, so one hot cell
# that a route clips for 30 m does not become the route's headline.
PEAK_WINDOW_M = 300.0
PEAK_PIECE_M = 20.0

# A transit stop is scored as this much standing in its cell, on boarding and
# on alighting -- waiting is time spent there.
STOP_M = 50.0

# The "lower reported activity" rule (plan S3).
EXTRA_SHARE = 0.20  # of the fastest route's duration...
EXTRA_MIN_S = 180.0  # ...but never less than three minutes...
EXTRA_CAP_S = {"walk": 600.0, "bike": 480.0, "drive": 480.0, "transit": 900.0}  # ...nor more than this
MIN_RELATIVE_DROP = 0.10  # at least 10% lower than the fastest...
MIN_ABSOLUTE_DROP = 0.10  # ...and at least 0.10 of the city average lower

# Two routes sharing this much of their length are the same route for the
# purpose of offering an alternative.
SAME_ROUTE_OVERLAP = 0.85

# The deadband relativeStat uses: within ±5% reads "about the city average".
DEADBAND = 0.05


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


@dataclass
class Run:
    """A stretch of the route inside one cell.

    `points` is the stretch's own line (vertices and samples, in order) and
    ends on the next run's first point, so consecutive runs drawn one after
    another leave no gap. `t_offset_s` is when the route enters the stretch,
    from its start.
    """

    cell: str
    length_m: float
    t_offset_s: float
    points: list[tuple[float, float]] = field(default_factory=list)


def _densify(points: Sequence[tuple[float, float]], step_m: float) -> list[tuple[float, float, float]]:
    """(lat, lng, distance from start) for every vertex and every `step_m` between."""
    out = [(points[0][0], points[0][1], 0.0)]
    along = 0.0
    for a, b in zip(points, points[1:]):
        seg = haversine_m(a, b)
        if seg <= 0:
            continue
        n = int(seg // step_m)
        for k in range(1, n + 1):
            f = k * step_m / seg
            if f >= 1:
                break
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, along + seg * f))
        along += seg
        out.append((b[0], b[1], along))
    return out


def sample_runs(
    points: Sequence[tuple[float, float]],
    step_m: float,
    duration_s: float = 0.0,
    t0_s: float = 0.0,
) -> list[Run]:
    """The line as consecutive runs of res-9 cells.

    Each sample point is assigned its own cell, and a stretch between two
    samples is credited half to each end's cell, so a run's length is the
    distance the route spends in it to within one step. Time is spread over
    the line in proportion to distance (`duration_s` for the whole line,
    starting at `t0_s`).
    """
    if len(points) < 2:
        return []
    dense = _densify(points, step_m)
    total = dense[-1][2]
    if total <= 0:
        return []
    speed = duration_s / total if duration_s > 0 else 0.0

    runs: list[Run] = []
    for i, (lat, lng, d) in enumerate(dense):
        cell = h3.latlng_to_cell(lat, lng, SCORE_RES)
        prev_d = dense[i - 1][2] if i else d
        next_d = dense[i + 1][2] if i + 1 < len(dense) else d
        share = (d - prev_d) / 2 + (next_d - d) / 2
        if runs and runs[-1].cell == cell:
            runs[-1].length_m += share
            runs[-1].points.append((lat, lng))
        else:
            if runs:
                runs[-1].points.append((lat, lng))
            start = max(0.0, d - (d - prev_d) / 2)
            runs.append(Run(cell, share, t0_s + start * speed, [(lat, lng)]))
    return runs


def stop_run(lat: float, lng: float, t_offset_s: float) -> Run:
    """A transit stop, as STOP_M of standing in its cell. No line of its own."""
    return Run(h3.latlng_to_cell(lat, lng, SCORE_RES), STOP_M, t_offset_s, [])


def route_runs(route: Route) -> list[Run]:
    """The runs a route is scored on.

    Street modes: the whole line. Transit: the walking legs and each boarding
    and alighting stop -- time on board is not scored, and the response says
    so. Leg times are offsets from the itinerary's own start.
    """
    step = SAMPLE_M.get(route.mode, 20.0)
    if route.mode != "transit":
        return sample_runs(route.geometry, step, route.duration_s)

    runs: list[Run] = []
    start = _epoch(route.start_time)
    t = 0.0
    for leg in route.legs:
        leg_start = _epoch(leg.start_time)
        if leg_start is not None and start is not None:
            t = max(0.0, leg_start - start)
        if leg.transit:
            runs.append(stop_run(leg.origin.point.lat, leg.origin.point.lng, t))
            runs.append(
                stop_run(leg.destination.point.lat, leg.destination.point.lng, t + leg.duration_s)
            )
        else:
            runs.extend(sample_runs(leg.geometry, step, leg.duration_s, t))
        t += leg.duration_s
    return runs


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def route_cells(runs: Iterable[Run]) -> set[str]:
    return {r.cell for r in runs}


def overlap(a: list[Run], b: list[Run]) -> float:
    """Share of the shorter route's length in cells the other also crosses."""
    cells_a, cells_b = route_cells(a), route_cells(b)
    len_a = sum(r.length_m for r in a) or 1.0
    len_b = sum(r.length_m for r in b) or 1.0
    in_b = sum(r.length_m for r in a if r.cell in cells_b) / len_a
    in_a = sum(r.length_m for r in b if r.cell in cells_a) / len_b
    return max(in_a, in_b)


# ---------------------------------------------------------------------------
# The comparison the map makes
# ---------------------------------------------------------------------------


@dataclass
class Baseline:
    """City averages for one track: all hours, and per hour block where built.

    The port of valueDomain (web/app.js) applied to the stored layer: the
    exposure-weighted mean of the smoothed rate over cells with something of
    the track reported (tier != 0), or the plain mean if no cell carries a
    population figure.
    """

    overall: float | None
    hourly: dict[int, float] = field(default_factory=dict)


def domain_average(rows: Iterable[Mapping[str, Any]], value_key: str, tier_key: str) -> float | None:
    """valueDomain: `rows` carry the value, the tier and `exposure`."""
    total = 0.0
    painted = 0
    weighted = 0.0
    people = 0.0
    for p in rows:
        if (p.get(tier_key) or 0) == 0:
            continue
        value = p.get(value_key)
        if not isinstance(value, (int, float)):
            continue
        total += value
        painted += 1
        exposure = p.get("exposure")
        if isinstance(exposure, (int, float)) and exposure > 0:
            weighted += value * exposure
            people += exposure
    return average_from_sums(weighted, people, total, painted)


def average_from_sums(weighted: float, people: float, total: float, painted: int) -> float | None:
    """valueDomain's last two lines, for sums accumulated elsewhere (in SQL,
    repo.route_baseline): per person where any cell has people, else a plain
    mean; None for no painted cell or a non-positive average."""
    if not painted:
        return None
    average = weighted / people if people > 0 else total / painted
    return average if average > 0 else None


def cell_ratio(
    cell: Mapping[str, Any] | None, baseline: Baseline, hour: int | None
) -> float | None:
    """A cell's multiple of its city average, or None where the map paints it
    in the neutral fill. The port of cellRatio (web/app.js), for one track.

    `cell` carries `sm` and `tier` (all hours) and, when `hour` is not None,
    `hsm` / `htier` for that hour block. As on the map: a cell with something
    of the track reported in that hour is compared at that hour; any other
    cell with an all-hours rate borrows it, against the all-hours average.
    Shared cases: tests/fixtures/routing/ratio_cases.json, run against both.
    """
    if cell is None:
        return None
    own = baseline.hourly.get(hour) if hour is not None else baseline.overall
    if hour is not None:
        painted = (cell.get("htier") or 0) != 0
        value = cell.get("hsm")
    else:
        painted = (cell.get("tier") or 0) != 0
        value = cell.get("sm")
    if painted:
        return (value or 0) / own if own else None
    if baseline.overall and isinstance(cell.get("sm"), (int, float)):
        return cell["sm"] / baseline.overall
    return None


# ---------------------------------------------------------------------------
# One route, one track
# ---------------------------------------------------------------------------


def _peak(pieces: list[tuple[float, float | None]]) -> float | None:
    """Highest length-weighted mean over any PEAK_WINDOW_M of the route, among
    windows that are at least half rated. `pieces` is (length, ratio) in order."""
    best: float | None = None
    lo = 0
    span = 0.0
    rated_len = 0.0
    rated_sum = 0.0
    for hi, (length, ratio) in enumerate(pieces):
        span += length
        if ratio is not None:
            rated_len += length
            rated_sum += length * ratio
        while lo < hi and span - pieces[lo][0] >= PEAK_WINDOW_M:
            old_len, old_ratio = pieces[lo]
            span -= old_len
            if old_ratio is not None:
                rated_len -= old_len
                rated_sum -= old_len * old_ratio
            lo += 1
        window_full = span >= PEAK_WINDOW_M or (lo == 0 and hi == len(pieces) - 1)
        if window_full and rated_len >= span / 2 and rated_len > 0:
            mean = rated_sum / rated_len
            best = mean if best is None else max(best, mean)
    return best


def _bucket(ratio: float | None) -> float | None:
    # Two decimal places: segments that would draw in the same colour merge.
    return None if ratio is None else round(ratio, 2)


def track_activity(
    runs: list[Run],
    ratio_of: Mapping[int, float | None],
) -> dict[str, Any]:
    """Metrics for one track. `ratio_of[i]` is run i's ratio (None: unrated).

    `segments` colour the route on the map: each is a run of the line in one
    ratio, as GeoJSON-ordered [lng, lat] coordinates. Stops (no line) are
    scored but not drawn.
    """
    total = sum(r.length_m for r in runs)
    rated = [(r.length_m, ratio_of[i]) for i, r in enumerate(runs) if ratio_of[i] is not None]
    rated_len = sum(length for length, _ in rated)
    rated_share = rated_len / total if total > 0 else 0.0
    comparable = rated_share >= MIN_RATED_SHARE and rated_len > 0

    mean = sum(length * ratio for length, ratio in rated) / rated_len if rated_len else None
    above = sum(length for length, ratio in rated if ratio >= 2.0) / rated_len if rated_len else None
    # Runs are a cell long, ~350 m: slid whole, a 300 m window would jump
    # from one run to the next. Cut into ~20 m pieces first.
    pieces: list[tuple[float, float | None]] = []
    for i, r in enumerate(runs):
        n = max(1, round(r.length_m / PEAK_PIECE_M))
        pieces.extend([(r.length_m / n, ratio_of[i])] * n)
    peak = _peak(pieces)

    segments: list[dict[str, Any]] = []
    for i, run in enumerate(runs):
        if len(run.points) < 2:
            continue
        ratio = _bucket(ratio_of[i])
        coords = [[round(lng, 6), round(lat, 6)] for lat, lng in run.points]
        last = segments[-1] if segments else None
        if last is not None and last["ratio"] == ratio and last["coordinates"][-1] == coords[0]:
            last["coordinates"].extend(coords[1:])
        else:
            segments.append({"ratio": ratio, "coordinates": coords})

    return {
        "comparable": comparable,
        "mean_ratio": round(mean, 3) if comparable and mean is not None else None,
        "peak_ratio": round(peak, 3) if comparable and peak is not None else None,
        "share_above_2x": round(above, 3) if comparable and above is not None else None,
        "rated_share": round(rated_share, 3),
        "sense": sense(mean) if comparable and mean is not None else None,
        "segments": segments,
    }


def sense(ratio: float) -> str:
    """relativeStat's words: only ever relative to the city."""
    if ratio > 1 + DEADBAND:
        return "higher"
    if ratio < 1 - DEADBAND:
        return "lower"
    return "about"


# ---------------------------------------------------------------------------
# The alternative
# ---------------------------------------------------------------------------


def extra_allowance_s(mode: str, fastest_s: float) -> float:
    return min(max(EXTRA_SHARE * fastest_s, EXTRA_MIN_S), EXTRA_CAP_S.get(mode, EXTRA_MIN_S))


def pick(mode: str, routes: Sequence[Route], activity: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The fastest route and, when one clearly qualifies, the lower one.

    `routes` are fastest first; `activity[i]` is route i's track_activity.
    "Lower" is the qualifying route with the lowest mean ratio, and only if it
    is both MIN_RELATIVE_DROP and MIN_ABSOLUTE_DROP below the fastest: a
    difference smaller than that is inside what a re-smoothing could move, and
    offering a detour on it would be selling noise. Otherwise `why` says which
    test failed, so the page can say so plainly.
    """
    fastest = routes[0].duration_s
    allowance = extra_allowance_s(mode, fastest)
    base = activity[0]
    result: dict[str, Any] = {
        "fastest": 0,
        "lower": None,
        "extra_allowed_s": round(allowance),
        "why": None,
    }
    if len(routes) < 2:
        result["why"] = "single_route"
        return result
    if not base["comparable"]:
        result["why"] = "not_rated"
        return result

    within = [
        i
        for i in range(1, len(routes))
        if routes[i].duration_s - fastest <= allowance and activity[i]["comparable"]
    ]
    if not within:
        result["why"] = "too_slow"
        return result
    best = min(within, key=lambda i: activity[i]["mean_ratio"])
    lowest = activity[best]["mean_ratio"]
    fastest_mean = base["mean_ratio"]
    if lowest > fastest_mean * (1 - MIN_RELATIVE_DROP) or fastest_mean - lowest < MIN_ABSOLUTE_DROP:
        # Is there a clearly lower route that only fails on time?
        slower = [
            i
            for i in range(1, len(routes))
            if activity[i]["comparable"]
            and activity[i]["mean_ratio"] <= fastest_mean * (1 - MIN_RELATIVE_DROP)
            and fastest_mean - activity[i]["mean_ratio"] >= MIN_ABSOLUTE_DROP
        ]
        result["why"] = "too_slow" if slower else "not_lower"
        return result
    result["lower"] = best
    result["extra_s"] = round(routes[best].duration_s - fastest)
    return result


def distinct_routes(routes: list[Route], runs: list[list[Run]]) -> list[int]:
    """Indexes of the routes to keep: drop any that is the same route as one
    already kept (SAME_ROUTE_OVERLAP). Input fastest first; output likewise."""
    kept: list[int] = []
    for i in range(len(routes)):
        if all(overlap(runs[i], runs[j]) < SAME_ROUTE_OVERLAP for j in kept):
            kept.append(i)
    return kept


def worst_run(runs: list[Run], ratio_of: Mapping[int, float | None]) -> int | None:
    """The run with the highest ratio, by length-weighted contribution."""
    best, idx = -math.inf, None
    for i, r in enumerate(runs):
        ratio = ratio_of[i]
        if ratio is not None and ratio * min(r.length_m, PEAK_WINDOW_M) > best:
            best, idx = ratio * min(r.length_m, PEAK_WINDOW_M), i
    return idx
