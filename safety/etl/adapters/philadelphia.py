"""Philadelphia adapter -- Carto SQL API (design doc S4, S8.1).

Source quirks isolated here, and nowhere else in the pipeline:

* Carto's SQL endpoint takes a literal SQL string; there is no cursor or
  `$offset` convention, so this adapter paginates by calendar month. Each month
  is ~11k rows / ~1.5 MB, which the endpoint serves reliably.
* `dc_key` is published as a numeric, so CSV renders it as
  "202601000996.00000000". The integer part is the real incident number.
* `point_x` / `point_y` are longitude / latitude respectively -- not a typo.
* A small share of records (~0.1%) carry `point_x` / `point_y` in NAD83 /
  Pennsylvania South State Plane feet (EPSG:2272) instead of WGS84 -- the
  City's internal projection, evidently never converted on export. Those rows
  are at real Philadelphia addresses, so they are reprojected here rather than
  discarded, and tagged so the conversion is visible downstream.
* The dataset publishes a police *dispatch* timestamp, not an observed
  occurrence time. That distinction is carried through to
  silver.incident.occurred_basis rather than being quietly flattened.
* Part I / Part II is not published as a column; it is implied by the
  `ucr_general` code range (100-900 = Part I, 1000+ = Part II).
* There is no `city_limits` table on phl.carto, so the coverage polygon is the
  union of `police_districts` -- which is the police-jurisdiction boundary S3.1
  explicitly allows.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar

import httpx
import pyproj

from safety.config import settings
from safety.etl.adapters.base import NormalizedIncident, RawChunk, SourceAdapter

log = logging.getLogger(__name__)

# NAD83 / Pennsylvania South (US survey feet) -- the City of Philadelphia's
# working projection. Building the transformer is expensive, so it is created
# once and reused across the whole pull.
_STATE_PLANE_EPSG = "EPSG:2272"
_STATE_PLANE_TO_WGS84 = pyproj.Transformer.from_crs(
    _STATE_PLANE_EPSG, "EPSG:4326", always_xy=True
)

_INCIDENT_COLUMNS = (
    "cartodb_id",
    "objectid",
    "dc_key",
    "dc_dist",
    "psa",
    "dispatch_date",
    "dispatch_date_time",
    "dispatch_time",
    "hour",
    "location_block",
    "ucr_general",
    "text_general_code",
    "point_x",
    "point_y",
)


class PhiladelphiaCartoAdapter(SourceAdapter):
    api_type: ClassVar[str] = "carto_sql"

    # ------------------------------------------------------------------ fetch

    def _get(self, params: dict[str, str]) -> httpx.Response:
        """GET with bounded exponential backoff on transient upstream failures."""
        last_exc: Exception | None = None
        for attempt in range(1, settings.http_max_retries + 1):
            try:
                response = httpx.get(
                    self.config.base_url,
                    params=params,
                    timeout=settings.http_timeout_seconds,
                    follow_redirects=True,
                )
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"upstream {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_exc = exc
                backoff = 2.0**attempt
                log.warning(
                    "carto request failed (attempt %s/%s), retrying in %.0fs: %s",
                    attempt,
                    settings.http_max_retries,
                    backoff,
                    exc,
                )
                time.sleep(backoff)
        raise RuntimeError(f"Carto request failed after retries: {last_exc}")

    @staticmethod
    def _month_starts(since: datetime, until: datetime) -> Iterator[tuple[datetime, datetime]]:
        cursor = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while cursor < until:
            if cursor.month == 12:
                nxt = cursor.replace(year=cursor.year + 1, month=1)
            else:
                nxt = cursor.replace(month=cursor.month + 1)
            yield cursor, min(nxt, until)
            cursor = nxt

    def fetch_incidents(self, since: datetime, until: datetime) -> Iterator[RawChunk]:
        for chunk_start, chunk_end in self._month_starts(since, until):
            query = (
                f"SELECT {', '.join(_INCIDENT_COLUMNS)} "
                f"FROM {self.config.incident_dataset} "
                f"WHERE dispatch_date_time >= '{chunk_start.isoformat()}' "
                f"  AND dispatch_date_time <  '{chunk_end.isoformat()}' "
                f"ORDER BY cartodb_id"
            )
            params = {"q": query, "format": "csv"}
            response = self._get(params)
            payload = response.content
            # Row count = lines minus header; cheap and only used for logging
            # and the S8.5 volume-anomaly check.
            record_count = max(payload.count(b"\n") - 1, 0)
            log.info(
                "phl: fetched %s rows for %s",
                record_count,
                chunk_start.strftime("%Y-%m"),
            )
            yield RawChunk(
                name=f"{chunk_start.strftime('%Y-%m')}.csv",
                content_type="text/csv",
                payload=payload,
                request_url=str(response.url),
                fetched_at=datetime.now(timezone.utc),
                record_count=record_count,
                meta={
                    "window_start": chunk_start.isoformat(),
                    "window_end": chunk_end.isoformat(),
                },
            )

    def fetch_boundary(self) -> RawChunk | None:
        if not self.config.boundary_dataset:
            return None
        query = (
            f"SELECT ST_AsGeoJSON(ST_Union(the_geom)) AS geometry "
            f"FROM {self.config.boundary_dataset}"
        )
        response = self._get({"q": query})
        geometry = json.loads(response.json()["rows"][0]["geometry"])
        feature_collection = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        "source_id": self.source_id,
                        "boundary_kind": "police_jurisdiction",
                        "derived_from": f"ST_Union({self.config.boundary_dataset})",
                    },
                }
            ],
        }
        return RawChunk(
            name="boundary.geojson",
            content_type="application/geo+json",
            payload=json.dumps(feature_collection).encode("utf-8"),
            request_url=str(response.url),
            fetched_at=datetime.now(timezone.utc),
            record_count=1,
        )

    # ------------------------------------------------------------------ parse

    def parse_incidents(self, chunk: RawChunk) -> Iterator[dict[str, Any]]:
        text = chunk.payload.decode("utf-8-sig")
        yield from csv.DictReader(io.StringIO(text))

    # -------------------------------------------------------------- normalize

    @staticmethod
    def _clean(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _to_float(value: Any) -> float | None:
        text = PhiladelphiaCartoAdapter._clean(value)
        if text is None:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _parse_timestamp(value: str) -> datetime | None:
        """Carto renders timestamptz as '2026-01-12 20:20:00+00'."""
        text = PhiladelphiaCartoAdapter._clean(value)
        if text is None:
            return None
        candidate = text.replace(" ", "T")
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        # '+00' -> '+00:00' so fromisoformat accepts it on every 3.x
        if len(candidate) >= 3 and candidate[-3] in "+-":
            candidate = candidate + ":00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _resolve_coordinates(
        point_x: float | None, point_y: float | None
    ) -> tuple[float | None, float | None, str]:
        """Return (latitude, longitude, provenance).

        Anything outside WGS84's valid range cannot be a degree pair, so it is
        treated as State Plane feet and reprojected. Values that survive
        neither test are handed back as-is for the validator to reject.
        """
        if point_x is None or point_y is None:
            return None, None, "missing"

        if abs(point_x) <= 180.0 and abs(point_y) <= 90.0:
            return point_y, point_x, "published_wgs84"

        try:
            lng, lat = _STATE_PLANE_TO_WGS84.transform(point_x, point_y)
        except Exception:
            return None, None, "unprojectable"

        if abs(lat) > 90.0 or abs(lng) > 180.0:
            return None, None, "unprojectable"
        return lat, lng, "reprojected_epsg2272"

    @staticmethod
    def _ucr_part(ucr_general: str | None) -> str | None:
        """Part I / Part II is implied by the code range, not published."""
        if not ucr_general:
            return None
        try:
            code = int(float(ucr_general))
        except ValueError:
            return None
        return "Part I" if code < 1000 else "Part II"

    def normalize(self, record: dict[str, Any]) -> NormalizedIncident | None:
        raw_key = self._clean(record.get("dc_key"))
        if raw_key is None:
            # No usable incident identifier at all: structurally unusable.
            return None
        # "202601000996.00000000" -> "202601000996"
        source_incident_id = raw_key.split(".")[0]

        occurred_at = self._parse_timestamp(record.get("dispatch_date_time", ""))
        local_date_text = self._clean(record.get("dispatch_date"))
        local_date: date | None = None
        if local_date_text:
            try:
                local_date = date.fromisoformat(local_date_text[:10])
            except ValueError:
                local_date = None

        if occurred_at is None and local_date is not None:
            occurred_at = datetime.combine(local_date, datetime.min.time(), tzinfo=timezone.utc)
        if occurred_at is None:
            # Let the validator record this as missing_timestamp against a real
            # incident id rather than dropping it silently here.
            occurred_at = datetime.min.replace(tzinfo=timezone.utc)
        if local_date is None:
            local_date = occurred_at.date()

        has_clock_time = self._clean(record.get("dispatch_time")) is not None

        latitude, longitude, coordinate_source = self._resolve_coordinates(
            self._to_float(record.get("point_x")),
            self._to_float(record.get("point_y")),
        )

        return NormalizedIncident(
            source_incident_id=source_incident_id,
            occurred_at=occurred_at,
            occurred_local_date=local_date,
            occurred_precision="exact" if has_clock_time else "date",
            occurred_basis="dispatch",
            latitude=latitude,
            longitude=longitude,
            coordinate_source=coordinate_source,
            raw_offense_code=self._normalize_code(record.get("ucr_general")),
            raw_offense_text=self._clean(record.get("text_general_code")),
            raw_source_category=self._ucr_part(self._clean(record.get("ucr_general"))),
            reported_at=None,  # Philadelphia publishes no separate report time.
            location_type="unknown",  # No location-type field in this dataset (S7.5).
            location_block=self._clean(record.get("location_block")),
            district=self._clean(record.get("dc_dist")),
        )

    @staticmethod
    def _normalize_code(value: Any) -> str | None:
        """ucr_general is a varchar but arrives as '300' or occasionally '300.0'."""
        text = PhiladelphiaCartoAdapter._clean(value)
        if text is None:
            return None
        try:
            return str(int(float(text)))
        except ValueError:
            return text


def default_backfill_window(months: int) -> tuple[datetime, datetime]:
    """Trailing-window bounds, padded a day each side.

    Chunk boundaries are UTC while Philadelphia reports local dates, so the
    fetch range is widened slightly. Loading a few extra days is harmless:
    gold time windows are computed from occurred_local_date, not from what the
    fetch happened to cover.
    """
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=1)
    start_year = now.year - (months // 12)
    start_month = now.month - (months % 12)
    if start_month <= 0:
        start_month += 12
        start_year -= 1
    since = datetime(start_year, start_month, 1, tzinfo=timezone.utc) - timedelta(days=1)
    return since, until
