"""Table-driven tests for REST status classification and ApiFailure building."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.ebay.api import build_api_failure, classify_rest_status
from custom_components.ebay.models import FailureClassification

# (status, error_payload, partial_category, expected_classification)
_CLASSIFY_CASES: list[tuple[int, dict[str, Any] | None, str, FailureClassification]] = [
    (204, None, "orders", "unknown"),
    (400, None, "feedback", "malformed_request"),
    (401, None, "orders", "unknown"),
    (403, None, "orders", "permission"),
    (
        403,
        {"errors": [{"message": "Account is not eligible for this API"}]},
        "feedback",
        "unsupported",
    ),
    (404, None, "finances", "unknown"),
    (429, None, "orders", "transient"),
    (500, None, "messages", "transient"),
]


@pytest.mark.parametrize(
    ("status", "error_payload", "partial_category", "expected"),
    _CLASSIFY_CASES,
    ids=[str(case[0]) + ("-elig" if case[1] else "") for case in _CLASSIFY_CASES],
)
def test_classify_rest_status_table(
    status: int,
    error_payload: dict[str, Any] | None,
    partial_category: str,
    expected: FailureClassification,
) -> None:
    assert classify_rest_status(status, error_payload, partial_category) == expected


@pytest.mark.parametrize(
    ("status", "error_payload", "partial_category", "classification"),
    [
        (204, None, "orders", "unknown"),
        (400, None, "feedback", "malformed_request"),
        (401, None, "orders", "unknown"),
        (403, None, "orders", "permission"),
        (404, None, "finances", "unknown"),
        (429, None, "orders", "transient"),
        (500, None, "messages", "transient"),
    ],
    ids=["204", "400", "401", "403", "404", "429", "500"],
)
def test_build_api_failure_table(
    status: int,
    error_payload: dict[str, Any] | None,
    partial_category: str,
    classification: FailureClassification,
) -> None:
    failure = build_api_failure(
        "test.op",
        "https://api.ebay.com/sell/fulfillment/v1/order",
        status,
        error_payload,
        partial_category,
        classification,
    )
    assert failure.http_status == status
    assert failure.classification == classification
    assert failure.mapped_category == partial_category
    assert failure.endpoint_key == "/sell/fulfillment/v1/order"
    assert failure.operation == "test.op"
