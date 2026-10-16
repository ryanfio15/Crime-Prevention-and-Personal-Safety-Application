"""Coverage polygons from TIGER/Line PLACE (design doc S3.1, S8.4).

S3.1 scopes the product to incorporated city boundaries, or police-jurisdiction
boundaries where they differ slightly. Philadelphia's adapter builds the latter
out of `ST_Union(police_districts)` on the city's own Carto endpoint, which was
the right call for one city and does not generalize: the other five publish
boundaries across three different API paradigms, in three different formats,
none of which is the paradigm their *incident* data uses.

So the five new cities take their polygon from one source instead: the Census
Bureau's TIGER/Line incorporated-place shapefiles. That buys several things at
once.

* One code path, one archive format -- and one this package already reads, since
  `census.py` pulls TABBLOCK20 from the same host.
* The same vintage as the population denominator. A boundary and a block layer
  from different years disagree at the edges, and the retention check in
  `census.build_cell_exposure` would report that disagreement as a bug.
* A verifiable answer to "whose line is this", which a dissolved union of
  police beats is not.

What it costs: a city-limits polygon is not a police-jurisdiction polygon. Where
those differ, S3.1 permits either. The boundary row records which one it is, so
nothing downstream has to guess.

Philadelphia is deliberately left alone. It has a working police-jurisdiction
polygon and re-deriving it from a different source would move every cell
boundary in the city for no gain.
"""

from __future__ import annotations

import io
import logging
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
import shapefile

from safety.etl import census
from safety.etl.adapters.base import RawChunk, SourceConfig

log = logging.getLogger(__name__)

DATASET = "place_boundary"

# Same TIGER vintage as the population denominator, on purpose -- see the module
# docstring. Bumping one without the other reintroduces the edge disagreement.
VINTAGE = census.POP_VINTAGE

_PLACE_URL = (
    "https://www2.census.gov/geo/tiger/TIGER{vintage}/PLACE/"
    "tl_{vintage}_{state_fips}_place.zip"
)

ATTRIBUTION = (
    "City boundary: U.S. Census Bureau, TIGER/Line Shapefiles, "
    "incorporated places (PLACE)."
)

# The fields the match and the provenance note are built from. Named rather than
# assumed, for the same reason census.py names its own: the wrong vintage
# downloads happily and then fails somewhere less obvious.
_REQUIRED_DBF_FIELDS = ("PLACEFP", "NAME", "NAMELSAD", "LSAD")

# LSAD codes that denote an incorporated municipality, as opposed to a census
# designated place or a consolidated entity. A state file carries hundreds of
# places and several can share a NAME -- Washington is both a city in DC and a
# handful of townships elsewhere -- so the match is name plus incorporation.
#
#   25  city
#   43  consolidated city / balance
#   47  village
#   21  borough
#   57  CDP  (accepted only as a last resort; see _select_place)
_INCORPORATED_LSAD = {"25", "43", "47", "21"}
_CDP_LSAD = "57"


def fetch_place_file(config: SourceConfig) -> RawChunk:
    """Download the state's TIGER/Line incorporated-place shapefile."""
    if not config.state_fips:
        raise LookupError(
            f"source '{config.source_id}' has no state_fips in the registry, so "
            "there is no TIGER PLACE file to fetch; set it and re-run"
        )
    url = _PLACE_URL.format(vintage=VINTAGE, state_fips=config.state_fips)
    log.info("fetching place boundaries: %s", url)
    response = census._get(url)
    return RawChunk(
        name=f"tl_{VINTAGE}_{config.state_fips}_place.zip",
        content_type="application/zip",
        payload=response.content,
        request_url=str(response.url),
        fetched_at=datetime.now(timezone.utc),
        meta={"vintage": VINTAGE, "state_fips": config.state_fips},
    )


