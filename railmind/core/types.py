"""The domain types the rest of the system speaks.

Deliberately thin: the wire models generated from `openapi.yaml` are re-exported
here rather than copied, so there is exactly one definition of `Leg` in the
codebase. What lives in this file is the behaviour the wire cannot carry — a
provider-neutral `Query`, and correct money arithmetic.

Everything downstream imports from `railmind.core.types`, never from
`railmind.wire.models`, so if the generated layout changes the blast radius is
one file.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from railmind.core.errors import InvalidQuery
from railmind.wire.models import (
    Ambiguity,
    Availability,
    BookingChannel,
    BookingIntent,
    FareComponent,
    GeoPoint,
    Handoff,
    HandoffStep,
    HandoffSummary,
    Health,
    Itinerary,
    Leg,
    LineString,
    LiveStatus,
    Metrics,
    Mode,
    Money,
    Passenger,
    Problem,
    ProviderInfo,
    Quota,
    Risk,
    ScoreBreakdown,
    SearchMeta,
    ServiceRef,
    Stop,
    TimeWindow,
    Title,
)

__all__ = [
    "Ambiguity",
    "Availability",
    "BookingChannel",
    "BookingIntent",
    "FareComponent",
    "GeoPoint",
    "Handoff",
    "HandoffStep",
    "HandoffSummary",
    "Health",
    "Itinerary",
    "Leg",
    "LineString",
    "LiveStatus",
    "Metrics",
    "Mode",
    "Money",
    "Passenger",
    "Problem",
    "ProviderInfo",
    "Query",
    "Quota",
    "Risk",
    "ScoreBreakdown",
    "SearchMeta",
    "ServiceRef",
    "Stop",
    "TimeWindow",
    "Title",
    "add_money",
    "format_inr",
    "inr",
    "normalise_class",
    "rupees",
]

# --- money -------------------------------------------------------------------

#: Class codes as IRCTC spells them. The aliases exist because users and LLMs
#: write "3ac", "3 AC", "third ac" and the wire must receive exactly "3A".
_CLASS_ALIASES = {
    "1a": "1A",
    "first ac": "1A",
    "first": "1A",
    "2a": "2A",
    "2ac": "2A",
    "second ac": "2A",
    "3a": "3A",
    "3ac": "3A",
    "third ac": "3A",
    "cc": "CC",
    "ac chair": "CC",
    "chair car": "CC",
    "ec": "EC",
    "executive chair": "EC",
    "sl": "SL",
    "sleeper": "SL",
    "2s": "2S",
    "second sitting": "2S",
}


def normalise_class(value: str) -> str:
    """`"3ac"` -> `"3A"`. Unknown values pass through upper-cased, not dropped.

    Passing an unknown code through is intentional: a provider may invent a class
    and guessing would silently change the user's booking. The provider rejects
    what it does not know, with `InvalidQuery`, which is a fixable error.
    """
    cleaned = " ".join((value or "").split()).strip().lower()
    if not cleaned:
        raise InvalidQuery("a travel class cannot be blank")
    # Users write "3 AC", "3 A", "3AC". Collapse internal spaces before lookup so
    # one alias table covers all of them.
    alias = _CLASS_ALIASES.get(cleaned) or _CLASS_ALIASES.get(cleaned.replace(" ", ""))
    return alias or cleaned.upper()


def inr(amount: int | float | Decimal | str) -> Money:
    """Build `Money` from rupees without ever touching a float for the wire.

    `inr(2605.50)` -> 260550 paise. The Decimal conversion keeps 0.1+0.2 honest,
    because a fare that is off by a paisa is a fare that fails a reconciliation.
    """
    value = Decimal(str(amount))
    minor = int((value * 100).to_integral_value())
    if minor < 0:
        raise InvalidQuery("money cannot be negative")
    return Money(amount_minor=minor, currency="INR")


def rupees(money: Money) -> Decimal:
    """Exact rupee value of a `Money`, for display and for comparisons."""
    return (Decimal(money.amount_minor) / 100).quantize(Decimal("0.01"))


def add_money(*amounts: Money) -> Money:
    """Sum, refusing to mix currencies.

    Silently adding INR to USD is the kind of bug that shows up as a wrong total
    on the one screen the user actually reads.
    """
    if not amounts:
        raise InvalidQuery("nothing to add")
    currency = amounts[0].currency
    for money in amounts:
        if money.currency != currency:
            raise InvalidQuery(f"cannot add {currency} and {money.currency}")
    return Money(amount_minor=sum(m.amount_minor for m in amounts), currency=currency)


def format_inr(money: Money) -> str:
    """`260550` -> `₹2,605.50`, using Indian digit grouping (2,60,550 style).

    Written out rather than delegated to a locale, because the server runs with
    whatever locale the host has and a demo must not render `2,605.50` on one
    machine and `2 605,50` on another.
    """
    whole, paise = divmod(money.amount_minor, 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    symbol = "₹" if money.currency == "INR" else money.currency + " "
    return f"{symbol}{digits}.{paise:02d}"


# --- query -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Query:
    """A provider-neutral search request.

    Beckn `Intent` in spirit: origin, destination, when, what mode, which class.
    It carries no provider id and no provider-specific field, because a provider
    that cannot answer it should say `NotSupported` rather than receive a
    half-translated request.
    """

    origin: Stop
    destination: Stop
    service_date: dt.date
    modes: tuple[Mode, ...] = (Mode.rail,)
    classes: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    quota: Quota = Quota.general
    passengers: tuple[Passenger, ...] = ()
    arrive_by: dt.datetime | None = None
    depart_window: TimeWindow | None = None
    max_price: Money | None = None
    tags: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.origin.stop_id == self.destination.stop_id:
            raise InvalidQuery("origin and destination are the same stop")
        if not self.modes:
            raise InvalidQuery("a search must name at least one mode")
        if not self.classes:
            # Providers decide their own default class; an empty tuple means
            # "you choose", not "no class".
            object.__setattr__(self, "classes", ())

    @property
    def cache_key_parts(self) -> tuple[str, str, str]:
        """The route triple that keys every cache entry: `(from, to, date)`.

        Note what is absent: the user. One hundred people searching CSMT to PUNE
        tomorrow share one upstream fetch, which is what makes a 100-request
        monthly budget usable at all.
        """
        return (self.origin.stop_id, self.destination.stop_id, self.service_date.isoformat())

    @classmethod
    def from_intent(cls, intent: BookingIntent) -> Query:
        """Build a query from a parsed intent, refusing rather than guessing.

        If the intent is missing a station or a date it is the caller's job to ask
        the human. Inventing "tomorrow" here would produce a confident, wrong
        answer, which is worse than an error.
        """
        if intent.origin is None:
            raise InvalidQuery("the request has no origin station")
        if intent.destination is None:
            raise InvalidQuery("the request has no destination station")

        window = intent.depart_window
        if window is not None:
            service_date = window.start.date()
        elif intent.arrive_by is not None:
            service_date = intent.arrive_by.date()
        else:
            raise InvalidQuery("the request has no travel date")

        return cls(
            origin=intent.origin,
            destination=intent.destination,
            service_date=service_date,
            modes=tuple(intent.modes or (Mode.rail,)),
            classes=tuple(normalise_class(c) for c in (intent.class_preferences or ())),
            services=tuple(intent.service_preferences or ()),
            quota=intent.quota,
            passengers=tuple(intent.passengers or ()),
            arrive_by=intent.arrive_by,
            depart_window=window,
            max_price=intent.budget,
        )
