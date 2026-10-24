"""Shared fixtures. Deliberately small: each test builds what it needs."""

from __future__ import annotations

import datetime as dt

import pytest

from railmind.core.types import Query
from railmind.providers.fake import make_stop


class FakeClock:
    """A clock you can move. TTL tests must never sleep."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def csmt():
    return make_stop("IN:CSMT", "Mumbai CSMT", lat=18.94, lon=72.835)


@pytest.fixture
def ndls():
    return make_stop("IN:NDLS", "New Delhi", lat=28.64, lon=77.22)


@pytest.fixture
def service_date() -> dt.date:
    return dt.date(2026, 10, 25)


@pytest.fixture
def query(csmt, ndls, service_date) -> Query:
    return Query(origin=csmt, destination=ndls, service_date=service_date)
