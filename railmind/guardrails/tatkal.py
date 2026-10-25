"""Tatkal guardrails: the rules that must hold before a card claims to be usable.

Why this is its own module rather than a few `if` statements in the engine. Every
rule here is a published constraint with a date, an hour, or a hard cap, and the
project's whole argument rests on obeying them rather than working around them.
Scattered checks cannot be audited; one function with one test per rule can, and
a reviewer can read it in a minute.

What this module refuses to do: it does not decide whether a booking is legally
ours to make, because it never is. RailMind does not book. These rules decide
whether the *advice* is sound right now: is Tatkal even open, is the party size
legal for this quota, and what must the human be told before they tap.

The three facts behind the rules, all measured or published, recorded in
RESEARCH-TATKAL.md and docs/CAPABILITY-LEDGER.md:

- Tatkal booking opens one day before the journey, at 10:00 for AC classes and
  11:00 for non-AC. Before that hour there is nothing to book, so a card that
  says "available" would be a lie regardless of what a vendor returned.
- The Tatkal party cap is 4 passengers per booking, against 6 on general quota.
- Authorised agents are barred from the first 30 minutes of each opening. We are
  not an agent and we never submit a booking, so this is a warning for the human,
  not a refusal for us. Representing it as a refusal would be dishonest in the
  other direction: it would deny a user their own legal action.

`check_tatkal` is pure. The clock is a parameter, so every boundary is testable
without waiting for 10:00 IST.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from railmind.wire.models import Quota

#: Indian Railways runs on IST, and Tatkal is defined in IST wall-clock terms. A
#: server in UTC deciding "is it 10:00 yet" from its own clock is a bug waiting
#: for a demo in another timezone.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

#: Tatkal opens one day before the journey.
ADVANCE_DAYS = 1

#: Opening hours by class family. AC classes open an hour earlier than non-AC.
AC_OPEN_HOUR = 10
NON_AC_OPEN_HOUR = 11

#: The window in which authorised agents must not act. Kept as data because the
#: report quotes it and the warning text is built from it.
AGENT_BLACKOUT_MINUTES = 30

#: Party size limits. The Tatkal cap is the one that surprises people.
TATKAL_MAX_PASSENGERS = 4
GENERAL_MAX_PASSENGERS = 6

#: Classes that count as air-conditioned for the opening hour, and therefore
#: carry GST. Anything unrecognised is treated as AC, which takes the earlier
#: opening hour: being early with a warning is safe, being late is not.
_AC_CLASSES = frozenset({"1A", "2A", "3A", "3E", "CC", "EC"})
_NON_AC_CLASSES = frozenset({"SL", "2S", "GN"})


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of a check, in the shape a card or an API error needs."""

    allowed: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Machine-readable rule ids, so a client can style or route each one.
    codes: tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        return not self.allowed


def is_ac(class_codes: tuple[str, ...]) -> bool | None:
    """Whether this journey is air-conditioned, or None when no class was given."""
    normalised = {code.upper() for code in class_codes}
    if normalised & _AC_CLASSES:
        return True
    if normalised & _NON_AC_CLASSES:
        return False
    if normalised:
        return True
    return None


def opening_time(service_date: dt.date, *, ac: bool) -> dt.datetime:
    """When Tatkal booking opens for this journey, in IST."""
    day = service_date - dt.timedelta(days=ADVANCE_DAYS)
    hour = AC_OPEN_HOUR if ac else NON_AC_OPEN_HOUR
    return dt.datetime.combine(day, dt.time(hour), tzinfo=IST)


def blackout_window(service_date: dt.date, *, ac: bool) -> tuple[dt.datetime, dt.datetime]:
    """The 30 minutes after opening in which authorised agents must not act."""
    start = opening_time(service_date, ac=ac)
    return start, start + dt.timedelta(minutes=AGENT_BLACKOUT_MINUTES)


def check_tatkal(
    *,
    quota: Quota | str,
    service_date: dt.date,
    now: dt.datetime,
    passenger_count: int = 0,
    class_codes: tuple[str, ...] = (),
) -> Verdict:
    """Whether Tatkal advice is sound at `now`, and what the human must be told.

    Refuses in exactly two situations, because in exactly two situations the
    advice would be wrong rather than merely risky: the window has not opened, and
    the party is larger than the quota allows. Everything else is a warning, since
    the remaining risks are the human's to weigh.
    """
    if Quota(quota) is not Quota.tatkal:
        return _general(passenger_count)

    ac = is_ac(class_codes)
    # Unknown class takes the earlier opening hour: if we are wrong, we are wrong
    # by being cautious about what might be bookable.
    assumed_ac = True if ac is None else ac
    opens = opening_time(service_date, ac=assumed_ac)
    local_now = now.astimezone(IST)

    reasons: list[str] = []
    codes: list[str] = []
    warnings: list[str] = []

    if local_now < opens:
        if local_now.date() < opens.date():
            # A different day: not a timing problem, a calendar one.
            codes.append("tatkal_too_early")
            reasons.append(
                f"Tatkal for {service_date:%d %b} cannot be booked yet. It opens at "
                f"{opens:%H:%M} IST on {opens:%d %b}, one day before the journey."
            )
        else:
            codes.append("tatkal_not_open")
            reasons.append(
                f"Tatkal opens at {opens:%H:%M} IST on {opens:%d %b}. "
                f"Right now it is {local_now:%H:%M} IST on {local_now:%d %b}."
            )

    if passenger_count > TATKAL_MAX_PASSENGERS:
        codes.append("tatkal_passenger_limit")
        reasons.append(
            f"Tatkal allows at most {TATKAL_MAX_PASSENGERS} passengers in one booking, "
            f"and you have {passenger_count}. Split the party or use general quota."
        )

    blackout_start, blackout_end = blackout_window(service_date, ac=assumed_ac)
    if blackout_start <= local_now < blackout_end:
        warnings.append(
            f"The first {AGENT_BLACKOUT_MINUTES} minutes after Tatkal opens "
            f"({blackout_start:%H:%M}-{blackout_end:%H:%M} IST) are reserved for "
            "individual passengers; authorised agents stay out. You are booking as "
            "yourself, so you may proceed, but the site will be at its busiest."
        )

    if not reasons:
        warnings.append(
            "Tatkal fares are not refundable if you cancel, only if the train is cancelled."
        )
        if ac is None:
            warnings.append(
                "No travel class was given, so the opening time above assumes an AC class."
            )

    return Verdict(
        allowed=not reasons,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
        codes=tuple(codes),
    )


def _general(passenger_count: int) -> Verdict:
    if passenger_count > GENERAL_MAX_PASSENGERS:
        return Verdict(
            allowed=False,
            reasons=(
                f"One booking holds at most {GENERAL_MAX_PASSENGERS} passengers, "
                f"and you have {passenger_count}.",
            ),
            codes=("passenger_limit",),
        )
    return Verdict(allowed=True)
