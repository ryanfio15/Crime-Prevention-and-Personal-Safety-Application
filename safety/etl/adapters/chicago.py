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

from datetime import date, datetime, timezone
from typing import Any

import pyproj

from safety.etl import location
from safety.etl.adapters.base import NormalizedIncident
from safety.etl.adapters.socrata import SocrataAdapter

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


class ChicagoSocrataAdapter(SocrataAdapter):
    incident_columns = _INCIDENT_COLUMNS
    # Filtered on `date`, the occurrence timestamp, not on `updated_on`. That is
    # a deliberate limitation: a record revised today but occurring two years
    # ago is not re-read by an incremental pull, only by a backfill.
    # `revision_lookback_days` covers the window revisions actually cluster in,
    # and bronze makes a deeper reprocess cheap (S5, S8.3). A Chicago month is
    # roughly 17,000 rows, well inside the per-request ceiling the base class
    # guards.
    occurred_field = "date"
    order_field = "id"

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
