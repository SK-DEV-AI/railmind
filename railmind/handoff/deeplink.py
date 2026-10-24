"""The IRCTC handoff: a real link, an honest checklist, no automation.

This is the product's last step and the reason the whole design is legal. We do
not submit anything. We open the operator's own booking page and we tell the human
exactly which steps only they can do.

What is verified and what is not — **verified 2026-10-24, and it is not good news**:

- IRCTC does **not** read `fromStation`, `toStation` or `date` from the URL on load.
  Tested on the old site (`/nget/train-search`) and the new beta site
  (`/eticket/train-search`), five parameter spellings, with rendered screenshots as
  evidence. The form opens empty every time.
- The results page (`/eticket/booking/train-list`) carries no query string at all, so
  it cannot be linked to either.
- Therefore this URL's job is **"open the booking page"**, not "open it filled in".
  The copy in `warnings` says so, because promising a prefill and delivering an empty
  form is worse than promising nothing.

See `docs/IRCTC-DEEPLINK.md` for the three experiments and the screenshots.
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
    """Build the booking-page URL.

    The parameters are kept because they cost nothing and the site may start reading
    them one day, but do not describe the result as pre-filled: measured on
    2026-10-24, the form opens empty. `date` is `YYYYMMDD` because that is the shape
    the site's own client uses, not ISO.
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
