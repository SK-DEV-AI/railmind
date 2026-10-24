#!/usr/bin/env python3
"""RailRadar probe: one authenticated call, with an IPv6-first transport and a
hard local budget guard.

Why this exists
---------------
1. Local network. On the current hotspot IPv4 is dead and IPv6 is healthy;
   plain `curl` picks IPv4, stalls for 40s, and looks like a server problem.
   So we resolve AAAA first and only fall back to A when IPv6 is unavailable.
   Never hardcode an address: Cloudflare rotates them.

2. Budget. Free tier is 100 requests/month. That is 3.3 per day. A runaway loop
   or a curious retry can burn a month in a minute, so every call is counted in
   a local ledger and the script refuses to cross the ceiling.

3. Evidence. Every successful response is saved verbatim under
   tests/fixtures/railradar/ so the test suite and the demo can run offline.

Usage
-----
    python3 scripts/railradar_probe.py /v1/trains/12951
    python3 scripts/railradar_probe.py /v1/trains/12951 --out tests/fixtures/rr.json
    python3 scripts/railradar_probe.py /v1/trains/12951 --dry-run    # show, do not call

Environment
-----------
    RAILRADAR_API_KEY   required
    RAILRADAR_BUDGET    optional, default 90 (leaves 10 of 100 as headroom)
    RAILRADAR_LEDGER    optional, default data/railradar-usage.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlencode

BASE_HOST = "api.railradar.in"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = ROOT / "data" / "railradar-usage.json"


# --- transport ---------------------------------------------------------------


def resolve_prefer_v6(host: str, port: int) -> list[tuple[int, tuple]]:
    """Addresses to try, IPv6 first. Measured reason, not superstition."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    v6 = [i for i in infos if i[0] is socket.AF_INET6]
    v4 = [i for i in infos if i[0] is socket.AF_INET]
    return [(i[0], i[4]) for i in v6] + [(i[0], i[4]) for i in v4]


def connect(host: str, port: int, timeout: float) -> tuple[ssl.SSLSocket, str]:
    """TLS connection, IPv6 preferred, and it reports which family it used."""
    last: Exception | None = None
    ctx = ssl.create_default_context()
    for family, sockaddr in resolve_prefer_v6(host, port):
        try:
            raw = socket.socket(family, socket.SOCK_STREAM)
            raw.settimeout(timeout)
            raw.connect(sockaddr)
            tls = ctx.wrap_socket(raw, server_hostname=host)
            return tls, ("ipv6" if family is socket.AF_INET6 else "ipv4")
        except Exception as exc:
            last = exc
            continue
    raise RuntimeError(f"no usable address for {host}: {last}")


def https_get(path: str, api_key: str, timeout: float = 20.0) -> tuple[int, dict, dict, str]:
    tls, family = connect(BASE_HOST, 443, timeout)
    try:
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {BASE_HOST}\r\n"
            f"Authorization: Bearer {api_key}\r\n"
            "Accept: application/json\r\n"
            "User-Agent: railmind-probe/0.1\r\n"
            "Connection: close\r\n\r\n"
        )
        tls.sendall(request.encode())

        chunks: list[bytes] = []
        while True:
            data = tls.recv(65536)
            if not data:
                break
            chunks.append(data)
    finally:
        tls.close()

    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    # chunked transfer-encoding, because we speak HTTP/1.1 by hand here
    if headers.get("transfer-encoding", "").lower() == "chunked":
        decoded = b""
        rest = body
        while rest:
            size_line, _, rest = rest.partition(b"\r\n")
            if not size_line:
                break
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                break
            decoded += rest[:size]
            rest = rest[size + 2 :]
        body = decoded

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {"_raw": body.decode("utf-8", "replace")[:2000]}

    return status, headers, payload, family


# --- budget ------------------------------------------------------------------


def month_key(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.UTC)
    return now.strftime("%Y-%m")


def read_ledger(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def used_this_month(path: Path) -> int:
    return int(read_ledger(path).get(month_key(), {}).get("count", 0))


def record_use(path: Path, status: int, path_called: str) -> int:
    ledger = read_ledger(path)
    key = month_key()
    bucket = ledger.setdefault(key, {"count": 0, "calls": []})
    bucket["count"] = int(bucket.get("count", 0)) + 1
    bucket.setdefault("calls", []).append(
        {
            "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "path": path_called,
            "status": status,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2) + "\n")
    return bucket["count"]


# --- main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "endpoint", nargs="?", default="/v1/trains/12951", help="path beginning with /v1/"
    )
    parser.add_argument("--out", type=Path, help="write the JSON body here")
    parser.add_argument("--query", action="append", default=[], help="key=value, repeatable")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--budget", type=int, default=int(os.environ.get("RAILRADAR_BUDGET", "90")))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    key = os.environ.get("RAILRADAR_API_KEY", "").strip()
    if not key:
        print("FAIL RAILRADAR_API_KEY is not set", file=sys.stderr)
        return 2

    endpoint = args.endpoint if args.endpoint.startswith("/") else "/" + args.endpoint
    if args.query:
        endpoint += "?" + urlencode(dict(q.split("=", 1) for q in args.query))

    used = used_this_month(args.ledger)
    print(f"budget: {used}/{args.budget} used this month")

    if used >= args.budget:
        print(
            "REFUSED monthly budget reached. Raise --budget deliberately, or wait for reset.",
            file=sys.stderr,
        )
        return 3

    if args.dry_run:
        print(f"would GET https://{BASE_HOST}{endpoint}")
        return 0

    try:
        status, headers, payload, family = https_get(endpoint, key)
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        print("note: a connection failure does not consume quota", file=sys.stderr)
        return 1

    count = record_use(args.ledger, status, endpoint)
    print(f"HTTP {status} via {family} · {count}/{args.budget} used this month")

    for header in (
        "ratelimit-limit",
        "ratelimit-remaining",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "retry-after",
        "cf-ray",
    ):
        if header in headers:
            print(f"  {header}: {headers[header]}")

    ok = isinstance(payload, dict) and payload.get("success") is True
    print("envelope:", "success" if ok else "NOT success")
    if isinstance(payload, dict) and "meta" in payload:
        meta = payload["meta"]
        print("  executionTime:", meta.get("executionTime"), "ms  source:", meta.get("source"))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {args.out}")
    else:
        print(json.dumps(payload)[:800])

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
