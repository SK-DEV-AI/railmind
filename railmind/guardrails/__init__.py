"""Guardrails: the published rules that decide whether advice is sound right now.

Separate from the engine because these are policy, not ranking: they come from
Indian Railways' own terms and circulars, they are dated, and they must be
auditable in one place rather than scattered through the request path.
"""

from railmind.guardrails.tatkal import (
    ADVANCE_DAYS,
    AGENT_BLACKOUT_MINUTES,
    GENERAL_MAX_PASSENGERS,
    IST,
    TATKAL_MAX_PASSENGERS,
    Verdict,
    blackout_window,
    check_tatkal,
    is_ac,
    opening_time,
)

__all__ = [
    "ADVANCE_DAYS",
    "AGENT_BLACKOUT_MINUTES",
    "GENERAL_MAX_PASSENGERS",
    "IST",
    "TATKAL_MAX_PASSENGERS",
    "Verdict",
    "blackout_window",
    "check_tatkal",
    "is_ac",
    "opening_time",
]
