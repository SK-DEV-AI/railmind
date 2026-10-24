"""Errors: one closed set, and no upstream text on the wire."""

from __future__ import annotations

import pytest

from railmind.core.errors import (
    AmbiguousIntent,
    Conflict,
    DeadlineExceeded,
    InvalidQuery,
    NotFound,
    NotSupported,
    ProviderError,
    RateLimited,
    SoldOut,
    UnknownProvider,
    UpstreamDown,
    as_problem,
)
from railmind.wire.models import Code


class TestCodes:
    @pytest.mark.parametrize(
        ("error_class", "code", "status", "retryable"),
        [
            (NotSupported, Code.not_supported, 422, False),
            (RateLimited, Code.rate_limited, 429, True),
            (UpstreamDown, Code.upstream_down, 502, True),
            (SoldOut, Code.sold_out, 409, False),
            (InvalidQuery, Code.invalid_query, 400, False),
            (DeadlineExceeded, Code.deadline_exceeded, 504, True),
            (NotFound, Code.not_found, 404, False),
            (Conflict, Code.conflict, 409, False),
            (AmbiguousIntent, Code.ambiguous_intent, 422, False),
        ],
    )
    def test_maps_to_contract_code(self, error_class, code, status, retryable):
        problem = error_class("nope", provider_id="p").to_problem()
        assert problem.code is code
        assert problem.status == status
        assert problem.retryable is retryable

    def test_every_error_class_uses_a_contract_code(self):
        for error_class in ProviderError.__subclasses__():
            assert isinstance(error_class.code, Code), error_class

    def test_unknown_provider_is_not_supported_but_404(self):
        error = UnknownProvider("no such provider")
        assert error.code is Code.not_supported
        assert error.http_status == 404


class TestProblemSerialisation:
    def test_carries_provider_and_request_id(self):
        problem = RateLimited(
            "Give it a minute", provider_id="railradar", retry_after_s=30
        ).to_problem(request_id="req_1", instance="/search")
        assert problem.provider_id == "railradar"
        assert problem.retry_after_s == 30
        assert problem.request_id == "req_1"
        assert problem.instance == "/search"

    def test_type_url_follows_the_code(self):
        assert str(UpstreamDown("x").to_problem().type).endswith("/errors/upstream-down")

    def test_cause_detail_never_reaches_the_wire(self):
        error = UpstreamDown(
            "The data source is unavailable",
            provider_id="railradar",
            cause_detail="HTTP 503 from upstream: <html>Akamai reference 18.abc</html>",
        )
        problem = error.to_problem()
        serialised = problem.model_dump_json()
        assert "Akamai" not in serialised
        assert "18.abc" not in serialised
        # The detail is still available for logs, which is the point.
        assert "Akamai" in (error.cause_detail or "")


class TestAsProblem:
    def test_provider_error_keeps_its_code(self):
        problem = as_problem(SoldOut("No seats left"), request_id="req_2")
        assert problem.code is Code.sold_out
        assert problem.detail == "No seats left"
        assert problem.request_id == "req_2"

    def test_unexpected_exception_becomes_internal_and_says_nothing(self):
        leaky = RuntimeError("secret upstream detail: <html>503 Service Unavailable</html>")
        problem = as_problem(leaky)
        assert problem.code is Code.internal
        assert problem.status == 500
        assert problem.retryable is False
        assert "secret" not in problem.model_dump_json()
        assert "html" not in problem.model_dump_json()

    def test_key_error_is_not_treated_as_a_provider_error(self):
        problem = as_problem(KeyError("data"))
        assert problem.code is Code.internal
