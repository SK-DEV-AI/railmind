"""RailRadar client: the only file that knows this vendor's wire format.

Verified against the live API on 2026-10-24 with a free-tier key:

- envelope is `{"success": bool, "data": ..., "meta": {...}}`, errors carry
  `error: {code, message}` and a `traceId`
- `/v1/lookup/stations` returns `{code: name}` for 13,010 stations
- `/v1/trains/between/{from}/{to}` returns direct trains with departure/arrival,
  day numbers, distance, duration, run days
- `/v1/trains/{number}` returns the full route: 285 stops for Punjab Mail, each
  with station code, name, lat/lon, arrival, departure, day and distance
- `/v1/trains/{n}/fare` needs `journeyDate`, `from`, `to`, `classCode`;
  it proxied an upstream failure (`SYSTEM:UNKNOWN_ERROR`) during testing, so it is
  treated as best-effort
- `/v1/trains/{n}/seats` and PNR are **paid-plan only**: `PLAN_UPGRADE_REQUIRED`
  on the free tier. So availability is advertised as unavailable, not faked.

Every failure maps onto our closed error set here. Nothing above this file ever
sees a vendor error code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from railmind.core.errors import (
    InvalidQuery,
    NotFound,
    NotSupported,
    ProviderError,
    RateLimited,
    UpstreamDown,
)
from railmind.providers import _http
from railmind.providers.quota import QuotaLedger

HOST = "api.railradar.in"
PROVIDER_ID = "railradar"

#: `(host, path, *, params, headers, timeout) -> Response`
Transport = Callable[..., Awaitable[_http.Response]]


def _error_message(payload: dict) -> tuple[str, str]:
    """Split a vendor error into (machine code, vendor text).

    The vendor text is for logs only. Our own message is written below.
    """
    error = payload.get("error") or {}
    return str(error.get("code") or ""), str(error.get("message") or "")


def map_error(status: int, payload: dict) -> ProviderError:
    """Convert one vendor failure into exactly one of our errors."""
    code, vendor_message = _error_message(payload)

    if code == "PLAN_UPGRADE_REQUIRED":
        return NotSupported(
            "Seat availability and PNR are not included in this data plan",
            provider_id=PROVIDER_ID,
            cause_detail=vendor_message,
        )
    if code == "VALIDATION_ERROR":
        return InvalidQuery(
            "The data provider rejected the query parameters",
            provider_id=PROVIDER_ID,
            cause_detail=vendor_message,
        )
    if status == 404 or code in {"NOT_FOUND", "TRAIN_NOT_FOUND", "STATION_NOT_FOUND"}:
        return NotFound(
            "The data provider has no such train or station",
            provider_id=PROVIDER_ID,
            cause_detail=vendor_message,
        )
    if status == 429:
        return RateLimited(
            "The data provider is rate limiting us",
            provider_id=PROVIDER_ID,
            cause_detail=vendor_message,
        )
    if status in {401, 403}:
        return UpstreamDown(
            "The data provider rejected our credentials",
            provider_id=PROVIDER_ID,
            cause_detail=vendor_message,
        )
    if status >= 500 or status == 0:
        return UpstreamDown(
            "The data provider is unavailable right now",
            provider_id=PROVIDER_ID,
            cause_detail=vendor_message,
        )
    return UpstreamDown(
        "The data provider returned an unexpected response",
        provider_id=PROVIDER_ID,
        cause_detail=f"status={status} code={code} message={vendor_message}",
    )


@dataclass(slots=True)
class RailRadarClient:
    """Thin, typed access to the four endpoints we actually use."""

    api_key: str
    transport: Transport = _http.get
    timeout: float = _http.DEFAULT_DEADLINE_S
    #: Flipped by the provider when the plan turns out to exclude availability.
    seat_availability_allowed: bool = True
    #: Counts and caps real requests. Optional so tests need no file.
    ledger: QuotaLedger | None = None

    async def _get(self, path: str, params: dict[str, str] | None = None) -> object:
        """One request. Unwrap the envelope, or raise a mapped provider error."""
        if self.ledger is not None:
            self.ledger.ensure_allowed()

        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            response = await self.transport(
                HOST, path, params=params, headers=headers, deadline_s=self.timeout
            )
        except _http.TransportError as exc:
            # Nothing reached the server, so nothing was spent.
            raise UpstreamDown(
                "Could not reach the data provider",
                provider_id=PROVIDER_ID,
                cause_detail=str(exc),
            ) from exc

        if self.ledger is not None:
            self.ledger.record(path, response.status)

        payload = response.json()
        if not response.ok or payload.get("success") is not True:
            raise map_error(response.status, payload)
        return payload.get("data")

    # -- endpoints --

    async def stations(self) -> dict[str, str]:
        """`{code: name}` for every station the vendor knows (13,010 at test time)."""
        data = await self._get("/v1/lookup/stations")
        if not isinstance(data, dict):
            raise UpstreamDown(
                "The data provider returned an unexpected station list",
                provider_id=PROVIDER_ID,
            )
        return {str(code): str(name) for code, name in data.items()}

    async def trains_between(self, from_code: str, to_code: str) -> list[dict]:
        """Direct trains between two station codes. One request per pair."""
        path = f"/v1/trains/between/{from_code}/{to_code}"
        data = await self._get(path)
        trains = (data or {}).get("trains") if isinstance(data, dict) else None
        if not isinstance(trains, list):
            raise UpstreamDown(
                "The data provider returned an unexpected route response",
                provider_id=PROVIDER_ID,
            )
        return trains

    async def train_schedule(self, number: str) -> dict:
        """Full route for a train: `{train: {...}, route: [stops]}`."""
        data = await self._get(f"/v1/trains/{number}")
        if not isinstance(data, dict) or "route" not in data:
            raise UpstreamDown(
                "The data provider returned an unexpected schedule",
                provider_id=PROVIDER_ID,
                cause_detail=f"train {number}",
            )
        return data

    async def fare(
        self,
        number: str,
        journey_date: str,
        from_code: str,
        to_code: str,
        class_code: str,
    ) -> dict:
        """Itemised fare. Best effort: the vendor proxies a flaky upstream here."""
        data = await self._get(
            f"/v1/trains/{number}/fare",
            params={
                "journeyDate": journey_date,
                "from": from_code,
                "to": to_code,
                "classCode": class_code,
            },
        )
        if not isinstance(data, dict):
            raise UpstreamDown(
                "The data provider returned an unexpected fare",
                provider_id=PROVIDER_ID,
            )
        return data

    async def seats(self, number: str, journey_date: str, class_code: str) -> dict:
        """Seat availability. Paid plan only; raises `NotSupported` on free tier."""
        if not self.seat_availability_allowed:
            raise NotSupported(
                "Seat availability is not included in this data plan",
                provider_id=PROVIDER_ID,
            )
        data = await self._get(
            f"/v1/trains/{number}/seats",
            params={"journeyDate": journey_date, "classCode": class_code},
        )
        if not isinstance(data, dict):
            raise UpstreamDown(
                "The data provider returned an unexpected availability response",
                provider_id=PROVIDER_ID,
            )
        return data

    async def live(self, number: str) -> dict:
        """Running position and delay for a train."""
        data = await self._get(f"/v1/trains/{number}/live")
        if not isinstance(data, dict):
            raise UpstreamDown(
                "The data provider returned an unexpected live status",
                provider_id=PROVIDER_ID,
            )
        return data

    async def route_geometry(self, number: str) -> dict:
        """GeoJSON route for the map."""
        data = await self._get(f"/v1/trains/{number}/route")
        if not isinstance(data, dict):
            raise UpstreamDown(
                "The data provider returned unexpected route geometry",
                provider_id=PROVIDER_ID,
            )
        return data