def _select_place(
    candidates: list[dict[str, Any]], config: SourceConfig
) -> dict[str, Any]:
    """Pick this city's place record, or fail naming what it found.

    Matching is by name, because that is the only join available -- but a name
    alone is not unique within a state, so incorporation status breaks the tie.
    An ambiguous match raises rather than guessing: a boundary is the denominator
    of every percentile in the city, and the wrong one would produce a complete,
    plausible, wrong map.
    """
    if config.place_fips:
        pinned = [c for c in candidates if c["placefp"] == config.place_fips]
        if not pinned:
            raise LookupError(
                f"registry pins '{config.source_id}' to place_fips "
                f"{config.place_fips}, which is not in the "
                f"{VINTAGE} TIGER file for state {config.state_fips}"
            )
        return pinned[0]

    wanted = config.city_name.strip().upper()
    named = [c for c in candidates if c["name"].strip().upper() == wanted]
    if not named:
        near = sorted(c["name"] for c in candidates if wanted[:5] in c["name"].upper())
        raise LookupError(
            f"no TIGER place named '{config.city_name}' in state "
            f"{config.state_fips}. Closest names: {near[:10] or 'none'}. Set "
            "place_fips on the registry row to pin it explicitly."
        )

    incorporated = [c for c in named if c["lsad"] in _INCORPORATED_LSAD]
    if len(incorporated) == 1:
        return incorporated[0]
    if len(incorporated) > 1:
        raise LookupError(
            f"'{config.city_name}' matches {len(incorporated)} incorporated "
            f"places in state {config.state_fips}: "
            f"{[(c['placefp'], c['namelsad']) for c in incorporated]}. Set "
            "place_fips on the registry row to pin one."
        )

    # No incorporated match. A CDP is a statistical boundary rather than a legal
    # one, so it is accepted only with the reason stated out loud.
    cdps = [c for c in named if c["lsad"] == _CDP_LSAD]
    if len(cdps) == 1:
        log.warning(
            "'%s' matches no incorporated place in state %s; falling back to the "
            "census designated place (%s). A CDP is a statistical boundary, not a "
            "municipal one -- confirm it is the intended coverage area.",
            config.city_name,
            config.state_fips,
            cdps[0]["namelsad"],
        )
        return cdps[0]

    raise LookupError(
        f"'{config.city_name}' matches {len(named)} place(s) in state "
        f"{config.state_fips}, none of them an incorporated place or a single "
        f"CDP: {[(c['placefp'], c['namelsad'], c['lsad']) for c in named]}"
    )


def load_boundary(
    conn: psycopg.Connection, payload: bytes, config: SourceConfig
) -> dict[str, Any]:
    """Extract this city's place polygon and upsert reference.city_boundary.

    Returns the chosen place record, so the caller can write `place_fips` back to
    the registry and report what was matched.
    """
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            archive.extractall(tmp)
        # Same reason census.load_blocks extracts rather than reading in place:
        # pyshp seeks in the .shx index and a zipfile stream is not dependably
        # seekable.
        stem = census._shapefile_stem(Path(tmp))

        reader = shapefile.Reader(str(stem))
        fields = [f[0] for f in reader.fields[1:]]
        missing = [f for f in _REQUIRED_DBF_FIELDS if f not in fields]
        if missing:
            raise ValueError(
                f"{stem.name} is missing {missing}; this does not look like a "
                f"TIGER{VINTAGE} PLACE file. Got fields: {fields}"
            )
        idx = {name: i for i, name in enumerate(fields)}

        candidates: list[dict[str, Any]] = []
        for shape_record in reader.iterShapeRecords():
            record = shape_record.record
            candidates.append(
                {
                    "placefp": record[idx["PLACEFP"]],
                    "name": record[idx["NAME"]],
                    "namelsad": record[idx["NAMELSAD"]],
                    "lsad": record[idx["LSAD"]],
                    "geometry": census._as_multipolygon_geojson(
                        shape_record.shape.__geo_interface__
                    ),
                }
            )
        reader.close()

        chosen = _select_place(candidates, config)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO reference.city_boundary
                (source_id, boundary_kind, geom, area_km2, source_note, fetched_at)
            VALUES (
                %s, 'city_limits',
                ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
                ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)::geography) / 1e6,
                %s, now()
            )
            ON CONFLICT (source_id) DO UPDATE SET
                boundary_kind = EXCLUDED.boundary_kind,
                geom          = EXCLUDED.geom,
                area_km2      = EXCLUDED.area_km2,
                source_note   = EXCLUDED.source_note,
                fetched_at    = EXCLUDED.fetched_at
            RETURNING area_km2
            """,
            (
                config.source_id,
                chosen["geometry"],
                chosen["geometry"],
                f"TIGER{VINTAGE} PLACE {config.state_fips}{chosen['placefp']} "
                f"({chosen['namelsad']})",
            ),
        )
        area_km2 = cur.fetchone()["area_km2"]

        # Write the resolved code back, so a later run pins the same polygon
        # rather than re-running the name match against a new TIGER vintage.
        cur.execute(
            """
            UPDATE reference.source_registry
               SET place_fips = %s, updated_at = now()
             WHERE source_id = %s
            """,
            (chosen["placefp"], config.source_id),
        )
    conn.commit()

    log.info(
        "stored coverage boundary for %s: %s (place %s%s), %.1f km2",
        config.source_id,
        chosen["namelsad"],
        config.state_fips,
        chosen["placefp"],
        area_km2,
    )
    return {**chosen, "area_km2": area_km2}
