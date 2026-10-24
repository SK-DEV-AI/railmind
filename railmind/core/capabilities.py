"""Capabilities: what a provider can do, asked as data instead of as a type.

The rule this module exists to enforce: nothing anywhere may write
`if isinstance(provider, RailProvider)`. The planner asks "which providers can
answer `live` for mode `metro` in region `IN`?" and gets a list. Adding a mode is
then registering a provider and its capabilities, not editing a chain of branches
in the engine, the API, and the UI.

`Capabilities` subclasses the generated wire model on purpose. The contract stays
the single source of truth for the serialised shape, and the behaviour lives
here, so the two can never drift.
"""

from __future__ import annotations

from enum import StrEnum

from railmind.core.errors import NotSupported
from railmind.wire.models import Booking, Cancel, Mode
from railmind.wire.models import Capabilities as WireCapabilities


class Feature(StrEnum):
    """A capability, named for what the caller wants, not for how it is served."""

    SEARCH = "search"
    AVAILABILITY = "availability"
    PRICE = "price"
    LIVE = "live"
    BOOKING = "booking"
    SEAT_MAP = "seat_map"
    SEAT_SELECT = "seat_select"
    CANCEL = "cancel"


class Capabilities(WireCapabilities):
    """Typed view over `wire.models.Capabilities` with the questions we actually ask."""

    def features(self) -> frozenset[Feature]:
        """Every feature this provider can serve right now.

        `booking` and `cancel` are tri-state on the wire (`api`, `deeplink`,
        `none`). They count as a feature only when the provider can at least hand
        off; `none` means the leg is informational only.
        """
        found: set[Feature] = set()
        if self.search:
            found.add(Feature.SEARCH)
        if self.availability:
            found.add(Feature.AVAILABILITY)
        if self.price:
            found.add(Feature.PRICE)
        if self.live:
            found.add(Feature.LIVE)
        if self.booking in (Booking.api, Booking.deeplink):
            found.add(Feature.BOOKING)
        if self.seat_map:
            found.add(Feature.SEAT_MAP)
        if self.seat_select:
            found.add(Feature.SEAT_SELECT)
        if self.cancel in (Cancel.api, Cancel.deeplink):
            found.add(Feature.CANCEL)
        return frozenset(found)

    def supports(self, feature: Feature | str) -> bool:
        """True when the feature is available. Never raises, safe for UI branching."""
        return Feature(feature) in self.features()

    def missing(self) -> frozenset[Feature]:
        """The features this provider cannot serve. Useful for honest UI copy."""
        return frozenset(Feature) - self.features()

    def require(self, feature: Feature | str, *, provider_id: str | None = None) -> None:
        """Raise `NotSupported` if the feature is absent.

        Called at the top of a provider method so an unsupported call fails with a
        normalised error instead of a `NotImplementedError` leaking out of a
        half-written adapter.
        """
        wanted = Feature(feature)
        if not self.supports(wanted):
            raise NotSupported(
                f"This provider cannot {wanted.value.replace('_', ' ')}",
                provider_id=provider_id,
            )

    def summary(self) -> str:
        """One line for logs and the report: the feature names, space separated."""
        have = sorted(f.value for f in self.features())
        return " ".join(have) if have else "none"


def rail_capabilities() -> Capabilities:
    """Indian Railways as we actually wire it: read everything, book by hand off.

    `booking=DEEPLINK` states a deliberate legal position rather than a missing
    feature: our rail path must never submit a booking.
    """
    return Capabilities(
        search=True,
        availability=True,
        price=True,
        live=True,
        booking=Booking.deeplink,
        seat_map=False,
        seat_select=False,
        cancel=Cancel.none,
    )


def static_capabilities(modes: set[Mode]) -> Capabilities:
    """A timetable-only provider: it can search and price, nothing live, no booking."""
    if not modes:
        raise ValueError("a provider must serve at least one mode")
    return Capabilities(
        search=True,
        availability=False,
        price=False,
        live=False,
        booking=Booking.none,
        cancel=Cancel.none,
    )
