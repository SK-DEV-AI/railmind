"""The request log, and the numbers we are actually graded on.

Two reasons this is a file and not a counter in memory. First, a percentile is
only honest if it comes from real recorded requests, not from a number someone
chose before the demo. Second, the headline claim of this project is a latency
number, and a claim with no log behind it is a slide, not a measurement.

So every search appends one line of JSON to a file. The file survives a restart,
can be read by hand, and is the only source `/metrics` is allowed to count from.
`counted_from` says so on the wire, so a reader knows whether they are looking at
recorded traffic or at nothing at all.

What is deliberately not here: any timing of the human. `t_tap_ms` is a separate
measurement with a beginning and an end in the real world, and filling it from
server numbers would be the one dishonest metric in the whole system.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from railmind.wire.models import (
    Metrics,
    Requests,
    StageMetrics,
    Stages,
)

#: Stages the contract reports. `intent` is parsing, `search` is the fan-out,
#: `handoff` and `live` are the two other request kinds. A stage with no samples
#: is reported with n=0 rather than omitted, because a missing stage and a stage
#: with no traffic are different facts and the wire shape can only say one.
STAGES: tuple[str, ...] = ("intent", "search", "handoff", "live")

#: The commitment. T90 is the 90th percentile of end-to-end search latency, so
#: one slow request in ten may exceed it; that is what a percentile means.
T90_BUDGET_MS = 2000.0

DEFAULT_LOG_PATH = Path(os.environ.get("RAILMIND_REQUEST_LOG", "data/requests.jsonl"))


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """One completed request, as recorded. Immutable so a reader cannot edit history."""

    request_id: str
    at: dt.datetime
    route: str
    elapsed_ms: float
    stage_ms: Mapping[str, float] = field(default_factory=dict)
    result_count: int = 0
    degraded: bool = False
    error: str | None = None
    providers_used: tuple[str, ...] = ()
    providers_failed: tuple[str, ...] = ()
    # None means "not instrumented", which is different from zero calls. A
    # provider that does not report its counters must not look perfectly cached.
    upstream_calls: int | None = None
    cache_hits: int | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "at": self.at.isoformat(),
            "route": self.route,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "stage_ms": {name: round(value, 3) for name, value in self.stage_ms.items()},
            "result_count": self.result_count,
            "degraded": self.degraded,
            "error": self.error,
            "providers_used": list(self.providers_used),
            "providers_failed": list(self.providers_failed),
            "upstream_calls": self.upstream_calls,
            "cache_hits": self.cache_hits,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> RequestRecord:
        """Rebuild a record, raising ValueError on anything malformed.

        The caller skips bad lines rather than dying: a log that is half written
        because the process was killed mid-append must not take the API down.
        """
        try:
            return cls(
                request_id=str(payload["request_id"]),
                at=dt.datetime.fromisoformat(str(payload["at"])),
                route=str(payload["route"]),
                elapsed_ms=float(payload["elapsed_ms"]),
                stage_ms={
                    str(name): float(value)
                    for name, value in dict(payload.get("stage_ms") or {}).items()
                },
                result_count=int(payload.get("result_count") or 0),
                degraded=bool(payload.get("degraded") or False),
                error=str(payload["error"]) if payload.get("error") else None,
                providers_used=tuple(str(v) for v in payload.get("providers_used") or ()),
                providers_failed=tuple(str(v) for v in payload.get("providers_failed") or ()),
                upstream_calls=_optional_int(payload.get("upstream_calls")),
                cache_hits=_optional_int(payload.get("cache_hits")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed request record: {exc}") from exc


class RequestLog:
    """Append-only JSONL log. One line per request, newline-terminated and fsynced.

    Appending is the whole write path on purpose. A rewrite would need a lock, a
    temp file and a rename, and would lose records if the process died halfway.
    Reading is tolerant of a truncated final line for the same reason.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_LOG_PATH

    def append(self, record: RequestRecord) -> None:
        """Add one record. Creates the directory and file when missing."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_json(), ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def records(
        self,
        *,
        window_seconds: int | None = None,
        now: dt.datetime | None = None,
    ) -> list[RequestRecord]:
        """Every readable record, optionally limited to the recent window."""
        if not self.path.exists():
            return []
        cutoff = None
        if window_seconds is not None:
            reference = now or dt.datetime.now(dt.UTC)
            cutoff = reference - dt.timedelta(seconds=window_seconds)
        return [record for record in self._iter() if cutoff is None or record.at >= cutoff]

    def _iter(self) -> Iterator[RequestRecord]:
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    yield RequestRecord.from_json(json.loads(text))
                except ValueError:
                    # A partial line is expected after a hard kill. Skip it and
                    # keep counting the rest; a broken tail must not blind us.
                    continue

    def metrics(
        self,
        *,
        window_seconds: int = 86_400,
        now: dt.datetime | None = None,
    ) -> Metrics:
        """The `/metrics` payload, computed from the log and nothing else."""
        records = self.records(window_seconds=window_seconds, now=now)
        searches = [record for record in records if record.route == "search"]
        stage_samples = {
            stage: [record.stage_ms[stage] for record in records if stage in record.stage_ms]
            for stage in STAGES
        }
        reported_calls = [
            record.upstream_calls for record in records if record.upstream_calls is not None
        ]
        reported_hits = [record.cache_hits for record in records if record.cache_hits is not None]
        # Two different savings, kept apart on purpose. `provider_calls` is outbound
        # network traffic, the number that spends quota; a search answered from the
        # local timetable contributes zero to it. `cache_hits` is executions served
        # by an in-flight sibling request, i.e. work that was coalesced away.
        upstream = sum(reported_calls) if reported_calls else None
        cache_hits = sum(reported_hits) if reported_hits else None
        lookups = (upstream or 0) + (cache_hits or 0)

        return Metrics(
            window_seconds=window_seconds,
            counted_from="request_log",
            sample_size=len(records),
            # None when no search has been recorded. A percentile of nothing is
            # not zero milliseconds, and reporting 0.0 would look like a triumph.
            t90_ms=(
                _round(percentile([record.elapsed_ms for record in searches], 0.90))
                if searches
                else None
            ),
            # Not measurable on the server: it starts when the user taps and ends
            # when the payment succeeds, both outside this process.
            t_tap_ms=None,
            provider_calls=upstream,
            cache_hits=cache_hits,
            requests=Requests(
                total=len(records),
                errors=sum(1 for record in records if record.error),
                degraded=sum(1 for record in records if record.degraded),
            ),
            stages=Stages(**{stage: _stage_metrics(stage_samples[stage]) for stage in STAGES}),
            cache_hit_rate=_round(cache_hits / lookups, 4) if lookups else None,
            breakers=None,
        )


def percentile(values: list[float], quantile: float) -> float:
    """Linear-interpolation percentile. Small, exact, no numpy in the hot path.

    The same convention as numpy's default so a number quoted in the report can
    be reproduced with `statistics` or on a calculator without a surprise.
    """
    if not values:
        raise ValueError("percentile of an empty sample")
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    fraction = position - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def _stage_metrics(samples: list[float]) -> StageMetrics:
    if not samples:
        # n=0 with zeroed percentiles is the honest shape: no traffic, so no
        # claim. It cannot be confused with a fast stage because n says so.
        return StageMetrics(n=0, p50_ms=0.0, p95_ms=0.0, p99_ms=0.0, max_ms=0.0)
    return StageMetrics(
        n=len(samples),
        p50_ms=_round(percentile(samples, 0.50)),
        p95_ms=_round(percentile(samples, 0.95)),
        p99_ms=_round(percentile(samples, 0.99)),
        max_ms=_round(max(samples)),
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def scratch_path(prefix: str = "requests-") -> Path:
    """A throwaway log path for tests that must not touch the real log."""
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=".jsonl")
    os.close(handle)
    return Path(name)
