"""Step 1 acceptance, straight from BUILD-ORDER:

"a fake provider can be registered, searched, and never leaks a provider-specific
error."

Plus the rule that makes the abstraction real: no branch anywhere may ask what
class a provider is.
"""

from __future__ import annotations

import pathlib

import pytest

from railmind.core.capabilities import Feature
from railmind.core.errors import (
    NotSupported,
    ProviderError,
    RateLimited,
    SoldOut,
    UpstreamDown,
    as_problem,
)
from railmind.core.ids import parse_leg_id
from railmind.core.types import Mode
from railmind.providers.fake import BrokenProvider, FakeProvider, make_itinerary
from railmind.providers.registry import ProviderRegistry
from railmind.wire.models import Code

VERBS = ("search", "availability", "price", "live", "handoff")


class TestEndToEndWithFake:
    async def test_register_then_search(self, query):
        registry = ProviderRegistry()
        registry.register(FakeProvider())

        providers = registry.find(Feature.SEARCH, mode=Mode.rail, region="IN")
        assert len(providers) == 1

        itineraries = await providers[0].search(query)
        assert len(itineraries) == 1

        itinerary = itineraries[0]
        assert itinerary.itinerary_id.startswith("itin_")
        # The ids the engine will cache and link are derivable and parseable.
        assert parse_leg_id(itinerary.legs[0].leg_id).provider_id == "fake_rail"
        assert itinerary.total_price is not None
        assert itinerary.modes == [Mode.rail]

    async def test_availability_round_trip(self, query):
        provider = FakeProvider()
        itinerary = (await provider.search(query))[0]
        availability = await provider.availability(itinerary)
        assert availability.status == "available"
        assert availability.source == "fake_rail"
        assert availability.stale is False

    async def test_handoff_never_claims_to_book(self, query):
        provider = FakeProvider()
        itinerary = (await provider.search(query))[0]
        handoff = await provider.handoff(itinerary, [])

        assert handoff.method == "deeplink"
        assert "does not book" in handoff.legal_notice.lower()
        assert handoff.steps[0].requires == "human"

    async def test_provider_info_comes_from_capabilities_not_a_class_name(self):
        provider = FakeProvider()
        info = provider.info()
        assert info.provider_id == "fake_rail"
        assert info.capabilities.booking.value == "deeplink"
        assert info.modes == [Mode.rail]

    async def test_every_verb_is_present_on_the_protocol(self):
        provider = FakeProvider()
        for verb in VERBS:
            assert callable(getattr(provider, verb, None)), verb


class TestNoProviderErrorLeaks:
    @pytest.mark.parametrize("verb", VERBS)
    @pytest.mark.parametrize("error", [RateLimited, UpstreamDown, SoldOut, NotSupported])
    async def test_provider_raises_only_normalised_errors(self, verb, error, query):
        # One error injected per verb; every one must be a ProviderError, and every
        # one must serialise to a code from the contract's closed set.
        provider = FakeProvider(fail={verb: error})
        itinerary = make_itinerary()

        call = {
            "search": lambda: provider.search(query),
            "availability": lambda: provider.availability(itinerary),
            "price": lambda: provider.price(itinerary),
            "live": lambda: provider.live(itinerary.legs[0]),
            "handoff": lambda: provider.handoff(itinerary, []),
        }[verb]

        with pytest.raises(ProviderError) as caught:
            await call()

        problem = caught.value.to_problem(request_id="req_x")
        assert problem.code in set(Code)
        assert problem.request_id == "req_x"

    async def test_unexpected_exception_is_converted_and_its_text_never_survives(self, query):
        with pytest.raises(RuntimeError):
            await BrokenProvider().search(query)

        # At the API edge this is what a client would receive.
        try:
            await BrokenProvider().search(query)
        except RuntimeError as exc:
            problem = as_problem(exc, request_id="req_y")

        assert problem.code is Code.internal
        assert problem.status == 500
        assert "secret upstream detail" not in problem.model_dump_json()


class TestNoTypeBranches:
    """The abstraction is only real if nothing asks a provider what it is.

    The scan looks at *code*, not at prose: comments and docstrings are allowed
    to name the anti-pattern they forbid, which is how a future contributor
    learns the rule instead of rediscovering it.
    """

    FORBIDDEN = (
        "isinstance(provider",
        "isinstance(prov,",
        "type(provider)",
        "RailProvider",
        "MetroProvider(",
    )

    @staticmethod
    def code_only(text: str) -> str:
        """Strip comments and string literals, keeping executable code."""
        import io
        import tokenize

        pieces: list[str] = []
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            for token in tokens:
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                pieces.append(token.string)
        except tokenize.TokenError:
            return text
        return "\n".join(pieces)

    def test_no_provider_type_branches_in_source(self):
        root = pathlib.Path(__file__).resolve().parent.parent / "railmind"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            code = self.code_only(path.read_text(encoding="utf-8"))
            for needle in self.FORBIDDEN:
                if needle in code:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == [], offenders

    def test_engine_never_imports_a_concrete_provider(self):
        """core/ may only know the protocol, never an adapter."""
        core = pathlib.Path(__file__).resolve().parent.parent / "railmind" / "core"
        offenders = [
            path.name
            for path in core.rglob("*.py")
            if "from railmind.providers" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], offenders
