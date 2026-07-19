"""HTTP coverage for seller-ops REST helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
import xml.etree.ElementTree as ET

import pytest

from custom_components.ebay.api import (
    EBAY_XML_NS,
    EbayApiClient,
    EbayApiError,
    EbayAuthError,
    EbayPartialFailure,
    GET_USER_CALL_NAME,
    build_api_failure,
    classify_rest_status,
    endpoint_key_from_url,
    map_rest_failure_category,
    merge_partial_failure_details,
    sanitize_ebay_rest_errors,
)
from custom_components.ebay.seller_ops import (
    FEEDBACK_ITEMS_MAX_PAGES,
    FEEDBACK_ITEMS_PAGE_LIMIT,
)


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        json_payload: Any | None = None,
        headers: dict[str, str] | None = None,
        text: str | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._json_payload = json_payload
        self._text = text

    async def json(self, *, content_type: str | None = None) -> Any:
        if self._json_payload is None and self._text is not None:
            if not self._text.strip():
                raise json.JSONDecodeError("Expecting value", self._text, 0)
            return json.loads(self._text)
        return self._json_payload if self._json_payload is not None else {}

    async def text(self) -> str:
        if self._text is not None:
            return self._text
        if self._json_payload is None:
            return ""
        return json.dumps(self._json_payload)


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


def _get_user_root(username: str = "seller_user") -> ET.Element:
    return ET.fromstring(
        f"""<?xml version="1.0" encoding="utf-8"?>
<GetUserResponse xmlns="{EBAY_XML_NS}">
  <Ack>Success</Ack>
  <User><UserID>{username}</UserID></User>
