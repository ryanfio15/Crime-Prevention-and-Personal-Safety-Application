"""Seattle adapter -- Socrata SODA API, SPD Crime Data (design doc S4, S8.1).

Source quirks isolated here:

* **NIBRS codes are published on every record** (`nibrs_offense_code`), so the
  crosswalk maps code to code and is `exact` (scripts/build_nibrs_crosswalk.py).
  Two non-NIBRS codes appear: `500` (SPD's no-contact-order extension) and `999`
  ("not reportable to NIBRS"); both are carried and filed under the residual.
* `offense_date` is the **offense start time**, a real occurrence timestamp --
  the difference from Philadelphia's dispatch time that `occurred_basis` exists
  to record. Socrata floating, read as Seattle local wall clock.
* `report_date_time` is when the report was taken, which is what `reported_at`
  means, so unlike Chicago it is filled.
* **Coordinates are withheld for some offense types.** SPD publishes the literal
  string `REDACTED` in latitude, longitude and block for roughly one record in
  six -- sexual offenses and others where the location identifies a victim. They
  arrive here as missing coordinates and the validator rejects them, so
  Seattle's rejection rate sits in the mid-teens by publication policy rather
  than by adapter error, and the map under-counts exactly the offense types SPD
  protects. There is no State Plane pair to fall back on.
* No premise / location-type field is published, so `location_type` stays
  `unknown`.
* `offense_id` is the per-offense key; `report_number` is per report and one
  report can carry several offenses.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from safety.etl.adapters.base import NormalizedIncident
from safety.etl.adapters.socrata import SocrataAdapter

_INCIDENT_COLUMNS = (
    "offense_id",
    "report_number",
    "offense_date",
    "report_date_time",
    "nibrs_offense_code",
    "nibrs_offense_code_description",
    "offense_category",
    "block_address",
    "latitude",
    "longitude",
    "precinct",
)

# SPD's placeholder for a withheld value, and the dash it uses for "none".
_WITHHELD = {"REDACTED", "-"}


class SeattleSocrataAdapter(SocrataAdapter):
    incident_columns = _INCIDENT_COLUMNS
    # A Seattle month is roughly 6,500 rows.
    occurred_field = "offense_date"
    order_field = "offense_id"

    @classmethod
    def _value(cls, value: Any) -> str | None:
        text = cls._clean(value)
        if text is None or text.upper() in _WITHHELD:
            return None
        return text

    def normalize(self, record: dict[str, Any]) -> NormalizedIncident | None:
        source_incident_id = self._value(record.get("offense_id"))
        if source_incident_id is None:
            return None

        occurred_at = self._parse_local(record.get("offense_date"))
        if occurred_at is None:
            occurred_at = datetime.min.replace(tzinfo=timezone.utc)
            local_date: date = occurred_at.date()
            local_hour: int | None = None
            precision = "date"
        else:
            local_date = occurred_at.date()
            local_hour = occurred_at.hour
            precision = "exact"

        latitude, longitude, coordinate_source = self._wgs84_or_none(
            self._to_float(self._value(record.get("latitude"))),
            self._to_float(self._value(record.get("longitude"))),
        )
        if coordinate_source == "missing" and (
            self._clean(record.get("latitude")) or ""
        ).upper() == "REDACTED":
            coordinate_source = "withheld_by_source"

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
            raw_offense_code=self._value(record.get("nibrs_offense_code")),
            raw_offense_text=self._value(record.get("nibrs_offense_code_description")),
            raw_source_category=self._value(record.get("offense_category")),
            reported_at=self._parse_local(record.get("report_date_time")),
            location_block=self._value(record.get("block_address")),
            district=self._value(record.get("precinct")),
        )
