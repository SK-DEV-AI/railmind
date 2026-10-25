"""Intent parsing: rules first, because rules cannot hallucinate a station code.

The architecture puts an LLM in front of the hot path only as a fallback, and
this file is why that is affordable. Almost everything a traveller types has a
shape: a place, then another place, a day, sometimes a class, a quota, a budget.
That is a dozen regular expressions and one database lookup, it runs in
microseconds, it is deterministic, and when it fails it can say exactly what it
could not read.

Three rules that shape the code:

1. **Never invent a station.** A name that matches nothing, or matches several,
   becomes an `Ambiguity` with the options attached. A confidently wrong code
   sends someone to the wrong city faster than any error message.
2. **Never invent a date.** "Friday" is resolved against the supplied `now`;
   with no date at all the intent comes back without a `depart_window` and
   `POST /search` will refuse it. Refusing is one round trip; guessing is a
   wasted booking window during Tatkal.
3. **Confidence is arithmetic, not vibes.** It starts low and each resolved
   field adds a fixed amount, so a reviewer can reproduce the number by reading
   the code rather than trusting a model's self-report.

`parsed_by` on the returned intent is always `rules` here. If an LLM ever fills
the gaps, it must say so itself, and the UI shows that field verbatim so nobody
thinks a model is guessing silently.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from railmind.core.ids import intent_id
from railmind.core.types import inr
from railmind.data.timetable import StationRow, TimetableRepo
from railmind.wire.models import (
    Ambiguity,
    BookingIntent,
    GeoPoint,
    Mode,
    Optimise,
    ParsedBy,
    Passenger,
    Quota,
    Stop,
    TimeWindow,
)

#: Words that never name a place. Stripped before a station lookup, so "from
#: Mumbai to Pune please" does not search for "please".
_NOISE = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "before",
        "below",
        "book",
        "booking",
        "by",
        "cheapest",
        "for",
        "from",
        "in",
        "me",
        "need",
        "on",
        "please",
        "reach",
        "ticket",
        "tickets",
        "to",
        "train",
        "want",
        "would",
        "under",
        "the",
    }
)

#: Time-of-day words to a window, in local hours. Deliberately wide: a user who
#: says "morning" has not told us they mean 06:00, and pretending otherwise
#: hides trains they would have taken.
_WINDOWS: dict[str, tuple[int, int]] = {
    "early morning": (4, 8),
    "morning": (4, 12),
    "afternoon": (12, 17),
    "evening": (17, 22),
    "night": (22, 28),  # past midnight, resolved against the service date
    "late night": (22, 28),
}

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

#: How many station candidates per side are checked for connectivity. Eight squared
#: is 64 indexed lookups, measured at well under a millisecond each, and it is enough
#: to see a whole city: Mumbai alone contributes CSMT, LTT, MMCT, DR, BCL, BCT.
MAX_CANDIDATES = 8

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

#: Spoken class names to IRCTC class codes. The code is what the provider takes;
#: the words are what a human says.
_CLASSES: dict[str, str] = {
    "1a": "1A",
    "1ac": "1A",
    "first ac": "1A",
    "first class": "1A",
    "2a": "2A",
    "2ac": "2A",
    "second ac": "2A",
    "3a": "3A",
    "3ac": "3A",
    "third ac": "3A",
    "3e": "3E",
    "ac 3": "3A",
    "ac 2": "2A",
    "ac 1": "1A",
    "sleeper": "SL",
    "sl": "SL",
    "chair car": "CC",
    "cc": "CC",
    "executive chair car": "EC",
    "ec": "EC",
    "second sitting": "2S",
    "2s": "2S",
}

_QUOTAS: dict[str, Quota] = {
    "tatkal": Quota.tatkal,
    "tatkal quota": Quota.tatkal,
    "ladies": Quota.ladies,
    "ladies quota": Quota.ladies,
    "senior": Quota.lower_berth,
    "lower berth": Quota.lower_berth,
    "student": Quota.student,
    "general": Quota.general,
}

_OPTIMISE: dict[str, Optimise] = {
    "cheapest": Optimise.cheapest,
    "cheap": Optimise.cheapest,
    "budget": Optimise.cheapest,
    "fastest": Optimise.fastest,
    "quickest": Optimise.fastest,
    "quick": Optimise.fastest,
    "least delay": Optimise.least_delay,
    "on time": Optimise.least_delay,
    "not late": Optimise.least_delay,
    "arrive late": Optimise.least_delay,
    "avoid delay": Optimise.least_delay,
    "punctual": Optimise.least_delay,
    "comfortable": Optimise.most_comfortable,
    "comfort": Optimise.most_comfortable,
    "balanced": Optimise.balanced,
}


@dataclass(frozen=True, slots=True)
class Parsed:
    """The intent, plus the questions a human still has to answer."""

    intent: BookingIntent
    ambiguities: tuple[Ambiguity, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def ready(self) -> bool:
        """Whether `POST /search` can use this intent without asking anything."""
        return (
            self.intent.origin is not None
            and self.intent.destination is not None
            and self.intent.depart_window is not None
        )


def parse(
    text: str,
    *,
    repo: TimetableRepo,
    now: dt.datetime | None = None,
    passengers: list[Passenger] | None = None,
    locale: str = "en-IN",
) -> Parsed:
    """Turn one sentence into an intent. Never raises on unparseable input.

    Unparseable is a normal state: the intent records what was understood, the
    ambiguities record what was not, and the caller asks only about the gap.
    """
    reference = now or dt.datetime.now(dt.UTC).astimezone()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=dt.UTC)

    lowered = text.lower()
    origin, destination, place_ambiguities = _places(text, repo)
    if origin is not None and destination is not None and origin.stop_id == destination.stop_id:
        # Same station on both sides is a sentence we did not understand, not a
        # journey. Saying so beats handing the engine a query it must reject.
        place_ambiguities.append(
            Ambiguity(
                field="origin",
                question=(
                    f"Both stations came out as {origin.code}. Which station are you starting from?"
                ),
            )
        )
    service_date, window, date_ambiguities = _travel_date(lowered, reference)
    classes = _classes(lowered)
    quota = _quota(lowered)
    budget = _budget(lowered)
    optimise = _optimise(lowered)

    ambiguities = list(place_ambiguities) + list(date_ambiguities)
    notes: list[str] = []

    mentioned = _passenger_count(lowered)
    if mentioned and (passengers is None or len(passengers) < mentioned):
        ambiguities.append(
            Ambiguity(
                field="passengers",
                question=(
                    f"You mentioned {mentioned} passengers. Names and ages are needed "
                    "before a booking page can be filled."
                ),
            )
        )

    if quota is Quota.tatkal:
        notes.append(
            "Tatkal booking opens one day before travel and this agent stays out of the "
            "first 30 minutes, as IRCTC requires."
        )

    depart_window = None
    if service_date is not None:
        start_hour, end_hour = window
        start = dt.datetime.combine(service_date, dt.time(start_hour % 24), tzinfo=reference.tzinfo)
        end = start + dt.timedelta(hours=end_hour - start_hour)
        depart_window = TimeWindow(start=start, end=end)

    intent = BookingIntent(
        intent_id=intent_id(),
        created_at=reference,
        # Twenty minutes is long enough to answer a clarifying question and short
        # enough that a stale intent cannot be searched against yesterday's prices.
        expires_at=reference + dt.timedelta(minutes=20),
        raw_text=text,
        locale=locale,
        origin=origin,
        destination=destination,
        depart_window=depart_window,
        modes=[Mode.rail],
        passengers=passengers,
        class_preferences=classes or None,
        quota=quota,
        budget=budget,
        optimise=optimise,
        ambiguities=[item for item in ambiguities] or None,
        confidence=_confidence(origin, destination, depart_window, classes, ambiguities),
        parsed_by=ParsedBy.rules,
    )
    return Parsed(intent=intent, ambiguities=tuple(ambiguities), notes=tuple(notes))


# -- places -------------------------------------------------------------------


def _places(text: str, repo: TimetableRepo) -> tuple[Stop | None, Stop | None, list[Ambiguity]]:
    """Origin and destination from "X to Y", resolved against the station table."""
    parts = _split_route(text)
    ambiguities: list[Ambiguity] = []

    if parts is None:
        codes = _explicit_codes(text, repo)
        if len(codes) == 2:
            return codes[0], codes[1], ambiguities
        ambiguities.append(
            Ambiguity(
                field="origin",
                question="Which two stations? For example: Mumbai CSMT to Pune.",
            )
        )
        return None, None, ambiguities

    origin_phrase, destination_phrase = parts
    origin_rows, origin_ambiguity = _resolve(origin_phrase, repo, field_name="origin")
    destination_rows, destination_ambiguity = _resolve(
        destination_phrase, repo, field_name="destination"
    )
    if not origin_rows or not destination_rows:
        ambiguities.extend([item for item in (origin_ambiguity, destination_ambiguity) if item])
        return (
            _stop(origin_rows[0]) if origin_rows else None,
            _stop(destination_rows[0]) if destination_rows else None,
            ambiguities,
        )

    origin_row, destination_row = _pick_connected(origin_rows, destination_rows, repo)

    # A tie that the timetable itself resolves is not an ambiguity any more: if only
    # one pair of stations actually has trains between them, that is the answer.
    if origin_row is not origin_rows[0] or destination_row is not destination_rows[0]:
        return _stop(origin_row), _stop(destination_row), ambiguities

    ambiguities.extend([item for item in (origin_ambiguity, destination_ambiguity) if item])
    return _stop(origin_row), _stop(destination_row), ambiguities


def _pick_connected(
    origins: list[StationRow], destinations: list[StationRow], repo: TimetableRepo
) -> tuple[StationRow, StationRow]:
    """Choose the station pair that actually has trains between them.

    This is why resolution reads the timetable and not just the station directory.
    "mumbai to pune" matches Mumbai Central first by traffic, and Mumbai Central to
    Pune has zero trains, while CSMT to Pune has fifteen. Answering the first match
    would be arithmetically defensible and practically useless, so connectivity
    outranks popularity when the two disagree.

    Falls back to the best pair when nothing connects, because a route with no
    direct trains is a real answer too.
    """
    best_pair = (origins[0], destinations[0])
    best_trains = 0
    for origin_rank, origin in enumerate(origins[:MAX_CANDIDATES]):
        for destination_rank, destination in enumerate(destinations[:MAX_CANDIDATES]):
            if origin.code == destination.code:
                continue
            count = len(repo.trains_between(origin.code, destination.code))
            # Most service wins, not merely any service: "mumbai to pune" should
            # prefer a terminus with fifteen trains over one with two, and both are
            # more useful than the busiest station in the city with none.
            score = (count, -(origin_rank + destination_rank))
            if count and score > (best_trains, -99):
                best_trains = count
                best_pair = (origin, destination)
    return best_pair


def _split_route(text: str) -> tuple[str, str] | None:
    """ "from A to B" or "A to B", stopping at the first date, class or quota word."""
    stopper = (
        r"(?:$|[.,;!?]|\b(?:on|for|in|under|below|tomorrow|today|tonight|next|this|"
        r"at|with|by|tatkal|morning|afternoon|evening|night)\b)"
    )
    with_from = re.search(
        rf"\bfrom\s+(?P<origin>[a-z0-9 .'-]+?)\s+to\s+(?P<destination>[a-z0-9 .'-]+?){stopper}",
        text,
        re.IGNORECASE,
    )
    if with_from:
        return with_from.group("origin").strip(), with_from.group("destination").strip()
    bare = re.search(
        rf"\b(?P<origin>[a-z0-9 .'-]+?)\s+to\s+(?P<destination>[a-z0-9 .'-]+?){stopper}",
        text,
        re.IGNORECASE,
    )
    if bare:
        return bare.group("origin").strip(), bare.group("destination").strip()
    return None


def _resolve(
    phrase: str, repo: TimetableRepo, *, field_name: str
) -> tuple[list[StationRow], Ambiguity | None]:
    """One phrase to one station, or to a question with the options attached.

    Three signals decide the answer, in this order, because the vendor's station
    directory is not a list of places a person can travel to: it also contains
    loco sheds, military sidings and parcel offices. Verified against the real
    table, where "mumbai" matches "Diesel Loco Shed Ltt Mumbai" as happily as it
    matches "Mumbai CSMT".

    1. an exact code or exact name wins outright;
    2. a station one of our seeded trains actually stops at beats one we cannot
       reach, because we can answer for it;
    3. a name that starts with what the user typed beats one that merely
       contains it.

    Anything still tied becomes a question. Asking costs one turn; sending
    someone to a shed costs their booking window.
    """
    words = [word for word in _words(phrase) if word not in _NOISE]
    if not words:
        return [], Ambiguity(field=field_name, question=f"Which station is the {field_name}?")

    traffic = repo.station_traffic()
    for attempt in _attempts(words):
        rows = _candidates(attempt, repo)
        if not rows:
            continue
        ordered = sorted(rows, key=lambda row: _station_rank(row, attempt, traffic))
        best = ordered[0]
        rivals = [
            row
            for row in ordered[1:]
            if _station_rank(row, attempt, traffic)[:4] == _station_rank(best, attempt, traffic)[:4]
        ]
        candidates = [best, *rivals]
        if not rivals:
            # Everything ordered is still returned: the caller may need to look past
            # the busiest station in a city for one that actually has trains to the
            # other end. That is not an ambiguity, it is a choice the timetable makes.
            return ordered, None
        options = [f"IN:{row.code.upper()}" for row in candidates][:5]
        names = ", ".join(f"{row.code} ({row.name})" for row in candidates[:4])
        return ordered, Ambiguity(
            field=field_name,
            question=f"{phrase.strip()!r} could be more than one station: {names}. Which one?",
            options=options,
        )

    return [], Ambiguity(
        field=field_name,
        question=f"I could not find a station called {phrase.strip()!r}. Which one did you mean?",
    )


def _attempts(words: list[str]) -> list[str]:
    """The phrase, then the phrase without its leading words.

    "go to delhi" must still find Delhi. Dropping from the front, not the back,
    because the useful noun in a spoken sentence is almost always last. Anything
    shorter than three characters is skipped: a single letter matches a hundred
    stations and tells us nothing.
    """
    attempts = [" ".join(words[start:]) for start in range(len(words))]
    return [attempt for attempt in attempts if len(attempt) >= 3]


#: Name fragments that belong to railway infrastructure rather than to a place a
#: passenger can board. Verified in the real table: Nagpur has four of them, and
#: a confidently resolved "Nagpur Cabin" is worse than asking.
_NOT_BOOKABLE = re.compile(
    r"\b(cabin|shed|siding|depot|workshop|yard|goods|marshalling|halt cabin)\b",
    re.IGNORECASE,
)


def _candidates(attempt: str, repo: TimetableRepo) -> list[StationRow]:
    """Rows where every typed word starts a word of the name, or the code."""
    words = attempt.split()
    kept: list[StationRow] = []
    for row in repo.search_stations(attempt, limit=40):
        if "-" in row.code or _NOT_BOOKABLE.search(row.name):
            continue
        tokens = set(re.split(r"[^a-z0-9]+", row.name.lower()))
        code = row.code.lower()
        if all(
            code.startswith(word) or any(token.startswith(word) for token in tokens)
            for word in words
        ):
            kept.append(row)
    return kept


def _station_rank(row: StationRow, attempt: str, traffic: dict[str, int]) -> tuple:
    """Lower is better: the four signals that matter, then two stable tiebreakers.

    Traffic (how many stop records name this station) is the tiebreaker that
    turns "delhi" into New Delhi and Hazrat Nizamuddin, its two busiest, instead
    of Delhi Kishan Ganj, whose name happens to start with the word typed.
    """
    name = row.name.lower()
    return (
        0 if row.code.lower() == attempt.lower() else 1,
        0 if name == attempt.lower() else 1,
        -traffic.get(row.code.upper(), 0),
        0 if name.startswith(attempt.lower()) else 1,
        len(name),
        name,
    )


def _explicit_codes(text: str, repo: TimetableRepo) -> list[Stop]:
    """Station codes typed directly, in the order they appear."""
    stops: list[Stop] = []
    for token in re.findall(r"\b[A-Za-z]{2,5}\b", text):
        row = repo.station(token.upper())
        if row is not None and row.code.upper() == token.upper():
            stops.append(_stop(row))
    return stops


def _stop(row: StationRow) -> Stop:
    """A wire `Stop` from a station row, with geo left out when the vendor has none."""
    return Stop(
        stop_id=f"IN:{row.code.upper()}",
        name=row.name,
        code=row.code.upper(),
        city=None,
        country="IN",
        geo=GeoPoint(lat=row.lat, lon=row.lon)
        if row.lat is not None and row.lon is not None
        else None,
        timezone="Asia/Kolkata",
        modes=[Mode.rail],
    )


def _words(phrase: str) -> list[str]:
    return [word for word in re.split(r"[^a-z0-9]+", phrase.lower()) if word]


# -- date and time ------------------------------------------------------------


def _travel_date(
    text: str, now: dt.datetime
) -> tuple[dt.date | None, tuple[int, int], list[Ambiguity]]:
    today = now.date()

    if "day after tomorrow" in text:
        return today + dt.timedelta(days=2), (0, 24), []
    if "tomorrow" in text:
        return today + dt.timedelta(days=1), _window(text), []
    if "today" in text or "tonight" in text:
        return today, _window(text), []

    relative = re.search(r"\bin\s+(\d{1,2})\s+days?\b", text)
    if relative:
        return today + dt.timedelta(days=int(relative.group(1))), _window(text), []

    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso:
        parsed = _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), now)
        if parsed:
            return parsed, _window(text), []

    slash = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
    if slash:
        day, month = int(slash.group(1)), int(slash.group(2))
        year = _year(slash.group(3), now)
        parsed = _safe_date(year, month, day, now)
        if parsed:
            return parsed, _window(text), []

    named = re.search(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+([a-z]{3,9})\b", text)
    if named and named.group(2)[:3] in _MONTHS:
        parsed = _safe_date(now.year, _MONTHS[named.group(2)[:3]], int(named.group(1)), now)
        if parsed:
            return parsed, _window(text), []

    for name, weekday in _WEEKDAYS.items():
        if name not in text:
            continue
        ahead = (weekday - today.weekday()) % 7
        if "next" in text and ahead == 0:
            ahead = 7
        # A bare weekday means the next one, not the one a week later.
        return today + dt.timedelta(days=ahead), _window(text), []

    return (
        None,
        (0, 24),
        [
            Ambiguity(
                field="depart_window",
                question=(
                    "Which date are you travelling? For example: tomorrow, 26 Oct, or this Friday."
                ),
            )
        ],
    )


def _window(text: str) -> tuple[int, int]:
    """Four hours around an explicitly stated time, else the word, else the day.

    An explicit clock time is the only precise thing a user can give us, so it
    wins. "6am" becomes 06:00 to 10:00, which is the shape of a real choice
    rather than a guess at the exact minute they can reach the station.
    """
    explicit = _explicit_hour(text)
    if explicit is not None:
        end = explicit + 4
        return (explicit, end if end <= 24 else 28)
    for phrase, window in _WINDOWS.items():
        if phrase in text:
            return window
    return (0, 24)


def _explicit_hour(text: str) -> int | None:
    """A 24-hour hour from "6am", "6:30 pm" or "at 18:00", or None."""
    match = re.search(r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)\b", text)
    if match is None:
        # No am/pm, so the clock time has to be introduced by a word: "at 18:30",
        # never a bare number, which is far more likely to be a date or a fare.
        match = re.search(
            r"\b(?:at|by|around|before)\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\b", text
        )
    if match is None:
        return None
    hour = int(match.group("hour"))
    meridiem = match.groupdict().get("meridiem")
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23:
        return None
    return hour


def _safe_date(year: int, month: int, day: int, now: dt.datetime) -> dt.date | None:
    try:
        candidate = dt.date(year, month, day)
    except ValueError:
        return None
    if now.tzinfo is not None and candidate < now.date():
        # A day and month without a year that has already passed means next year.
        try:
            candidate = dt.date(year + 1, month, day)
        except ValueError:
            return None
    return candidate


def _year(raw: str | None, now: dt.datetime) -> int:
    if raw is None:
        return now.year
    value = int(raw)
    return value + 2000 if value < 100 else value


# -- the rest of the sentence -------------------------------------------------


def _classes(text: str) -> list[str]:
    found: list[str] = []
    for phrase, code in _CLASSES.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) and code not in found:
            found.append(code)
    return found


def _quota(text: str) -> Quota:
    for phrase, quota in _QUOTAS.items():
        if phrase in text:
            return quota
    return Quota.general


def _budget(text: str):
    match = re.search(
        r"(?:under|below|less than|upto|up to|max)\s*(?:rs\.?|inr|₹)?\s*([\d,]+)", text
    )
    if match is None:
        return None
    amount = int(match.group(1).replace(",", ""))
    # Below a hundred is a passenger count or a class number, not a fare.
    return inr(amount) if amount >= 100 else None


def _optimise(text: str) -> Optimise:
    for phrase, choice in _OPTIMISE.items():
        if phrase in text:
            return choice
    return Optimise.balanced


def _passenger_count(text: str) -> int | None:
    match = re.search(r"\b(\d)\s*(?:people|passengers?|persons?|adults?|tickets?)\b", text)
    if match is None:
        return None
    return int(match.group(1))


def _confidence(
    origin: Stop | None,
    destination: Stop | None,
    window: TimeWindow | None,
    classes: list[str],
    ambiguities: list[Ambiguity],
) -> float:
    """Fixed increments, so the number is reproducible by reading this function."""
    value = 0.20
    if origin is not None:
        value += 0.30
    if destination is not None:
        value += 0.30
    if window is not None:
        value += 0.15
    if classes:
        value += 0.05
    value -= 0.05 * len(ambiguities)
    # A rule-based read is capped below certainty on purpose: without a model
    # confirming it, 0.9 is as confident as a regular expression may be.
    return round(max(0.05, min(0.9, value)), 3)
