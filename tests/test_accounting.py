"""Upstream accounting tests: what `/metrics` is allowed to call a call.

The distinction this file protects: a search answered from the local timetable has
made no upstream call, and reporting one would make the quota number meaningless.
"""

from __future__ import annotations

import json

import pytest

from railmind.providers.accounting import count_upstream, current_calls, meter
from railmind.providers.railradar import RailRadarClient


def test_outside_a_meter_nothing_is_counted():
    count_upstream()
    assert current_calls() is None


def test_a_meter_counts_and_then_resets():
    with meter() as counter:
        count_upstream()
        count_upstream()
        assert current_calls() == 2
    assert counter["calls"] == 2
    assert current_calls() is None


def test_nested_meters_do_not_leak_into_each_other():
    with meter() as outer:
        count_upstream()
        with meter() as inner:
            count_upstream()
            assert inner["calls"] == 1
        assert outer["calls"] == 1


class CountingTransport:
    """Stands in for the HTTP layer, which is where a real call would happen."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, host, path, *, params=None, headers=None, deadline_s=None):
        self.calls += 1

        class Response:
            status = 200
            ok = True
            headers: dict = {}
            body = json.dumps({"success": True, "data": {"CSMT": "Mumbai CSMT"}}).encode()
            family = "ipv4"

            @staticmethod
            def json():
                return {"success": True, "data": {"CSMT": "Mumbai CSMT"}}

        return Response()


@pytest.mark.asyncio
async def test_a_real_vendor_call_is_counted_exactly_once():
    transport = CountingTransport()
    client = RailRadarClient(api_key="test", transport=transport)

    with meter() as counter:
        await client.stations()

    assert transport.calls == 1
    assert counter["calls"] == 1


@pytest.mark.asyncio
async def test_a_local_answer_costs_zero_upstream_calls():
    """The whole point: a seeded corridor must not look like it spent quota."""
    transport = CountingTransport()

    with meter() as counter:
        pass  # no client call at all, which is what a seeded search does

    assert transport.calls == 0
    assert counter["calls"] == 0
