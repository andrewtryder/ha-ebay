"""Tests for per-section API fetch merge behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

from custom_components.ebay.api import EbayApiClient
from custom_components.ebay.const import (
    SECTION_FEEDBACK,
    SECTION_FULFILLMENT,
    SECTION_MESSAGES,
)
from custom_components.ebay.seller_ops import empty_seller_ops


def _client() -> EbayApiClient:
    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )

    return Client()


def test_section_merge_preserves_skipped_seller_ops() -> None:
    previous_ops = empty_seller_ops()
    previous_ops["feedback"] = {
        "score": 99,
        "positive_percent": 100.0,
        "recent_positive": 1,
        "recent_neutral": 0,
        "recent_negative": 0,
        "awaiting_count": 0,
    }
    previous = {
        "watched": {},
        "bidding": {},
        "selling": {},
        "seller_ops": previous_ops,
        "selling_classification": {},
        "summary": {"sold_items_count": None, "unsold_items_count": None},
        "partial_failures": ["messages"],
        "truncated_collections": {"messages": False},
        "api_warnings": [],
        "section_last_fetched": {
            SECTION_FEEDBACK: datetime.now(timezone.utc),
        },
    }
    client = _client()
    client.async_fetch_messages = AsyncMock(
        return_value=(
            {
                "unread_count": 2,
                "buyer_question_count": 1,
                "oldest_unanswered_hours": 3,
                "by_id": {},
            },
            False,
        )
    )

    async def run() -> dict[str, Any]:
        return await client.async_fetch_sections(
            {SECTION_MESSAGES},
            previous=previous,
            buying_enabled=False,
            selling_enabled=False,
            analytics_enabled=False,
            messages_enabled=True,
            ending_soon_threshold_seconds=3600,
            granted_scopes=(
                "https://api.ebay.com/oauth/api_scope "
                "https://api.ebay.com/oauth/api_scope/commerce.message"
            ),
        )

    payload = asyncio.run(run())
    assert payload["seller_ops"]["feedback"]["score"] == 99
    assert payload["seller_ops"]["messages"]["unread_count"] == 2
    assert "messages" not in payload["partial_failures"]
    assert SECTION_MESSAGES in payload["section_last_fetched"]
    assert payload["refreshed_sections"] == [SECTION_MESSAGES]


def test_fulfillment_only_refresh_updates_orders() -> None:
    previous = {
        "watched": {"w1": {"item_id": "w1"}},
        "bidding": {},
        "selling": {},
        "seller_ops": empty_seller_ops(),
        "selling_classification": {},
        "summary": {},
        "partial_failures": [],
        "truncated_collections": {},
        "api_warnings": [{"code": "1", "short_message": "warn"}],
        "section_last_fetched": {},
    }
    client = _client()
    client.async_fetch_orders = AsyncMock(
        return_value=({"awaiting_shipment": 4, "by_id": {"o1": {}}}, False)
    )
    client.async_fetch_payment_disputes = AsyncMock(
        return_value=({"open_count": 0, "by_id": {}}, False)
    )

    async def run() -> dict[str, Any]:
        return await client.async_fetch_sections(
            {SECTION_FULFILLMENT},
            previous=previous,
            buying_enabled=True,
            selling_enabled=False,
            analytics_enabled=False,
            fulfillment_enabled=True,
            ending_soon_threshold_seconds=3600,
            granted_scopes=(
                "https://api.ebay.com/oauth/api_scope "
                "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly "
                "https://api.ebay.com/oauth/api_scope/sell.payment.dispute"
            ),
        )

    payload = asyncio.run(run())
    assert payload["watched"] == previous["watched"]
    assert payload["seller_ops"]["orders"]["awaiting_shipment"] == 4
    assert payload["api_warnings"] == previous["api_warnings"]
    assert SECTION_FULFILLMENT in payload["section_last_fetched"]
