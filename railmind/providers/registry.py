"""The registry: providers announce what they can do, callers ask by capability.

`find(Feature.LIVE, mode=Mode.metro)` is the whole API surface the planner needs.
There is no `if provider is RailProvider` anywhere in the engine, because the
engine never learns a provider's class name.

Registration validates shapes at startup rather than trusting adapters. A
provider that forgets `capabilities` must fail when the app boots, not when a
user taps pay.
"""

from __future__ import annotations

from collections.abc import Iterator

from railmind.core.capabilities import Capabilities, Feature
from railmind.core.errors import UnknownProvider
from railmind.core.provider import TransportProvider
from railmind.core.types import Mode, ProviderInfo


class ProviderRegistry:
    """An ordered set of providers, keyed by id, queryable by capability."""

    def __init__(self) -> None:
        self._providers: dict[str, TransportProvider] = {}

    # -- registration --

    def register(self, provider: TransportProvider) -> None:
        """Add a provider. Raises on anything that would fail later at request time."""
        provider_id = getattr(provider, "id", "")
        if not provider_id:
            raise ValueError("provider has no id")
        if provider_id in self._providers:
            raise ValueError(f"provider already registered: {provider_id!r}")

        modes = getattr(provider, "modes", frozenset())
        if not modes:
            raise ValueError(f"provider {provider_id!r} serves no modes")

        capabilities = getattr(provider, "capabilities", None)
        if not isinstance(capabilities, Capabilities):
            # Structural check on our own capability type, not a provider-type branch.
            raise TypeError(
                f"provider {provider_id!r} must expose railmind Capabilities, "
                f"got {type(capabilities).__name__}"
            )
        if not capabilities.supports(Feature.SEARCH):
            raise ValueError(f"provider {provider_id!r} cannot search, so it can never be used")

        for verb in ("search", "availability", "price", "live", "handoff"):
            if not callable(getattr(provider, verb, None)):
                raise TypeError(f"provider {provider_id!r} is missing {verb}()")

        self._providers[provider_id] = provider

    def unregister(self, provider_id: str) -> None:
        self.get(provider_id)
        del self._providers[provider_id]

    # -- lookup --

    def get(self, provider_id: str) -> TransportProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise UnknownProvider(
                f"No provider registered as {provider_id!r}",
                provider_id=provider_id,
            ) from exc

    def find(
        self,
        feature: Feature | str,
        *,
        mode: Mode | str | None = None,
        region: str | None = None,
    ) -> list[TransportProvider]:
        """Providers that can serve `feature`, optionally narrowed by mode and region.

        Returns providers in registration order, so results are reproducible and a
        demo does not reshuffle itself between runs.
        """
        wanted = Feature(feature)
        wanted_mode = Mode(mode) if mode is not None else None
        region_code = region.upper() if region else None

        found: list[TransportProvider] = []
        for provider in self._providers.values():
            if not provider.capabilities.supports(wanted):
                continue
            if wanted_mode is not None and wanted_mode not in provider.modes:
                continue
            if region_code is not None and region_code not in {r.upper() for r in provider.regions}:
                continue
            found.append(provider)
        return found

    def for_mode(self, mode: Mode | str) -> list[TransportProvider]:
        wanted = Mode(mode)
        return [p for p in self._providers.values() if wanted in p.modes]

    # -- description for the API and the report --

    def info(self) -> list[ProviderInfo]:
        """`GET /providers` payload. Sorted, so the output is diffable."""
        result: list[ProviderInfo] = []
        for provider in self._providers.values():
            describe = getattr(provider, "info", None)
            if callable(describe):
                result.append(describe())
            else:
                result.append(
                    ProviderInfo(
                        provider_id=provider.id,
                        display_name=getattr(provider, "display_name", provider.id),
                        modes=sorted(provider.modes, key=lambda m: m.value),
                        capabilities=provider.capabilities,
                        regions=sorted(getattr(provider, "regions", frozenset())),
                        enabled=True,
                    )
                )
        return sorted(result, key=lambda info: info.provider_id)

    def capability_matrix(self) -> dict[str, dict[str, bool]]:
        """Provider x feature grid, for the writeup and the slides."""
        matrix: dict[str, dict[str, bool]] = {}
        for provider in self._providers.values():
            matrix[provider.id] = {
                feature.value: provider.capabilities.supports(feature) for feature in Feature
            }
        return matrix

    # -- dunder --

    def __contains__(self, provider_id: object) -> bool:
        return provider_id in self._providers

    def __len__(self) -> int:
        return len(self._providers)

    def __iter__(self) -> Iterator[TransportProvider]:
        return iter(self._providers.values())
