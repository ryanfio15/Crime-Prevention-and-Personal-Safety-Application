"""The common adapter interface every source implements (design doc S8.1).

One internal contract -- "fetch new/updated records since timestamp X for city
Y" -- implemented separately per source, so that Socrata's, Carto's, and Esri's
differing pagination/filtering conventions never leak into shared code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ClassVar

import psycopg


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """One row of reference.source_registry (S8.4)."""

    source_id: str
    city_name: str
    state_code: str
    agency_name: str
    api_type: str
    base_url: str
    incident_dataset: str
    boundary_dataset: str | None
    expected_cadence: str
    publication_lag_days: int
    revision_lookback_days: int
    crosswalk_version: str
    backfill_start_date: date | None
    last_success_watermark: datetime | None
    timezone: str
    attribution_text: str
    terms_url: str | None
    freshness_note: str | None
    location_precision_note: str | None
    enabled: bool

    @classmethod
    def load(cls, conn: psycopg.Connection, source_id: str) -> SourceConfig:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM reference.source_registry WHERE source_id = %s", (source_id,)
            )
            row = cur.fetchone()
        if row is None:
            raise LookupError(f"source '{source_id}' is not in reference.source_registry")
        return cls(**{f: row[f] for f in cls.__slots__})


@dataclass(slots=True)
class RawChunk:
    """One unit of payload exactly as received, destined for bronze verbatim (S5)."""

    name: str
    content_type: str
    payload: bytes
    request_url: str
    fetched_at: datetime
    record_count: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedIncident:
    """A source record mapped onto the city-agnostic silver field set (S6).

    Coordinates are deliberately optional: an adapter reports what the source
    published, and the shared validator (S8.5) decides whether a record with
    missing or zeroed coordinates is promoted.
    """

    source_incident_id: str
    occurred_at: datetime
    occurred_local_date: date
    occurred_precision: str
    occurred_basis: str
    latitude: float | None
    longitude: float | None
    raw_offense_code: str | None
    raw_offense_text: str | None
    raw_source_category: str | None
    # Provenance for the coordinate pair: 'published_wgs84' when the source
    # gave usable lat/lng directly, otherwise a label naming what the adapter
    # converted from. Never silently rewrite coordinates without saying so.
    coordinate_source: str = "published_wgs84"
    reported_at: datetime | None = None
    location_type: str = "unknown"
    location_block: str | None = None
    district: str | None = None


class SourceAdapter(ABC):
    """Base class for per-source connectors."""

    api_type: ClassVar[str]

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    @property
    def source_id(self) -> str:
        return self.config.source_id

    @abstractmethod
    def fetch_incidents(self, since: datetime, until: datetime) -> Iterator[RawChunk]:
        """Yield raw payload chunks covering [since, until).

        Implementations choose their own chunking (date ranges, cursor pages,
        per-year files) -- whatever the upstream API is actually reliable at.
        """

    @abstractmethod
    def fetch_boundary(self) -> RawChunk | None:
        """Return the city's coverage polygon as GeoJSON (S3.1), or None."""

    @abstractmethod
    def parse_incidents(self, chunk: RawChunk) -> Iterator[dict[str, Any]]:
        """Decode a raw chunk into per-record dicts, still in source field names."""

    @abstractmethod
    def normalize(self, record: dict[str, Any]) -> NormalizedIncident | None:
        """Map one source record onto NormalizedIncident.

        Return None only for structurally unusable rows (e.g. no ID at all).
        Data-quality judgements belong to the shared validator, not here.
        """

    def incident_key(self, source_incident_id: str) -> str:
        """S6: raw IDs collide across cities, so the silver key is namespaced."""
        return f"{self.source_id}:{source_incident_id}"
