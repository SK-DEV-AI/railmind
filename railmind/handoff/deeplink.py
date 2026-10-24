"""The IRCTC handoff: a real, pre-filled link and an honest checklist.

This is the product's last step and the reason the whole design is legal. We do
not submit anything. We open the operator's own page with the journey already
filled in, and we tell the human exactly which steps only they can do.

What is verified and what is not:

- The URL shape `.../nget/train-search?fromStation=X&toStation=Y&date=YYYYMMDD`
  comes from the project's research on IRCTC's NGET client (`PROJECT-OPTIONS.md`
  §1, Handoff A). It is the documented query-parameter form.
- It has **not** been verified in a browser against the live site by this code.
  IRCTC sits behind Akamai and the check is a manual demo step. Until someone
  confirms it, the function is honest about carrying only these three values and
  puts everything else in `prefill` and `steps` rather than inventing parameters
  that the site may silently ignore.

That distinction matters: a link that claims to pre-fill a class but does not is
worse than one that admits it cannot, because the user only discovers the
difference at the payment screen.
"""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlencode

from railmind.core.ids import IdError, as_service_date

IRCTC_BASE = "https://www.irctc.co.in/nget/train-search"
_STATION_CODE = re.compile(r"^[A-Z0-9]{2,10}$")

LEGAL_NOTICE = (
    "RailMind does not book and does not take payment. This opens Indian Railways' "
    "own booking site, where you complete the booking yourself. Your login, the "
    "captcha, the Aadhaar OTP and the payment are yours to enter."
)


def _check_code(code: str) -> str:
    cleaned = (code or "").strip().upper()
    if not _STATION_CODE.match(cleaned):
        raise IdError(f"not a station code: {code!r}")
    return cleaned


def build_irctc_deeplink(
    *,
    from_code: str,
    to_code: str,
    service_date: dt.date | dt.datetime | str,
) -> str:
    """Build the pre-filled search URL.

    `date` is `YYYYMMDD` because that is what the NGET client reads, not ISO.
    """
    origin = _check_code(from_code)
    destination = _check_code(to_code)
    if origin == destination:
        raise IdError("origin and destination are the same station")

    day = as_service_date(service_date)
    today = dt.datetime.now(dt.UTC).date()
    if day < today - dt.timedelta(days=1):
        # A link to a past date is a link to an error page. Refuse early.
        raise IdError(f"service date {day} is in the past")

    query = urlencode(
        {
            "fromStation": origin,
            "toStation": destination,
            "date": day.strftime("%Y%m%d"),
        }
    )
    return f"{IRCTC_BASE}?{query}"
