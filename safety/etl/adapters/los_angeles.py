"""Los Angeles adapter -- Socrata SODA API, LAPD NIBRS Offenses (S4, S8.1).

The registry was seeded with a placeholder dataset id; the real one is
`k7nn-b2ep`, "LAPD NIBRS Offenses Dataset", fixed in 015_onboard_sources.sql.
The companion Victims dataset (`gqf2-vm2j`) is a different grain -- one row per
victim -- and is deliberately not read.

Source quirks isolated here:

* **NIBRS codes are published** (`nibr_code`), so the crosswalk is code-only and
  `exact`. `nibr_description` embeds the California penal-code section and
  varies for the same NIBRS code, which is why the crosswalk keys on the code.
* **Date and time are split.** `date_occ` is a floating timestamp whose clock is
  always midnight; the occurrence time lives in `time_occ` as `HHMM`. Both are
  read, and the hour comes from `time_occ`. Midnight and noon are mildly heaped
  (about 2.7% and 2.3% of records) -- the usual signature of an unknown time
  entered as a round one -- which is not enough to treat either as missing.
* `date_rptd` carries a real report timestamp, so `reported_at` is filled.
* **Coordinates are rounded to the hundred block** (`hndrdth_lat` /
  `hndrdth_lon`), with (0, 0) for unresolved addresses. No projected fallback is
  published.
* `premis_desc` is LAPD's premise code and text ("244 - TOBACCO SHOP"); the code
  prefix is stripped before keyword bucketing.
* `uniquenibrno` is the per-offense key; `caseno` repeats across the offenses on
  one report.
* The NIBRS series begins with LAPD's March 2024 RMS cut-over and ramps up
  through early 2025 -- October to December 2024 carry roughly 60-75% of a
  typical month's records. The registry's `backfill_start_date` is 2024-10-01.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from safety.etl import location
from safety.etl.adapters.base import NormalizedIncident
from safety.etl.adapters.socrata import SocrataAdapter

_INCIDENT_COLUMNS = (
    "uniquenibrno",
    "caseno",
    "date_occ",
    "time_occ",
    "date_rptd",
    "nibr_code",
    "nibr_description",
    "crime_against",
    "premis_desc",
    "area_name",
    "hndrdth_loc_chk",
    "hndrdth_lat",
    "hndrdth_lon",
)


class LosAngelesSocrataAdapter(SocrataAdapter):
    incident_columns = _INCIDENT_COLUMNS
    # A Los Angeles month is roughly 19,000 offense rows.
    occurred_field = "date_occ"
    order_field = "uniquenibrno"

    @classmethod
    def _parse_hhmm(cls, value: Any) -> tuple[int, int] | None:
        text = cls._clean(value)
        if text is None or not text.isdigit() or len(text) > 4:
            return None
        text = text.zfill(4)
        hour, minute = int(text[:2]), int(text[2:])
        if hour > 23 or minute > 59:
            return None
        return hour, minute

    @staticmethod
    def _strip_code(value: str | None) -> str | None:
        """'244 - TOBACCO SHOP' -> 'TOBACCO SHOP'."""
        if value is None:
            return None
        head, sep, tail = value.partition(" - ")
        return tail.strip() if sep and head.strip().isdigit() else value

    def normalize(self, record: dict[str, Any]) -> NormalizedIncident | None:
        source_incident_id = self._clean(record.get("uniquenibrno"))
        if source_incident_id is None:
            return None

        day = self._parse_local(record.get("date_occ"))
        hhmm = self._parse_hhmm(record.get("time_occ"))
        if day is None:
            occurred_at = datetime.min.replace(tzinfo=timezone.utc)
            local_date: date = occurred_at.date()
            local_hour: int | None = None
            precision = "date"
        elif hhmm is None:
            occurred_at = day
            local_date = day.date()
            local_hour = None
            precision = "date"
        else:
            occurred_at = day.replace(hour=hhmm[0], minute=hhmm[1])
            local_date = occurred_at.date()
            local_hour = hhmm[0]
            precision = "exact"

        latitude, longitude, coordinate_source = self._wgs84_or_none(
            self._to_float(record.get("hndrdth_lat")),
            self._to_float(record.get("hndrdth_lon")),
        )
        area = self._clean(record.get("area_name"))

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
            raw_offense_code=self._clean(record.get("nibr_code")),
            raw_offense_text=self._clean(record.get("nibr_description")),
            raw_source_category=self._clean(record.get("crime_against")),
            reported_at=self._parse_local(record.get("date_rptd")),
            location_type=location.bucket(
                self._strip_code(self._clean(record.get("premis_desc")))
            ),
            location_block=self._clean(record.get("hndrdth_loc_chk")),
            district=area,
        )
