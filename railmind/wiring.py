"""Composition root: the one place that knows about concrete classes.

Everything else in the package is written against protocols and the registry. This
file is allowed to import the real provider, read the environment, and hand back a
ready registry, because something has to.

Kept deliberately boring. If this file ever grows conditionals per environment or
per provider, the abstraction has sprung a leak.
"""

from __future__ import annotations

import os
from pathlib import Path

from railmind.data.timetable import DEFAULT_DB, TimetableRepo
from railmind.providers.cache import RouteCache
from railmind.providers.quota import DEFAULT_BUDGET, QuotaLedger
from railmind.providers.rail_india import RailIndiaProvider
from railmind.providers.railradar import RailRadarClient
from railmind.providers.registry import ProviderRegistry

#: The free data plan excludes seat availability; set this to 1 only with a paid
#: plan, or the provider would advertise a capability the vendor will refuse.
_SEAT_ENV = "RAILRADAR_SEAT_AVAILABILITY"


def timetable_path() -> Path:
    return Path(os.environ.get("RAILMIND_TIMETABLE_DB", str(DEFAULT_DB)))


def seat_availability_enabled() -> bool:
    return os.environ.get(_SEAT_ENV, "0").strip() in {"1", "true", "yes", "on"}


def data_budget() -> int:
    """Monthly request ceiling, kept below the plan limit so headroom exists."""
    try:
        return int(os.environ.get("RAILRADAR_BUDGET", str(DEFAULT_BUDGET)))
    except ValueError:
        return DEFAULT_BUDGET


def build_rail_provider(
    *,
    api_key: str | None = None,
    db_path: str | Path | None = None,
    cache: RouteCache | None = None,
    ledger: QuotaLedger | None = None,
) -> RailIndiaProvider:
    """Build the rail provider from the environment. Raises if the key is missing.

    A missing key is a configuration error and should fail loudly at startup, not
    silently at the first user request.
    """
    key = (api_key or os.environ.get("RAILRADAR_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("RAILRADAR_API_KEY is not set; copy .env.example to .env")

    repo = TimetableRepo(db_path or timetable_path())
    return RailIndiaProvider(
        client=RailRadarClient(
            api_key=key,
            ledger=ledger or QuotaLedger(budget=data_budget()),
        ),
        repo=repo,
        cache=cache or RouteCache(),
        seat_availability=seat_availability_enabled(),
    )


def build_registry(provider: RailIndiaProvider | None = None) -> ProviderRegistry:
    """Registry with everything this build can actually serve."""
    registry = ProviderRegistry()
    registry.register(provider or build_rail_provider())
    return registry
