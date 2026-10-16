"""Write reference/crosswalk/austin_v1.csv from APD's own NIBRS classification.

    python scripts/build_austin_crosswalk.py > reference/crosswalk/austin_v1.csv

The CrimeViewer services the Austin adapter reads publish APD's local four-digit
offence code and its description ("0601", "BURGLARY OF VEHICLE"), not NIBRS.
APD's open-data NIBRS dataset (`i7fg-wrk5`) has no usable location, but it pairs
the same description (`nibrs_offense_code_and_extension_description`) with the
NIBRS offence APD filed it under (`nibrs_desc`, "23F Theft: from Motor Vehicle
(BOV)"). Every description there maps to exactly one NIBRS code, so the mapping
for those rows is APD's own and is marked `exact`. That pairing is the opposite
of what the field names suggest -- the "nibrs_offense_code..." column holds
APD's text and the NIBRS code is the leading token of `nibrs_desc`.

Rows, per APD code seen in the services:

1. An exact (code, description) row for every description APD's pairing
   covers, or that `_TEXT_OVERRIDES` maps by hand.
2. A code-only `*` row, consulted when the description is new or spelled
   differently ("ASSAULT W/INJURY-FAM/DATE VIOL" is the services' truncation of
   a description the pairing knows in full). It takes the code's `_CODE_OVERRIDES`
   entry if there is one, otherwise the NIBRS code most of the code's paired
   descriptions map to, and is marked `approximate`.

A code with neither is a hard error: it needs a line in `_CODE_OVERRIDES`, read
by a person, before the crosswalk can be written. The codes in the overrides are
mostly NIBRS Group B -- DWI, public intoxication, disorderly conduct -- which
`i7fg-wrk5` does not cover because it is Group A only.

Codes the adapter does not promote (calls that are not offences) are skipped;
the list is the adapter's own, so the two cannot disagree.

Categories, buckets and UCR parts come from `_NIBRS` in build_nibrs_crosswalk.py,
so Austin cannot drift from Seattle and Los Angeles on, say, whether robbery is
violent.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from build_nibrs_crosswalk import _FIELDNAMES, _NIBRS

# Run as `python scripts/build_austin_crosswalk.py`, so the repo root is not on
# sys.path; the adapter is the single source of the service URLs and the
# not-an-offence list.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safety.etl.adapters.austin import (  # noqa: E402
    _LEGACY_LAYERS,
    _NEW_LAYERS,
    _NOT_AN_OFFENSE,
    LEGACY_URL,
)

NEW_URL = (
    "https://maps.austintexas.gov/arcgis/rest/services/"
    "CrimeViewer_new/APD_Reported_Crimes_new/FeatureServer"
)
NIBRS_PAIRING = "https://data.austintexas.gov/resource/i7fg-wrk5.json"

CROSSWALK_VERSION = "aus_v1"
SOURCE_ID = "aus"
# The CrimeViewer services begin in late September 2021.
EFFECTIVE_FROM = "2021-09-01"

# Code-only mappings, read by hand. Used for codes APD's pairing does not cover
# and where a code's paired descriptions do not settle the fallback.
_CODE_OVERRIDES: dict[str, tuple[str, str]] = {
    # Paired descriptions split evenly between 09A and 09B.
    "0102": ("09B", "Manslaughter; the paired descriptions split, so set by hand."),
    "0702": ("240", "Unauthorized use of a vehicle: Texas joyriding, NIBRS motor vehicle theft."),
    "0904": ("90Z", "Reckless conduct (Texas deadly conduct, no injury); no Group A target."),
    "1100": ("90A", "Issuance of a bad check."),
    "1101": ("90A", "Theft by check: NIBRS files bad checks under 90A, not larceny."),
    "1705": ("90Z", "Public lewdness: FBI guidance files indecent exposure under 90Z."),
    "2001": ("90F", "Interference with child custody: nonviolent family offence."),
    "2003": ("90F", "Criminal nonsupport: nonviolent family offence."),
    # DWI and DUI variants.
    "2100": ("90D", "DWI."),
    "2102": ("90D", "DWI, second offence."),
    "2103": ("90D", "DWI, drug recognition expert."),
    "2106": ("90D", "DUI under 21."),
    "2107": ("90D", "DUI, age 17 to 20."),
    "2108": ("90D", "DWI with a child passenger."),
    "2109": ("90D", "Felony DWI."),
    "2110": ("90D", "Boating while intoxicated."),
    "2111": ("90D", "DWI, 0.15 BAC or above."),
    # Intoxication assault is Texas's serious-bodily-injury DWI. NIBRS has no
    # DUI-with-injury code; the injury makes it aggravated assault, not 90D.
    "2105": ("13A", "Intoxication assault (DWI causing serious bodily injury)."),
    # Liquor law.
    "2200": ("90G", "Possession of alcohol by a minor."),
    "2202": ("90G", "Sale of liquor without a permit."),
    "2206": ("90G", "Delivery of alcohol to a minor."),
    "2208": ("90G", "Liquor law violation, other."),
    "2209": ("90G", "Possession of alcohol, age 17 to 20."),
    "2210": ("90G", "Misrepresentation of age by a minor."),
    "3211": ("90G", "City ordinance: alcohol consumption."),
    "2300": ("90E", "Public intoxication."),
    # Texas disorderly conduct (Penal Code 42.01). The firearm subsections are
    # weapon law violations; exposure and peeping have their own NIBRS homes.
    "2401": ("90C", "Disorderly conduct: abusive language."),
    "2402": ("90C", "Disorderly conduct: offensive gesture."),
    "2403": ("90C", "Disorderly conduct: noxious odour."),
    "2404": ("90C", "Disorderly conduct: abuse or threat in public."),
    "2405": ("90C", "Disorderly conduct: unreasonable noise."),
    "2407": ("90H", "Disorderly conduct: window peeping / voyeurism."),
    "2417": ("90H", "Disorderly conduct: window peeping, hotel."),
    "2408": ("520", "Disorderly conduct: discharging a firearm in a public place."),
    "2409": ("520", "Disorderly conduct: displaying a firearm or deadly weapon in public."),
    "2410": ("520", "Disorderly conduct: discharging a firearm across a public road."),
    "2411": ("90Z", "Disorderly conduct: exposure. Indecent exposure is 90Z."),
    "2416": ("90C", "Riot."),
    "3213": ("90B", "City ordinance: curfew."),
    "3303": ("90C", "Disruptive acts at a school."),
    "3305": ("90C", "Disruption of classes."),
}

# (code, description) pairs whose code-only fallback would be wrong.
_TEXT_OVERRIDES: dict[tuple[str, str], tuple[str, str]] = {
    # The 0902 fallback is simple assault; the sexual-contact variant is fondling,
    # as APD files the same offence elsewhere in its pairing.
    ("0902", "ASSAULT  CONTACT-SEXUAL NATURE"): (
        "11D", "Assault by contact of a sexual nature: fondling, not simple assault."
    ),
    # The services' truncation of "BURGLARY OF RESIDENCE-FAM/DATING VIO", which
    # APD files as the assault (13B), not the burglary the 0500 fallback gives.
    ("0500", "BURG OF RES - FAM/DATING ASLT"): (
        "13B", "Truncated form of a description APD files as simple assault (13B)."
    ),
}


def _get(url: str, params: dict[str, str]) -> Any:
    response = httpx.get(url, params=params, timeout=120.0, follow_redirects=True)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, dict) and "error" in body:
        raise SystemExit(f"esri error from {url}: {body['error']}")
    return body


def fetch_service_pairs() -> collections.Counter[tuple[str, str]]:
    """(APD code, description) -> record count, across every layer the adapter reads."""
    stats = json.dumps(
        [{"statisticType": "count", "onStatisticField": "OBJECTID", "outStatisticFieldName": "n"}]
    )
    pairs: collections.Counter[tuple[str, str]] = collections.Counter()
    for service, wanted in ((NEW_URL, _NEW_LAYERS), (LEGACY_URL, _LEGACY_LAYERS)):
        layers = {
            layer["name"].strip(): layer["id"]
            for layer in _get(service, {"f": "json"}).get("layers", [])
        }
        for name in wanted:
            if name not in layers:
                raise SystemExit(f"layer {name!r} not found at {service}")
            body = _get(
                f"{service}/{layers[name]}/query",
                {
                    "where": "1=1",
                    "groupByFieldsForStatistics": "PRIMARY_OFFENSE_CODE,CRIME_DESCRIPTION",
                    "outStatistics": stats,
                    "f": "json",
                },
            )
            for feature in body.get("features", []):
                attrs = {k.lower(): v for k, v in feature["attributes"].items()}
                code = (attrs.get("primary_offense_code") or "").strip()
                text = (attrs.get("crime_description") or "").strip()
                if code:
                    pairs[(code, text)] += int(attrs["n"])
    return pairs


def fetch_nibrs_pairing() -> dict[str, str]:
    """APD description (uppercased) -> NIBRS code, from APD's NIBRS dataset."""
    rows = _get(
        NIBRS_PAIRING,
        {
            "$select": "nibrs_offense_code_and_extension_description,nibrs_desc,count(*)",
            "$group": "nibrs_offense_code_and_extension_description,nibrs_desc",
            "$limit": "5000",
        },
    )
    pairing: dict[str, str] = {}
    for row in rows:
        text = (row.get("nibrs_offense_code_and_extension_description") or "").strip().upper()
        nibrs = (row.get("nibrs_desc") or "").split(" ", 1)[0].strip()
        if not text or not nibrs:
            continue
        if pairing.get(text, nibrs) != nibrs:
            raise SystemExit(
                f"i7fg-wrk5 maps {text!r} to both {pairing[text]} and {nibrs}; "
                "the description is no longer a one-to-one key"
            )
        pairing[text] = nibrs
    if not pairing:
        raise SystemExit("i7fg-wrk5 returned no description/NIBRS pairs")
    return pairing


