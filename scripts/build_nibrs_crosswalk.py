"""Write the code-only crosswalk for a source that publishes NIBRS codes itself.

    python scripts/build_nibrs_crosswalk.py sea > reference/crosswalk/seattle_v1.csv
    python scripts/build_nibrs_crosswalk.py lax > reference/crosswalk/los_angeles_v1.csv

Chicago needs a rule-based generator because IUCR is not NIBRS. Seattle and Los
Angeles publish the NIBRS offense code on every record, so the target code is
the source's own and needs no inference -- every row here is `exact` on the
NIBRS side. What still needs a judgement is the product category and severity
bucket, and that judgement is made once, in `_NIBRS` below, so the two cities
cannot drift apart on, say, whether robbery is violent.

Rows use the `*` text sentinel (see migrate.load_crosswalks): the match is on
the code alone. Both sources pair the code with free text that varies -- LA's
descriptions embed the penal-code section -- so an exact (code, text) match
would miss most records for no gain.

The category calls follow the Philadelphia and Chicago crosswalks: robbery is
violent though NIBRS files it against property; weapon law violations are
`other`, since violence involving a weapon is already counted under assault,
robbery and homicide; enforcement-driven offenses land in `quality_of_life`.
"""

from __future__ import annotations

import csv
import sys

# code: (name, group, crime_against, ucr_part, severity_bucket, product_category)
_V, _P, _Q, _O = "violent", "property", "quality_of_life", "other"
_PIV, _PIP, _PII = "part_i_violent", "part_i_property", "part_ii"
_NIBRS: dict[str, tuple[str, str, str, str, str, str]] = {
    "09A": ("Murder and Nonnegligent Manslaughter", "A", "person", "I", _PIV, _V),
    "09B": ("Negligent Manslaughter", "A", "person", "II", _PIV, _V),
    # Not a crime: a lawful killing reported for completeness. Kept out of the
    # violent track so a police or self-defence shooting does not rank a cell.
    "09C": ("Justifiable Homicide", "A", "person", "II", _PII, _O),
    "100": ("Kidnapping/Abduction", "A", "person", "II", _PIV, _V),
    "11A": ("Rape", "A", "person", "I", _PIV, _V),
    "11B": ("Sodomy", "A", "person", "I", _PIV, _V),
    "11C": ("Sexual Assault With An Object", "A", "person", "I", _PIV, _V),
    "11D": ("Fondling", "A", "person", "II", _PII, _V),
    "120": ("Robbery", "A", "property", "I", _PIV, _V),
    "13A": ("Aggravated Assault", "A", "person", "I", _PIV, _V),
    "13B": ("Simple Assault", "A", "person", "II", _PII, _V),
    "13C": ("Intimidation", "A", "person", "II", _PII, _V),
    "200": ("Arson", "A", "property", "I", _PIP, _P),
    "210": ("Extortion/Blackmail", "A", "property", "II", _PII, _P),
    "220": ("Burglary/Breaking and Entering", "A", "property", "I", _PIP, _P),
    "23A": ("Pocket-picking", "A", "property", "I", _PIP, _P),
    "23B": ("Purse-snatching", "A", "property", "I", _PIP, _P),
    "23C": ("Shoplifting", "A", "property", "I", _PIP, _P),
    "23D": ("Theft From Building", "A", "property", "I", _PIP, _P),
    "23E": ("Theft From Coin-Operated Machine or Device", "A", "property", "I", _PIP, _P),
    "23F": ("Theft From Motor Vehicle", "A", "property", "I", _PIP, _P),
    "23G": ("Theft of Motor Vehicle Parts or Accessories", "A", "property", "I", _PIP, _P),
    "23H": ("All Other Larceny", "A", "property", "I", _PIP, _P),
    "240": ("Motor Vehicle Theft", "A", "property", "I", _PIP, _P),
    "250": ("Counterfeiting/Forgery", "A", "property", "II", _PII, _P),
    "26A": ("False Pretenses/Swindle/Confidence Game", "A", "property", "II", _PII, _P),
    "26B": ("Credit Card/Automated Teller Machine Fraud", "A", "property", "II", _PII, _P),
    "26C": ("Impersonation", "A", "property", "II", _PII, _P),
    "26D": ("Welfare Fraud", "A", "property", "II", _PII, _P),
    "26E": ("Wire Fraud", "A", "property", "II", _PII, _P),
    "26F": ("Identity Theft", "A", "property", "II", _PII, _P),
    "26G": ("Hacking/Computer Invasion", "A", "property", "II", _PII, _P),
    "26H": ("Money Laundering", "A", "property", "II", _PII, _P),
    "270": ("Embezzlement", "A", "property", "II", _PII, _P),
    "280": ("Stolen Property Offenses", "A", "property", "II", _PII, _P),
    "290": ("Destruction/Damage/Vandalism of Property", "A", "property", "II", _PII, _P),
    "35A": ("Drug/Narcotic Violations", "A", "society", "II", _PII, _Q),
    "35B": ("Drug Equipment Violations", "A", "society", "II", _PII, _Q),
    "36A": ("Incest", "A", "person", "II", _PII, _O),
    "36B": ("Statutory Rape", "A", "person", "II", _PII, _V),
    "370": ("Pornography/Obscene Material", "A", "society", "II", _PII, _O),
    "39A": ("Betting/Wagering", "A", "society", "II", _PII, _Q),
    "39B": ("Operating/Promoting/Assisting Gambling", "A", "society", "II", _PII, _Q),
    "39C": ("Gambling Equipment Violations", "A", "society", "II", _PII, _Q),
    "39D": ("Sports Tampering", "A", "society", "II", _PII, _Q),
    "40A": ("Prostitution", "A", "society", "II", _PII, _Q),
    "40B": ("Assisting or Promoting Prostitution", "A", "society", "II", _PII, _Q),
    "40C": ("Purchasing Prostitution", "A", "society", "II", _PII, _Q),
    "510": ("Bribery", "A", "property", "II", _PII, _P),
    "520": ("Weapon Law Violations", "A", "society", "II", _PII, _O),
    "64A": ("Human Trafficking, Commercial Sex Acts", "A", "person", "II", _PIV, _V),
    "64B": ("Human Trafficking, Involuntary Servitude", "A", "person", "II", _PIV, _V),
    "720": ("Animal Cruelty", "A", "society", "II", _PII, _O),
    "90A": ("Bad Checks", "B", "group_b", "II", _PII, _P),
    "90B": ("Curfew/Loitering/Vagrancy Violations", "B", "group_b", "II", _PII, _Q),
    "90C": ("Disorderly Conduct", "B", "group_b", "II", _PII, _Q),
    "90D": ("Driving Under the Influence", "B", "group_b", "II", _PII, _Q),
    "90E": ("Drunkenness", "B", "group_b", "II", _PII, _Q),
    "90F": ("Family Offenses, Nonviolent", "B", "group_b", "II", _PII, _O),
    "90G": ("Liquor Law Violations", "B", "group_b", "II", _PII, _Q),
    "90H": ("Peeping Tom", "B", "group_b", "II", _PII, _Q),
    "90I": ("Runaway", "B", "group_b", "II", _PII, _O),
    "90J": ("Trespass of Real Property", "B", "group_b", "II", _PII, _Q),
    "90Z": ("All Other Offenses", "B", "group_b", "II", _PII, _O),
}

