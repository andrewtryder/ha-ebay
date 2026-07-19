"""Seller-ops parser and aggregate tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.ebay.seller_ops import (
    normalize_conversation,
    normalize_dispute,
    normalize_order,
    parse_feedback_items,
    parse_feedback_summary,
    parse_seller_standards_profiles,
    summarize_messages,
    summarize_orders,
)


def test_normalize_order_awaiting_and_overdue() -> None:
    now = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    order = normalize_order(
        {
            "orderId": "order-1",
            "orderPaymentStatus": "PAID",
            "orderFulfillmentStatus": "NOT_STARTED",
            "creationDate": "2026-07-10T12:00:00.000Z",
            "lineItems": [
                {
                    "legacyItemId": "111",
                    "title": "Widget",
                    "lineItemFulfillmentInstructions": {"shipByDate": yesterday},
                }
            ],
            "shippingFulfillments": [],
        },
        now=now,
    )
    assert order["awaiting_shipment"] is True
    assert order["overdue"] is True
    assert order["shipped"] is False
    assert order["item_id"] == "111"


def test_normalize_order_tracking_and_shipped() -> None:
    order = normalize_order(
        {
            "orderId": "order-2",
            "orderPaymentStatus": "PAID",
            "orderFulfillmentStatus": "FULFILLED",
            "lineItems": [{"legacyItemId": "222", "title": "Gadget"}],
            "shippingFulfillments": [{"shipmentTrackingNumber": "1Z999"}],
        }
    )
    assert order["shipped"] is True
    assert order["has_tracking"] is True
    summary = summarize_orders([order])
    assert summary["shipped"] == 1
    assert "order-2" in summary["by_id"]


def test_normalize_dispute_strips_buyer_username() -> None:
    dispute = normalize_dispute(
        {
            "paymentDisputeId": "d1",
            "orderId": "o1",
            "paymentDisputeStatus": "OPEN",
            "reason": "FRAUD",
            "buyerUsername": "secret-buyer",
            "openDate": "2026-07-01T00:00:00.000Z",
        }
    )
    assert dispute["dispute_id"] == "d1"
    assert "buyerUsername" not in dispute
    assert "secret-buyer" not in str(dispute)


def test_parse_seller_standards_at_risk() -> None:
    parsed = parse_seller_standards_profiles(
        {
            "standardsProfiles": [
                {
                    "standardsLevel": "BELOW_STANDARD",
                    "program": "PROGRAM_US",
                    "defaultProgram": True,
                    "cycle": {
                        "cycleType": "CURRENT",
                        "evaluationDate": "2026-08-01T00:00:00.000Z",
                    },
                    "metrics": [
                        {
                            "metricKey": "TRANSACTION_DEFECT_RATE",
                            "value": {"value": "2.5"},
                            "level": "BELOW_STANDARD",
                        },
                        {
                            "metricKey": "LATE_SHIPMENT_RATE",
                            "value": {"value": "1.1"},
                            "level": "ABOVE_STANDARD",
                        },
                    ],
                }
            ]
        }
    )
    assert parsed["seller_level"] == "BELOW_STANDARD"
    assert parsed["at_risk"] is True
    assert parsed["transaction_defect_rate"] == 2.5


def test_parse_feedback_summary_and_items() -> None:
    summary = parse_feedback_summary(
        {
            "sellerMetrics": {
                "feedbackScore": 120,
                "positiveFeedbackPercent": 99.5,
            }
        }
    )
    assert summary["score"] == 120
    assert summary["positive_percent"] == 99.5
    counts = parse_feedback_items(
        {
            "feedbackEntries": [
                {
                    "feedbackId": "fb-1",
                    "commentType": "POSITIVE",
                    "commentText": "secret",
                    "user": "buyer",
                },
                {"feedbackId": "fb-2", "commentType": "NEGATIVE"},
            ]
        }
    )
    assert counts == {
        "recent_positive": 1,
        "recent_neutral": 0,
        "recent_negative": 1,
        "ids": ["fb-1", "fb-2"],
        "negative_ids": ["fb-2"],
    }


def test_parse_feedback_summary_documented_shape() -> None:
    """Parse feedbackRatingSummary / ratingSummaryByRatingType / metrics."""
    summary = parse_feedback_summary(
        {
            "feedbackRatingSummary": {
                "ratingSummaryByRatingType": [
                    {
                        "ratingType": "OVERALL_EXPERIENCE",
                        "period": {"value": 30, "unit": "DAY"},
                        "feedbackMetrics": [
                            {"metricName": "FEEDBACK_SCORE", "value": "88"},
                            {
                                "metricName": "POSITIVE_FEEDBACK_PERCENTAGE",
                                "value": "98.5",
                            },
                        ],
                        "feedbackRatingValueDistribution": [
                            {"ratingValue": "POSITIVE", "count": 40},
                            {"ratingValue": "NEUTRAL", "count": 2},
                            {"ratingValue": "NEGATIVE", "count": 1},
                        ],
                    }
                ]
            }
        }
    )
    assert summary == {
        "score": 88,
        "positive_percent": 98.5,
        "recent_positive": 40,
        "recent_neutral": 2,
        "recent_negative": 1,
    }


def test_normalize_conversation_no_body() -> None:
    convo = normalize_conversation(
        {
            "conversationId": "c1",
            "conversationTitle": "Is this available?",
            "conversationStatus": "UNREAD",
            "reference": {"referenceId": "333", "referenceType": "LISTING"},
            "latestMessage": {
                "messageBody": "full secret body",
                "messageType": "QUESTION",
                "creationDate": "2026-07-11T10:00:00.000Z",
            },
            "otherPartyUsername": "buyer123",
        }
    )
    assert convo["unread"] is True
    assert convo["is_buyer_question"] is True
    assert convo["listing_id"] == "333"
    assert "full secret body" not in str(convo)
    assert "buyer123" not in str(convo)
    summary = summarize_messages(
        [convo], now=datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
    )
    assert summary["unread_count"] == 1
    assert summary["buyer_question_count"] == 1
    assert summary["oldest_unanswered_hours"] == 2.0


def test_parse_account_privileges_remaining_and_near_limit() -> None:
    from custom_components.ebay.seller_ops import parse_account_privileges

    parsed = parse_account_privileges(
        {
            "sellerRegistrationCompleted": True,
            "sellingLimit": {
                "amount": {"value": "50.00", "currency": "USD"},
                "quantity": 5,
            },
        }
    )
    assert parsed["seller_registration_completed"] is True
    assert parsed["amount_remaining"] == 50.0
    assert parsed["amount_currency"] == "USD"
    assert parsed["quantity_remaining"] == 5
    assert parsed["near_limit"] is True


def test_parse_account_privileges_defensive_empty() -> None:
    from custom_components.ebay.seller_ops import parse_account_privileges

    parsed = parse_account_privileges({})
    assert parsed["seller_registration_completed"] is None
    assert parsed["amount_remaining"] is None
    assert parsed["near_limit"] is None


def test_parse_finances_funds_and_last_payout() -> None:
    from custom_components.ebay.seller_ops import (
        parse_last_payout,
        parse_seller_funds_summary,
    )

    funds = parse_seller_funds_summary(
        {
            "availableFunds": {"value": "120.5", "currency": "USD"},
            "processingFunds": {"value": "10", "currency": "USD"},
            "totalFunds": {"value": "130.5", "currency": "USD"},
        }
    )
    assert funds["available_funds"] == 120.5
    assert funds["pending_funds"] == 10.0
    assert funds["funds_currency"] == "USD"

    payout = parse_last_payout(
        {
            "payouts": [
                {
                    "payoutStatus": "SUCCEEDED",
                    "amount": {"value": "99.99", "currency": "USD"},
                    "payoutDate": "2026-07-01T12:00:00.000Z",
                }
            ]
        }
    )
    assert payout["last_payout_amount"] == 99.99
    assert payout["last_payout_status"] == "SUCCEEDED"
    assert payout["last_payout_date"] is not None


def test_normalize_order_title_and_nested_tracking() -> None:
    long_title = "T" * 130
    order = normalize_order(
        {
            "orderId": "order-3",
            "orderPaymentStatus": "PAID",
            "orderFulfillmentStatus": "IN_PROGRESS",
            "lineItems": [{"listingId": "333", "title": long_title}],
            "shippingFulfillments": [
                {"lineItems": [{"shipmentTrackingNumber": "TRACK1"}]}
            ],
        }
    )
    assert order["title"].endswith("…")
    assert len(order["title"]) == 120
    assert order["has_tracking"] is True
    assert order["item_id"] == "333"


def test_parse_ebay_datetime_edge_cases() -> None:
    from custom_components.ebay.seller_ops import _parse_ebay_datetime

    assert _parse_ebay_datetime(None) is None
    assert _parse_ebay_datetime("") is None
    assert _parse_ebay_datetime("   ") is None
    assert _parse_ebay_datetime("not-a-date") is None
    naive = _parse_ebay_datetime("2026-07-01T12:00:00")
    assert naive is not None and naive.tzinfo is not None
    aware = _parse_ebay_datetime("2026-07-01T12:00:00+00:00")
    assert aware is not None


def test_parse_seller_standards_fallback_profiles() -> None:
    empty = parse_seller_standards_profiles({"standardsProfiles": []})
    assert empty == {}

    current_only = parse_seller_standards_profiles(
        {
            "standardsProfiles": [
                {
                    "standardsLevel": "TOP_RATED",
                    "cycle": {"cycleType": "CURRENT"},
                    "metrics": [
                        {"metricKey": "TRANSACTION_DEFECT_RATE", "value": "x"},
                        {
                            "metricKey": "LATE_SHIPMENT_RATE",
                            "value": {"value": "0.5"},
                            "level": "ABOVE_STANDARD",
                        },
                    ],
                }
            ]
        }
    )
    assert current_only["seller_level"] == "TOP_RATED"
    assert current_only["late_shipment_rate"] == 0.5

    first_profile = parse_seller_standards_profiles(
        {
            "standardsProfiles": [
                {
                    "standardsLevel": "ABOVE_STANDARD",
                    "cycle": {"cycleType": "PROJECTED"},
                    "metrics": [],
                }
            ]
        }
    )
    assert first_profile["seller_level"] == "ABOVE_STANDARD"


def test_parse_feedback_period_summaries() -> None:
    summary = parse_feedback_summary(
        {
            "summary": {
                "periodSummaries": [
                    {
                        "periodInDays": 30,
                        "counts": {
                            "positiveFeedbackCount": 4,
                            "neutralFeedbackCount": 1,
                            "negativeFeedbackCount": 0,
                        },
                    },
                    {"periodInDays": 365, "counts": {"positive": "bad"}},
                ]
            }
        }
    )
    assert summary["recent_positive"] == 4
    assert summary["recent_neutral"] == 1
    assert summary["recent_negative"] == 0
