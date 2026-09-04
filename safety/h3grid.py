"""H3 cell helpers (design doc S3.2, S10).

Cell membership is a pure function of (lat, lng, resolution), so nothing here
touches the database. The only reason cell *geometry* is materialized at all is
map rendering and viewport bbox filtering -- never cell lookup.
"""

from __future__ import annotations

from typing import Any

import h3

# S3.2: resolution 8 (~461 m edge, ~0.74 km^2) is the closest standard
# resolution to the 500 m product requirement; resolution 9 (~174 m edge) is
# the drill-down for dense urban cores. Both are stored on every incident.
PRIMARY_RES = 8
DETAIL_RES = 9
RESOLUTIONS = (PRIMARY_RES, DETAIL_RES)


def cell_for(lat: float, lng: float, res: int) -> str:
    return h3.latlng_to_cell(lat, lng, res)


def cells_for_point(lat: float, lng: float) -> dict[int, str]:
    """Both stored resolutions for one incident, computed once at ingest (S6)."""
    return {res: h3.latlng_to_cell(lat, lng, res) for res in RESOLUTIONS}


def cells_covering(geojson_geometry: dict[str, Any], res: int) -> list[str]:
    """Every H3 cell of `res` whose center falls inside the given polygon.

    This is the city cell universe used for the relative ranking in S3.3.
    Note the center-containment rule: a cell straddling the city boundary whose
    center lies outside is not returned here, so callers must union this with
    the cells incidents actually landed in (see gold.build_cell_universe).
    """
    shape = h3.geo_to_h3shape(geojson_geometry)
    return list(h3.h3shape_to_cells(shape, res))


def cell_polygon_geojson(cell: str) -> dict[str, Any]:
    """Closed GeoJSON ring for a cell, in (lng, lat) order."""
    boundary = h3.cell_to_boundary(cell)
    ring = [[lng, lat] for lat, lng in boundary]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def cell_centroid(cell: str) -> tuple[float, float]:
    """Return (lng, lat) for the cell center."""
    lat, lng = h3.cell_to_latlng(cell)
    return lng, lat


def cell_area_km2(cell: str) -> float:
    return h3.cell_area(cell, unit="km^2")


def cell_resolution(cell: str) -> int:
    return h3.get_resolution(cell)


def grid_disk(cell: str, k: int) -> list[str]:
    """Cells within k rings of `cell` -- the O(1) map-surround query in S10."""
    return list(h3.grid_disk(cell, k))


def is_valid_cell(cell: str) -> bool:
    try:
        return h3.is_valid_cell(cell)
    except (ValueError, TypeError):
        return False
