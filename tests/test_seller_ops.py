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
                {"commentType": "POSITIVE", "commentText": "secret", "user": "buyer"},
                {"commentType": "NEGATIVE"},
            ]
        }
    )
    assert counts == {
        "recent_positive": 1,
        "recent_neutral": 0,
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
