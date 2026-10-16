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

**The years before** are in LAPD's two legacy "Crime Data" datasets, read only
by the history load (`LosAngelesLegacyAdapter`, `HISTORY_DATASETS`):

* `63jg-8b9z`, 2010-2019, and `2nrs-mtv8`, 2020-2024. Same columns in both. The
  legacy system stopped taking new reports at the cut-over, so through 2024 the
  two systems split the reports between them: legacy rows taper from 16.3k in
  March to 4.7k in December as NIBRS ramps up, the two together stay at the
  usual 18-20k a month, and in a week of June 2024 five legacy reports out of
  about two thousand matched a NIBRS record on time and place and none on case
  number (checked 2026-10-09). So both are loaded for 2024, not one or the other.
* **One row per report** (`dr_no`), classified by LAPD's own crime code
  (`crm_cd`). A report's further offences (`crm_cd_2..4`, on about one report in
  fifteen) are not separate rows the way NIBRS offences are; the primary code
  is the one read, and reference.source_series_caveat says what that does to
  counts.
* **LAPD crime codes collide with NIBRS codes** -- 510 is a stolen vehicle to
  LAPD and bribery to NIBRS, 220 an attempted robbery and a burglary -- and the
  2024 overlap means the date cannot tell them apart. The code is therefore
  stored as `CRM-510`: still the published code, with the scheme it belongs to
  in front, and a crosswalk key no NIBRS code can match.
* 2016 is published with 58k exact duplicate rows (284k rows, 226k reports);
  the validator drops repeated ids within a pull.
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


_LEGACY_COLUMNS = (
    "dr_no",
    "date_occ",
    "time_occ",
    "date_rptd",
    "crm_cd",
    "crm_cd_desc",
    "part_1_2",
    "premis_desc",
    "area_name",
    "location",
    "lat",
    "lon",
)

# The prefix that marks a code as LAPD's own rather than NIBRS (see above).
LEGACY_CODE_PREFIX = "CRM-"

# Older datasets the history load reads after the NIBRS one, newest first:
# (dataset, first day, day after the last). The NIBRS dataset itself is read
# back to NIBRS_HISTORY_START; before that it holds only a scatter of late
# reports of old offences (about 2k a year at most).
NIBRS_HISTORY_START = date(2024, 1, 1)
HISTORY_DATASETS: tuple[tuple[str, date, date], ...] = (
    ("2nrs-mtv8", date(2020, 1, 1), date(2025, 1, 1)),
    ("63jg-8b9z", date(2010, 1, 1), date(2020, 1, 1)),
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

    @classmethod
    def _occurrence(
        cls, record: dict[str, Any]
    ) -> tuple[datetime, date, int | None, str]:
        """(occurred_at, local date, local hour, precision) from date_occ + time_occ."""
        day = cls._parse_local(record.get("date_occ"))
        hhmm = cls._parse_hhmm(record.get("time_occ"))
        if day is None:
            occurred_at = datetime.min.replace(tzinfo=timezone.utc)
            return occurred_at, occurred_at.date(), None, "date"
        if hhmm is None:
            return day, day.date(), None, "date"
        occurred_at = day.replace(hour=hhmm[0], minute=hhmm[1])
        return occurred_at, occurred_at.date(), hhmm[0], "exact"

    def normalize(self, record: dict[str, Any]) -> NormalizedIncident | None:
        source_incident_id = self._clean(record.get("uniquenibrno"))
        if source_incident_id is None:
            return None

        occurred_at, local_date, local_hour, precision = self._occurrence(record)
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


class LosAngelesLegacyAdapter(LosAngelesSocrataAdapter):
    """LAPD's legacy "Crime Data" datasets (63jg-8b9z, 2nrs-mtv8), history only."""

    incident_columns = _LEGACY_COLUMNS
    # Up to about 26,000 rows a month (2016, before duplicates are dropped).
    order_field = "dr_no"

    def normalize(self, record: dict[str, Any]) -> NormalizedIncident | None:
        source_incident_id = self._clean(record.get("dr_no"))
        if source_incident_id is None:
            return None

        occurred_at, local_date, local_hour, precision = self._occurrence(record)
        latitude, longitude, coordinate_source = self._wgs84_or_none(
            self._to_float(record.get("lat")),
            self._to_float(record.get("lon")),
        )
        code = self._clean(record.get("crm_cd"))
        part = self._clean(record.get("part_1_2"))
        block = self._clean(record.get("location"))

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
            raw_offense_code=f"{LEGACY_CODE_PREFIX}{code}" if code else None,
            raw_offense_text=self._clean(record.get("crm_cd_desc")),
            raw_source_category=f"Part {part}" if part else None,
            reported_at=self._parse_local(record.get("date_rptd")),
            location_type=location.bucket(self._clean(record.get("premis_desc"))),
            # Published padded to fixed widths: "300 E  GAGE      AV".
            location_block=" ".join(block.split()) if block else None,
            district=self._clean(record.get("area_name")),
        )
