"""Bucketing a source's free-text location field (design doc S6, S7.5).

S7.5 is explicit that this is the *lower-confidence* standardization problem and
should be treated differently from the offense crosswalk: keyword matching
against each city's free-text values is adequate, and it does not need a
maintained, versioned reference table the way offense codes do. This module is
that keyword matching, shared rather than duplicated per adapter.

Philadelphia does not publish a location field at all, which is why this arrives
with the second city rather than the first. Chicago's `location_description`
carries about 200 distinct values, Seattle and Austin publish NIBRS location
codes with their own vocabularies, and DC publishes almost nothing -- so the
rules below are ordered by specificity and every adapter is free to pre-map its
own coded vocabulary before falling back here.

Two things substring matching gets wrong unless they are handled explicitly, and
both were found by testing rather than by reading:

* **Negation.** "PARKING LOT / GARAGE (NON RESIDENTIAL)" contains "RESIDEN", and
  "VEHICLE NON-COMMERCIAL" contains "COMMERCIAL". Both matched the exact bucket
  they are declaring themselves *not* to be. Negated phrases are therefore
  stripped before any rule runs, which also leaves the affirmative variants
  ("PARKING LOT / GARAGE (RESIDENTIAL)") matching correctly.
* **Keywords that are substrings of unrelated words.** "STATION" made "GAS
  STATION" and "FIRE STATION" transit; " BUS" made "SMALL BUSINESS" transit.
  Broad tokens like these are out, and the specific compounds are in instead.

Ordering still carries the rest: transit before street and residential, so a
rail platform's "PLATFORM" and a CTA lot's "PARKING" do not win; school before
commercial, so a campus bookstore is a school.

Anything unmatched is 'unknown', deliberately. A wrong bucket is worse than an
absent one: this field is displayed as fact and nothing downstream can detect a
misclassification.
"""

from __future__ import annotations

# The standardized buckets, from S6's list. Adapters should not invent others --
# the API and any future filter UI are written against exactly this set.
BUCKETS = (
    "residential",
    "commercial",
    "street_public_way",
    "transit",
    "school",
    "park_recreation",
    "other",
    "unknown",
)

# Ordered most specific first; the first bucket with a matching keyword wins.
# Substring matching on an uppercased string, so entries are fragments rather
# than whole values -- the sources publish too many variants to enumerate.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Transit before street and before parking: a rail platform is transit even
    # though its value often contains "PLATFORM" or "PARKING".
    #
    # No bare "STATION" or "BUS": the first made "GAS STATION" and "FIRE
    # STATION" transit, the second made "SMALL BUSINESS" transit.
    (
        "transit",
        (
            "CTA", "TRANSIT", "SUBWAY", "TRAIN", "RAILROAD", "RAIL ", "PLATFORM",
            "BUS STOP", "BUS SHELTER", "BUS TERMINAL", "BUSWAY", "AIRPORT",
            "AIRCRAFT", "TAXI", "FERRY", "TERMINAL",
        ),
    ),
    # School before commercial: a campus bookstore is a school location.
    (
        "school",
        ("SCHOOL", "COLLEGE", "UNIVERSITY", "CAMPUS", "DAY CARE", "DAYCARE"),
    ),
    (
        "park_recreation",
        (
            "PARK PROPERTY", "PARK,", "PLAYGROUND", "ATHLETIC", "STADIUM",
            "POOL", "BEACH", "FOREST", "RIVER", "LAKE", "CEMETERY", "GOLF",
        ),
    ),
    # Residential before commercial and before street: "PARKING LOT / GARAGE
    # (RESIDENTIAL)" and "PORCH" belong here.
    (
        "residential",
        (
            "RESIDEN", "APARTMENT", "HOUSE", "DWELLING", "PORCH", "YARD",
            "DRIVEWAY", "HOTEL", "MOTEL", "NURSING", "DORM", "TRAILER",
        ),
    ),
    (
        "commercial",
        (
            "STORE", "SHOP", "RESTAURANT", "TAVERN", "BAR ", "BANK", "OFFICE",
            "COMMERCIAL", "RETAIL", "GROCERY", "GAS STATION", "WAREHOUSE",
            "FACTORY", "BUSINESS", "CLUB", "THEATER", "THEATRE", "BARBER",
            "SALON", "LAUNDRY", "CURRENCY EXCHANGE", "ATM", "PAWN",
        ),
    ),
    # "VACANT" and "ABANDONED" sit here rather than in `other` because they have
    # to beat the street rule: "VACANT LOT" is not a public way.
    ("other", ("VACANT", "ABANDONED", "CONSTRUCTION")),
    (
        "street_public_way",
        (
            "STREET", "SIDEWALK", "ALLEY", "HIGHWAY", "EXPRESSWAY", "ROAD",
            "BRIDGE", "PARKING", "GARAGE", "VEHICLE", "AUTO",
        ),
    ),
    (
        "other",
        (
            "GOVERNMENT", "HOSPITAL", "MEDICAL", "CHURCH", "SYNAGOGUE", "MOSQUE",
            "WORSHIP", "JAIL", "PRISON", "POLICE", "FIRE STATION", "LIBRARY",
            "OTHER",
        ),
    ),
)

