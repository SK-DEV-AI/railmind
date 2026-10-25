"""Freshness policy: how old each kind of fact is allowed to get, and why.

The question this file answers, in the project's own words: "if we crawl the whole
timetable, does it go stale, and how do we keep it up to date?"

The honest answer is that railway data has a **volatility ladder**, and the mistake
is treating it as one thing:

| Tier | What | Changes | How we keep it |
|---|---|---|---|
| Live | position, delay | seconds | per request, 60 s TTL |
| Availability | seats, waitlist | minutes | 15 min TTL, finalists only |
| Operational | runs today, diversions | daily | run-days + exceptions table |
| Schedule | times, halts, distances | yearly-ish | rolling refresh, hashed |
| Static | station names, coordinates | years | with the schedule sweep |

Two consequences that matter more than the numbers:

1. **You never need to keep everything current, you need to keep what is being
   asked current.** A corridor nobody searches can be 90 days old and harm nobody.
   So freshness is enforced per corridor, on demand, not by a nightly full crawl.
2. **A refresh must be able to say "unchanged".** Otherwise every sweep rewrites
   every row and you cannot tell a quiet week from a broken feed. Every schedule is
   stored with a content hash, and a refresh reports new, changed, or same.

The thresholds below are policy, not measurements. They are set generously on
purpose: being wrong about a timetable age costs a slightly inaccurate departure
time, while hammering the free tier costs the whole project.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

#: A schedule older than this must be refreshed before we answer a search for it.
#: Thirty days is a compromise: revisions happen a few times a year, but halts and
#: timings move more often than the annual book, and 30 days keeps the worst case to
#: a rounding error rather than a wrong train.
SCHEDULE_MAX_AGE_DAYS = 30

#: Station names and coordinates change on the order of years. Refreshing them with
#: the schedule sweep is free, so this is only a backstop.
STATIC_MAX_AGE_DAYS = 180

#: Run-day exceptions (special trains, cancellations, diversions) are daily facts and
#: belong to the operational tier, not here. Recorded so the ladder is complete in
#: one place.
OPERATIONAL_MAX_AGE_HOURS = 24


@dataclass(frozen=True, slots=True)
class Age:
    """How old a fact is, with the judgement already made about it."""

    seconds: float
    limit_seconds: float
    tier: str

    @property
    def days(self) -> float:
        return self.seconds / 86_400

    @property
    def stale(self) -> bool:
        return self.seconds > self.limit_seconds

    def describe(self) -> str:
        """One line a human can read, used in reports and in /health notes."""
        age = _humanise(self.seconds)
        limit = _humanise(self.limit_seconds)
        state = "stale" if self.stale else "fresh"
        return f"{self.tier} data is {age} old ({state}, limit {limit})"


def age_of(
    fetched_at: dt.datetime | None,
    *,
    now: dt.datetime | None = None,
    tier: str = "schedule",
) -> Age:
    """Classify one timestamp against its tier's limit.

    A missing timestamp is treated as infinitely old, not as fresh. An unknown age
    is the strongest reason to refresh, and the one state a naive implementation
    always gets wrong.
    """
    reference = now or dt.datetime.now(dt.UTC)
    if fetched_at is None:
        return Age(seconds=float("inf"), limit_seconds=_limit_for(tier), tier=tier)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=dt.UTC)
    seconds = (reference - fetched_at).total_seconds()
    return Age(seconds=max(0.0, seconds), limit_seconds=_limit_for(tier), tier=tier)


def _limit_for(tier: str) -> float:
    if tier == "schedule":
        return SCHEDULE_MAX_AGE_DAYS * 86_400.0
    if tier == "static":
        return STATIC_MAX_AGE_DAYS * 86_400.0
    if tier == "operational":
        return OPERATIONAL_MAX_AGE_HOURS * 3_600.0
    raise ValueError(f"unknown freshness tier {tier!r}")


def _humanise(seconds: float) -> str:
    if seconds == float("inf"):
        return "an unknown amount of time"
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} minutes"
    if seconds < 48 * 3_600:
        return f"{seconds / 3_600:.1f} hours"
    return f"{seconds / 86_400:.0f} days"
