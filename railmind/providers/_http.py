"""Minimal async HTTP/1.1 client for JSON APIs.

Written instead of pulling in a library for three reasons, each measured on this
machine rather than assumed:

1. **Address-family control.** On the current hotspot IPv4 and IPv6 keep swapping
   which one works. `urlopen` and `requests` pick one and hang for 40 seconds when
   they pick wrong. This client tries AAAA first, then A, per request, and reports
   which family answered.
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
    """GET `path` from `host`, trying each address family in order.

    `families` is ordered by what tends to work here; it is a hint, not a belief.
    Whichever family answers is recorded on the response so a log can show it.
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

    last_error: Exception | None = None
    for family in families:
        try:
            reader, writer, label = await _connect(host, 443, deadline_s=deadline_s, family=family)
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            last_error = exc
            continue

        try:
            writer.write(request.encode())
            await writer.drain()
            return await asyncio.wait_for(_read_response(reader, label), timeout=deadline_s)
        except (OSError, TimeoutError, ssl.SSLError, asyncio.IncompleteReadError) as exc:
            last_error = exc
            continue
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass

    raise TransportError(
        f"could not reach {host}: {type(last_error).__name__}: {last_error}",
    )
