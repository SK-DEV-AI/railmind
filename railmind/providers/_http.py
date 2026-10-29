"""Minimal async HTTP/1.1 client for JSON APIs.

Written instead of pulling in a library for three reasons, each measured on this
machine rather than assumed:

1. **Address-family control.** On the current hotspot IPv4 and IPv6 both work but
   which one is faster changes minute to minute (measured: IPv6 0.43-3.98 s, IPv4
   0.50-1.80 s to the same host). `urlopen` and `requests` pick one, so a slow
   family costs the whole request. This client races AAAA and A with a 250 ms head
   start, so the faster one wins, and it reports which family answered.
2. **No hidden retries or redirects.** A silent retry is an extra request against a
   100-per-month budget. Every call here is exactly one request.
3. **One place to read.** 200 lines we own beats a dependency we would have to
   configure to behave.

Only GET is implemented, because that is all this project needs. Adding POST later
is a small, explicit change rather than an accident.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlencode

DEFAULT_DEADLINE_S = 20.0

#: How long the preferred family gets on its own before the next one is raced
#: against it. RFC 8305's value, and far longer than a healthy handshake needs, so
#: on a link where the first family works the second is never contacted.
HAPPY_EYEBALLS_DELAY_S = 0.25


class TransportError(Exception):
    """A connection failed at the network or TLS layer.

    Carries no upstream body, because there is none. Callers convert this into a
    provider error; it must never reach a client.
    """

    def __init__(self, message: str, *, family: str | None = None) -> None:
        super().__init__(message)
        self.family = family


@dataclass(frozen=True, slots=True)
class Response:
    """One HTTP response, already parsed."""

    status: int
    headers: dict[str, str]
    body: bytes
    family: str

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return {"_unparsed": self.body[:400].decode("utf-8", "replace")}

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


async def _connect(
    host: str, port: int, *, deadline_s: float, family: int | None = None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    """Open one TLS connection, optionally restricted to an address family."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            host,
            port,
            ssl=_ssl_context(),
            server_hostname=host,
            family=family if family is not None else 0,
        ),
        timeout=deadline_s,
    )
    label = {socket.AF_INET6: "ipv6", socket.AF_INET: "ipv4"}.get(family or 0, "auto")
    return reader, writer, label


async def _first_connection(
    host: str,
    port: int,
    *,
    deadline_s: float,
    families: tuple[int | None, ...],
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    """Connect over whichever family answers first, staggered by 250 ms.

    Trying one family all the way to the deadline is what cost whole requests:
    measured on this hotspot, IPv6 to api.railradar.in answered in 0.43-3.98 s and
    IPv4 in 0.50-1.80 s across ten samples, both of them working. "IPv6 first and
    wait" therefore means one slow IPv6 handshake spends the entire 1.8 s search
    deadline before the family that would have answered in half a second is even
    asked. RFC 8305 fixes this by starting the next family only if the first has
    not connected after 250 ms, and taking the first that connects.
    """

    async def attempt(family: int | None, delay_s: float) -> tuple:
        if delay_s:
            await asyncio.sleep(delay_s)
        return await _connect(host, port, deadline_s=deadline_s, family=family)

    tasks = [
        asyncio.create_task(attempt(family, index * HAPPY_EYEBALLS_DELAY_S))
        for index, family in enumerate(families)
    ]
    pending = set(tasks)
    last_error: BaseException | None = None
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            winner: tuple | None = None
            for task in done:
                error = task.exception()
                if error is not None:
                    last_error = error
                    continue
                if winner is None:
                    winner = task.result()
                    continue
                # Two families connected. Keep the first, close the second: an
                # abandoned TLS connection is a file descriptor we would never see
                # again.
                task.result()[1].close()
            if winner is not None:
                return winner
    finally:
        # Anything still in flight is cancelled, and a cancelled attempt that
        # finished connecting anyway is closed rather than leaked.
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                _, writer, _ = await task
            except BaseException:  # noqa: BLE001 - cancelled or failed, nothing to keep
                continue
            writer.close()

    raise last_error or OSError(f"no address family reached {host}")


async def _read_response(reader: asyncio.StreamReader, family: str) -> Response:
    raw_head = await reader.readuntil(b"\r\n\r\n")
    lines = raw_head.decode("iso-8859-1").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError) as exc:
        raise TransportError(f"malformed status line: {lines[0]!r}", family=family) from exc

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = bytearray()
        while True:
            size_line = await reader.readuntil(b"\r\n")
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                await reader.readuntil(b"\r\n")  # trailing CRLF
                break
            body += await reader.readexactly(size)
            await reader.readexactly(2)  # chunk terminator
        payload = bytes(body)
    else:
        length = int(headers.get("content-length", "0") or 0)
        payload = await reader.readexactly(length) if length else await reader.read()

    return Response(status=status, headers=headers, body=payload, family=family)


async def get(
    host: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    deadline_s: float = DEFAULT_DEADLINE_S,
    families: tuple[int | None, ...] = (socket.AF_INET6, socket.AF_INET),
) -> Response:
    """GET `path` from `host`, racing the address families against each other.

    `families` is ordered by preference, not by belief: the first one gets a 250 ms
    head start and the next is only started if it has not connected. Whichever
    answer arrives first is used, and the family is recorded on the response so a
    log can show it.
    """
    if params:
        path = f"{path}{'&' if '?' in path else '?'}{urlencode(params)}"

    default_headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "railmind/0.1 (+student project)",
        "Connection": "close",
    }
    merged = {**default_headers, **(headers or {})}
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        + "".join(f"{key}: {value}\r\n" for key, value in merged.items())
        + "\r\n"
    )

    try:
        reader, writer, label = await _first_connection(
            host, 443, deadline_s=deadline_s, families=families
        )
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        raise TransportError(
            f"could not reach {host}: {type(exc).__name__}: {exc}",
        ) from exc

    try:
        writer.write(request.encode())
        await writer.drain()
        return await asyncio.wait_for(_read_response(reader, label), timeout=deadline_s)
    except (OSError, TimeoutError, ssl.SSLError, asyncio.IncompleteReadError) as exc:
        # One request, one attempt: a connection that breaks mid-answer is reported,
        # not retried on the other family. A silent second request is exactly what
        # this client exists to avoid.
        raise TransportError(
            f"{host} answered over {label} but the read failed: {type(exc).__name__}: {exc}",
            family=label,
        ) from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass
