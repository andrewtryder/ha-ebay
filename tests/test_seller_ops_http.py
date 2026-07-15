"""HTTP coverage for seller-ops REST helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from custom_components.ebay.api import (
    EbayApiClient,
    EbayPartialFailure,
    map_rest_failure_category,
)


class _Response:
    def __init__(self, *, status: int = 200, json_payload: Any | None = None) -> None:
        self.status = status
        self._json_payload = json_payload if json_payload is not None else {}

    async def json(self, *, content_type: str | None = None) -> Any:
        return self._json_payload


class _Context:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *args: Any) -> None:
        return None


def _client(
    responses: list[_Response], *, capture: list[dict[str, Any]] | None = None
) -> EbayApiClient:
    session = Mock()
    queue = list(responses)

    def _get(*args: Any, **kwargs: Any) -> _Context:
        if capture is not None:
            capture.append({"args": args, "kwargs": kwargs})
        return _Context(queue.pop(0))

    session.get = Mock(side_effect=_get)
    client = EbayApiClient(
        session,
        environment="production",
        client_id="client",
        client_secret="secret",
        runame="runame",
        refresh_token="refresh",
        site_id="0",
    )
    client._access_token = "token"
    client._access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _identity_response(username: str = "seller_user") -> _Response:
    return _Response(json_payload={"userId": "007IND2xyeBay", "username": username})


def test_fetch_orders_and_disputes() -> None:
    now = datetime.now(timezone.utc)
    ship_by = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    client = _client(
        [
            _Response(
                json_payload={
                    "orders": [
                        {
                            "orderId": "o1",
                            "orderPaymentStatus": "PAID",
                            "orderFulfillmentStatus": "NOT_STARTED",
                            "lineItems": [
                                {
                                    "legacyItemId": "1",
                                    "title": "Item",
                                    "lineItemFulfillmentInstructions": {
                                        "shipByDate": ship_by
                                    },
                                }
                            ],
                        }
                    ],
                    "total": 1,
                }
            ),
            _Response(json_payload={"orders": [], "total": 0}),
            _Response(
                json_payload={
                    "paymentDisputeSummaries": [
                        {
                            "paymentDisputeId": "d1",
                            "orderId": "o1",
                            "paymentDisputeStatus": "OPEN",
                            "reason": "FRAUD",
                        }
                    ],
                    "total": 1,
                }
            ),
        ]
    )
    orders, truncated = asyncio.run(client.async_fetch_orders())
    assert truncated is False
    assert orders["awaiting_shipment"] == 1
    disputes, d_truncated = asyncio.run(client.async_fetch_payment_disputes())
    assert d_truncated is False
    assert disputes["open_count"] == 1


def test_fetch_standards_feedback_messages() -> None:
    capture: list[dict[str, Any]] = []
    client = _client(
        [
            _Response(
                json_payload={
                    "standardsProfiles": [
                        {
                            "standardsLevel": "TOP_RATED",
                            "program": "PROGRAM_US",
                            "defaultProgram": True,
                            "cycle": {
                                "cycleType": "CURRENT",
                                "evaluationDate": "2026-08-01T00:00:00.000Z",
                            },
                            "metrics": [],
                        }
                    ]
                }
            ),
            _Response(
                json_payload={
                    "dimensionMetrics": [
                        {
                            "metrics": [
                                {
                                    "benchmark": {"rating": "HIGH"},
                                    "value": {"value": "LOW"},
                                }
                            ]
                        }
                    ]
                }
            ),
            _Response(json_payload={"dimensionMetrics": []}),
            _identity_response("seller_user"),
            _Response(
                json_payload={
                    "feedbackRatingSummary": {
                        "ratingSummaryByRatingType": [
                            {
                                "ratingType": "OVERALL_EXPERIENCE",
                                "period": {"value": 30, "unit": "DAY"},
                                "feedbackMetrics": [
                                    {"metricName": "FEEDBACK_SCORE", "value": "50"},
                                    {
                                        "metricName": "POSITIVE_FEEDBACK_PERCENTAGE",
                                        "value": "100",
                                    },
                                ],
                                "feedbackRatingValueDistribution": [
                                    {"ratingValue": "POSITIVE", "count": 10},
                                ],
                            }
                        ]
                    }
                }
            ),
            _Response(
                json_payload={
                    "feedbackEntries": [{"commentType": "POSITIVE"}],
                }
            ),
            _Response(json_payload={"total": 2, "itemsAwaitingFeedback": [{}, {}]}),
            _Response(
                json_payload={
                    "conversations": [
                        {
                            "conversationId": "c1",
                            "conversationStatus": "UNREAD",
                            "conversationTitle": "Hi",
                            "reference": {"referenceId": "9"},
                            "latestMessage": {
                                "messageType": "QUESTION",
                                "creationDate": "2026-07-11T10:00:00.000Z",
                            },
                        }
                    ],
                    "total": 1,
                }
            ),
        ],
        capture=capture,
    )
    standards = asyncio.run(client.async_fetch_seller_standards())
    assert standards["seller_level"] == "TOP_RATED"
    assert standards["item_not_received_above_benchmark"] is True

    feedback = asyncio.run(client.async_fetch_feedback())
    assert feedback["score"] == 50
    assert feedback["positive_percent"] == 100.0
    # Item counts overwrite summary distribution when any item counts are non-zero.
    assert feedback["recent_positive"] == 1
    assert feedback["awaiting_count"] == 2

    summary_calls = [
        call
        for call in capture
        if call["args"] and "feedback_rating_summary" in str(call["args"][0])
    ]
    assert summary_calls
    assert summary_calls[0]["kwargs"]["params"]["user_id"] == "seller_user"
    assert (
        "ratingType:OVERALL_EXPERIENCE"
        in summary_calls[0]["kwargs"]["params"]["filter"]
    )

    items_calls = [
        call
        for call in capture
        if call["args"]
        and str(call["args"][0]).endswith("/feedback")
        and "feedback_rating_summary" not in str(call["args"][0])
    ]
    assert items_calls
    assert items_calls[0]["kwargs"]["params"]["user_id"] == "seller_user"
    assert items_calls[0]["kwargs"]["params"]["feedback_type"] == "FEEDBACK_RECEIVED"

    awaiting_calls = [
        call
        for call in capture
        if call["args"] and "awaiting_feedback" in str(call["args"][0])
    ]
    assert awaiting_calls
    assert "user_id" not in awaiting_calls[0]["kwargs"]["params"]

    messages, truncated = asyncio.run(client.async_fetch_messages())
    assert truncated is False
    assert messages["unread_count"] == 1


def test_rest_partial_failure_on_403() -> None:
    client = _client([_Response(status=403)])
    with pytest.raises(EbayPartialFailure):
        asyncio.run(
            client._async_rest_get(
                "https://api.ebay.com/sell/fulfillment/v1/order",
                partial_category="orders",
            )
        )


def test_fetch_orders_marks_truncated_at_page_cap() -> None:
    """Exhausting ORDERS_MAX_PAGES sets truncated without dropping earlier pages."""
    from custom_components.ebay.seller_ops import ORDERS_MAX_PAGES, ORDERS_PAGE_LIMIT

    def _page(start: int) -> _Response:
        orders = [
            {
                "orderId": f"o{start + i}",
                "orderPaymentStatus": "PAID",
                "orderFulfillmentStatus": "NOT_STARTED",
                "lineItems": [{"legacyItemId": str(start + i), "title": "Item"}],
            }
            for i in range(ORDERS_PAGE_LIMIT)
        ]
        return _Response(
            json_payload={
                "orders": orders,
                "total": ORDERS_MAX_PAGES * ORDERS_PAGE_LIMIT * 2,
            }
        )

    # First filter exhausts the page cap; second filter returns empty immediately.
    responses = [_page(page * ORDERS_PAGE_LIMIT) for page in range(ORDERS_MAX_PAGES)]
    responses.append(_Response(json_payload={"orders": [], "total": 0}))
    client = _client(responses)

    orders, truncated = asyncio.run(client.async_fetch_orders())
    assert truncated is True
    assert len(orders["by_id"]) == ORDERS_MAX_PAGES * ORDERS_PAGE_LIMIT


def test_fetch_disputes_and_messages_mark_truncated_at_page_cap() -> None:
    """Dispute and message pagination set truncated when the page cap is hit."""
    from custom_components.ebay.seller_ops import (
        CONVERSATIONS_MAX_PAGES,
        CONVERSATIONS_PAGE_LIMIT,
        DISPUTES_MAX_PAGES,
        DISPUTES_PAGE_LIMIT,
    )

    dispute_pages = [
        _Response(
            json_payload={
                "paymentDisputeSummaries": [
                    {
                        "paymentDisputeId": f"d{page}-{i}",
                        "orderId": "o1",
                        "paymentDisputeStatus": "OPEN",
                        "reason": "FRAUD",
                    }
                    for i in range(DISPUTES_PAGE_LIMIT)
                ],
                "total": DISPUTES_MAX_PAGES * DISPUTES_PAGE_LIMIT * 2,
            }
        )
        for page in range(DISPUTES_MAX_PAGES)
    ]
    message_pages = [
        _Response(
            json_payload={
                "conversations": [
                    {
                        "conversationId": f"c{page}-{i}",
                        "conversationStatus": "UNREAD",
                        "conversationTitle": "Hi",
                        "latestMessage": {
                            "messageType": "MESSAGE",
                            "creationDate": "2026-07-11T10:00:00.000Z",
                        },
                    }
                    for i in range(CONVERSATIONS_PAGE_LIMIT)
                ],
                "total": CONVERSATIONS_MAX_PAGES * CONVERSATIONS_PAGE_LIMIT * 2,
            }
        )
        for page in range(CONVERSATIONS_MAX_PAGES)
    ]
    client = _client(dispute_pages + message_pages)

    disputes, d_truncated = asyncio.run(client.async_fetch_payment_disputes())
    assert d_truncated is True
    assert disputes["open_count"] == DISPUTES_MAX_PAGES * DISPUTES_PAGE_LIMIT

    messages, m_truncated = asyncio.run(client.async_fetch_messages())
    assert m_truncated is True
    assert len(messages["by_id"]) == CONVERSATIONS_MAX_PAGES * CONVERSATIONS_PAGE_LIMIT


def test_fetch_feedback_keeps_summary_when_items_soft_fail() -> None:
    """Feedback item/awaiting soft-failures keep the successful summary."""
    from unittest.mock import AsyncMock

    client = _client([])
    client._feedback_user_id = "seller_user"
    summary = {
        "feedbackRatingSummary": {
            "ratingSummaryByRatingType": [
                {
                    "ratingType": "OVERALL_EXPERIENCE",
                    "feedbackMetrics": [
                        {"metricName": "FEEDBACK_SCORE", "value": "50"},
                        {"metricName": "POSITIVE_FEEDBACK_PERCENTAGE", "value": "100"},
                    ],
                }
            ]
        }
    }

    async def _rest_get(url: str, **kwargs: Any) -> dict[str, Any]:
        if "feedback_rating_summary" in url:
            assert kwargs.get("params", {}).get("user_id") == "seller_user"
            return summary
        raise EbayPartialFailure("feedback")

    client._async_rest_get = AsyncMock(side_effect=_rest_get)  # type: ignore[method-assign]
    feedback = asyncio.run(client.async_fetch_feedback())
    assert feedback["score"] == 50
    assert feedback["awaiting_count"] is None


def test_map_rest_failure_category_feedback() -> None:
    assert map_rest_failure_category("feedback", 400) == "feedback_invalid_request"
    assert map_rest_failure_category("feedback", 403) == "feedback_forbidden"
    assert (
        map_rest_failure_category(
            "feedback",
            403,
            {
                "errors": [
                    {
                        "category": "REQUEST",
                        "message": "Account is not eligible for this API",
                    }
                ]
            },
        )
        == "feedback"
    )
    assert map_rest_failure_category("orders", 400) == "orders"


def test_feedback_http_400_maps_to_invalid_request() -> None:
    client = _client(
        [
            _identity_response(),
            _Response(
                status=400,
                json_payload={
                    "errors": [
                        {
                            "errorId": 35001,
                            "message": "Invalid request: user_id is required",
                        }
                    ]
                },
            ),
        ]
    )
    with pytest.raises(EbayPartialFailure, match="feedback_invalid_request"):
        asyncio.run(client.async_fetch_feedback())


def test_feedback_user_id_is_cached_across_calls() -> None:
    capture: list[dict[str, Any]] = []
    client = _client(
        [
            _identity_response("cached_seller"),
            _Response(json_payload={"feedbackRatingSummary": {}}),
            _Response(json_payload={"feedbackEntries": []}),
            _Response(json_payload={"total": 0}),
            _Response(json_payload={"feedbackRatingSummary": {}}),
            _Response(json_payload={"feedbackEntries": []}),
            _Response(json_payload={"total": 0}),
        ],
        capture=capture,
    )
    asyncio.run(client.async_fetch_feedback())
    asyncio.run(client.async_fetch_feedback())
    identity_calls = [
        call
        for call in capture
        if call["args"] and "commerce/identity" in str(call["args"][0])
    ]
    assert len(identity_calls) == 1
    assert client._feedback_user_id == "cached_seller"


def test_fetch_seller_standards_skips_metrics_for_unknown_site() -> None:
    """Unknown site IDs return profiles without INR/INAD marketplace calls."""
    from unittest.mock import AsyncMock

    client = _client([])
    client.site_id = "999"
    calls: list[str] = []

    async def _rest_get(url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(url)
        return {
            "standardsProfiles": [
                {
                    "standardsLevel": "TOP_RATED",
                    "program": "PROGRAM_US",
                    "defaultProgram": True,
                    "cycle": {
                        "cycleType": "CURRENT",
                        "evaluationDate": "2026-08-01T00:00:00.000Z",
                    },
                    "metrics": [],
                }
            ]
        }

    client._async_rest_get = AsyncMock(side_effect=_rest_get)  # type: ignore[method-assign]
    standards = asyncio.run(client.async_fetch_seller_standards())
    assert standards["seller_level"] == "TOP_RATED"
    assert len(calls) == 1
    assert "customer_service_metric" not in calls[0]
