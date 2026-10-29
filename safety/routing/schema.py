"""The shape every provider is normalised to, and the JSON the API sends.

Three providers, three vocabularies -- Valhalla maneuvers in km, MOTIS legs in
metres with UTC timestamps, Photon GeoJSON features -- and one client. Every
provider string that reaches the page goes through clean_text() here first, so
the frontend has one kind of input to treat as untrusted text, not three.

Geometry is held as (lat, lng) tuples, which is what scoring (Phase 3) walks,
and written as GeoJSON [lng, lat] only at the edge, in to_dict().
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

# Six decimal places is ~0.1 m: finer than any provider's own precision, and
# the response stays a third smaller than with repr() floats.
_COORD_DP = 6

# C0/C1 controls and the Unicode line/paragraph separators. A provider string
# with these in it is either broken or hostile; neither belongs on the page.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_SPACES = re.compile(r"\s+")


def clean_text(value: Any, max_len: int = 160) -> str | None:
    """A provider string made safe to show: str, no control characters,
    whitespace collapsed, bounded length. None for anything empty."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = _SPACES.sub(" ", _CONTROL.sub(" ", str(value))).strip()
    if not text:
        return None
    return text[:max_len].rstrip()


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _line(points: list[tuple[float, float]]) -> dict[str, Any]:
    return {
        "type": "LineString",
        "coordinates": [[round(lng, _COORD_DP), round(lat, _COORD_DP)] for lat, lng in points],
    }


@dataclass(frozen=True)
class Point:
    lat: float
    lng: float

    def rounded(self, dp: int) -> tuple[float, float]:
        """For cache keys: two requests a few metres apart share an entry."""
        return (round(self.lat, dp), round(self.lng, dp))

    def to_dict(self) -> dict[str, float]:
        return {"lat": round(self.lat, _COORD_DP), "lng": round(self.lng, _COORD_DP)}


@dataclass
class Step:
    """One turn-by-turn instruction (a Valhalla maneuver).

    begin/end index into the owning Route's geometry, so the page can
    highlight the stretch a step covers without a second geometry.
    """

    instruction: str
    distance_m: float
    duration_s: float
    street_names: list[str]
    maneuver_type: int | None
    begin_index: int
    end_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "instruction": self.instruction,
            "distance_m": round(self.distance_m, 1),
            "duration_s": round(self.duration_s, 1),
            "street_names": self.street_names,
            "maneuver_type": self.maneuver_type,
            "begin_index": self.begin_index,
            "end_index": self.end_index,
        }


@dataclass
class TransitStop:
    name: str | None
    point: Point
    stop_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # stop_id stays server-side: it is the dedupe key, and of no use to
        # the page.
        return {"name": self.name, **self.point.to_dict()}


@dataclass
class TransitLeg:
    """One leg of a transit itinerary: a walk, or a ride on one vehicle.

    `transit` is False for the street modes (WALK, BIKE, CAR, RENTAL). Only
    those are scored (Phase 3); time on board is not, and the response says so.
    """

    mode: str
    transit: bool
    start_time: str | None
    end_time: str | None
    duration_s: float
    distance_m: float | None
    origin: TransitStop
    destination: TransitStop
    geometry: list[tuple[float, float]]
    route_name: str | None = None
    route_long_name: str | None = None
    headsign: str | None = None
    agency: str | None = None
    color: str | None = None
    text_color: str | None = None
    num_stops: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "transit": self.transit,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_s": round(self.duration_s, 1),
            "distance_m": None if self.distance_m is None else round(self.distance_m, 1),
            "from": self.origin.to_dict(),
            "to": self.destination.to_dict(),
            "geometry": _line(self.geometry),
            "route_name": self.route_name,
            "route_long_name": self.route_long_name,
            "headsign": self.headsign,
            "agency": self.agency,
            "color": self.color,
            "text_color": self.text_color,
            "num_stops": self.num_stops,
        }


@dataclass
class Route:
    """One candidate route in any mode.

    `legs` is filled for transit only; `steps` for the street modes. For
    transit, `geometry` is every leg's geometry joined, and `also_departs`
    lists the later trips over the same stops that were folded into this one.
    """

    mode: str
    duration_s: float
    distance_m: float
    geometry: list[tuple[float, float]]
    steps: list[Step] = field(default_factory=list)
    legs: list[TransitLeg] = field(default_factory=list)
    start_time: str | None = None
    end_time: str | None = None
    transfers: int | None = None
    also_departs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "duration_s": round(self.duration_s, 1),
            "distance_m": round(self.distance_m, 1),
            "geometry": _line(self.geometry),
            "steps": [s.to_dict() for s in self.steps],
            "legs": [leg.to_dict() for leg in self.legs],
            "start_time": self.start_time,
            "end_time": self.end_time,
            "transfers": self.transfers,
            "also_departs": self.also_departs,
        }


@dataclass
class Place:
    """A geocoder result. `title` is the name or address line, `subtitle` the
    rest; `label` is both, joined, and is what results are deduplicated on."""

    label: str
    title: str
    subtitle: str
    point: Point
    kind: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "title": self.title,
            "subtitle": self.subtitle,
            **self.point.to_dict(),
            "kind": self.kind,
        }


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle metres between two (lat, lng) points; same constant
    (Earth diameter 12742 km) as distanceKm in web/app.js."""
    rad = math.pi / 180
    dlat = (b[0] - a[0]) * rad
    dlng = (b[1] - a[1]) * rad
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(a[0] * rad) * math.cos(b[0] * rad) * math.sin(dlng / 2) ** 2
    )
    return 12_742_000 * math.asin(min(1.0, math.sqrt(h)))


def line_length_m(points: list[tuple[float, float]]) -> float:
    return sum(haversine_m(points[i - 1], points[i]) for i in range(1, len(points)))
