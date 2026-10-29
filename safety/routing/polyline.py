"""Google encoded-polyline decoding, at any precision.

Valhalla returns polyline6. Transitous (MOTIS) states the precision on every
leg's geometry, and it is not always the same within one itinerary -- R2 saw 6
and 7 -- so precision is an argument, never a module constant.
"""

from __future__ import annotations

from collections.abc import Iterable


def _varints(encoded: str):
    """The signed integers an encoded string carries, in order."""
    result = shift = 0
    for ch in encoded:
        byte = ord(ch) - 63
        if byte < 0 or byte > 0x3F:
            raise ValueError("invalid character in encoded polyline")
        result |= (byte & 0x1F) << shift
        shift += 5
        if byte < 0x20:
            yield ~(result >> 1) if result & 1 else result >> 1
            result = shift = 0
        elif shift > 64:
            # A run of continuation bytes this long is garbage, not a number,
            # and left alone it would grow an unbounded Python int.
            raise ValueError("malformed encoded polyline")
    if shift:
        raise ValueError("truncated encoded polyline")


def decode(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """(lat, lng) pairs from an encoded polyline at `precision` decimal places."""
    if not 1 <= precision <= 10:
        raise ValueError("precision must be between 1 and 10")
    factor = 10.0**precision
    values = list(_varints(encoded))
    if len(values) % 2:
        raise ValueError("encoded polyline has an odd number of values")
    points: list[tuple[float, float]] = []
    lat = lng = 0
    for i in range(0, len(values), 2):
        lat += values[i]
        lng += values[i + 1]
        points.append((lat / factor, lng / factor))
    return points


def _encode_value(value: int) -> str:
    value = ~(value << 1) if value < 0 else value << 1
    out = []
    while value >= 0x20:
        out.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    out.append(chr(value + 63))
    return "".join(out)


def encode(points: Iterable[tuple[float, float]], precision: int = 5) -> str:
    """The inverse of decode(); used by tests and by synthetic fixtures."""
    factor = 10**precision
    out = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        ilat, ilng = round(lat * factor), round(lng * factor)
        out.append(_encode_value(ilat - prev_lat))
        out.append(_encode_value(ilng - prev_lng))
        prev_lat, prev_lng = ilat, ilng
    return "".join(out)
