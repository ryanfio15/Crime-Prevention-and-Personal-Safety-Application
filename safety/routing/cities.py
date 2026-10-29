"""Which served city a point is in: the server-side copy of web/app.js cityAt.

The same rule, line for line, so the page and the API never disagree about
which city a point belongs to: inside a city's bounding box, and where two
boxes overlap, the nearer city centre wins. A box is a rectangle, so it takes
in ground past the city line (Mercer Island sits inside Seattle's); where that
matters -- a geocode result -- the caller checks the point's cell against the
city's served cells as well (safety/api/directions.py).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from safety.routing.schema import haversine_m


def in_bbox(city: Mapping[str, Any], lat: float, lng: float) -> bool:
    west = city.get("bbox_west")
    if west is None or not math.isfinite(west):
        return False
    return (
        west <= lng <= city["bbox_east"] and city["bbox_south"] <= lat <= city["bbox_north"]
    )


def city_at(
    cities: Iterable[Mapping[str, Any]], lat: float, lng: float
) -> Mapping[str, Any] | None:
    """The served city (a list_cities() row) a point falls in, or None."""
    hits = [c for c in cities if in_bbox(c, lat, lng)]
    hits.sort(key=lambda c: haversine_m((lat, lng), (c["center_lat"], c["center_lng"])))
    return hits[0] if hits else None
