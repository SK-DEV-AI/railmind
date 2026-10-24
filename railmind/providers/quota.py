"""A hard local budget for a metered data plan.

The free RailRadar plan is 100 requests per month. That is 3.3 per day, which means
one careless loop can end the project's data access for four weeks. A ledger that
refuses is cheaper than a week of guessing why the API returns 429s.

Measured on 2026-10-24: validation errors (HTTP 400) consume quota too. Only a
connection that never reached the server is free. So the ledger records what the
server returned, and does not record transport failures.

This is the app's counter, separate from `scripts/railradar_probe.py`, which has
its own ledger for exploratory calls. Both write the same format, so the number in
the file is the truth for the month.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from railmind.core.errors import RateLimited

DEFAULT_LEDGER = Path("data/railradar-usage.json")
DEFAULT_BUDGET = 90


def month_key(now: dt.datetime | None = None) -> str:
    moment = now or dt.datetime.now(dt.UTC)
    return moment.strftime("%Y-%m")


@dataclass
class QuotaLedger:
    """Counts real requests per UTC month and refuses to exceed the budget."""

    path: Path = DEFAULT_LEDGER
    budget: int = DEFAULT_BUDGET
    _state: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._state = self._read()

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            # Do not silently pretend nothing was spent: keep the broken file as
            # evidence next to the ledger, then start a fresh count.
            self.path.replace(self.path.with_suffix(self.path.suffix + ".corrupt"))
            return {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._state, indent=2) + "\n")

    @property
    def used(self) -> int:
        return int(self._state.get(month_key(), {}).get("count", 0))

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def ensure_allowed(self) -> None:
        """Raise before spending the request, not after."""
        if self.used >= self.budget:
            raise RateLimited(
                f"The monthly data budget of {self.budget} requests is spent; "
                "serving cached data only",
                provider_id="railradar",
            )

    def record(self, path_called: str, status: int) -> int:
        bucket = self._state.setdefault(month_key(), {"count": 0, "calls": []})
        bucket["count"] = int(bucket.get("count", 0)) + 1
        bucket.setdefault("calls", []).append(
            {
                "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                "path": path_called,
                "status": status,
            }
        )
        self._write()
        return bucket["count"]

    def summary(self) -> str:
        return f"{self.used}/{self.budget} data requests used this month"
