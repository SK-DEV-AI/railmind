"""Derivable identifiers.

Every id in this system can be recomputed from its parts. None of them is a
random session token that dies with the process, so a cache entry, an audit log
line, and a deep link all agree about which journey they mean.

Two ids are deliberately not derivable: `intent_id` and `search_id`, because a
human can issue the identical request twice and those are two separate events.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from dataclasses import dataclass

SEP = ":"

_COUNTRY = re.compile(r"^[A-Z]{2}$")
_SAFE_PART = re.compile(r"^[A-Za-z0-9._-]+$")


class IdError(ValueError):
    """An identifier could not be built or parsed. Always a programming error."""


# --- stops -------------------------------------------------------------------


def stop_id(country: str, code: str) -> str:
    """`stop_id("IN", "CSMT")` -> `IN:CSMT`.

    Country is the ISO-3166 alpha-2 code, upper case, because two-letter codes
    are guessable and a lower-case one in a URL is a bug waiting to happen.
    """
    country = (country or "").strip().upper()
    code = (code or "").strip().upper()
    if not _COUNTRY.match(country):
        raise IdError(f"country must be a 2-letter code, got {country!r}")
    if not code or SEP in code or not _SAFE_PART.match(code):
        raise IdError(f"invalid stop code {code!r}")
    return f"{country}{SEP}{code}"


def parse_stop_id(value: str) -> tuple[str, str]:
    """Inverse of `stop_id`. Returns `(country, code)`."""
    country, _, code = (value or "").partition(SEP)
    if not code:
        raise IdError(f"not a stop id: {value!r}")
    # Round-trip so the two functions can never disagree about what is valid.
    if stop_id(country, code) != value:
        raise IdError(f"not a canonical stop id: {value!r}")
    return country.upper(), code.upper()


# --- legs --------------------------------------------------------------------


def as_service_date(value: dt.date | dt.datetime | str) -> dt.date:
    """Normalise anything date-ish to a plain date.

    A datetime contributes its own date, not UTC-converted: the booking date is
    local to the origin station, and converting would silently shift late-night
    departures to the previous day.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise IdError(f"not an ISO date: {value!r}") from exc
    raise IdError(f"cannot read a service date from {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class LegId:
    """The parsed form of a leg id. Cheaper to work with than the string."""

    provider_id: str
    service_id: str
    service_date: dt.date
    sequence: int = 1

    @property
    def value(self) -> str:
        return leg_id(self.provider_id, self.service_id, self.service_date, self.sequence)


def leg_id(
    provider_id: str,
    service_id: str,
    service_date: dt.date | dt.datetime | str,
    sequence: int = 1,
) -> str:
    """`leg_id("rail_india", "12951", "2026-10-25")` -> `rail_india:12951:2026-10-25:1`.

    The sequence exists because one itinerary can contain two legs on the same
    service, and two same-service legs must not share an id.
    """
    for name, part in (("provider_id", provider_id), ("service_id", service_id)):
        part = (part or "").strip()
        if not part or not _SAFE_PART.match(part):
            raise IdError(f"invalid {name} {part!r}: letters, digits, . _ - only, no colons")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise IdError(f"sequence must be a positive integer, got {sequence!r}")
    date_part = as_service_date(service_date).isoformat()
    provider_part = provider_id.strip()
    service_part = service_id.strip()
    return f"{provider_part}{SEP}{service_part}{SEP}{date_part}{SEP}{sequence}"


def parse_leg_id(value: str) -> LegId:
    """Parse a leg id back into its parts. Raises `IdError` on anything non-canonical."""
    parts = (value or "").split(SEP)
    if len(parts) != 4:
        raise IdError(f"not a leg id: {value!r}")
    provider_id, service_id, date_part, seq_part = parts
    if not seq_part.isdigit():
        raise IdError(f"leg sequence must be numeric: {value!r}")
    parsed = LegId(provider_id, service_id, as_service_date(date_part), int(seq_part))
    # Round-trip: rejects lower-case dates, zero padding errors, stray spaces.
    if parsed.value != value:
        raise IdError(f"not a canonical leg id: {value!r}")
    return parsed


# --- itineraries -------------------------------------------------------------


def itinerary_id(leg_ids: list[str] | tuple[str, ...]) -> str:
    """Content hash over the ordered leg ids. Same trip, same id, forever.

    Order matters: A-then-B is not B-then-A, and the hash must say so.
    """
    if not leg_ids:
        raise IdError("an itinerary needs at least one leg")
    for value in leg_ids:
        parse_leg_id(value)  # validate, do not trust the caller
    digest = hashlib.sha256("\n".join(leg_ids).encode()).hexdigest()
    return f"itin_{digest[:20]}"


# --- events (not derivable, on purpose) --------------------------------------


def intent_id() -> str:
    """Two identical requests are two events. Never a hash of the request."""
    return f"int_{uuid.uuid4().hex}"


def search_id() -> str:
    """One search is one event, even if it returns the same itineraries twice."""
    return f"srch_{uuid.uuid4().hex}"
