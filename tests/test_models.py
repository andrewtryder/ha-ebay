"""Tests for shared ApiFailure model helpers."""

from __future__ import annotations

from custom_components.ebay.models import ApiFailure


def test_api_failure_to_diagnostics_and_dict() -> None:
    failure = ApiFailure(
        operation="get_orders",
        endpoint_key="fulfillment.orders",
        http_status=503,
        ebay_error_id=1001,
        ebay_domain="API",
        ebay_category="REQUEST",
        message="unavailable",
        classification="transient",
        mapped_category="orders",
    )
    diagnostics = failure.to_diagnostics()
    assert diagnostics["status"] == 503
    assert diagnostics["transient"] is True
    assert diagnostics["mapped_category"] == "orders"
    assert failure.to_dict()["operation"] == "get_orders"


def test_api_failure_from_dict_rejects_invalid() -> None:
    assert ApiFailure.from_dict(None) is None
    assert ApiFailure.from_dict("x") is None  # type: ignore[arg-type]
    assert ApiFailure.from_dict({"operation": 1, "endpoint_key": "e"}) is None
    assert (
        ApiFailure.from_dict(
            {"operation": "op", "endpoint_key": "e", "mapped_category": 1}
        )
        is None
    )


def test_api_failure_from_dict_normalizes_classification_and_category() -> None:
    failure = ApiFailure.from_dict(
        {
            "operation": "op",
            "endpoint_key": "endpoint",
            "mapped_category": "orders",
            "classification": "not-a-real-class",
            "status": 400,
            "domain": "API",
            "category": "REQUEST",
            "message": "bad",
            "ebay_error_id": "E1",
        }
    )
    assert failure is not None
    assert failure.classification == "unknown"
    assert failure.http_status == 400
    assert failure.ebay_domain == "API"
    assert failure.ebay_category == "REQUEST"
    assert failure.message == "bad"

    with_class = ApiFailure.from_dict(
        {
            "operation": "op",
            "endpoint_key": "endpoint",
            "mapped_category": "orders",
            "classification": "permission",
            "http_status": 403,
            "ebay_category": "AUTH",
        }
    )
    assert with_class is not None
    assert with_class.classification == "permission"
    assert with_class.ebay_category == "AUTH"

    no_category = ApiFailure.from_dict(
        {
            "operation": "op",
            "endpoint_key": "endpoint",
            "mapped_category": "orders",
            "category": 12,
        }
    )
    assert no_category is not None
    assert no_category.ebay_category is None
