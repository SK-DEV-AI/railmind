"""Capabilities and the registry: the agnosticism claim, tested."""

from __future__ import annotations

import pytest

from railmind.core.capabilities import (
    Capabilities,
    Feature,
    rail_capabilities,
    static_capabilities,
)
from railmind.core.errors import NotSupported, UnknownProvider
from railmind.core.provider import BaseProvider
from railmind.core.types import Mode
from railmind.providers.fake import FakeProvider, TimetableProvider
from railmind.providers.registry import ProviderRegistry
from railmind.wire.models import Booking, Cancel, Code


class TestFeatures:
    def test_rail_advertises_reads_and_a_deeplink(self):
        caps = rail_capabilities()
        assert caps.supports(Feature.SEARCH)
        assert caps.supports(Feature.AVAILABILITY)
        assert caps.supports(Feature.PRICE)
        assert caps.supports(Feature.LIVE)
        assert caps.supports(Feature.BOOKING)

    def test_deeplink_booking_counts_as_bookable_none_does_not(self):
        deeplink = rail_capabilities()
        none = static_capabilities({Mode.metro})
        assert Feature.BOOKING in deeplink.features()
        assert Feature.BOOKING not in none.features()

    def test_rail_cannot_cancel(self):
        assert not rail_capabilities().supports(Feature.CANCEL)

    def test_require_raises_not_supported_with_provider_id(self):
        caps = static_capabilities({Mode.metro})
        with pytest.raises(NotSupported) as caught:
            caps.require(Feature.LIVE, provider_id="metro_delhi")
        problem = caught.value.to_problem()
        assert problem.code is Code.not_supported
        assert problem.provider_id == "metro_delhi"

    def test_missing_lists_what_is_absent(self):
        missing = rail_capabilities().missing()
        assert Feature.SEAT_MAP in missing
        assert Feature.SEARCH not in missing

    def test_summary_is_stable(self):
        assert rail_capabilities().summary().split() == [
            "availability",
            "booking",
            "live",
            "price",
            "search",
        ]

    def test_static_capabilities_refuses_empty_modes(self):
        with pytest.raises(ValueError):
            static_capabilities(set())


class TestRegistry:
    def test_register_and_get(self):
        registry = ProviderRegistry()
        provider = FakeProvider()
        registry.register(provider)
        assert len(registry) == 1
        assert registry.get("fake_rail") is provider
        assert "fake_rail" in registry

    def test_duplicate_id_is_refused(self):
        registry = ProviderRegistry()
        registry.register(FakeProvider())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(FakeProvider())

    def test_unknown_provider_raises_normalised_error(self):
        registry = ProviderRegistry()
        with pytest.raises(UnknownProvider) as caught:
            registry.get("nope")
        assert caught.value.to_problem().status == 404

    def test_find_by_feature(self):
        registry = ProviderRegistry()
        registry.register(FakeProvider())
        registry.register(TimetableProvider())
        assert [p.id for p in registry.find(Feature.LIVE)] == ["fake_rail"]
        assert {p.id for p in registry.find(Feature.SEARCH)} == {"fake_rail", "fake_timetable"}

    def test_find_by_mode(self):
        registry = ProviderRegistry()
        registry.register(FakeProvider())
        registry.register(TimetableProvider())
        assert [p.id for p in registry.find(Feature.SEARCH, mode=Mode.metro)] == ["fake_timetable"]
        assert [p.id for p in registry.find(Feature.SEARCH, mode="rail")] == ["fake_rail"]

    def test_find_by_region(self):
        registry = ProviderRegistry()
        registry.register(FakeProvider())
        assert registry.find(Feature.SEARCH, region="in")
        assert registry.find(Feature.SEARCH, region="FR") == []

    def test_info_is_sorted_and_carries_capabilities(self):
        registry = ProviderRegistry()
        registry.register(TimetableProvider())
        registry.register(FakeProvider())
        info = registry.info()
        assert [item.provider_id for item in info] == ["fake_rail", "fake_timetable"]
        assert info[0].capabilities.live is True
        assert info[1].capabilities.live is False

    def test_capability_matrix(self):
        registry = ProviderRegistry()
        registry.register(FakeProvider())
        matrix = registry.capability_matrix()
        assert matrix["fake_rail"]["live"] is True
        assert matrix["fake_rail"]["cancel"] is False

    def test_provider_without_capabilities_is_refused(self):
        class NoCaps:
            id = "no_caps"
            modes = frozenset({Mode.rail})

            async def search(self, query):  # pragma: no cover - never called
                return []

            async def availability(self, itinerary):  # pragma: no cover
                return None

            async def price(self, itinerary):  # pragma: no cover
                return None

            async def live(self, leg):  # pragma: no cover
                return None

            async def handoff(self, itinerary, passengers):  # pragma: no cover
                return None

        with pytest.raises(TypeError, match="Capabilities"):
            ProviderRegistry().register(NoCaps())

    def test_provider_that_cannot_search_is_refused(self):
        class Unsearchable(FakeProvider):
            id = "unsearchable"
            capabilities = Capabilities(
                search=False,
                availability=False,
                price=False,
                live=False,
                booking=Booking.none,
                cancel=Cancel.none,
            )

        with pytest.raises(ValueError, match="cannot search"):
            ProviderRegistry().register(Unsearchable())

    def test_unregister(self):
        registry = ProviderRegistry()
        registry.register(FakeProvider())
        registry.unregister("fake_rail")
        assert len(registry) == 0


class TestCapabilityGating:
    async def test_unsupported_verb_raises_not_implemented_error(self):
        """A missing verb must fail with a provider error, not NotImplementedError."""
        provider = TimetableProvider()
        with pytest.raises(NotSupported):
            await provider.live(None)  # type: ignore[arg-type]

    async def test_missing_implementation_on_an_advertised_verb_is_not_supported(self):
        class AdvertisesButDoesNotImplement(FakeProvider):
            id = "half_done"
            live = BaseProvider.live

        provider = AdvertisesButDoesNotImplement()
        with pytest.raises(NotSupported, match="no live data"):
            await provider.live(None)  # type: ignore[arg-type]
