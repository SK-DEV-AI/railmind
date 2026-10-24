"""The data budget: a counter that must never lie, and must refuse.

Losing this is losing the project's data for the rest of the month, so the tests
are about behaviour under pressure rather than happy paths.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from railmind.core.errors import NotSupported, RateLimited, UpstreamDown
from railmind.providers._http import Response, TransportError
from railmind.providers.quota import QuotaLedger, month_key
from railmind.providers.railradar import RailRadarClient


class TestLedger:
    def test_starts_empty(self, tmp_path):
        ledger = QuotaLedger(path=tmp_path / "usage.json", budget=10)
        assert ledger.used == 0
        assert ledger.remaining == 10

    def test_records_and_persists(self, tmp_path):
        path = tmp_path / "usage.json"
        ledger = QuotaLedger(path=path, budget=10)
        ledger.record("/v1/trains/1", 200)
        ledger.record("/v1/trains/2", 200)

        assert ledger.used == 2
        stored = json.loads(path.read_text())
        assert stored[month_key()]["count"] == 2
        assert len(stored[month_key()]["calls"]) == 2

    def test_survives_a_restart(self, tmp_path):
        path = tmp_path / "usage.json"
        QuotaLedger(path=path, budget=10).record("/v1/x", 200)
        assert QuotaLedger(path=path, budget=10).used == 1

    def test_refuses_at_the_budget_before_spending(self, tmp_path):
        ledger = QuotaLedger(path=tmp_path / "usage.json", budget=2)
        ledger.record("/v1/a", 200)
        ledger.record("/v1/b", 200)

        with pytest.raises(RateLimited) as caught:
            ledger.ensure_allowed()
        assert caught.value.to_problem().code.value == "rate_limited"
        assert ledger.used == 2  # refusing did not spend anything

    def test_remaining_never_negative(self, tmp_path):
        ledger = QuotaLedger(path=tmp_path / "usage.json", budget=1)
        ledger.record("/v1/a", 200)
        ledger.record("/v1/b", 200)  # over budget, as a crash would do
        assert ledger.remaining == 0

    def test_a_corrupt_file_is_kept_as_evidence(self, tmp_path):
        path = tmp_path / "usage.json"
        path.write_text("{not json at all")
        ledger = QuotaLedger(path=path, budget=10)

        assert ledger.used == 0
        assert (tmp_path / "usage.json.corrupt").exists()

    def test_counts_a_validation_error_because_the_server_counted_it(self, tmp_path):
        # Measured 2026-10-24: HTTP 400 spends quota too. Only a transport failure is free.
        ledger = QuotaLedger(path=tmp_path / "usage.json", budget=10)
        assert ledger.record("/v1/trains/1/fare", 400) == 1


class TestClientIntegration:
    async def test_every_http_response_is_counted(self, tmp_path):
        ledger = QuotaLedger(path=tmp_path / "usage.json", budget=10)

        async def transport(host, path, *, params=None, headers=None, deadline_s=None):
            return Response(
                status=200,
                headers={},
                body=json.dumps({"success": True, "data": {"ok": 1}}).encode(),
                family="ipv4",
            )

        client = RailRadarClient(api_key="k", transport=transport, ledger=ledger)
        await client._get("/v1/trains/1")
        await client._get("/v1/trains/2")

        assert ledger.used == 2

    async def test_refuses_before_calling_the_vendor(self, tmp_path):
        ledger = QuotaLedger(path=tmp_path / "usage.json", budget=1)
        ledger.record("/v1/trains/0", 200)
        called: list[str] = []

        async def transport(host, path, *, params=None, headers=None, deadline_s=None):
            called.append(path)
            return Response(200, {}, b'{"success": true, "data": {}}', "ipv4")

        client = RailRadarClient(api_key="k", transport=transport, ledger=ledger)
        with pytest.raises(RateLimited):
            await client._get("/v1/trains/1")

        assert called == []  # nothing was spent
        assert ledger.used == 1

    async def test_a_transport_failure_is_not_counted(self, tmp_path):
        ledger = QuotaLedger(path=tmp_path / "usage.json", budget=10)

        async def transport(host, path, *, params=None, headers=None, deadline_s=None):
            raise TransportError("connection refused")

        client = RailRadarClient(api_key="k", transport=transport, ledger=ledger)
        with pytest.raises(UpstreamDown):
            await client._get("/v1/trains/1")

        assert ledger.used == 0  # the request never reached the server

    async def test_a_vendor_error_is_still_counted(self, tmp_path):
        ledger = QuotaLedger(path=tmp_path / "usage.json", budget=10)

        async def transport(host, path, *, params=None, headers=None, deadline_s=None):
            body = {"success": False, "error": {"code": "PLAN_UPGRADE_REQUIRED", "message": "x"}}
            return Response(403, {}, json.dumps(body).encode(), "ipv4")

        client = RailRadarClient(api_key="k", transport=transport, ledger=ledger)
        with pytest.raises(NotSupported):
            await client._get("/v1/trains/1/seats")

        assert ledger.used == 1


def test_month_key_is_utc():
    assert month_key(dt.datetime(2026, 10, 31, 23, 59, tzinfo=dt.UTC)) == "2026-10"
    assert month_key(dt.datetime(2026, 11, 1, 0, 1, tzinfo=dt.UTC)) == "2026-11"
