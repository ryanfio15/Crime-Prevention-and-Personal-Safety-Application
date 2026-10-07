"""Characterisation tests for each source adapter's normalize() (F5).

Hand-made records in each source's own field names (tests/fixtures/adapters/),
run through the real adapter with no network. Expected values were derived by
reading each adapter.

A note on `occurred_at`: by the pipeline's documented convention
(socrata.py `_parse_local`, washington_dc.py `_local_wall_clock`,
009_time_of_day.sql) it holds the city's *local wall clock* tagged UTC, not the
true UTC instant -- occurred_local_date and occurred_local_hour are read off it
directly. These tests assert that convention, not a UTC conversion.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from safety.etl.adapters import SourceConfig, get_adapter
from safety.etl.adapters.austin import AustinEsriAdapter
from safety.etl.adapters.los_angeles import LosAngelesSocrataAdapter
from safety.etl.adapters.philadelphia import PhiladelphiaCartoAdapter
from safety.etl.adapters.washington_dc import WashingtonDcEsriAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"
TIMEZONES = {
    "phl": "America/New_York",
    "chi": "America/Chicago",
    "sea": "America/Los_Angeles",
    "lax": "America/Los_Angeles",
    "dc": "America/New_York",
    "aus": "America/Chicago",
}


def _config(source_id: str) -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        city_name=source_id,
        state_code="XX",
        agency_name="test",
        api_type="test",
        base_url="https://example.invalid",
        incident_dataset="test",
        boundary_dataset=None,
        expected_cadence="daily",
        publication_lag_days=1,
        revision_lookback_days=30,
        crosswalk_version=f"{source_id}_v1",
        backfill_start_date=None,
        last_success_watermark=None,
        timezone=TIMEZONES[source_id],
        state_fips=None,
        county_fips=None,
        place_fips=None,
        attribution_text="test",
        terms_url=None,
        freshness_note=None,
        location_precision_note=None,
        occurrence_basis_note=None,
        denominator_examples_note=None,
        enabled=True,
    )


def _normalize(source_id: str, name: str):
    records = json.loads((FIXTURES / f"{source_id}.json").read_text())
    return get_adapter(_config(source_id)).normalize(records[name])


def _wall(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _near(lat, lng, expect_lat, expect_lng, tol=0.01):
    return abs(lat - expect_lat) < tol and abs(lng - expect_lng) < tol


# ------------------------------------------------------------------ Philadelphia


def test_phl_normal_record():
    n = _normalize("phl", "normal")
    assert n.source_incident_id == "202401000996"  # numeric dc_key's fraction dropped
    assert n.occurred_at == _wall(2024, 1, 12, 20, 20)
    assert n.occurred_local_date == date(2024, 1, 12)
    assert n.occurred_local_hour == 20
    assert n.occurred_precision == "exact"
    assert n.occurred_basis == "dispatch"
    assert (n.latitude, n.longitude) == (39.9526, -75.1652)  # point_y / point_x
    assert n.coordinate_source == "published_wgs84"
    assert n.raw_offense_code == "600"  # '600.0' normalised
    assert n.raw_offense_text == "Thefts"
    assert n.raw_source_category == "Part I"
    assert n.district == "09"


def test_phl_state_plane_and_no_clock_time():
    n = _normalize("phl", "state_plane")
    assert n.coordinate_source == "reprojected_epsg2272"
    assert _near(n.latitude, n.longitude, 39.952, -75.160)
    assert n.occurred_local_hour is None  # neither `hour` nor dispatch_time given
    assert n.occurred_precision == "date"
    assert n.raw_source_category == "Part II"


def test_phl_no_key_is_unusable():
    assert _normalize("phl", "no_key") is None


def test_phl_timestamp_parsing():
    parse = PhiladelphiaCartoAdapter._parse_timestamp
    assert parse("2024-01-12 20:20:00+00") == _wall(2024, 1, 12, 20, 20)
    assert parse("2024-01-12T20:20:00Z") == _wall(2024, 1, 12, 20, 20)
    assert parse("not a time") is None
    assert parse("") is None


# ------------------------------------------------------------------ Chicago


def test_chi_normal_record():
    n = _normalize("chi", "normal")
    assert n.source_incident_id == "13300001"
    assert n.occurred_at == _wall(2024, 1, 12, 20, 20)
    assert n.occurred_local_date == date(2024, 1, 12)
    assert n.occurred_local_hour == 20
    assert n.occurred_precision == "exact"
    assert n.occurred_basis == "occurrence"
    assert (n.latitude, n.longitude) == (41.8781, -87.6298)
    assert n.coordinate_source == "published_wgs84"
    assert (n.raw_offense_code, n.raw_offense_text, n.raw_source_category) == (
        "0820",
        "$500 AND UNDER",
        "06",
    )


def test_chi_missing_wgs84_falls_back_to_state_plane():
    n = _normalize("chi", "state_plane_only")
    assert n.coordinate_source == "reprojected_epsg3435"
    assert _near(n.latitude, n.longitude, 41.881, -87.629)


def test_chi_no_usable_coordinates():
    n = _normalize("chi", "no_coordinates")
    assert (n.latitude, n.longitude, n.coordinate_source) == (None, None, "missing")


# ------------------------------------------------------------------ Seattle


def test_sea_normal_record():
    n = _normalize("sea", "normal")
    assert n.source_incident_id == "56000001"
    assert n.occurred_at == _wall(2024, 7, 4, 23, 15)
    assert n.occurred_local_date == date(2024, 7, 4)
    assert n.occurred_local_hour == 23
    assert n.reported_at == _wall(2024, 7, 5, 1, 0)
    assert n.occurred_basis == "occurrence"
    assert (n.latitude, n.longitude, n.coordinate_source) == (47.6097, -122.3422, "published_wgs84")
    assert (n.raw_offense_code, n.raw_offense_text) == ("13B", "Simple Assault")
    assert n.district == "West"


def test_sea_redacted_location_is_withheld_not_missing():
    n = _normalize("sea", "redacted")
    assert (n.latitude, n.longitude) == (None, None)
    assert n.coordinate_source == "withheld_by_source"
    assert n.location_block is None


def test_sea_not_an_offense_is_dropped():
    assert _normalize("sea", "not_an_offense") is None


# ------------------------------------------------------------------ Los Angeles


def test_lax_normal_record():
    n = _normalize("lax", "normal")
    assert n.source_incident_id == "240100001-1"
    assert n.occurred_at == _wall(2024, 3, 1, 0, 45)  # time_occ "45" is 00:45
    assert n.occurred_local_date == date(2024, 3, 1)
    assert n.occurred_local_hour == 0
    assert n.occurred_precision == "exact"
    assert n.reported_at == _wall(2024, 3, 2)
    assert (n.latitude, n.longitude, n.coordinate_source) == (34.0453, -118.2507, "published_wgs84")
    assert (n.raw_offense_code, n.raw_offense_text) == ("23H", "All Other Larceny")
    assert n.district == "Central"


def test_lax_invalid_time_and_zero_coordinates():
    n = _normalize("lax", "bad_time_zero_coords")
    assert n.occurred_precision == "date"  # 2400 is not a clock time
    assert n.occurred_local_hour is None
    assert n.occurred_at == _wall(2024, 3, 1)
    assert (n.latitude, n.longitude, n.coordinate_source) == (None, None, "missing")


def test_lax_hhmm_parsing():
    parse = LosAngelesSocrataAdapter._parse_hhmm
    assert parse("0930") == (9, 30)
    assert parse("930") == (9, 30)
    assert parse("2359") == (23, 59)
    assert parse("2400") is None
    assert parse("1260") is None
    assert parse("12345") is None
    assert parse("ab") is None


# ------------------------------------------------------------------ Washington, DC


def test_dc_epoch_ms_becomes_new_york_wall_clock():
    n = _normalize("dc", "summer")
    # 1720132200000 ms is 2024-07-04 22:30 UTC, 18:30 EDT.
    assert n.occurred_at == _wall(2024, 7, 4, 18, 30)
    assert n.occurred_local_date == date(2024, 7, 4)
    assert n.occurred_local_hour == 18
    assert n.occurred_basis == "occurrence"
    assert n.reported_at == _wall(2024, 7, 4, 19, 30)
    assert (n.latitude, n.longitude, n.coordinate_source) == (38.917, -77.028, "published_wgs84")
    assert (n.raw_offense_code, n.raw_offense_text) == ("THEFT/OTHER", "Theft/Other")
    assert n.raw_source_category == "OTHERS"


def test_dc_report_time_fallback_and_state_plane():
    n = _normalize("dc", "report_only_state_plane")
    # No START_DATE: the report time stands in, and the basis says so.
    # 1705323600000 ms is 2024-01-15 13:00 UTC, 08:00 EST.
    assert n.occurred_basis == "report"
    assert n.occurred_at == _wall(2024, 1, 15, 8, 0)
    assert n.coordinate_source == "reprojected_epsg26985"
    assert _near(n.latitude, n.longitude, 38.901, -77.035)


def test_dc_wall_clock_helper():
    assert WashingtonDcEsriAdapter._local_wall_clock(None) is None
    assert WashingtonDcEsriAdapter._local_wall_clock("") is None
    assert WashingtonDcEsriAdapter._local_wall_clock(1705323600000) == _wall(2024, 1, 15, 8, 0)


# ------------------------------------------------------------------ Austin


def test_aus_new_layer_record():
    n = _normalize("aus", "new_layer")
    # 1710056700000 ms is 2024-03-10 07:45 UTC, 01:45 CST (before the 02:00 DST jump).
    assert n.source_incident_id == "2024000001"
    assert n.occurred_at == _wall(2024, 3, 10, 1, 45)
    assert n.occurred_local_date == date(2024, 3, 10)
    assert n.occurred_local_hour == 1
    assert n.occurred_basis == "occurrence"
    assert (n.latitude, n.longitude, n.coordinate_source) == (30.2672, -97.7431, "published_wgs84")
    assert n.location_block == "500 BLOCK CONGRESS AVE"
    assert n.raw_source_category == "Theft"
    assert n.district == "GE"


def test_aus_legacy_record_key_clock_and_state_plane():
    n = _normalize("aus", "legacy")
    # Legacy layers: the date from OCCURRENCE_DATE (local midnight), the clock
    # from the HHMM OCCURRENCE_TIME.
    assert n.occurred_at == _wall(2019, 5, 1, 15, 30)
    assert n.occurred_local_hour == 15
    assert n.source_incident_id.startswith("aa-") and len(n.source_incident_id) == 23
    assert n.coordinate_source == "reprojected_epsg2277"
    assert _near(n.latitude, n.longitude, 30.266, -97.744)


def test_aus_legacy_key_is_stable_and_content_derived():
    records = json.loads((FIXTURES / "aus.json").read_text())
    legacy = records["legacy"]
    key = AustinEsriAdapter._legacy_key(legacy)
    assert AustinEsriAdapter._legacy_key(dict(legacy)) == key
    assert AustinEsriAdapter._legacy_key({**legacy, "OCCURRENCE_TIME": 1531}) != key


def test_aus_clock_and_non_offense():
    assert AustinEsriAdapter._clock(1530) == (15, 30)
    assert AustinEsriAdapter._clock(2360) is None
    assert AustinEsriAdapter._clock(None) is None
    assert _normalize("aus", "not_an_offense") is None


@pytest.mark.parametrize("source_id", sorted(TIMEZONES))
def test_every_source_keys_are_namespaced(source_id):
    adapter = get_adapter(_config(source_id))
    assert adapter.incident_key("123") == f"{source_id}:123"
