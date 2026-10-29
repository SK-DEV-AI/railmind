"""The HTTP client's address-family behaviour, driven by fake sockets.

No network here: every connection is a coroutine that sleeps for a chosen time and
then hands back a fake reader and writer. That is enough to pin the three
behaviours that matter, each of which cost a real request before it was fixed:

1. A slow family must not be waited out to the end - the other one is raced against
   it (RFC 8305) and the first connection wins.
2. A failed family must not stop the other one from being tried.
3. A single attempt is a single request: when the first family connects in time the
   second is never contacted, because the vendor budget is 100 requests a month.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

import pytest

from railmind.providers import _http
from railmind.providers._http import TransportError

HEAD = b"HTTP/1.1 200 OK\r\ncontent-length: 11\r\n\r\n"
BODY = b'{"ok": true'
_LABELS = {socket.AF_INET6: "ipv6", socket.AF_INET: "ipv4"}


class FakeReader:
    def __init__(self, head: bytes = HEAD, body: bytes = BODY) -> None:
        self._head = head
        self._body = body
        self._read = 0

    async def readuntil(self, separator: bytes) -> bytes:
        return self._head

    async def readexactly(self, count: int) -> bytes:
        chunk = self._body[self._read : self._read + count]
        if len(chunk) != count:
            raise asyncio.IncompleteReadError(chunk, count)
        self._read += count
        return chunk


class FakeWriter:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def fake_connect(
    plan: dict[int | None, float],
    outcome: dict[int | None, BaseException] | None = None,
) -> Any:
    """Build a `_connect` replacement: `plan` is family -> seconds to connect.

    Every connection it hands back is recorded on `.calls` and `.writers` so a test
    can see which families were asked and which sockets were left open.
    """
    outcome = outcome or {}
    calls: list[tuple[int | None, float]] = []
    writers: list[tuple[int | None, FakeWriter]] = []

    async def connect(host, port, *, deadline_s, family=None):
        calls.append((family, time.perf_counter()))
        await asyncio.sleep(plan[family])
        if family in outcome:
            raise outcome[family]
        writer = FakeWriter()
        writers.append((family, writer))
        return FakeReader(), writer, _LABELS[family]

    connect.calls = calls  # type: ignore[attr-defined]
    connect.writers = writers  # type: ignore[attr-defined]
    return connect


async def test_a_stalled_family_is_raced_not_waited_out(monkeypatch):
    """IPv6 hanging for 5 s used to be 5 s of the request. Now IPv4 answers first."""
    connect = fake_connect({socket.AF_INET6: 5.0, socket.AF_INET: 0.01})
    monkeypatch.setattr(_http, "_connect", connect)

    started = time.perf_counter()
    response = await _http.get("api.example", "/v1/x", deadline_s=2.0)
    elapsed = time.perf_counter() - started

    assert response.status == 200
    assert response.family == "ipv4"
    # 250 ms stagger + 10 ms connect, and nothing waits for the stalled family.
    assert elapsed < 0.5, elapsed
    assert {family for family, _ in connect.calls} == {socket.AF_INET6, socket.AF_INET}


async def test_the_preferred_family_wins_when_it_is_fast_enough(monkeypatch):
    """No wasted second connection when the preferred family answers promptly."""
    connect = fake_connect({socket.AF_INET6: 0.01, socket.AF_INET: 0.01})
    monkeypatch.setattr(_http, "_connect", connect)

    response = await _http.get("api.example", "/v1/x", deadline_s=2.0)

    assert response.family == "ipv6"
    assert [family for family, _ in connect.calls] == [socket.AF_INET6]


async def test_a_failing_family_falls_through_to_the_next(monkeypatch):
    connect = fake_connect(
        {socket.AF_INET6: 0.0, socket.AF_INET: 0.0},
        outcome={socket.AF_INET6: OSError("network unreachable")},
    )
    monkeypatch.setattr(_http, "_connect", connect)

    response = await _http.get("api.example", "/v1/x", deadline_s=2.0)

    assert response.family == "ipv4"
    assert len(connect.calls) == 2


async def test_every_family_failing_is_a_transport_error(monkeypatch):
    connect = fake_connect(
        {socket.AF_INET6: 0.0, socket.AF_INET: 0.0},
        outcome={
            socket.AF_INET6: OSError("network unreachable"),
            socket.AF_INET: TimeoutError("timed out"),
        },
    )
    monkeypatch.setattr(_http, "_connect", connect)

    with pytest.raises(TransportError) as caught:
        await _http.get("api.example", "/v1/x", deadline_s=2.0)

    assert "could not reach api.example" in str(caught.value)


async def test_the_losing_connection_is_closed(monkeypatch):
    """Two families connect in the same tick; the one we do not use must be closed."""
    # A zero head start makes both attempts connect before the waiter wakes, which is
    # the only way both can be live at once. Whichever wins, both sockets get closed.
    monkeypatch.setattr(_http, "HAPPY_EYEBALLS_DELAY_S", 0.0)
    connect = fake_connect({socket.AF_INET6: 0.0, socket.AF_INET: 0.0})
    monkeypatch.setattr(_http, "_connect", connect)

    response = await _http.get("api.example", "/v1/x", deadline_s=2.0)

    assert response.family in {"ipv4", "ipv6"}
    assert len(connect.writers) == 2
    assert all(writer.closed for _, writer in connect.writers)
