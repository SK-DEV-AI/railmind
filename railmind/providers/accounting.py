"""Counting outbound upstream calls, per request, without a global counter.

Why this is not a simple integer on the client. `/metrics` reports `provider_calls`,
which the contract defines as "outbound upstream requests in the window, the number
that must stay small on a free tier". A naive implementation reads a counter before
and after a request and subtracts. Under a burst every request reads the same counter
at nearly the same instant, so each reports a delta that includes other requests'
calls, and the total inflates. That is what happened here first: a 5-request burst
that made one call reported five.

So the counter is carried in a `ContextVar` holding a per-request mutable dict. The
engine opens a meter for one request; every HTTP call made inside it increments that
same dict. Tasks inherit the context, and because the dict itself is shared by
reference, increments land where the engine can read them.

The rules this keeps true:

- a search answered entirely from the local timetable reports **zero** upstream
  calls, because nothing left the machine;
- a search that hits the vendor once reports one, whether or not it was coalesced
  with a sibling request;
- code that is not inside a meter simply does not count, which is what a test, a
  script, or a CLI call should do.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypedDict


class Metered(TypedDict):
    calls: int


#: The meter for the request currently being served, or None when nothing is counting.
_CURRENT: ContextVar[Metered | None] = ContextVar("railmind_upstream_meter", default=None)


@contextmanager
def meter() -> Iterator[Metered]:
    """Count upstream calls made inside this block. Yields the counter it fills."""
    counter: Metered = {"calls": 0}
    token = _CURRENT.set(counter)
    try:
        yield counter
    finally:
        _CURRENT.reset(token)


def count_upstream() -> None:
    """Record one outbound call. A no-op outside a meter, by design."""
    current = _CURRENT.get()
    if current is not None:
        current["calls"] += 1


def current_calls() -> int | None:
    """Calls counted so far for this request, or None when nothing is counting."""
    current = _CURRENT.get()
    return None if current is None else current["calls"]
