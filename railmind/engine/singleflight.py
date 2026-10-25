"""Single-flight: one upstream call per question, however many people ask at once.

Why this exists, in the project's own terms. A hundred students searching CSMT to
PUNE tomorrow at 10:00:00 is one question asked a hundred times. Without
coalescing that is a hundred vendor calls against a hundred-request monthly
budget, which is the difference between a working demo and a dead key. It is also
the exact pattern that makes a service look like a bot: our own retries, amplified.

Two things this is not:

- It is not a cache. A cache answers later from a stored value; single-flight only
  merges work that is happening right now. Once the call finishes, the entry is
  gone and the next caller asks again. That keeps it honest about freshness.
- It is not a lock. Nobody queues. Late arrivals join the in-progress call; after
  it resolves, the next arrival starts a new one.

The cancellation rule is the part that needs care. If the leader is cancelled -
and in this codebase it will be, every time a deadline expires - every waiter must
be released rather than left hanging on a future nobody will complete. So the
leader cancels the shared future on the way out, and waiters see `CancelledError`
from their own await, which is what they would have got anyway from their deadline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import TypeVar

T = TypeVar("T")


class SingleFlight:
    """Keyed coalescing of concurrent coroutines. One execution per key at a time."""

    def __init__(self) -> None:
        self._inflight: dict[Hashable, asyncio.Future] = {}
        #: Keys whose execution actually ran. The number that costs quota.
        self.leaders = 0
        #: Callers served by someone else's execution. The number that was saved.
        self.joiners = 0

    async def do(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        """Run `factory` once for `key`, or join the run already in progress."""
        existing = self._inflight.get(key)
        if existing is not None:
            self.joiners += 1
            # Shielded: a waiter's own deadline must not cancel the shared work,
            # because other waiters may still be inside their deadlines.
            return await asyncio.shield(existing)

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        self.leaders += 1

        try:
            result = await factory()
        except asyncio.CancelledError:
            # The leader died. Release the waiters with the same outcome rather
            # than leaving them on a future that will never resolve.
            if not future.done():
                future.cancel()
            raise
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
                # Mark retrieved: if nobody joined, Python would otherwise log an
                # "exception was never retrieved" warning at gc time. Waiters still
                # receive the raise when they await.
                future.exception()
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    def stats(self) -> dict[str, int]:
        return {"leaders": self.leaders, "joiners": self.joiners, "inflight": self.inflight}
