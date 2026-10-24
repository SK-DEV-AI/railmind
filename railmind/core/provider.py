"""The provider protocol: five verbs, no provider-specific vocabulary.

A provider is anything that can answer these five questions for its mode. The
engine knows nothing else about it. That is the whole multi-modal plan, in one
file: rail today, metro or a Beckn seller tomorrow, and the engine does not move.

`BaseProvider` is the set of behaviours every adapter would otherwise copy: the
capability gate, the wire description, and honest failure when a verb is not
implemented.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from railmind.core.capabilities import Capabilities, Feature
from railmind.core.errors import NotSupported
from railmind.core.types import (
    Availability,
    Handoff,
    Itinerary,
    Leg,
    LiveStatus,
    Mode,
    Money,
    Passenger,
    ProviderInfo,
    Query,
)


@runtime_checkable
class TransportProvider(Protocol):
    """What the engine and the registry are allowed to assume about any provider.

    Every verb is async because every real one touches the network, and a sync
    interface would push a thread pool into every call site.

    Every verb raises only `railmind.core.errors.ProviderError` subclasses. If an
    adapter lets an `httpx` or `json` exception escape, it has broken its contract.
    """

    id: str
    display_name: str
    modes: frozenset[Mode]
    regions: frozenset[str]
    capabilities: Capabilities

    async def search(self, query: Query) -> list[Itinerary]:
        """Candidate itineraries for a query. May return an empty list, never `None`."""
        ...

    async def availability(self, itinerary: Itinerary) -> Availability:
        """Current seat state. Must set `checked_at` and `stale=False` for live reads."""
        ...

    async def price(self, itinerary: Itinerary) -> Money:
        """Itemised total for the itinerary as it stands."""
        ...

    async def live(self, leg: Leg) -> LiveStatus:
        """Position and delay for a leg that is running or about to."""
        ...

    async def handoff(self, itinerary: Itinerary, passengers: Sequence[Passenger]) -> Handoff:
        """Build the human's path to a real booking. Never books.

        A provider whose capabilities say `booking=none` must raise `NotSupported`
        here rather than return a link to nothing.
        """
        ...


class BaseProvider:
    """Convenience base for real adapters. Optional; the protocol is the contract.

    Subclasses set `id`, `display_name`, `modes`, `regions`, `capabilities`, and
    implement the verbs they advertise. Anything they advertise but do not
    implement fails with `NotSupported` instead of `NotImplementedError`, which
    keeps the error set closed.
    """

    id: str = ""
    display_name: str = ""
    modes: frozenset[Mode] = frozenset()
    regions: frozenset[str] = frozenset()
    capabilities: Capabilities
    #: Text a UI must show to satisfy a data source's terms. Empty when not required.
    attribution: str = ""

    def __init__(self) -> None:
        if not self.id:
            raise ValueError(f"{type(self).__name__} must set an id")
        if not self.modes:
            raise ValueError(f"{type(self).__name__} must serve at least one mode")

    # -- helpers used by adapters --

    def _require(self, feature: Feature) -> None:
        """Gate a verb on the advertised capability, with a normalised error."""
        self.capabilities.require(feature, provider_id=self.id)

    def info(self) -> ProviderInfo:
        """The wire description used by `GET /providers` and by the report."""
        return ProviderInfo(
            provider_id=self.id,
            display_name=self.display_name or self.id,
            modes=sorted(self.modes, key=lambda m: m.value),
            capabilities=self.capabilities,
            regions=sorted(self.regions),
            enabled=True,
            attribution=self.attribution or None,
        )

    # -- safe defaults: unimplemented verbs fail inside the closed error set --

    async def search(self, query: Query) -> list[Itinerary]:
        self._require(Feature.SEARCH)
        raise NotSupported(
            f"{self.display_name or self.id} has no search implementation",
            provider_id=self.id,
        )

    async def availability(self, itinerary: Itinerary) -> Availability:
        self._require(Feature.AVAILABILITY)
        raise NotSupported(
            f"{self.display_name or self.id} cannot check availability",
            provider_id=self.id,
        )

    async def price(self, itinerary: Itinerary) -> Money:
        self._require(Feature.PRICE)
        raise NotSupported(
            f"{self.display_name or self.id} cannot price this itinerary",
            provider_id=self.id,
        )

    async def live(self, leg: Leg) -> LiveStatus:
        self._require(Feature.LIVE)
        raise NotSupported(
            f"{self.display_name or self.id} has no live data",
            provider_id=self.id,
        )

    async def handoff(self, itinerary: Itinerary, passengers: Sequence[Passenger]) -> Handoff:
        self._require(Feature.BOOKING)
        raise NotSupported(
            f"{self.display_name or self.id} cannot hand off this itinerary",
            provider_id=self.id,
        )