def _row(code: str, text: str, nibrs: str, confidence: str, note: str) -> dict[str, str]:
    if nibrs not in _NIBRS:
        raise SystemExit(f"{code} {text!r}: NIBRS code {nibrs} is not in _NIBRS")
    name, group, against, ucr, bucket, product = _NIBRS[nibrs]
    return {
        "crosswalk_version": CROSSWALK_VERSION, "source_id": SOURCE_ID,
        "raw_offense_code": code, "raw_offense_text": text,
        "raw_source_category": "", "nibrs_code": nibrs,
        "nibrs_offense_name": name, "nibrs_group": group,
        "nibrs_crime_against": against, "ucr_part": ucr,
        "severity_bucket": bucket, "product_category": product,
        "mapping_confidence": confidence, "effective_from": EFFECTIVE_FROM,
        "effective_to": "", "notes": note,
    }


def build_rows(
    pairs: collections.Counter[tuple[str, str]], pairing: dict[str, str]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    paired_by_code: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    codes: set[str] = set()

    for (code, text), count in sorted(pairs.items()):
        if code in _NOT_AN_OFFENSE:
            continue
        codes.add(code)
        if (code, text) in _TEXT_OVERRIDES:
            nibrs, note = _TEXT_OVERRIDES[(code, text)]
            rows.append(_row(code, text, nibrs, "approximate", note))
            continue
        nibrs = pairing.get(text.upper())
        if nibrs is not None:
            paired_by_code[code][nibrs] += count
            rows.append(_row(code, text, nibrs, "exact", "APD's own NIBRS classification (i7fg-wrk5)."))

    unresolved: list[str] = []
    for code in sorted(codes):
        if code in _CODE_OVERRIDES:
            nibrs, note = _CODE_OVERRIDES[code]
            rows.append(_row(code, "*", nibrs, "approximate", note))
        elif paired_by_code[code]:
            (nibrs, top), *rest = paired_by_code[code].most_common()
            note = "Fallback: the NIBRS code most of this code's paired descriptions map to."
            if rest:
                note += " Others: " + ", ".join(f"{n} ({c})" for n, c in rest) + f"; {nibrs} ({top})."
            rows.append(_row(code, "*", nibrs, "approximate", note))
        else:
            examples = [t for (c, t) in pairs if c == code][:3]
            unresolved.append(f"{code} {examples}")

    if unresolved:
        raise SystemExit(
            "APD code(s) with no paired description and no _CODE_OVERRIDES entry; "
            "map them by hand:\n  " + "\n  ".join(unresolved)
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    pairs = fetch_service_pairs()
    pairing = fetch_nibrs_pairing()
    rows = build_rows(pairs, pairing)

    writer = csv.DictWriter(sys.stdout, fieldnames=_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)

    total = sum(n for (c, _), n in pairs.items() if c not in _NOT_AN_OFFENSE)
    exact = sum(
        n for (c, t), n in pairs.items()
        if c not in _NOT_AN_OFFENSE and (c, t) not in _TEXT_OVERRIDES and t.upper() in pairing
    )
    print(
        f"\n{len(rows)} row(s) for {len({r['raw_offense_code'] for r in rows})} APD code(s); "
        f"{exact / total:.1%} of records match APD's own NIBRS pairing exactly, "
        "the rest fall to a code-only row.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
