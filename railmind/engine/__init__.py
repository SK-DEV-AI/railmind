"""The decision engine: ranking, fan-out, and the measurements behind the claim.

Above the provider boundary and below the API. Nothing here knows about HTTP,
and nothing here touches the network except through a provider passed in, which
is what lets the whole engine be tested without one.
"""

from railmind.engine.metrics import RequestLog, RequestRecord, percentile
from railmind.engine.orchestrator import SearchEngine
from railmind.engine.ranking import pareto_front, rank

__all__ = [
    "RequestLog",
    "RequestRecord",
    "SearchEngine",
    "pareto_front",
    "percentile",
    "rank",
]
