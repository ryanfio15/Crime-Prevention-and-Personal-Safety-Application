"""Austin adapter -- Esri ArcGIS services behind APD's public CrimeViewer (S4, S8.1).

Neither of APD's open-data datasets is usable: `fdj4-gpfu` and `i7fg-wrk5`
locate an incident no finer than its census block group. The CrimeViewer map
(maps.austintexas.gov/GIS/CrimeViewer/) is fed by two ArcGIS services on the
City's own server that do publish hundred-block points, and those are what this
adapter reads. Source quirks isolated here:

* **Two services, one layer each from the older.** The registry's `base_url` is
  the current FeatureServer (`CrimeViewer_new/APD_Reported_Crimes_new`), one
  point layer per offence family. Its "Aggravated Assault" layer is a copy of
  the robbery layer -- same rows, same RINs -- and no other layer carries an
  04xx code, so aggravated assault is read from the older MapServer
  (`APDCrimeViewer/APD_Reported_Crimes`, note: no `/services/` in that path),
  which still serves it. Without it Austin would have simple assault and no
  aggravated assault, under-ranking exactly the cells the violent track exists
  to find. Layers are discovered by name on every pull and the run fails if an
  expected one is missing, because both are undocumented app backends and the
  newer has already been renamed once.
* **The two services date the same field differently.** In the new service
  `OCCURRENCE_DATE` is a true UTC instant (verified against `OCCURRENCE_TIME`,
  which agrees with the Austin clock); in the old one it is the local date at
  midnight and the time of day lives only in `OCCURRENCE_TIME` (HHMM). Both
  end up as America/Chicago wall clock, which is what `occurred_at` means
  everywhere else in the pipeline (009_time_of_day.sql).
* **Layer is not category.** The "Theft" layer carries 0601 BURGLARY OF
  VEHICLE. Classification comes from the APD offence code and description via
  the crosswalk (austin_v1.csv), never from which layer a record came from;
  the layer name is kept as `raw_source_category` for provenance only.
* **APD codes are local, not NIBRS.** Four digits plus a description that
  carries the family-violence and similar extensions ("ASSAULT W/INJURY-FAM/DATE
  VIOL"). The crosswalk is keyed on code and description, with a code-only
  fallback (scripts/build_austin_crosswalk.py).
* **Calls that are not offences** -- family and dating disturbances, suspicious
  persons, alarm calls -- are published in the Part II layer. They are not
  promoted (see `normalize`), the same call Seattle makes for its code 999; the
  raw rows stay in bronze.
* **One row per incident**, the primary offence only (`SORT_ORDER` is always 1),
  where Seattle and Los Angeles publish one row per offence.
* **Sex offences are withheld** by the source: no 02xx layer and almost no
  11x codes. They are not on the map, as with Seattle's redacted records.
* **Residential burglary is published twice**, in the Burglary layer and again
  in Part II, with the same RIN and identical fields -- about five a day. The
  shared validator drops the second copy and logs it as
  `duplicate_incident_id`, so a steady trickle of those is expected, not a bug.
* `RIN` is the key in the new service. The old one publishes no key at all and
  its OBJECTIDs are reassigned on reload, so its records are keyed on a hash of
  what identifies them. Neither is APD's incident report number, so these
  records cannot be joined to the open-data datasets.
* Geometry is requested as WGS84 (`outSR=4326`); the native Texas State Plane
  Central (EPSG:2277, US feet) pair is reprojected if the server ignores that.

The filter is on `OCCURRENCE_DATE`, widened by a day each side so the old
service's midnight-local dates are not lost at the window's UTC edges. Records
are upserted on their key, so the overlap costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx
import pyproj

from safety.config import settings
from safety.etl import location
from safety.etl.adapters.base import NormalizedIncident, RawChunk, SourceAdapter

log = logging.getLogger(__name__)

_LOCAL_TZ = ZoneInfo("America/Chicago")
_PAGE_SIZE = 2000

# The older service, read for aggravated assault only (see module docstring).
LEGACY_URL = "https://maps.austintexas.gov/gis/rest/APDCrimeViewer/APD_Reported_Crimes/MapServer"

# Layers read from each service, by name. "Aggravated Assault" is deliberately
# absent from the new service's list: that layer duplicates robbery.
_NEW_LAYERS = (
    "Homicide",
    "Robbery",
    "Burglary",
    "Auto Theft",
    "Theft",
    "Arson",
    "Compelling Prostitution",
    "Involuntary Servitude",
    "Part II Crimes",
)
_LEGACY_LAYERS = ("Aggravated Assault",)

_NEW_FIELDS = (
    "RIN",
    "OCCURRENCE_DATE",
    "OCCURRENCE_TIME",
    "ADDRESS_BLOCK",
    "STREET_NAME",
    "STREET_TYPE",
    "PRIMARY_OFFENSE_CODE",
    "CRIME_DESCRIPTION",
    "LOCATION_DESCRIPTION",
    "SECTOR",
)
_LEGACY_FIELDS = (
    "OCCURRENCE_DATE",
    "OCCURRENCE_TIME",
    "ADDRESS_BLOCK",
    "STREET_NAME",
    "STREET_TYPE",
    "PRIMARY_OFFENSE_CODE",
    "CRIME_DESCRIPTION",
)

# APD codes for reports that are calls, not offences: family, dating, parental
# and other disturbances, civil disturbance / demonstration, suspicious person,
# domestic-violence alarm.
_NOT_AN_OFFENSE = frozenset({"2400", "3400", "3401", "3402", "3403", "3458", "3459"})

_STATE_PLANE_TO_WGS84 = pyproj.Transformer.from_crs(
    "EPSG:2277", "EPSG:4326", always_xy=True
)


class AustinEsriAdapter(SourceAdapter):
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

    def _layer_ids(self, service_url: str, wanted: tuple[str, ...]) -> dict[str, int]:
        body = self._get(service_url, {"f": "json"})
        by_name = {
            layer.get("name", "").strip(): int(layer["id"]) for layer in body.get("layers", [])
        }
        missing = [name for name in wanted if name not in by_name]
        if missing:
            raise RuntimeError(
                f"aus: layer(s) {missing} not found at {service_url}; "
                "the CrimeViewer service layout has changed"
            )
        return {name: by_name[name] for name in wanted}

    def _services(self) -> list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
        # (chunk-name prefix, service url, layer names, fields). The prefix is
        # what tells parse_incidents which date convention a chunk uses: a
        # reprocess rebuilds chunks from bronze with their names and nothing else.
        return [
            ("new", self.config.base_url, _NEW_LAYERS, _NEW_FIELDS),
            ("legacy", LEGACY_URL, _LEGACY_LAYERS, _LEGACY_FIELDS),
        ]

    def fetch_incidents(self, since: datetime, until: datetime) -> Iterator[RawChunk]:
        lo = since - timedelta(days=1)
        hi = until + timedelta(days=1)
        where = (
            f"OCCURRENCE_DATE >= TIMESTAMP '{lo.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}' "
            f"AND OCCURRENCE_DATE < TIMESTAMP '{hi.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}'"
        )
        for prefix, service_url, wanted, fields in self._services():
            for name, layer_id in self._layer_ids(service_url, wanted).items():
                url = f"{service_url}/{layer_id}/query"
                offset = 0
                page = 0
                while True:
                    params = {
                        "where": where,
                        "outFields": ",".join(fields),
                        "returnGeometry": "true",
                        "outSR": "4326",
                        "orderByFields": "OBJECTID",
                        "resultOffset": str(offset),
                        "resultRecordCount": str(_PAGE_SIZE),
                        "f": "json",
                    }
                    body = self._get(url, params)
                    features = body.get("features", [])
                    # The layer name rides along in the payload, so bronze
                    # records which layer each row came from.
                    body["layerName"] = name
                    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
                    yield RawChunk(
                        name=f"{prefix}-l{layer_id:02d}-p{page:03d}.json",
                        content_type="application/json",
                        payload=payload,
                        request_url=f"{url}?resultOffset={offset}",
                        fetched_at=datetime.now(timezone.utc),
                        record_count=len(features),
                        meta={"service": prefix, "layer": layer_id, "offset": offset},
                    )
                    offset += len(features)
                    page += 1
                    if not features or not body.get("exceededTransferLimit"):
                        break
                log.info("aus: fetched %s rows from the %s %r layer", offset, prefix, name)

    def fetch_boundary(self) -> RawChunk | None:
        """None: the coverage polygon comes from TIGER/Line PLACE (boundary.py)."""
        return None

    # ------------------------------------------------------------------ parse

    def parse_incidents(self, chunk: RawChunk) -> Iterator[dict[str, Any]]:
        body = json.loads(chunk.payload.decode("utf-8"))
        legacy = chunk.name.startswith("legacy-")
        layer = body.get("layerName")
        for feature in body.get("features", []):
            record = dict(feature.get("attributes", {}))
            geometry = feature.get("geometry") or {}
            record["_x"] = geometry.get("x")
            record["_y"] = geometry.get("y")
            record["_legacy"] = legacy
            record["_layer"] = layer
            yield record

    # -------------------------------------------------------------- normalize

    @staticmethod
    def _clean(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _clock(hhmm: Any) -> tuple[int, int] | None:
        """OCCURRENCE_TIME (an HHMM integer) -> (hour, minute), or None."""
        try:
            value = int(hhmm)
        except (TypeError, ValueError):
            return None
        hour, minute = divmod(value, 100)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None

    @classmethod
    def _occurred(cls, epoch_ms: Any, hhmm: Any, legacy: bool) -> datetime | None:
        """Austin wall clock, tagged UTC like the rest of the pipeline's
        `occurred_at` values. None when the source gave no usable date."""
        if epoch_ms in (None, ""):
            return None
        try:
            instant = datetime.fromtimestamp(float(epoch_ms) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        local = instant.astimezone(_LOCAL_TZ)
        if not legacy:
            return local.replace(tzinfo=timezone.utc)
        # Legacy: the epoch is local midnight, so only its date is meaningful.
        clock = cls._clock(hhmm)
        if clock is None:
            return None
        return datetime(local.year, local.month, local.day, *clock, tzinfo=timezone.utc)

    @staticmethod
    def _resolve_coordinates(x: Any, y: Any) -> tuple[float | None, float | None, str]:
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            return None, None, "missing"
        if fx == 0 or fy == 0:
            return None, None, "missing"
        if abs(fy) <= 90.0 and abs(fx) <= 180.0:
            return fy, fx, "published_wgs84"
        # outSR was ignored and the native State Plane pair came back.
        lng, lat = _STATE_PLANE_TO_WGS84.transform(fx, fy)
        if abs(lat) > 90.0 or abs(lng) > 180.0:
            return None, None, "unprojectable"
        return lat, lng, "reprojected_epsg2277"

    @classmethod
    def _block(cls, record: dict[str, Any]) -> str | None:
        # Display text only: APD's street names carry no direction prefix, so
        # "300 5TH ST" does not say east or west. The geometry is the location.
        parts = [
            cls._clean(record.get("ADDRESS_BLOCK")),
            cls._clean(record.get("STREET_NAME")),
            cls._clean(record.get("STREET_TYPE")),
        ]
        text = " ".join(p for p in parts if p)
        return text or None

    @classmethod
    def _legacy_key(cls, record: dict[str, Any]) -> str:
        parts = (
            record.get("OCCURRENCE_DATE"),
            record.get("OCCURRENCE_TIME"),
            record.get("ADDRESS_BLOCK"),
            record.get("STREET_NAME"),
            record.get("STREET_TYPE"),
            record.get("PRIMARY_OFFENSE_CODE"),
            record.get("CRIME_DESCRIPTION"),
            None if record.get("_x") is None else round(float(record["_x"]), 6),
            None if record.get("_y") is None else round(float(record["_y"]), 6),
        )
        digest = hashlib.sha1(
            "|".join("" if p is None else str(p).strip() for p in parts).encode("utf-8")
        ).hexdigest()
        return f"aa-{digest[:20]}"

    def normalize(self, record: dict[str, Any]) -> NormalizedIncident | None:
        legacy = bool(record.get("_legacy"))
        code = self._clean(record.get("PRIMARY_OFFENSE_CODE"))
        # Not an offence by APD's own coding; see _NOT_AN_OFFENSE.
        if code in _NOT_AN_OFFENSE:
            return None

        if legacy:
            source_incident_id: str | None = self._legacy_key(record)
        else:
            source_incident_id = self._clean(record.get("RIN"))
        if source_incident_id is None:
            return None

        occurred_at = self._occurred(
            record.get("OCCURRENCE_DATE"), record.get("OCCURRENCE_TIME"), legacy
        )
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
            record.get("_x"), record.get("_y")
        )

        return NormalizedIncident(
            source_incident_id=source_incident_id,
            occurred_at=occurred_at,
            occurred_local_date=local_date,
            occurred_precision=precision,
            occurred_basis="occurrence",
            occurred_local_hour=local_hour,
            latitude=latitude,
            longitude=longitude,
            coordinate_source=coordinate_source,
            raw_offense_code=code,
            raw_offense_text=self._clean(record.get("CRIME_DESCRIPTION")),
            raw_source_category=self._clean(record.get("_layer")),
            location_type=location.bucket(record.get("LOCATION_DESCRIPTION")),
            location_block=self._block(record),
            district=self._clean(record.get("SECTOR")),
        )