# Codes a source publishes that are not NIBRS offenses, mapped to the residual
# and marked approximate rather than exact.
_LOCAL: dict[str, dict[str, tuple[str, str]]] = {
    "sea": {
        # SPD's own extension for no-contact-order violations; NIBRS has no
        # Group A target, and the FBI's guidance files these under 90Z.
        "500": ("90Z", "SPD local code for violation of a no-contact order; residual 90Z."),
        # "Not Reportable to NIBRS" (999) is deliberately absent: the Seattle
        # adapter does not promote those records, since they are not offenses.
    },
    "lax": {},
}

_CITIES = {
    # source_id: (crosswalk_version, effective_from)
    # Seattle's RMS moved to NIBRS in May 2019. Records from before it are
    # published too, already converted to NIBRS codes by SPD; see _EARLIER.
    "sea": ("sea_v1", "2019-05-01"),
    # LAPD's NIBRS datasets begin with the March 2024 RMS cut-over.
    "lax": ("lax_v1", "2024-03-01"),
}

# Earlier periods a source publishes under the same codes, each a separate set
# of rows so the crosswalk can say how far to trust them (S7.3). Seattle's
# 2008 to April 2019 records came from the previous records system and were
# converted to NIBRS codes by SPD; the codes match, the recording practice
# behind them does not (reference.source_series_caveat, migration 018), so they
# are approximate rather than exact. 62 codes, 949,633 offenses, all but '-'
# and '999' (0.2%) covered by the rows below (checked against the live dataset
# on 2026-10-08).
_EARLIER: dict[str, list[tuple[str, str, str]]] = {
    "sea": [
        (
            "2008-01-01",
            "2019-05-01",
            "Pre-May-2019 SPD record converted to NIBRS by SPD; same code, older "
            "recording practice. Category from scripts/build_nibrs_crosswalk.py.",
        ),
    ],
    "lax": [],
}

_FIELDNAMES = (
    "crosswalk_version", "source_id", "raw_offense_code", "raw_offense_text",
    "raw_source_category", "nibrs_code", "nibrs_offense_name", "nibrs_group",
    "nibrs_crime_against", "ucr_part", "severity_bucket", "product_category",
    "mapping_confidence", "effective_from", "effective_to", "notes",
)


def build_rows(source_id: str) -> list[dict[str, str]]:
    version, effective_from = _CITIES[source_id]
    rows: list[dict[str, str]] = []

    def row(
        raw_code: str,
        nibrs: str,
        confidence: str,
        note: str,
        start: str = effective_from,
        end: str = "",
    ) -> dict[str, str]:
        name, group, against, ucr, bucket, product = _NIBRS[nibrs]
        return {
            "crosswalk_version": version, "source_id": source_id,
            "raw_offense_code": raw_code, "raw_offense_text": "*",
            "raw_source_category": "", "nibrs_code": nibrs,
            "nibrs_offense_name": name, "nibrs_group": group,
            "nibrs_crime_against": against, "ucr_part": ucr,
            "severity_bucket": bucket, "product_category": product,
            "mapping_confidence": confidence, "effective_from": start,
            "effective_to": end, "notes": note,
        }

    for code in _NIBRS:
        rows.append(row(code, code, "exact",
                        "Source publishes the NIBRS code; category from scripts/build_nibrs_crosswalk.py."))
    for raw_code, (nibrs, note) in _LOCAL[source_id].items():
        rows.append(row(raw_code, nibrs, "approximate", note))
    for start, end, note in _EARLIER[source_id]:
        for code in _NIBRS:
            rows.append(row(code, code, "approximate", note, start, end))
        for raw_code, (nibrs, local_note) in _LOCAL[source_id].items():
            rows.append(row(raw_code, nibrs, "approximate", f"{local_note} {note}", start, end))
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in _CITIES:
        print(f"usage: build_nibrs_crosswalk.py {{{','.join(_CITIES)}}}", file=sys.stderr)
        return 2
    writer = csv.DictWriter(sys.stdout, fieldnames=_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    writer.writerows(build_rows(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
