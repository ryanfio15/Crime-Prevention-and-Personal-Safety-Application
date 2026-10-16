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
    # Records in the NIBRS dataset dated before the March 2024 cut-over: a few
    # hundred late reports of earlier offences, filed under NIBRS from the start.
    "lax": [
        (
            "2024-01-01",
            "2024-03-01",
            "NIBRS-coded report of an offence dated before LAPD's March 2024 "
            "cut-over. Category from scripts/build_nibrs_crosswalk.py.",
        ),
    ],
}

# LAPD's own crime codes (Crm Cd), as published in its legacy "Crime Data"
# datasets for 2010-2024 (safety/etl/adapters/los_angeles.py). The adapter
# stores them as 'CRM-<code>', because the numbers collide with NIBRS codes
# (510 is a stolen vehicle here and bribery in NIBRS). Mapped by hand from the
# codes' descriptions: `exact` where the LAPD code names one NIBRS offence,
# `approximate` where it spans or straddles several. Covers all 143 codes in the
# two datasets, 3,138,031 rows (checked against the live datasets on 2026-10-09).
_LAPD_CRM_FROM = "2010-01-01"
_E, _A = "exact", "approximate"
_LAPD_CRM: dict[str, tuple[str, str, str]] = {
    "110": ("09A", _E, "Criminal homicide."),
    "113": ("09B", _E, "Manslaughter, negligent."),
    "121": ("11A", _E, "Rape, forcible."),
    "122": ("11A", _E, "Rape, attempted: NIBRS counts an attempt as the offence."),
    "210": ("120", _E, "Robbery."),
    "220": ("120", _E, "Attempted robbery (NIBRS 220 is burglary; hence the CRM- key)."),
    "230": ("13A", _E, "Assault with a deadly weapon, aggravated assault."),
    "231": ("13A", _E, "Assault with a deadly weapon on a police officer."),
    "235": ("13A", _E, "Child abuse (physical), aggravated assault."),
    "236": ("13A", _E, "Intimate partner, aggravated assault."),
    "237": ("90F", _A, "Child neglect: nonviolent family offence."),
    "250": ("13A", _A, "Shots fired at a moving vehicle, train or aircraft; LAPD counts it as aggravated assault."),
    "251": ("13A", _A, "Shots fired at an inhabited dwelling; LAPD counts it as aggravated assault."),
    "310": ("220", _E, "Burglary."),
    "320": ("220", _E, "Burglary, attempted."),
    "330": ("23F", _A, "Burglary from vehicle: California vehicle burglary is NIBRS theft from motor vehicle."),
    "331": ("23F", _E, "Theft from motor vehicle, grand."),
    "341": ("23H", _A, "Grand theft, other than guns, fowl, livestock, produce."),
    "343": ("23C", _E, "Shoplifting, grand theft."),
    "345": ("270", _A, "Dishonest employee, grand theft: NIBRS embezzlement."),
    "347": ("26A", _A, "Grand theft, insurance fraud."),
    "349": ("26A", _A, "Grand theft, auto repair fraud."),
    "350": ("23A", _A, "Theft from the person."),
    "351": ("23B", _E, "Purse snatching."),
    "352": ("23A", _E, "Pickpocket."),
    "353": ("23A", _A, "Drunk roll: theft from an incapacitated person."),
    "354": ("26F", _E, "Theft of identity."),
    "410": ("23F", _A, "Burglary from vehicle, attempted."),
    "420": ("23F", _E, "Theft from motor vehicle, petty."),
    "421": ("23F", _E, "Theft from motor vehicle, attempt."),
    "432": ("90Z", _A, "Blocking door of an induction centre."),
    "433": ("240", _A, "Driving without owner consent: joyriding is NIBRS motor vehicle theft."),
    "434": ("100", _A, "False imprisonment: NIBRS kidnapping/abduction covers unlawful detention."),
    "435": ("90Z", _A, "'Lynching' (California: taking a person from police custody by riot)."),
    "436": ("90Z", _A, "'Lynching', attempted."),
    "437": ("90Z", _A, "Resisting arrest."),
    "438": ("90Z", _A, "Reckless driving."),
    "439": ("90Z", _A, "False police report."),
    "440": ("23H", _E, "Theft, plain, petty."),
    "441": ("23H", _E, "Theft, plain, attempt."),
    "442": ("23C", _E, "Shoplifting, petty."),
    "443": ("23C", _E, "Shoplifting, attempt."),
    "444": ("270", _A, "Dishonest employee, petty theft: NIBRS embezzlement."),
    "445": ("270", _A, "Dishonest employee, attempted theft."),
    "446": ("26A", _A, "Petty theft, auto repair fraud."),
    "450": ("23A", _A, "Theft from the person, attempt."),
    "451": ("23B", _E, "Purse snatching, attempt."),
    "452": ("23A", _E, "Pickpocket, attempt."),
    "453": ("23A", _A, "Drunk roll, attempt."),
    "470": ("23H", _A, "Till tap, grand theft."),
    "471": ("23H", _A, "Till tap, petty."),
    "472": ("23H", _A, "Till tap, attempt."),
    "473": ("23E", _E, "Theft from coin machine, grand."),
    "474": ("23E", _E, "Theft from coin machine, petty."),
    "475": ("23E", _E, "Theft from coin machine, attempt."),
    "480": ("23H", _E, "Bicycle stolen: NIBRS files bicycles under all other larceny."),
    "485": ("23H", _E, "Bicycle, attempted theft."),
    "487": ("23H", _A, "Boat stolen: NIBRS motor vehicles run on land."),
    "510": ("240", _E, "Vehicle stolen (NIBRS 510 is bribery; hence the CRM- key)."),
    "520": ("240", _E, "Vehicle, attempted theft."),
    "522": ("240", _A, "Vehicle stolen, other (motorised scooters, bikes)."),
    "622": ("13B", _E, "Battery on a firefighter."),
    "623": ("13B", _E, "Battery on police, simple."),
    "624": ("13B", _E, "Battery, simple assault."),
    "625": ("13B", _A, "Other assault."),
    "626": ("13B", _E, "Intimate partner, simple assault."),
    "627": ("13B", _E, "Child abuse (physical), simple assault."),
    "647": ("13B", _A, "Throwing an object at a moving vehicle."),
    "648": ("200", _E, "Arson."),
    "649": ("250", _E, "Document forgery."),
    "651": ("90A", _E, "Worthless document (bad cheque), over $200."),
    "652": ("90A", _E, "Worthless document (bad cheque), $200 and under."),
    "653": ("26B", _E, "Credit card fraud, over $950."),
    "654": ("26B", _E, "Credit card fraud, $950 and under."),
    "660": ("250", _E, "Counterfeit."),
    "661": ("26G", _E, "Unauthorized computer access."),
    "662": ("26A", _E, "Bunco (confidence game), grand theft."),
    "664": ("26A", _E, "Bunco, petty theft."),
    "666": ("26A", _E, "Bunco, attempt."),
    "668": ("270", _E, "Embezzlement, grand theft."),
    "670": ("270", _E, "Embezzlement, petty theft."),
    "740": ("290", _E, "Vandalism, felony."),
    "745": ("290", _E, "Vandalism, misdemeanour."),
    "753": ("520", _A, "Discharging a firearm / shots fired: weapon law violation."),
    "755": ("90Z", _A, "Bomb scare."),
    "756": ("520", _A, "Weapons possession / bombing."),
    "760": ("11D", _A, "Lewd or lascivious acts with a child."),
    "761": ("13A", _A, "Brandishing a weapon; LAPD counts it as aggravated assault."),
    "762": ("90Z", _A, "Lewd conduct: FBI guidance files public lewdness under 90Z."),
    "763": ("13C", _E, "Stalking: NIBRS intimidation."),
    "805": ("40B", _E, "Pimping."),
    "806": ("40B", _E, "Pandering."),
    "810": ("36B", _A, "Unlawful sex (including mutual consent with a minor): statutory rape."),
    "812": ("11D", _A, "Crime against a child 13 or under."),
    "813": ("90Z", _A, "Annoying a child."),
    "814": ("370", _E, "Child pornography."),
    "815": ("11C", _E, "Sexual penetration with a foreign object."),
    "820": ("11B", _E, "Oral copulation: NIBRS sodomy."),
    "821": ("11B", _E, "Sodomy."),
    "822": ("64A", _E, "Human trafficking, commercial sex acts."),
    "830": ("36A", _E, "Incest."),
    "840": ("720", _A, "Bestiality: NIBRS animal cruelty includes sexual abuse of animals."),
    "845": ("90Z", _A, "Sex offender registrant out of compliance."),
    "850": ("90Z", _A, "Indecent exposure: FBI guidance files it under 90Z."),
    "860": ("11D", _E, "Battery with sexual contact: fondling."),
    "865": ("35A", _A, "Drugs to a minor."),
    "870": ("90F", _A, "Child abandonment: nonviolent family offence."),
    "880": ("90C", _A, "Disrupting a school."),
    "882": ("90C", _A, "Inciting a riot."),
    "884": ("90C", _A, "Failure to disperse."),
    "886": ("90C", _E, "Disturbing the peace."),
    "888": ("90J", _E, "Trespassing."),
    "890": ("90Z", _A, "Failure to yield."),
    "900": ("90Z", _A, "Violation of a court order."),
    "901": ("90Z", _A, "Violation of a restraining order."),
    "902": ("90Z", _A, "Violation of a temporary restraining order."),
    "903": ("90Z", _A, "Contempt of court."),
    "904": ("90Z", _A, "Firearms emergency protective order."),
    "905": ("90Z", _A, "Firearms temporary restraining order."),
    "906": ("90Z", _A, "Firearms restraining order."),
    "910": ("100", _E, "Kidnapping."),
    "920": ("100", _E, "Kidnapping, attempt."),
    "921": ("64B", _E, "Human trafficking, involuntary servitude."),
    "922": ("100", _A, "Child stealing."),
    "924": ("290", _A, "Telephone property, damage."),
    "926": ("290", _A, "Train wrecking."),
    "928": ("13C", _E, "Threatening phone calls or letters: intimidation."),
    "930": ("13C", _E, "Criminal threats, no weapon displayed: intimidation."),
    "931": ("520", _A, "Replica firearms (sale, display, manufacture)."),
    "932": ("90H", _E, "Peeping Tom."),
    "933": ("90B", _A, "Prowler: loitering."),
    "940": ("210", _E, "Extortion."),
    "942": ("510", _E, "Bribery."),
    "943": ("720", _E, "Cruelty to animals."),
    "944": ("90Z", _A, "Conspiracy."),
    "946": ("90Z", _A, "Other miscellaneous crime."),
    "948": ("90Z", _A, "Bigamy."),
    "949": ("90Z", _A, "Illegal dumping."),
    "950": ("26A", _A, "Defrauding an innkeeper / theft of services, over $950."),
    "951": ("26A", _A, "Defrauding an innkeeper / theft of services, $950 and under."),
    "952": ("90Z", _A, "Illegal abortion."),
    "954": ("90F", _A, "Contributing to the delinquency of a minor."),
    # Lewd or annoying calls and letters without a threat (threats are 928).
    "956": ("90Z", _A, "Lewd letters or telephone calls, without a threat."),
}
_LEGACY_CODES: dict[str, tuple[str, str, dict[str, tuple[str, str, str]]]] = {
    # source_id: (raw code prefix, effective_from, code -> (nibrs, confidence, note))
    "lax": ("CRM-", _LAPD_CRM_FROM, _LAPD_CRM),
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
    if source_id in _LEGACY_CODES:
        prefix, start, codes = _LEGACY_CODES[source_id]
        for code, (nibrs, confidence, note) in codes.items():
            rows.append(row(f"{prefix}{code}", nibrs, confidence, f"LAPD crime code {code}. {note}", start))
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
