"""Measure T90 against a running server, and print the numbers from the log.

The project's headline claim is "a ranked, risk-assessed card in under two seconds,
at the 90th percentile". A claim like that is worth nothing unless it is measured on
the running system, with a sample size big enough to have a 90th percentile at all.
This script is how that number is produced, and `/metrics` reports the same figure
from the same log, so the report cannot quote something the API disagrees with.

What it does:

1. Builds `--requests` distinct search payloads. Distinctness matters: the engine
   coalesces identical concurrent queries on purpose, so a load test that repeats one
   payload measures the coalescer rather than the engine.
2. Sends them with `--concurrency` in flight, timing each round trip.
3. Reads `/metrics` afterwards and prints both its numbers and its own, so a
   disagreement is visible instead of hidden.

Usage:
    python scripts/measure_t90.py --requests 200 --concurrency 10
    python scripts/measure_t90.py --base-url http://127.0.0.1:8023 --requests 50

Requires a server already running. It does not start one, because a measurement
taken against a server you did not choose is not a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

#: Corridors that are seeded in the local timetable. A search for anything else
#: would return nothing and measure the empty path, which is fast and irrelevant.
CORRIDORS = (("CSMT", "PUNE"), ("CSMT", "NDLS"), ("MMCT", "NDLS"))


def payload(origin: str, destination: str, day: dt.date) -> dict:
    start = dt.datetime.combine(day, dt.time(6), tzinfo=IST)
    return {
        "intent": {
            "intent_id": f"int_bench_{origin}_{destination}_{day:%Y%m%d}",
            "created_at": start.isoformat(),
            "expires_at": (start + dt.timedelta(hours=1)).isoformat(),
            "raw_text": f"{origin} to {destination}",
            "origin": {
                "stop_id": f"IN:{origin}",
                "name": origin,
                "code": origin,
                "country": "IN",
                "timezone": "Asia/Kolkata",
            },
            "destination": {
                "stop_id": f"IN:{destination}",
                "name": destination,
                "code": destination,
                "country": "IN",
                "timezone": "Asia/Kolkata",
            },
            "depart_window": {
                "start": start.isoformat(),
                "end": (start + dt.timedelta(hours=14)).isoformat(),
            },
            "modes": ["rail"],
            "optimise": "balanced",
            "confidence": 0.9,
            "parsed_by": "rules",
        },
        "limit": 10,
    }


def build_payloads(count: int) -> list[dict]:
    """Distinct (corridor, date) pairs, so nothing is coalesced away."""
    payloads = []
    base = dt.date.today() + dt.timedelta(days=1)
    index = 0
    while len(payloads) < count:
        origin, destination = CORRIDORS[index % len(CORRIDORS)]
        day = base + dt.timedelta(days=index // len(CORRIDORS))
        payloads.append(payload(origin, destination, day))
        index += 1
    return payloads


def one(request: urllib.request.Request, wait_s: float) -> tuple[float, int, int]:
    """Send one blocking request. Returns (elapsed_ms, status, itineraries).

    Deliberately a plain function: it is handed to `asyncio.to_thread`, and an async
    function passed there would return an un-awaited coroutine instead of a result.
    """
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=wait_s) as response:
            body = json.loads(response.read())
            elapsed = (time.perf_counter() - started) * 1000
            return elapsed, response.status, len(body.get("itineraries") or [])
    except Exception:
        return (time.perf_counter() - started) * 1000, 0, 0


async def run(base_url: str, payloads: list[dict], concurrency: int, wait_s: float):
    semaphore = asyncio.Semaphore(concurrency)

    async def worker(body: dict):
        async with semaphore:
            request = urllib.request.Request(
                f"{base_url}/search",
                data=json.dumps(body).encode(),
                headers={"content-type": "application/json"},
                method="POST",
            )
            return await asyncio.to_thread(one, request, wait_s)

    return await asyncio.gather(*(worker(body) for body in payloads))


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no samples")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def fetch_metrics(base_url: str, window_seconds: int) -> dict:
    with urllib.request.urlopen(f"{base_url}/metrics?window_seconds={window_seconds}") as r:
        return json.loads(r.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    payloads = build_payloads(args.requests)
    started = time.perf_counter()
    results = asyncio.run(run(args.base_url, payloads, args.concurrency, args.timeout))
    wall_s = time.perf_counter() - started

    latencies = [elapsed for elapsed, _status, _count in results]
    ok = [item for item in results if item[1] == 200]
    empty = [item for item in ok if item[2] == 0]

    print(f"requests: {len(results)}  concurrency: {args.concurrency}  wall: {wall_s:.2f}s")
    print(f"throughput: {len(results) / wall_s:.1f} req/s")
    if not ok:
        print("every request failed; is the server running and is the database seeded?")
        return 1
    print(
        f"status: {len(ok)} ok, {len(results) - len(ok)} failed"
        + (f", {len(empty)} ok-but-empty" if empty else "")
    )
    print(
        "client-side latency ms: "
        f"p50 {percentile(latencies, 0.50):.1f}  "
        f"p90 {percentile(latencies, 0.90):.1f}  "
        f"p99 {percentile(latencies, 0.99):.1f}  "
        f"max {max(latencies):.1f}  "
        f"mean {statistics.fmean(latencies):.1f}"
    )

    metrics = fetch_metrics(args.base_url, 3600)
    print(
        f"server-side (/metrics): t90 {metrics.get('t90_ms')} ms  "
        f"samples {metrics.get('sample_size')}  "
        f"provider_calls {metrics.get('provider_calls')}  "
        f"coalesced {metrics.get('cache_hits')}  "
        f"degraded {metrics.get('requests', {}).get('degraded')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
