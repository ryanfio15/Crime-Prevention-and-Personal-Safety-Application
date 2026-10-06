"""Washington DC adapter -- Esri ArcGIS MapServer, MPD crime incidents (S4, S8.1).

The third API paradigm, after Carto SQL and Socrata. Source quirks isolated here:

* **One layer per year.** `FEEDS/MPD/MapServer` publishes "Crime Incidents -
  <year>" as separate layers whose numeric ids are not in year order (2026 is
  41, 2025 is 7). The ids are discovered from the service's layer list on every
  pull rather than hard-coded, so next January's new layer is picked up without
  a code change. The "Last 30 Days" layer duplicates the current-year one and is
  not read.
* **Paging** is `resultOffset` / `resultRecordCount` against the service's
  `maxRecordCount` (1,000), continued while `exceededTransferLimit` is set.
  Ordered by OBJECTID so pages are stable.
* **Dates are epoch milliseconds in true UTC** -- verified against the `SHIFT`
  column, which agrees with the New York clock 89% of the time and with the raw
  UTC clock 59%. They are converted to America/New_York wall clock before being
  stored, which is what `occurred_at` means everywhere else in the pipeline
  (009_time_of_day.sql). This is the opposite of the Socrata sources, whose
  floating timestamps are already local and must *not* be converted.
* **Occurrence basis varies per record.** `START_DATE` is when the offence began
  and is used when present; otherwise `REPORT_DAT`, and `occurred_basis` says
  which. This is the case the column was designed for.
* **Part I offences only.** The feed carries nine offence types -- homicide, sex
  abuse, assault with a dangerous weapon, robbery, burglary, arson, motor
  vehicle theft, theft from auto, other theft. There is no simple assault,
  vandalism or drug offence, so DC's violent track is aggravated-only and its
  totals are not comparable to another city's (they never are; S3.3).
* The offence is published as text with no code, so the text is used as the
  code and the crosswalk (washington_dc_v1.csv) is hand-written.
* `CCN` (Central Complaint Number) is the incident key.
* WGS84 `LATITUDE` / `LONGITUDE` are published; `XBLOCK` / `YBLOCK` (Maryland
  State Plane, EPSG:26985, metres) are the fallback, and (0, 0) is missing.

The filter is on `REPORT_DAT`, the field the annual layers are partitioned by,
widened by a day each side so a report filed near midnight on 31 December is
not lost between two layers' UTC bounds. Records are upserted on their key, so
the overlap costs nothing.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx
import pyproj

from safety.config import settings
from safety.etl.adapters.base import NormalizedIncident, RawChunk, SourceAdapter

log = logging.getLogger(__name__)

_LOCAL_TZ = ZoneInfo("America/New_York")
_LAYER_NAME = re.compile(r"^Crime Incidents - (\d{4})$")
_PAGE_SIZE = 1000

_STATE_PLANE_TO_WGS84 = pyproj.Transformer.from_crs(
    "EPSG:26985", "EPSG:4326", always_xy=True
)

_FIELDS = (
    "CCN",
    "REPORT_DAT",
    "START_DATE",
    "OFFENSE",
    "METHOD",
    "BLOCK",
    "DISTRICT",
    "LATITUDE",
    "LONGITUDE",
    "XBLOCK",
    "YBLOCK",
)


class WashingtonDcEsriAdapter(SourceAdapter):
    api_type: ClassVar[str] = "esri_featureserver"

    # ------------------------------------------------------------------ fetch

    def _get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, settings.http_max_retries + 1):
            try:
                response = httpx.get(
                    url,
                    params=params,
                    timeout=settings.http_timeout_seconds,
                    follow_redirects=True,
                )
                response.raise_for_status()
                body = response.json()
                # Esri reports failures as HTTP 200 with an `error` object.
                if "error" in body:
                    raise RuntimeError(f"esri error: {body['error']}")
                return body
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                last_exc = exc
                backoff = 2.0**attempt
                log.warning(
                    "esri request failed (attempt %s/%s), retrying in %.0fs: %s",
                    attempt,
                    settings.http_max_retries,
                    backoff,
                    exc,
                )
                time.sleep(backoff)
        raise RuntimeError(f"Esri request failed after retries: {last_exc}")

    def _year_layers(self) -> dict[int, int]:
        body = self._get(self.config.base_url, {"f": "json"})
        layers: dict[int, int] = {}
        for layer in body.get("layers", []):
            match = _LAYER_NAME.match(layer.get("name", "").strip())
            if match:
                layers[int(match.group(1))] = int(layer["id"])
        if not layers:
            raise RuntimeError(
                f"dc: no 'Crime Incidents - <year>' layers at {self.config.base_url}; "
                "the service layout has changed"
            )
        return layers

    def fetch_incidents(self, since: datetime, until: datetime) -> Iterator[RawChunk]:
        lo = since - timedelta(days=1)
        hi = until + timedelta(days=1)
        layers = self._year_layers()
        years = [y for y in sorted(layers) if lo.year <= y <= hi.year]
        missing = [y for y in range(max(lo.year, min(layers)), hi.year + 1) if y not in layers]
        if missing:
            log.warning("dc: no layer published for year(s) %s", missing)

        where = (
            f"REPORT_DAT >= TIMESTAMP '{lo.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}' "
            f"AND REPORT_DAT < TIMESTAMP '{hi.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}'"
        )
        for year in years:
            url = f"{self.config.base_url}/{layers[year]}/query"
            offset = 0
            page = 0
            while True:
                params = {
                    "where": where,
                    "outFields": ",".join(_FIELDS),
                    "returnGeometry": "false",
                    "orderByFields": "OBJECTID",
                    "resultOffset": str(offset),
                    "resultRecordCount": str(_PAGE_SIZE),
                    "f": "json",
                }
                body = self._get(url, params)
                features = body.get("features", [])
                payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
                yield RawChunk(
                    name=f"{year}-p{page:03d}.json",
                    content_type="application/json",
                    payload=payload,
                    request_url=f"{url}?resultOffset={offset}",
                    fetched_at=datetime.now(timezone.utc),
                    record_count=len(features),
                    meta={"layer": layers[year], "year": year, "offset": offset},
                )
                offset += len(features)
                page += 1
                if not features or not body.get("exceededTransferLimit"):
                    break
            log.info("dc: fetched %s rows from the %s layer", offset, year)

    def fetch_boundary(self) -> RawChunk | None:
        """None: the coverage polygon comes from TIGER/Line PLACE (boundary.py)."""
        return None

    # ------------------------------------------------------------------ parse

    def parse_incidents(self, chunk: RawChunk) -> Iterator[dict[str, Any]]:
        body = json.loads(chunk.payload.decode("utf-8"))
        for feature in body.get("features", []):
            yield feature.get("attributes", {})

    # -------------------------------------------------------------- normalize

    @staticmethod
    def _clean(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _local_wall_clock(epoch_ms: Any) -> datetime | None:
        """Epoch ms (true UTC) -> New York wall clock, tagged UTC like the rest
        of the pipeline's `occurred_at` values."""
        if epoch_ms in (None, ""):
            return None
        try:
            instant = datetime.fromtimestamp(float(epoch_ms) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return instant.astimezone(_LOCAL_TZ).replace(tzinfo=timezone.utc)

    @staticmethod
    def _resolve_coordinates(
        latitude: Any, longitude: Any, x_coord: Any, y_coord: Any
    ) -> tuple[float | None, float | None, str]:
        try:
            lat = float(latitude) if latitude is not None else None
            lng = float(longitude) if longitude is not None else None
        except (TypeError, ValueError):
            lat = lng = None
        if (
            lat is not None
            and lng is not None
            and not (lat == 0 and lng == 0)
            and abs(lat) <= 90.0
            and abs(lng) <= 180.0
        ):
            return lat, lng, "published_wgs84"
        try:
            x, y = float(x_coord), float(y_coord)
        except (TypeError, ValueError):
            return None, None, "missing"
        if x == 0 or y == 0:
            return None, None, "missing"
        lng2, lat2 = _STATE_PLANE_TO_WGS84.transform(x, y)
        if abs(lat2) > 90.0 or abs(lng2) > 180.0:
            return None, None, "unprojectable"
        return lat2, lng2, "reprojected_epsg26985"

    def normalize(self, record: dict[str, Any]) -> NormalizedIncident | None:
        source_incident_id = self._clean(record.get("CCN"))
        if source_incident_id is None:
            return None

        started = self._local_wall_clock(record.get("START_DATE"))
        reported = self._local_wall_clock(record.get("REPORT_DAT"))
        occurred_at = started or reported
        basis = "occurrence" if started is not None else "report"
        if occurred_at is None:
            occurred_at = datetime.min.replace(tzinfo=timezone.utc)
            local_date: date = occurred_at.date()
            local_hour: int | None = None
            precision = "date"
        else:
            local_date = occurred_at.date()
            local_hour = occurred_at.hour
            precision = "exact"

        latitude, longitude, coordinate_source = self._resolve_coordinates(
            record.get("LATITUDE"),
            record.get("LONGITUDE"),
            record.get("XBLOCK"),
            record.get("YBLOCK"),
        )
        offense = self._clean(record.get("OFFENSE"))

        return NormalizedIncident(
            source_incident_id=source_incident_id,
            occurred_at=occurred_at,
            occurred_local_date=local_date,
            occurred_precision=precision,
            occurred_basis=basis,
            occurred_local_hour=local_hour,
            latitude=latitude,
            longitude=longitude,
            coordinate_source=coordinate_source,
            raw_offense_code=offense.upper() if offense else None,
            raw_offense_text=offense,
            # GUN / KNIFE / OTHERS: the weapon, carried for provenance.
            raw_source_category=self._clean(record.get("METHOD")),
            reported_at=reported,
            location_block=self._clean(record.get("BLOCK")),
            district=self._clean(record.get("DISTRICT")),
        )