</GetUserResponse>
"""
    )


def _stub_get_user(client: EbayApiClient, username: str = "seller_user") -> AsyncMock:
    """Stub Trading GetUser for Feedback username resolution."""
    mock = AsyncMock(return_value=_get_user_root(username))

    async def _call(call_name: str, xml_body: str) -> ET.Element:
        assert call_name == GET_USER_CALL_NAME
        assert "GetUserRequest" in xml_body
        return await mock(call_name, xml_body)

    client.async_call_trading_api = AsyncMock(side_effect=_call)  # type: ignore[method-assign]
    return mock


def test_endpoint_key_from_url_strips_query() -> None:
    assert (
        endpoint_key_from_url(
            "https://api.ebay.com/commerce/feedback/v1/feedback_rating_summary"
            "?user_id=secret"
        )
        == "/commerce/feedback/v1/feedback_rating_summary"
    )


def test_classify_rest_status_table() -> None:
    assert classify_rest_status(400, None, "feedback") == "malformed_request"
    assert classify_rest_status(429, None, "orders") == "transient"
    assert classify_rest_status(503, None, "orders") == "transient"
    assert (
        classify_rest_status(
            403,
            {"errors": [{"message": "Account is not eligible for this API"}]},
            "feedback",
        )
        == "unsupported"
    )
    assert (
        classify_rest_status(
            403,
            {"errors": [{"category": "REQUEST", "message": "Access denied"}]},
            "orders",
        )
        == "permission"
    )


def test_build_api_failure_sanitizes_message() -> None:
    failure = build_api_failure(
        "feedback.get",
        "https://api.ebay.com/commerce/feedback/v1/feedback_rating_summary?user_id=x",
        400,
        {
            "errors": [
                {
                    "errorId": 35001,
                    "domain": "API_FEEDBACK",
                    "category": "REQUEST",
                    "message": "Invalid request token=secret-value",
                }
            ]
        },
        "feedback_invalid_request",
        "malformed_request",
    )
    assert failure.endpoint_key == "/commerce/feedback/v1/feedback_rating_summary"
    assert failure.ebay_error_id == 35001
    assert failure.message == "[redacted]"
    assert failure.classification == "malformed_request"


def test_rest_204_returns_empty_dict() -> None:
    client = _client([_Response(status=204, text="")])
    payload = asyncio.run(
        client._async_rest_get(
            "https://api.ebay.com/sell/fulfillment/v1/order",
            partial_category="orders",
        )
    )
    assert payload == {}


def test_rest_200_empty_body_returns_empty_dict() -> None:
    client = _client([_Response(status=200, text="")])
    payload = asyncio.run(
        client._async_rest_get(
            "https://api.ebay.com/sell/fulfillment/v1/order",
            partial_category="orders",
        )
    )
    assert payload == {}


def test_rest_401_raises_auth_error() -> None:
    client = _client([_Response(status=401)])
    with pytest.raises(EbayAuthError):
        asyncio.run(
            client._async_rest_get_once(
                "https://api.ebay.com/sell/fulfillment/v1/order",
                partial_category="orders",
            )
        )


def test_rest_429_soft_fails_transient_after_bounded_sleep() -> None:
    client = _client(
        [_Response(status=429, headers={"Retry-After": "30"}, json_payload={})]
    )
    with patch(
        "custom_components.ebay.api.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        with pytest.raises(EbayPartialFailure) as exc_info:
            asyncio.run(
                client._async_rest_get(
                    "https://api.ebay.com/sell/fulfillment/v1/order",
                    partial_category="orders",
                )
            )
    sleep.assert_awaited_once_with(5)
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "transient"
    assert client._last_api_failure is not None
    assert client._last_api_failure.classification == "transient"


def test_rest_5xx_soft_fails_transient() -> None:
    client = _client([_Response(status=503, json_payload={})])
    with pytest.raises(EbayPartialFailure) as exc_info:
        asyncio.run(
            client._async_rest_get(
                "https://api.ebay.com/sell/fulfillment/v1/order",
                partial_category="orders",
            )
        )
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "transient"
    assert exc_info.value.category == "orders"


def test_rest_accept_language_header_for_locale_site() -> None:
    capture: list[dict[str, Any]] = []
    client = _client([_Response(json_payload={})], capture=capture)
    client.site_id = "210"
    client._accept_language = "fr-CA"
    asyncio.run(
        client._async_rest_get(
            "https://api.ebay.com/sell/analytics/v1/seller_standards_profile",
            partial_category="seller_standards",
        )
    )
    assert capture[0]["kwargs"]["headers"]["Accept-Language"] == "fr-CA"


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
                    "feedbackEntries": [
                        {"feedbackId": "fb-1", "commentType": "POSITIVE"},
                    ],
                    "total": 1,
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
    _stub_get_user(client, "seller_user")
    standards = asyncio.run(client.async_fetch_seller_standards())
    assert standards["seller_level"] == "TOP_RATED"
    assert standards["item_not_received_above_benchmark"] is True

    feedback = asyncio.run(client.async_fetch_feedback())
    assert feedback["score"] == 50
    assert feedback["positive_percent"] == 100.0
    # Rating-summary distribution stays authoritative even when items are present.
    assert feedback["recent_positive"] == 10
    assert feedback["recent_feedback_ids"] == ["fb-1"]
    assert feedback["items_truncated"] is False
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
    with pytest.raises(EbayPartialFailure) as exc_info:
        asyncio.run(
            client._async_rest_get(
                "https://api.ebay.com/sell/fulfillment/v1/order",
                partial_category="orders",
            )
        )
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "permission"
    assert exc_info.value.failure.operation == "orders.get"


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
    assert feedback["recent_feedback_ids"] is None


def test_fetch_feedback_summary_counts_authoritative_over_items() -> None:
    """Summary distribution wins even when the first items page has non-zero counts."""
    client = _client(
        [
            _Response(
                json_payload={
                    "feedbackRatingSummary": {
                        "ratingSummaryByRatingType": [
                            {
                                "ratingType": "OVERALL_EXPERIENCE",
                                "feedbackRatingValueDistribution": [
                                    {"ratingValue": "POSITIVE", "count": 40},
                                    {"ratingValue": "NEUTRAL", "count": 2},
                                    {"ratingValue": "NEGATIVE", "count": 1},
                                ],
                            }
                        ]
                    }
                }
            ),
            _Response(
                json_payload={
                    "feedbackEntries": [
                        {"feedbackId": "p1", "commentType": "POSITIVE"},
                        {"feedbackId": "n1", "commentType": "NEGATIVE"},
                    ],
                    "total": 2,
                }
            ),
            _Response(json_payload={"total": 0}),
        ]
    )
    _stub_get_user(client, "seller_user")
    feedback = asyncio.run(client.async_fetch_feedback())
    assert feedback["recent_positive"] == 40
    assert feedback["recent_neutral"] == 2
    assert feedback["recent_negative"] == 1
    assert feedback["recent_feedback_ids"] == ["p1", "n1"]
    assert feedback["recent_negative_feedback_ids"] == ["n1"]
    assert feedback["items_truncated"] is False


def test_fetch_feedback_items_pagination_marks_truncated() -> None:
    """Feedback item pagination sets items_truncated when the page cap is hit."""
    summary = _Response(
        json_payload={
            "feedbackRatingSummary": {
                "ratingSummaryByRatingType": [
                    {
                        "ratingType": "OVERALL_EXPERIENCE",
                        "feedbackRatingValueDistribution": [
                            {"ratingValue": "POSITIVE", "count": 500},
                        ],
                    }
                ]
            }
        }
    )
    item_pages = [
        _Response(
            json_payload={
                "feedbackEntries": [
                    {
                        "feedbackId": f"fb-{page}-{i}",
                        "commentType": "POSITIVE",
                    }
                    for i in range(FEEDBACK_ITEMS_PAGE_LIMIT)
                ],
                "total": FEEDBACK_ITEMS_MAX_PAGES * FEEDBACK_ITEMS_PAGE_LIMIT * 2,
            }
        )
        for page in range(FEEDBACK_ITEMS_MAX_PAGES)
    ]
    client = _client([summary, *item_pages, _Response(json_payload={"total": 0})])
    _stub_get_user(client, "seller_user")
    feedback = asyncio.run(client.async_fetch_feedback())
    assert feedback["recent_positive"] == 500
    assert feedback["items_truncated"] is True
    assert len(feedback["recent_feedback_ids"]) == (
        FEEDBACK_ITEMS_MAX_PAGES * FEEDBACK_ITEMS_PAGE_LIMIT
    )


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
    assert (
        map_rest_failure_category("feedback", 400, classification="malformed_request")
        == "feedback_invalid_request"
    )


def test_sanitize_ebay_rest_errors_redacts_and_truncates() -> None:
    errors = sanitize_ebay_rest_errors(
        {
            "errors": [
                {
                    "errorId": 35001,
                    "domain": "API_FEEDBACK",
                    "category": "REQUEST",
                    "message": "Invalid request token=secret-value",
                    "longMessage": "x" * 300,
                },
                "skip-me",
            ]
        }
    )
    assert len(errors) == 1
    assert errors[0]["error_id"] == 35001
    assert errors[0]["message"] == "[redacted]"
    assert errors[0]["long_message"] is not None
    assert len(errors[0]["long_message"]) == 241
    assert errors[0]["long_message"].endswith("…")


def test_merge_partial_failure_details_keeps_active_newest() -> None:
    merged = merge_partial_failure_details(
        [
            {
                "mapped_category": "orders",
                "request_category": "orders",
                "http_status": 403,
                "errors": [],
            },
            {
                "mapped_category": "feedback_invalid_request",
                "request_category": "feedback",
                "http_status": 400,
                "errors": [{"error_id": 1}],
            },
        ],
        [
            {
                "mapped_category": "feedback_invalid_request",
                "request_category": "feedback",
                "http_status": 400,
                "errors": [{"error_id": 2}],
            }
        ],
        ["feedback_invalid_request"],
    )
    assert len(merged) == 1
    assert merged[0]["errors"][0]["error_id"] == 2


def test_feedback_http_400_maps_to_invalid_request() -> None:
    client = _client(
        [
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
    _stub_get_user(client)
    with pytest.raises(
        EbayPartialFailure, match="feedback_invalid_request"
    ) as exc_info:
        asyncio.run(client.async_fetch_feedback())
    assert exc_info.value.details is not None
    assert exc_info.value.details["http_status"] == 400
    assert exc_info.value.details["mapped_category"] == "feedback_invalid_request"
    assert exc_info.value.details["ebay_error_id"] == 35001
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "malformed_request"
    assert exc_info.value.failure.operation == "feedback.get"
    assert client._partial_failure_details[0]["mapped_category"] == (
        "feedback_invalid_request"
    )
    assert client._last_api_failure is not None
    assert client._last_api_failure.mapped_category == "feedback_invalid_request"


def test_feedback_user_lookup_failure_is_not_invalid_request() -> None:
    client = _client([])
    client.async_call_trading_api = AsyncMock(  # type: ignore[method-assign]
        side_effect=EbayApiError("eBay Trading API returned Ack='Failure'")
    )
    with pytest.raises(EbayPartialFailure, match="feedback_user_lookup") as exc_info:
        asyncio.run(client.async_fetch_feedback())
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.operation == "trading.get_user"
    assert exc_info.value.failure.mapped_category == "feedback_user_lookup"
    assert exc_info.value.details is not None
    assert exc_info.value.details["operation"] == "trading.get_user"
    assert client._partial_failure_details[0]["mapped_category"] == (
        "feedback_user_lookup"
    )


def test_feedback_user_id_is_cached_across_calls() -> None:
    capture: list[dict[str, Any]] = []
    client = _client(
        [
            _Response(json_payload={"feedbackRatingSummary": {}}),
            _Response(json_payload={"feedbackEntries": []}),
            _Response(json_payload={"total": 0}),
            _Response(json_payload={"feedbackRatingSummary": {}}),
            _Response(json_payload={"feedbackEntries": []}),
            _Response(json_payload={"total": 0}),
        ],
        capture=capture,
    )
    get_user = _stub_get_user(client, "cached_seller")
    asyncio.run(client.async_fetch_feedback())
    asyncio.run(client.async_fetch_feedback())
    assert get_user.await_count == 1
    assert client._feedback_user_id == "cached_seller"


def test_fetch_seller_standards_skips_metrics_for_unknown_site() -> None:
    """Unknown site IDs return profiles without INR/INAD marketplace calls."""
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


def test_fetch_account_privileges() -> None:
    capture: list[dict[str, Any]] = []
    client = _client(
        [
            _Response(
                json_payload={
                    "sellerRegistrationCompleted": True,
                    "sellingLimit": {
                        "amount": {"value": "500.00", "currency": "USD"},
                        "quantity": 50,
                    },
                }
            )
        ],
        capture=capture,
    )
    privileges = asyncio.run(client.async_fetch_account_privileges())
    assert privileges["seller_registration_completed"] is True
    assert privileges["amount_remaining"] == 500.0
    assert privileges["quantity_remaining"] == 50
    assert privileges["near_limit"] is False
    assert "/sell/account/v1/privilege" in capture[0]["args"][0]


def test_fetch_finances_summary_and_last_payout() -> None:
    capture: list[dict[str, Any]] = []
    client = _client(
        [
            _Response(
                json_payload={
                    "availableFunds": {"value": "200.00", "currency": "USD"},
                    "processingFunds": {"value": "25.00", "currency": "USD"},
                    "totalFunds": {"value": "225.00", "currency": "USD"},
                }
            ),
            _Response(
                json_payload={
                    "payouts": [
                        {
                            "payoutStatus": "SUCCEEDED",
                            "amount": {"value": "150.00", "currency": "USD"},
                            "payoutDate": "2026-07-10T00:00:00.000Z",
                        }
                    ]
                }
            ),
        ],
        capture=capture,
    )
    finances = asyncio.run(client.async_fetch_finances())
    assert finances["available_funds"] == 200.0
    assert finances["pending_funds"] == 25.0
    assert finances["last_payout_amount"] == 150.0
    assert finances["last_payout_status"] == "SUCCEEDED"
    assert "/sell/finances/v1/seller_funds_summary" in capture[0]["args"][0]
    assert "/sell/finances/v1/payout" in capture[1]["args"][0]
    assert capture[1]["kwargs"].get("params", {}).get("limit") == "1"


def test_fetch_finances_keeps_funds_when_payout_soft_fails() -> None:
    client = _client(
        [
            _Response(
                json_payload={
                    "availableFunds": {"value": "10.00", "currency": "USD"},
                }
            ),
            _Response(status=500, json_payload={"errors": [{"message": "boom"}]}),
        ]
    )
    finances = asyncio.run(client.async_fetch_finances())
    assert finances["available_funds"] == 10.0
    assert finances["last_payout_amount"] is None
