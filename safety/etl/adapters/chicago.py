"""Chicago adapter -- Socrata SODA API (design doc S4, S8.1).

The first of four Socrata sources, so this module is also the template the
Seattle, Los Angeles and Austin adapters follow. Source quirks isolated here,
and nowhere else in the pipeline:

* The dataset excludes **the most recent 7 days**, by publication policy rather
  than by lag. That is configuration, not code -- `publication_lag_days = 7` in
  the registry -- and the rollups already anchor their windows to the newest
  reported date rather than to today (gold.resolve_windows), so the gap shows up
  as an honest "data as of" rather than as a week with no crime in it.
* `date` and `updated_on` are Socrata *floating* timestamps: no offset, and the
  value is Chicago local wall clock. They are read as such and never localized,
  which is the same trap `philadelphia._parse_hour` documents from the other
  direction -- treating a local clock as a UTC instant shifts every incident by
  five or six hours, in a direction that changes with daylight saving.
* Coordinates are reduced to the **block** before publication, and a small share
  of records carry none at all. `x_coordinate` / `y_coordinate` are Illinois
  State Plane East (EPSG:3435, US survey feet) and are used as a fallback where
  the WGS84 pair is missing -- the pattern Philadelphia's adapter established for
  wrongly projected rows, which S8.5 did not anticipate.
* Offenses are **IUCR** codes, an Illinois state standard, paired with a
  `description` whose wording drifts. The crosswalk therefore leans on the
  code-only `*` fallback tier (see safety/etl/transform.py) rather than
  enumerating every code/text pair the way Philadelphia's can.
* `fbi_code` is Chicago's own UCR offense classification and is carried through
  as `raw_source_category`. It is *not* NIBRS and is not used as one.
* `location_description` is populated, unlike Philadelphia's dataset. This is the
  first source that can fill `location_type` at all (S7.5).
* `id` is the stable record identifier; `case_number` is the department's RD
  number and is **not** unique -- one case can produce several offense rows. The
  incident key uses `id`.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from collections.abc import Iterator
from datetime import date, datetime, timezone
from typing import Any, ClassVar

import httpx
import pyproj

from safety.config import settings
from safety.etl import location
from safety.etl.adapters.base import NormalizedIncident, RawChunk, SourceAdapter

log = logging.getLogger(__name__)

# NAD83 / Illinois East (US survey feet) -- the City of Chicago's working
# projection, published alongside the WGS84 pair. Built once; constructing a
# transformer is expensive.
_STATE_PLANE_EPSG = "EPSG:3435"
_STATE_PLANE_TO_WGS84 = pyproj.Transformer.from_crs(
    _STATE_PLANE_EPSG, "EPSG:4326", always_xy=True
)

# Requested explicitly rather than taking every column: the dataset carries
# fields this pipeline has no use for, and naming them here means a column
# disappearing upstream fails visibly instead of arriving as a silent NULL.
_INCIDENT_COLUMNS = (
    "id",
    "case_number",
    "date",
    "updated_on",
    "block",
    "iucr",
    "primary_type",
    "description",
    "location_description",
    "fbi_code",
    "beat",
    "district",
    "latitude",
    "longitude",
    "x_coordinate",
    "y_coordinate",
)

# Socrata's ceiling on $limit for a single request. A Chicago month is roughly
# 17,000 rows, so one request per month sits comfortably inside it -- but the
# guard below checks rather than assuming, because a month that actually hits the
# ceiling would be silently truncated.
_PAGE_LIMIT = 50_000


class ChicagoSocrataAdapter(SourceAdapter):
    api_type: ClassVar[str] = "socrata"

    # ------------------------------------------------------------------ fetch

    @property
    def _endpoint(self) -> str:
        return f"{self.config.base_url}/{self.config.incident_dataset}.csv"

    def _headers(self) -> dict[str, str]:
        """An app token raises Socrata's per-IP throttle, and is optional.

        Without one the API is still usable but shares an anonymous quota, which
        a 24-month backfill can exhaust. With four Socrata cities the value of
        setting it goes up, so its absence is logged once rather than silently
        tolerated.
        """
        if settings.socrata_app_token:
            return {"X-App-Token": settings.socrata_app_token}
        return {}

    def _get(self, params: dict[str, str]) -> httpx.Response:
        """GET with bounded exponential backoff on transient upstream failures.

        Same policy as the Carto adapter. Socrata answers throttling with 429,
        which is retried here for the same reason a 5xx is: it is a "come back"
        rather than a "no".
        """
        last_exc: Exception | None = None
        for attempt in range(1, settings.http_max_retries + 1):
            try:
                response = httpx.get(
                    self._endpoint,
                    params=params,
                    headers=self._headers(),
                    timeout=settings.http_timeout_seconds,
                    follow_redirects=True,
                )
                if response.status_code >= 500 or response.status_code == 429:
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
                    "socrata request failed (attempt %s/%s), retrying in %.0fs: %s",
                    attempt,
                    settings.http_max_retries,
                    backoff,
                    exc,
                )
                time.sleep(backoff)
        raise RuntimeError(f"Socrata request failed after retries: {last_exc}")

    @staticmethod
    def _month_starts(
        since: datetime, until: datetime
    ) -> Iterator[tuple[datetime, datetime]]:
        """Calendar-month windows, as the Carto adapter uses.

        Socrata does offer `$offset`, so paging is available -- but month chunks
        are chosen anyway, for two reasons that outlive the pagination question.
        Deep offsets on Socrata degrade badly, and a bronze snapshot partitioned
        by calendar month is directly comparable against the previous one, which
        is what makes the S8.5 volume-anomaly check meaningful.
        """
        cursor = since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while cursor < until:
            if cursor.month == 12:
                nxt = cursor.replace(year=cursor.year + 1, month=1)
            else:
                nxt = cursor.replace(month=cursor.month + 1)
            yield cursor, min(nxt, until)
            cursor = nxt

    def fetch_incidents(self, since: datetime, until: datetime) -> Iterator[RawChunk]:
        """Yield one chunk per calendar month.

        Filtered on `date`, the occurrence timestamp, not on `updated_on`. That
        is a deliberate limitation worth naming: a record revised today but
        occurring two years ago will not be re-read by an incremental pull, only
        by a backfill. `revision_lookback_days` covers the window in which
        revisions actually cluster, and the bronze layer is what makes a full
        reprocess cheap when a deeper correction is needed (S5, S8.3).

        The floating timestamps have no offset, so the bounds are formatted
        without one. Sending a UTC instant here would ask Chicago's local clock
        to be compared against a different quantity.
        """
        for chunk_start, chunk_end in self._month_starts(since, until):
            params = {
                "$select": ", ".join(_INCIDENT_COLUMNS),
                "$where": (
                    f"date >= '{_floating(chunk_start)}' "
                    f"AND date < '{_floating(chunk_end)}'"
                ),
                "$order": "id",
                "$limit": str(_PAGE_LIMIT),
            }
            response = self._get(params)
            payload = response.content
            record_count = max(payload.count(b"\n") - 1, 0)

            if record_count >= _PAGE_LIMIT:
                # Truncation here would look exactly like a quiet month. It is
                # not survivable silently: the month would be short in silver and
                # every percentile in the city would be computed from it.
                raise RuntimeError(
                    f"chicago: {chunk_start:%Y-%m} returned {record_count} rows, at "
                    f"or above the $limit ceiling of {_PAGE_LIMIT}, so the month is "
                    "probably truncated. Split the chunking finer than monthly."
                )

            log.info(
                "chi: fetched %s rows for %s", record_count, chunk_start.strftime("%Y-%m")
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
        """None: the coverage polygon comes from TIGER/Line PLACE.

        See safety/etl/boundary.py for why that is one shared loader rather than
        a boundary integration per city. Philadelphia returns a polygon here
        because it already had a working police-jurisdiction one.
        """
        return None

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
        text = ChicagoSocrataAdapter._clean(value)
        if text is None:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _parse_local(value: Any) -> datetime | None:
        """Read a Socrata floating timestamp: '2026-01-12T20:20:00.000'.

        No offset in the payload and none attached here beyond a nominal UTC tag,
        because the value is a local wall clock and the rest of the pipeline
        treats `occurred_at` that way (see 009_time_of_day.sql and
        migrate.backfill_incident_hour, which measures that assumption rather
        than trusting it).
        """
        text = ChicagoSocrataAdapter._clean(value)
        if text is None:
            return None
        candidate = text.replace(" ", "T")
        if candidate.endswith("Z"):
            candidate = candidate[:-1]
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        # Tagged UTC to keep the column timezone-aware, not converted. Nothing
        # here shifts the clock.
        return parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _resolve_coordinates(
        latitude: float | None,
        longitude: float | None,
        x_coord: float | None,
        y_coord: float | None,
    ) -> tuple[float | None, float | None, str]:
        """Return (latitude, longitude, provenance).

        The published WGS84 pair first. Where it is absent but the State Plane
        pair is present, reproject rather than discard -- those are real Chicago
        blocks, and the same judgement Philadelphia's adapter makes. (0, 0) is
        treated as absent, which is what it means; the validator rejects what
        survives neither path.
        """
        if (
            latitude is not None
            and longitude is not None
            and not (latitude == 0 and longitude == 0)
            and abs(latitude) <= 90.0
            and abs(longitude) <= 180.0
        ):
            return latitude, longitude, "published_wgs84"

        if x_coord in (None, 0) or y_coord in (None, 0):
            return None, None, "missing"

        try:
            lng, lat = _STATE_PLANE_TO_WGS84.transform(x_coord, y_coord)
        except Exception:
            return None, None, "unprojectable"
        if abs(lat) > 90.0 or abs(lng) > 180.0:
            return None, None, "unprojectable"
        return lat, lng, "reprojected_epsg3435"

    def normalize(self, record: dict[str, Any]) -> NormalizedIncident | None:
        source_incident_id = self._clean(record.get("id"))
        if source_incident_id is None:
            # No usable identifier: structurally unusable. `case_number` is not a
            # substitute -- one case can carry several offense rows.
            return None

        occurred_at = self._parse_local(record.get("date"))
        if occurred_at is None:
            # Let the validator record this as missing_timestamp against a real
            # incident id rather than dropping it silently here.
            occurred_at = datetime.min.replace(tzinfo=timezone.utc)
            local_date: date = occurred_at.date()
            local_hour: int | None = None
            precision = "date"
        else:
            local_date = occurred_at.date()
            local_hour = occurred_at.hour
            # Chicago publishes a clock time on every record. Midnight is a real
            # value here rather than a stand-in for "unknown", and
            # migrate.backfill_incident_hour's midnight-share check is what would
            # catch it if that ever stopped being true.
            precision = "exact"

        latitude, longitude, coordinate_source = self._resolve_coordinates(
            self._to_float(record.get("latitude")),
            self._to_float(record.get("longitude")),
            self._to_float(record.get("x_coordinate")),
            self._to_float(record.get("y_coordinate")),
        )

        return NormalizedIncident(
            source_incident_id=source_incident_id,
            occurred_at=occurred_at,
            occurred_local_date=local_date,
            occurred_precision=precision,
            # CPD's `date` is the recorded occurrence time, not a dispatch time.
            # That is a genuine difference from Philadelphia and the reason
            # occurred_basis is a column rather than a product-wide constant.
            occurred_basis="occurrence",
            occurred_local_hour=local_hour,
            latitude=latitude,
            longitude=longitude,
            coordinate_source=coordinate_source,
            raw_offense_code=self._clean(record.get("iucr")),
            # The crosswalk matches on (code, text), so the text has to be the
            # value the source actually publishes. Chicago splits the offense
            # across two columns and `description` is the specific half; the
            # broader `primary_type` is what `iucr` already implies.
            raw_offense_text=self._clean(record.get("description")),
            # CPD's own UCR offense classification. Carried for provenance, not
            # read as NIBRS -- the crosswalk is what maps to NIBRS.
            raw_source_category=self._clean(record.get("fbi_code")),
            # Deliberately None. Chicago publishes `updated_on`, which is when the
            # *record* was last modified in the RMS -- often months after the fact
            # and sometimes a bulk re-publication. S6 wants `reported_at` to be
            # when the offence was reported to police, for reporting-lag analysis.
            # Those are different quantities, and filling this column with the
            # wrong one would make every lag figure derived from it meaningless.
            # `updated_on` is still fetched: it lands in bronze verbatim, so the
            # analysis it does support remains possible from the snapshot.
            reported_at=None,
            location_type=location.bucket(record.get("location_description")),
            location_block=self._clean(record.get("block")),
            district=self._clean(record.get("district")),
        )


def _floating(value: datetime) -> str:
    """Format a bound as a Socrata floating timestamp, with no offset."""
    return value.strftime("%Y-%m-%dT%H:%M:%S")