# Phrases that declare what a value is *not*, removed before any rule runs.
# Without this, substring matching reads them as affirmative.
_NEGATIONS = (
    "NON RESIDENTIAL", "NON-RESIDENTIAL", "NONRESIDENTIAL",
    "NON COMMERCIAL", "NON-COMMERCIAL", "NONCOMMERCIAL",
    "NON RESIDENCE", "NON-RESIDENCE",
)


def bucket(raw: str | None) -> str:
    """Map one source's free-text location value onto a standardized bucket.

    Returns 'unknown' for an empty value or one no rule matches, which is a
    legitimate and common outcome -- see the module docstring on why guessing is
    the worse failure.
    """
    if raw is None:
        return "unknown"
    text = raw.strip().upper()
    if not text:
        return "unknown"

    for phrase in _NEGATIONS:
        text = text.replace(phrase, " ")

    for name, keywords in _RULES:
        if any(keyword in text for keyword in keywords):
            return name
    return "unknown"


# The cases that shaped the rules above, kept runnable so a future edit to the
# keyword lists cannot quietly undo them:
#
#     python -m safety.etl.location
#
# A self-check rather than a test file, because this project carries no test
# framework and one keyword table is not a reason to add one. Every entry here
# is a real published value from one of the six sources.
_CASES: tuple[tuple[str | None, str], ...] = (
    ("STREET", "street_public_way"),
    ("SIDEWALK", "street_public_way"),
    ("ALLEY", "street_public_way"),
    ("RESIDENCE", "residential"),
    ("APARTMENT", "residential"),
    ("RESIDENCE - PORCH / HALLWAY", "residential"),
    ("DRIVEWAY - RESIDENTIAL", "residential"),
    ("NURSING HOME", "residential"),
    # The negation cases. Both of these matched the opposite bucket before
    # _NEGATIONS existed.
    ("PARKING LOT / GARAGE (RESIDENTIAL)", "residential"),
    ("PARKING LOT / GARAGE (NON RESIDENTIAL)", "street_public_way"),
    ("VEHICLE NON-COMMERCIAL", "street_public_way"),
    # Transit has to beat both parking and street.
    ("CTA PARKING LOT / GARAGE (NON RESIDENTIAL)", "transit"),
    ("CTA TRAIN", "transit"),
    ("CTA PLATFORM", "transit"),
    ("CTA BUS", "transit"),
    ("BUS STOP", "transit"),
    ("AIRPORT TERMINAL LOWER LEVEL - SECURE AREA", "transit"),
    # The over-broad-keyword cases: "STATION" and " BUS" used to catch these.
    ("GAS STATION", "commercial"),
    ("FIRE STATION", "other"),
    ("SMALL BUSINESS", "commercial"),
    ("SMALL RETAIL STORE", "commercial"),
    ("GROCERY FOOD STORE", "commercial"),
    ("BAR OR TAVERN", "commercial"),
    ("SCHOOL, PUBLIC, BUILDING", "school"),
    ("PARK PROPERTY", "park_recreation"),
    ("SPORTS ARENA/STADIUM", "park_recreation"),
    ("HOSPITAL BUILDING/GROUNDS", "other"),
    ("CHURCH/SYNAGOGUE/PLACE OF WORSHIP", "other"),
    # "VACANT LOT" has to beat the street rule.
    ("VACANT LOT", "other"),
    ("CONSTRUCTION SITE", "other"),
    (None, "unknown"),
    ("", "unknown"),
    ("   ", "unknown"),
)


def _self_check() -> int:
    failures = [
        (value, want, bucket(value)) for value, want in _CASES if bucket(value) != want
    ]
    for value, want, got in failures:
        print(f"FAIL {value!r}: wanted {want}, got {got}")
    print(f"{len(_CASES) - len(failures)}/{len(_CASES)} location cases pass")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
