"""Normalised provider errors.

Every provider, whatever its transport or wire format, raises exactly one of
these. The engine never sees an `httpx.Timeout`, a `KeyError` from a JSON body,
or a provider's own error string. This boundary is the difference between adding
a provider in a day and discovering, three providers later, that every call site
knows about every provider's quirks.

Two rules that this module enforces by construction:

1. **The public message is ours.** `message` is written for a user and for a log,
   never copied from an upstream response. Anything that smells like upstream
   detail goes in `cause_detail`, which is logged and never serialised.
2. **A code is a small closed set.** Clients switch on `Problem.code`, so the set
   lives in the contract (`wire.models.Code`) and every error maps onto it.
"""

from __future__ import annotations

from typing import ClassVar

from railmind.wire.models import Code, Problem

_ERROR_TYPE_BASE = "https://railmind.dev/errors/"


class ProviderError(Exception):
    """Base class for every failure a provider is allowed to raise.

    Deliberately not a `ValueError` or an `IOError`: those are raised by accident
    too, and an accidental error must be treated as a bug in our code, not as
    something a provider is entitled to signal.
    """

    code: ClassVar[Code] = Code.internal
    http_status: ClassVar[int] = 502
    retryable: ClassVar[bool] = False
    #: Human-facing summary. Ours, safe to show.
    title: ClassVar[str] = "Provider failure"

    def __init__(
        self,
        message: str,
        *,
        provider_id: str | None = None,
        retry_after_s: int | None = None,
        cause_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider_id = provider_id
        self.retry_after_s = retry_after_s
        #: Upstream string, stack hints, response body. Logged only.
        self.cause_detail = cause_detail

    def __str__(self) -> str:
        suffix = f" [{self.provider_id}]" if self.provider_id else ""
        return f"{self.code.value}: {self.message}{suffix}"

    def to_problem(
        self,
        *,
        request_id: str | None = None,
        instance: str | None = None,
    ) -> Problem:
        """Serialise for the wire. `cause_detail` is never included."""
        return Problem(
            type=f"{_ERROR_TYPE_BASE}{self.code.value.replace('_', '-')}",
            title=self.title,
            status=self.http_status,
            detail=self.message,
            instance=instance,
            code=self.code,
            retryable=self.retryable,
            retry_after_s=self.retry_after_s,
            provider_id=self.provider_id,
            request_id=request_id,
        )


class NotSupported(ProviderError):
    """The provider was asked for something its capabilities say it cannot do."""

    code = Code.not_supported
    http_status = 422
    title = "Not supported by this provider"


class RateLimited(ProviderError):
    """Upstream is throttling us. Back off; do not retry immediately."""

    code = Code.rate_limited
    http_status = 429
    retryable = True
    title = "Upstream rate limit"


class UpstreamDown(ProviderError):
    """Upstream failed or is unreachable. Trip the circuit breaker."""

    code = Code.upstream_down
    http_status = 502
    retryable = True
    title = "Upstream unavailable"


class SoldOut(ProviderError):
    """Real answer, not a failure: nothing is left to sell."""

    code = Code.sold_out
    http_status = 409
    title = "Sold out"


class InvalidQuery(ProviderError):
    """The request cannot be answered as asked. Our fault, fixable by the caller."""

    code = Code.invalid_query
    http_status = 400
    title = "Invalid query"


class DeadlineExceeded(ProviderError):
    """The caller's deadline passed before the answer arrived.

    Retryable in the sense that the same query may succeed later, but not from
    the same request: the deadline has already been spent.
    """

    code = Code.deadline_exceeded
    http_status = 504
    retryable = True
    title = "Deadline exceeded"


class UnknownProvider(NotSupported):
    """No provider is registered under that id."""

    http_status = 404
    title = "Unknown provider"


class NotFound(ProviderError):
    """The resource does not exist, or has expired."""

    code = Code.not_found
    http_status = 404
    title = "Not found"


class Conflict(ProviderError):
    """State changed under us, for example availability moved before handoff."""

    code = Code.conflict
    http_status = 409
    title = "State changed"


class AmbiguousIntent(ProviderError):
    """The request needs a human decision before it can be answered."""

    code = Code.ambiguous_intent
    http_status = 422
    title = "Needs clarification"


def as_problem(
    error: BaseException,
    *,
    request_id: str | None = None,
    instance: str | None = None,
) -> Problem:
    """Convert anything into a `Problem`.

    Used at the API edge. A `ProviderError` keeps its own code. Anything else is
    a bug on our side, so it becomes `internal` with a message that reveals
    nothing about the exception.
    """
    if isinstance(error, ProviderError):
        return error.to_problem(request_id=request_id, instance=instance)
    return Problem(
        type=f"{_ERROR_TYPE_BASE}internal",
        title="Internal error",
        status=500,
        detail="The request could not be completed.",
        instance=instance,
        code=Code.internal,
        retryable=False,
        request_id=request_id,
    )
