"""Via points for walking and cycling alternatives (plan S3).

Valhalla's `alternates` is generous for driving and stingy on foot: a short
walk often comes back with one route, or with "alternates" that share all but
a block of it. When that happens the endpoint asks for up to three routes that
are made to go somewhere else first:

  * two points offset to either side of the trip's midpoint, perpendicular to
    the straight line between its ends, and
  * the quietest rated cell near the worst stretch of the fastest route,

each routed as from -> via -> to. A detour longer than MAX_DETOUR_FACTOR times
the fastest route is thrown away before it is offered; scoring.pick() then
applies the time allowance on top.

Pure geometry; the endpoint makes the calls.
"""

from __future__ import annotations

import math

from safety.routing.schema import Point, haversine_m

# A detour may be at most this many times the fastest route's length.
MAX_DETOUR_FACTOR = 1.3

# The side offset, as a share of the straight-line trip, within bounds: far
# enough to land on a parallel street, close enough to stay inside the 1.3x.
OFFSET_SHARE = 0.25
OFFSET_MIN_M = 150.0
OFFSET_MAX_M = 1200.0

# Rings of res-9 cells searched around the worst stretch for a quieter one.
NEAR_RINGS = 2

# Valhalla calls per request: 1 with alternates, plus at most this many detours.
MAX_DETOURS = 3

# Metres per degree of latitude, on the 12742 km Earth diameter used throughout.
_M_PER_DEG = math.pi * 12_742_000 / 360


def side_offsets(a: Point, b: Point) -> list[Point]:
    """Two points beside the midpoint of a -> b, one to each side."""
    straight = haversine_m((a.lat, a.lng), (b.lat, b.lng))
    if straight <= 0:
        return []
    offset = min(max(OFFSET_SHARE * straight, OFFSET_MIN_M), OFFSET_MAX_M)
    mid_lat = (a.lat + b.lat) / 2
    mid_lng = (a.lng + b.lng) / 2
    cos_lat = math.cos(math.radians(mid_lat)) or 1e-9
    # The trip's direction in metres east / north, then turned a quarter.
    east = (b.lng - a.lng) * _M_PER_DEG * cos_lat
    north = (b.lat - a.lat) * _M_PER_DEG
    norm = math.hypot(east, north)
    perp_e, perp_n = -north / norm, east / norm
    out = []
    for sign in (1, -1):
        out.append(
            Point(
                mid_lat + sign * perp_n * offset / _M_PER_DEG,
                mid_lng + sign * perp_e * offset / (_M_PER_DEG * cos_lat),
            )
        )
    return out


def within_detour(fastest_m: float, candidate_m: float) -> bool:
    return candidate_m <= fastest_m * MAX_DETOUR_FACTOR
