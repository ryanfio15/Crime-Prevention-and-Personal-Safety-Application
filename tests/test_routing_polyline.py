"""Encoded-polyline decoding at the precisions the providers use: 5 (Google's
reference), 6 (Valhalla, most MOTIS legs) and 7 (some MOTIS legs, R2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from safety.routing.polyline import decode, encode

FIXTURES = Path(__file__).parent / "fixtures" / "routing"


def test_googles_reference_example_at_precision_5():
    points = decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@", 5)
    assert points == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


@pytest.mark.parametrize("precision", [5, 6, 7])
def test_round_trip(precision):
    points = [(39.952335, -75.163789), (39.9496, -75.1503), (39.94, -75.2), (-33.86, 151.21)]
    decoded = decode(encode(points, precision), precision)
    assert len(decoded) == len(points)
    for (lat, lng), (dlat, dlng) in zip(points, decoded):
        assert abs(lat - dlat) < 10**-precision
        assert abs(lng - dlng) < 10**-precision


def test_wrong_precision_is_off_by_a_factor_of_ten():
    # Why precision is per call: reading a polyline6 as polyline5 lands in the
    # wrong hemisphere-sized place, not a few metres off.
    encoded = encode([(38.9, -77.0)], 6)
    assert decode(encoded, 5) == [(389.0, -770.0)]


def test_valhalla_fixture_shape_decodes_into_dc():
    trip = json.loads((FIXTURES / "valhalla_route_alt.json").read_text())["trip"]
    points = decode(trip["legs"][0]["shape"], 6)
    summary = trip["summary"]
    assert len(points) > 100
    for lat, lng in points:
        assert summary["min_lat"] - 1e-6 <= lat <= summary["max_lat"] + 1e-6
        assert summary["min_lon"] - 1e-6 <= lng <= summary["max_lon"] + 1e-6


def test_empty_is_empty():
    assert decode("", 6) == []


@pytest.mark.parametrize("bad", ["_p~iF~ps|U_", "_p~iF", "abc\x01def", "~~~~~~~~~~~~~~~"])
def test_malformed_input_raises(bad):
    with pytest.raises(ValueError):
        decode(bad, 5)


def test_precision_bounds():
    with pytest.raises(ValueError):
        decode("??", 0)
