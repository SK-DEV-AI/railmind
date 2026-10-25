"""City aliases: the names people say, mapped to the stations that serve them.

Why this is needed at all. The station table is authoritative about *stations* and
silent about *cities*. It contains "Kolkatta Terminal" (sic), "Howrah Jn" and
"Sealdah", and nothing at all containing the word "Kolkata", so a search for
"delhi to kolkata" found no destination. It also knows "KSR Bengaluru" but not
"Bangalore", "Varanasi" but not "Banaras", "Mumbai CSMT" but not "Bombay".

This is a fallback, not a lookup. `intent/parser.py` tries the table first and only
reaches for an alias when the table returns nothing, so an alias can never override
a real station name. That ordering matters: the table is data from the operator, and
this file is human knowledge, so data wins wherever it exists.

Two rules that keep it honest:

- Every target is a real station code, verified to exist in the database. A test
  checks this, so a typo here fails the build instead of quietly resolving nothing.
- The list is deliberately short and only holds exonyms (old or alternative names)
  and metro areas whose stations do not carry the city name. It is not a place to
  start inventing convenient shortcuts.

Ordering within an entry matters: candidates are tried in order when the parser looks
for one that actually has trains to the other end.
"""

from __future__ import annotations

#: Lowercased city or exonym -> real station codes, most useful first.
CITY_ALIASES: dict[str, tuple[str, ...]] = {
    # The city the vendor spells "Kolkatta" and whose busiest terminals are named
    # after other places entirely.
    "kolkata": ("HWH", "SDAH", "KOAA"),
    "calcutta": ("HWH", "SDAH", "KOAA"),
    "howrah": ("HWH",),
    # Bengaluru vs Bangalore, and the three terminals that matter.
    "bangalore": ("SBC", "BNC", "SMVB"),
    "bombay": ("CSMT", "MMCT", "LTT"),
    "madras": ("MAS",),
    "banaras": ("BSB",),
    "benares": ("BSB",),
    "kashi": ("BSB",),
    "mysore": ("MYS",),
    "trivandrum": ("TVC",),
    "calicut": ("CLT",),
    "cochin": ("ERS",),
    "ernakulam": ("ERS",),
    "gurgaon": ("GGN",),
    "gurugram": ("GGN",),
    "noida": ("NDLS",),  # no Noida terminal in the table; Delhi is the honest answer
    "vizag": ("VSKP",),
    "visakhapatnam": ("VSKP",),
    "vizianagaram": ("VZM",),
    "prayagraj": ("PRYJ",),
    "allahabad": ("PRYJ",),
    "varanasi": ("BSB",),
    "ahmedabad": ("ADI",),
    "amritsar": ("ASR",),
    "patna": ("PNBE",),
    "guwahati": ("GHY",),
    "bhopal": ("BPL",),
    "indore": ("INDB",),
    "nagpur": ("NGP",),
    "surat": ("ST",),
    "vadodara": ("BRC",),
    "baroda": ("BRC",),
    "lucknow": ("LKO",),
    "kanpur": ("CNB",),
    "coimbatore": ("CBE",),
    "madurai": ("MDU",),
    "ranchi": ("RNC",),
    "raipur": ("R",),
    "bhubaneswar": ("BBS",),
    "cuttack": ("CTC",),
    "dehradun": ("DDN",),
    "haridwar": ("HW",),
    "jodhpur": ("JU",),
    "udaipur": ("UDZ",),
    "ajmer": ("AII",),
    "agra": ("AGC",),
    "gwalior": ("GWL",),
    "jabalpur": ("JBP",),
    "udaipur city": ("UDZ",),
}


def candidates(city: str) -> tuple[str, ...]:
    """Real station codes for a city name or exonym, or empty when unknown."""
    return CITY_ALIASES.get(city.strip().lower(), ())
