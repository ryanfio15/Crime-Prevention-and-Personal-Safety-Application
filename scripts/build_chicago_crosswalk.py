"""Draft reference/crosswalk/chicago_v1.csv from Chicago's own IUCR code list.

    python scripts/build_chicago_crosswalk.py > reference/crosswalk/chicago_v1.csv

Philadelphia's crosswalk is 30 hand-written rows, which is the right way to do 30
rows. Chicago publishes an IUCR list an order of magnitude longer, and hand-typing
it would introduce transcription errors into the one table that decides what every
incident in the city is classified as. So the codes and their descriptions come
from the source's own published list, fetched here, and are never retyped.

**What this script does not do is finish the job.** It proposes a NIBRS mapping
for each code from the rules in `_RULES` below, keyed on IUCR's primary
description and its index flag. Those rules are a first pass. Every row lands
with `mapping_confidence` of `approximate` or `ambiguous` and is meant to be read
by a person before it is trusted -- which is exactly what S7.2 asks for by making
the crosswalk a maintained reference dataset rather than application code, and
what S7.4 protects by keeping the raw code alongside the mapped one.

Review order, highest value first:

1. Anything still `ambiguous`: no rule claimed it.
2. Everything mapped to `violent`, since that is the track the product leads with.
3. The `quality_of_life` bucket, because S13 singles out over-enforced offense
   types and this is where they land.

IUCR is an Illinois state standard, not NIBRS, and the two do not line up
one-to-one -- IUCR splits some NIBRS offenses by degree and merges others. Where
a precise NIBRS target is not available the coarse UCR Part I/II bucket carries
the record instead, which is the fallback S7.2 describes.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from typing import Any

import httpx

# Chicago's published IUCR code list, on the same Socrata portal as the incident
# data. Overridable, because a dataset identifier is exactly the kind of thing
# that changes without notice.
DEFAULT_DATASET = "c7ck-438e"
DEFAULT_BASE = "https://data.cityofchicago.org/resource"

CROSSWALK_VERSION = "chi_v1"
SOURCE_ID = "chi"
# IUCR has been the published classification for the whole span of this dataset,
# which starts in 2001. No RMS break to version around, unlike Seattle and LA.
EFFECTIVE_FROM = "2001-01-01"

# (required keywords, nibrs_code, nibrs_name, group, crime_against,
#  product_category, severity_bucket)
#
# A rule fires when *every* keyword in its tuple appears in the combined
# "PRIMARY / SECONDARY" description, and the first firing rule wins -- so the
# order below is the mapping, not just a list.
#
# Both halves are read, and that is not a detail. IUCR files aggravated battery
# under primary description "BATTERY" with the aggravation in the secondary, so a
# rule reading only the primary maps it to simple assault (13B) instead of
# aggravated assault (13A). The severity scale scores those 6.17 and 8.50, and
# 17.76 for the firearm variant, so the error would flow straight into the
# ranking. The same applies to theft: IUCR's subtypes -- pocket-picking, retail
# theft, theft from building -- live entirely in the secondary description.
_RULES: tuple[tuple[tuple[str, ...], str, str, str, str, str, str], ...] = (
    # Homicide. Reckless and involuntary variants are negligent manslaughter,
    # which NIBRS separates from murder, so they precede the general rule.
    (("RECKLESS", "HOMICIDE"), "09B", "Negligent Manslaughter", "A", "person", "violent", "part_i_violent"),
    (("INVOLUNTARY", "MANSLAUGHTER"), "09B", "Negligent Manslaughter", "A", "person", "violent", "part_i_violent"),
    (("HOMICIDE",), "09A", "Murder and Nonnegligent Manslaughter", "A", "person", "violent", "part_i_violent"),
    # Sexual offenses before the assault rules: "AGG CRIMINAL SEXUAL ASSAULT"
    # contains both "AGG" and "ASSAULT" and must not be read as an aggravated
    # assault.
    (("SEXUAL ASSAULT",), "11A", "Rape", "A", "person", "violent", "part_i_violent"),
    (("SEXUAL ABUSE",), "11D", "Fondling", "A", "person", "violent", "part_ii"),
    # NIBRS files robbery under Crimes Against Property; product_category follows
    # UCR so a personal-safety product presents it as violent. Same call, and the
    # same reasoning, as the Philadelphia crosswalk.
    (("ROBBERY",), "120", "Robbery", "A", "property", "violent", "part_i_violent"),
    (("KIDNAPPING",), "100", "Kidnapping/Abduction", "A", "person", "violent", "part_i_violent"),
    (("ABDUCTION",), "100", "Kidnapping/Abduction", "A", "person", "violent", "part_i_violent"),
    (("HUMAN TRAFFICKING",), "64A", "Human Trafficking, Commercial Sex Acts", "A", "person", "violent", "part_i_violent"),
    # Aggravated before simple, for both of IUCR's spellings.
    (("AGGRAVATED", "BATTERY"), "13A", "Aggravated Assault", "A", "person", "violent", "part_i_violent"),
    (("AGG", "BATTERY"), "13A", "Aggravated Assault", "A", "person", "violent", "part_i_violent"),
    (("AGGRAVATED", "ASSAULT"), "13A", "Aggravated Assault", "A", "person", "violent", "part_i_violent"),
    (("AGG", "ASSAULT"), "13A", "Aggravated Assault", "A", "person", "violent", "part_i_violent"),
    (("BATTERY",), "13B", "Simple Assault", "A", "person", "violent", "part_ii"),
    (("ASSAULT",), "13B", "Simple Assault", "A", "person", "violent", "part_ii"),
    (("INTIMIDATION",), "13C", "Intimidation", "A", "person", "violent", "part_ii"),
    (("STALKING",), "13C", "Intimidation", "A", "person", "violent", "part_ii"),
    (("EXTORTION",), "210", "Extortion/Blackmail", "A", "property", "property", "part_ii"),
    (("BURGLARY",), "220", "Burglary/Breaking and Entering", "A", "property", "property", "part_i_property"),
    # Vehicle theft before theft, or every stolen car becomes "all other larceny".
    (("MOTOR VEHICLE THEFT",), "240", "Motor Vehicle Theft", "A", "property", "property", "part_i_property"),
    # IUCR's theft subtypes, all of which live in the secondary description.
    (("POCKET-PICKING",), "23A", "Pocket-picking", "A", "property", "property", "part_i_property"),
    (("POCKET PICKING",), "23A", "Pocket-picking", "A", "property", "property", "part_i_property"),
    (("PURSE-SNATCHING",), "23B", "Purse-snatching", "A", "property", "property", "part_i_property"),
    (("PURSE SNATCHING",), "23B", "Purse-snatching", "A", "property", "property", "part_i_property"),
    (("RETAIL THEFT",), "23C", "Shoplifting", "A", "property", "property", "part_i_property"),
    (("SHOPLIFT",), "23C", "Shoplifting", "A", "property", "property", "part_i_property"),
    (("COIN-OP",), "23E", "Theft From Coin-Operated Machine or Device", "A", "property", "property", "part_i_property"),
    (("COIN OPERATED",), "23E", "Theft From Coin-Operated Machine or Device", "A", "property", "property", "part_i_property"),
    (("THEFT", "FROM BUILDING"), "23D", "Theft From Building", "A", "property", "property", "part_i_property"),
    (("THEFT",), "23H", "All Other Larceny", "A", "property", "property", "part_i_property"),
    (("ARSON",), "200", "Arson", "A", "property", "property", "part_i_property"),
    (("CRIMINAL DAMAGE",), "290", "Destruction/Damage/Vandalism of Property", "A", "property", "property", "part_ii"),
    (("CRIMINAL DEFACEMENT",), "290", "Destruction/Damage/Vandalism of Property", "A", "property", "property", "part_ii"),
    (("CRIMINAL TRESPASS",), "90J", "Trespass of Real Property", "B", "group_b", "quality_of_life", "part_ii"),
    (("FORGERY",), "250", "Counterfeiting/Forgery", "A", "property", "property", "part_ii"),
    (("COUNTERFEIT",), "250", "Counterfeiting/Forgery", "A", "property", "property", "part_ii"),
    (("EMBEZZL",), "270", "Embezzlement", "A", "property", "property", "part_ii"),
    (("STOLEN PROPERTY",), "280", "Stolen Property Offenses", "A", "property", "property", "part_ii"),
    (("DECEPTIVE PRACTICE",), "26A", "False Pretenses/Swindle/Confidence Game", "A", "property", "property", "part_ii"),
    # Enforcement-driven categories; see S13 on over-enforced offense types.
    (("NARCOTIC",), "35A", "Drug/Narcotic Violations", "A", "society", "quality_of_life", "part_ii"),
    (("CANNABIS",), "35A", "Drug/Narcotic Violations", "A", "society", "quality_of_life", "part_ii"),
    # "other", not "violent": the bucket is dominated by possession and carry
    # offenses, and violence involving a weapon is already counted under
    # homicide, assault and robbery. Same call as the Philadelphia crosswalk.
    (("WEAPON",), "520", "Weapon Law Violations", "A", "society", "other", "part_ii"),
    (("CONCEALED CARRY",), "520", "Weapon Law Violations", "A", "society", "other", "part_ii"),
    (("PROSTITUTION",), "40A", "Prostitution", "A", "society", "quality_of_life", "part_ii"),
    (("SEX OFFENSE",), "11D", "Fondling", "A", "person", "violent", "part_ii"),
    (("GAMBLING",), "39A", "Betting/Wagering", "A", "society", "quality_of_life", "part_ii"),
    (("LIQUOR",), "90G", "Liquor Law Violations", "B", "group_b", "quality_of_life", "part_ii"),
    (("OBSCENITY",), "370", "Pornography/Obscene Material", "A", "society", "other", "part_ii"),
    (("PORNOGRAPH",), "370", "Pornography/Obscene Material", "A", "society", "other", "part_ii"),
    (("PUBLIC PEACE",), "90C", "Disorderly Conduct", "B", "group_b", "quality_of_life", "part_ii"),
    (("PUBLIC INDECENCY",), "90Z", "All Other Offenses", "B", "group_b", "quality_of_life", "part_ii"),
    (("INTERFERENCE WITH PUBLIC OFFICER",), "90Z", "All Other Offenses", "B", "group_b", "other", "part_ii"),
    (("OFFENSE INVOLVING CHILDREN",), "90F", "Family Offenses, Nonviolent", "B", "group_b", "other", "part_ii"),
    # NIBRS added Animal Cruelty as a Group A offense in 2016, so this has a real
    # target rather than a residual one. Covers IUCR's animal abuse/neglect and
    # animal fighting codes, which sit under the "OTHER OFFENSE" primary.
    (("ANIMAL",), "720", "Animal Cruelty", "A", "society", "other", "part_ii"),
    # IUCR files ritual mutilation under its own primary description, so the
    # aggravated-assault rules above miss it -- they key on BATTERY or ASSAULT.
    # An aggravated mutilation is an aggravated assault.
    (("RITUAL", "AGGRAVATED"), "13A", "Aggravated Assault", "A", "person", "violent", "part_i_violent"),
    (("RITUAL",), "13B", "Simple Assault", "A", "person", "violent", "part_ii"),
)

# Per-code corrections from the review pass, applied after the rules. Keyword
# rules get most of IUCR right, but a handful of codes are misread by them
# whatever the ordering: "AGG." does not contain "AGGRAVATED", a ritual
# mutilation with a weapon matches WEAPON before RITUAL, and SEX OFFENSE is a
# catch-all primary that files bigamy and adultery beside fondling. Keyed by
# IUCR code so a fix cannot spill onto a neighbouring description.
#
# (nibrs_code, nibrs_name, group, crime_against, product_category,
#  severity_bucket, reason)
_AGG_ASSAULT = ("13A", "Aggravated Assault", "A", "person", "violent", "part_i_violent")
_SIMPLE_ASSAULT = ("13B", "Simple Assault", "A", "person", "violent", "part_ii")
_INTIMIDATION = ("13C", "Intimidation", "A", "person", "violent", "part_ii")
_ALL_OTHER = ("90Z", "All Other Offenses", "B", "group_b", "other", "part_ii")
_PROSTITUTION = ("40A", "Prostitution", "A", "society", "quality_of_life", "part_ii")
_OVERRIDES: dict[str, tuple[tuple[str, ...], str]] = {
    "0493": (_AGG_ASSAULT, "Aggravated ritual mutilation is an aggravated assault; the WEAPON rule fired first."),
    "0510": (_AGG_ASSAULT, "'AGG.' abbreviation missed by the AGGRAVATED rule."),
    "3970": (("210", "Extortion/Blackmail", "A", "property", "property", "part_ii"),
             "Filed under INTIMIDATION by IUCR, but the offense is extortion."),
    "3200": (_AGG_ASSAULT, "Illinois armed violence: a felony committed while armed. Not disorderly conduct."),
    "3400": (("23H", "All Other Larceny", "A", "property", "property", "part_i_property"),
             "Looting is a theft, not a public-peace offense."),
    "1750": (_SIMPLE_ASSAULT, "Child abuse is a crime against the person; NIBRS files it as assault."),
    "2820": (_INTIMIDATION, "A threat by telephone is NIBRS intimidation."),
    "1504": (_PROSTITUTION, "Solicitation of a sexual act (720 ILCS 5/11-14.1) is a prostitution offense, not fondling."),
    "1570": (("90Z", "All Other Offenses", "B", "group_b", "quality_of_life", "part_ii"),
             "Public indecency, not fondling; same bucket as IUCR's own PUBLIC INDECENCY primary."),
    "1572": (_ALL_OTHER, "Adultery is a status offense with no victim of force."),
    "1574": (_ALL_OTHER, "Fornication is a status offense with no victim of force."),
    "1576": (_ALL_OTHER, "Bigamy is not a sex offense against a person."),
    "1578": (_ALL_OTHER, "Marrying a bigamist is not a sex offense against a person."),
    "1564": (_ALL_OTHER, "Criminal transmission of HIV is not fondling."),
    "1581": (_ALL_OTHER, "Non-consensual image dissemination has no NIBRS Group A target."),
    "4255": (("90F", "Family Offenses, Nonviolent", "B", "group_b", "other", "part_ii"),
             "Unlawful visitation interference is a custody dispute, not a kidnapping."),
}

# Codes no rule claims whose residual 90Z mapping was read and confirmed: court
# order, registration, licensing and administrative violations with no Group A
# equivalent. Listed so the review is recorded rather than re-flagged.
_CONFIRMED_RESIDUAL = frozenset({
    "1147", "2825", "2826", "2830", "3610", "4386", "4387", "4388", "4389",
    "4410", "4420", "4510", "4625", "4650", "4651", "4652", "4740", "4750",
    "4800", "4810", "5000", "5001", "5002", "5008", "5009", "500E", "500N",
    "5011", "5013", "501H", "502P", "502R", "502T", "5110", "5111", "5112",
    "5130", "5131", "5132", "9901",
})

REVIEW_NOTE = "Reviewed 2026-10-05."

# Fallback for a code no rule claims. Deliberately loud: `ambiguous` plus the
# residual NIBRS code, so `safety.etl.run weights` and the review pass both
# surface it. It is never dropped -- S8.5 is explicit that an unmapped code is
# flagged, not discarded.
_UNMAPPED = ("90Z", "All Other Offenses", "B", "group_b", "other", "part_ii")

_FIELDNAMES = (
    "crosswalk_version",
    "source_id",
    "raw_offense_code",
    "raw_offense_text",
    "raw_source_category",
    "nibrs_code",
    "nibrs_offense_name",
    "nibrs_group",
    "nibrs_crime_against",
    "ucr_part",
    "severity_bucket",
    "product_category",
    "mapping_confidence",
    "effective_from",
    "effective_to",
    "notes",
)


def fetch_iucr(base: str, dataset: str, token: str | None) -> list[dict[str, Any]]:
    """Fetch the published IUCR list. 400-odd rows; one request is plenty."""
    headers = {"X-App-Token": token} if token else {}
    response = httpx.get(
        f"{base}/{dataset}.csv",
        params={"$limit": "5000", "$order": "iucr"},
        headers=headers,
        timeout=60.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))
    if not rows:
        raise SystemExit(f"IUCR dataset {dataset} returned no rows")

    expected = {"iucr", "primary_description", "secondary_description"}
    missing = expected - set(rows[0])
    if missing:
        raise SystemExit(
            f"IUCR dataset {dataset} is missing {sorted(missing)}; got "
            f"{sorted(rows[0])}. The dataset shape has changed -- check the "
            "portal before trusting anything this script emits."
        )
    return rows


def _classify(primary: str, secondary: str) -> tuple[tuple[str, ...], bool]:
    """First rule whose every keyword appears in the combined description.

    Returns the mapping and whether a rule actually claimed it. The flag has to
    be reported rather than inferred by comparing the result against _UNMAPPED:
    the INTERFERENCE WITH PUBLIC OFFICER rule maps to exactly the residual
    values, so twelve codes that matched it perfectly well came out labelled
    "no rule matched -- NEEDS REVIEW". Any rule whose target happens to equal
    the fallback would have the same problem.
    """
    haystack = f"{primary} / {secondary}".upper()
    for keywords, *mapping in _RULES:
        if all(keyword in haystack for keyword in keywords):
            return tuple(mapping), True
    return _UNMAPPED, False


def build_rows(iucr: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in iucr:
        code = (row["iucr"] or "").strip()
        primary = (row["primary_description"] or "").strip()
        secondary = (row["secondary_description"] or "").strip()
        if not code or not secondary:
            continue

        mapping, claimed = _classify(primary, secondary)
        override_reason = ""
        if code in _OVERRIDES:
            mapping, override_reason = _OVERRIDES[code]
            claimed = True
        elif code in _CONFIRMED_RESIDUAL:
            claimed = True
            override_reason = "No Group A equivalent; residual 90Z confirmed."
        nibrs_code, nibrs_name, group, against, product, bucket = mapping
        # IUCR's own index flag is the source's judgement on Part I versus Part
        # II, which is a better signal than inferring one from the description.
        index_flag = (row.get("index_code") or "").strip().upper()
        ucr_part = "I" if index_flag == "I" else "II"

        # Reconcile the source's index flag against the rule's severity bucket,
        # one way only. IUCR calling a code an index offense outranks a rule that
        # guessed part_ii -- the source knows its own taxonomy. The reverse is not
        # true: a missing index flag does not demote a homicide, and codes are
        # flagged inconsistently enough upstream that trusting its absence would.
        conflict = ""
        if ucr_part == "I" and bucket == "part_ii":
            bucket = "part_i_violent" if product == "violent" else "part_i_property"
            conflict = (
                " IUCR flags this as an index offense, so the severity bucket was "
                "promoted from part_ii to {bucket}; confirm.".format(bucket=bucket)
            )

        unclaimed = not claimed
        out.append(
            {
                "crosswalk_version": CROSSWALK_VERSION,
                "source_id": SOURCE_ID,
                "raw_offense_code": code,
                # The incident feed publishes `description`, which is IUCR's
                # secondary description. The crosswalk key has to be that exact
                # value, not the primary one the rules above read.
                "raw_offense_text": secondary,
                "raw_source_category": primary,
                "nibrs_code": nibrs_code,
                "nibrs_offense_name": nibrs_name,
                "nibrs_group": group,
                "nibrs_crime_against": against,
                "ucr_part": ucr_part,
                "severity_bucket": bucket,
                "product_category": product,
                "mapping_confidence": "ambiguous" if unclaimed else "approximate",
                "effective_from": EFFECTIVE_FROM,
                "effective_to": "",
                "notes": (
                    "NEEDS REVIEW: no rule matched this IUCR primary description, "
                    "so it carries the residual NIBRS code. Map it or confirm the "
                    "residual is right."
                    if unclaimed
                    else f"{REVIEW_NOTE} {override_reason}" + conflict
                    if override_reason
                    else f"{REVIEW_NOTE} Rule-based mapping from the published IUCR "
                    "list, read and kept." + conflict
                ),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--token", default=None, help="optional Socrata app token")
    args = parser.parse_args(argv)

    iucr = fetch_iucr(args.base, args.dataset, args.token)
    rows = build_rows(iucr)

    writer = csv.DictWriter(sys.stdout, fieldnames=_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)

    unclaimed = sum(1 for r in rows if r["mapping_confidence"] == "ambiguous")
    print(
        f"\n{len(rows)} row(s) from {len(iucr)} IUCR code(s); "
        f"{unclaimed} matched no rule and need mapping by hand.",
        file=sys.stderr,
    )
    print(
        "Every row is a first pass. Review before enabling the city -- see this "
        "script's docstring for the order.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
