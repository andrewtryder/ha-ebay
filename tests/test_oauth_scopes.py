"""Tests for modular OAuth scope helpers and seller-ops fetch gating."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from custom_components.ebay.api import (
    CORE_SCOPE,
    DEFAULT_SCOPE,
    EbayApiClient,
    SCOPE_FEEDBACK,
    SCOPE_FULFILLMENT_READONLY,
    SCOPE_PAYMENT_DISPUTE,
    has_core_scope,
    join_scopes,
    missing_scopes_for_options,
    parse_scope_set,
    resolve_granted_scopes,
    scopes_for_options,
)
from custom_components.ebay.const import (
    CONF_ANALYTICS_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
)


def test_scopes_for_options_core_only_by_default() -> None:
    assert scopes_for_options({}) == CORE_SCOPE
    assert (
        scopes_for_options(
            {
                CONF_ANALYTICS_ENABLED: False,
                CONF_FULFILLMENT_ENABLED: False,
                CONF_FEEDBACK_ENABLED: False,
                CONF_MESSAGES_ENABLED: False,
                CONF_SELLER_STANDARDS_ENABLED: False,
            }
        )
        == CORE_SCOPE
    )


def test_scopes_for_options_unions_enabled_modules() -> None:
    scopes = parse_scope_set(
        scopes_for_options(
            {
                CONF_FULFILLMENT_ENABLED: True,
                CONF_FEEDBACK_ENABLED: True,
            }
        )
    )
    assert CORE_SCOPE in scopes
    assert SCOPE_FULFILLMENT_READONLY in scopes
    assert SCOPE_PAYMENT_DISPUTE in scopes
    assert SCOPE_FEEDBACK in scopes
    assert len(scopes) == 4


def test_scopes_for_options_dedupes_shared_analytics_scope() -> None:
    scopes = scopes_for_options(
        {
            CONF_ANALYTICS_ENABLED: True,
            CONF_SELLER_STANDARDS_ENABLED: True,
        }
    ).split()
    assert (
        scopes.count("https://api.ebay.com/oauth/api_scope/sell.analytics.readonly")
        == 1
    )


def test_missing_scopes_for_options() -> None:
    missing = missing_scopes_for_options(
        CORE_SCOPE,
        {CONF_FEEDBACK_ENABLED: True, CONF_FULFILLMENT_ENABLED: False},
    )
    assert list(missing) == [CONF_FEEDBACK_ENABLED]
    assert missing[CONF_FEEDBACK_ENABLED] == [SCOPE_FEEDBACK]
    assert (
        missing_scopes_for_options(DEFAULT_SCOPE, {CONF_FEEDBACK_ENABLED: True}) == {}
    )


def test_has_core_scope_and_resolve_granted_scopes() -> None:
    assert has_core_scope(CORE_SCOPE)
    assert has_core_scope(DEFAULT_SCOPE)
    assert not has_core_scope("https://api.ebay.com/oauth/api_scope/commerce.feedback")
    assert not has_core_scope(None)
    assert resolve_granted_scopes(CORE_SCOPE, {"scope": DEFAULT_SCOPE}) == join_scopes(
        parse_scope_set(DEFAULT_SCOPE)
    )
    assert resolve_granted_scopes(CORE_SCOPE, {}) == CORE_SCOPE


def test_seller_ops_fetch_without_selling_when_scopes_granted() -> None:
    """Seller-ops REST calls run even when active-listing selling is disabled."""

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
            self.async_fetch_orders = AsyncMock(
                return_value=({"awaiting_shipment": 1, "by_id": {}}, False)
            )
            self.async_fetch_payment_disputes = AsyncMock(
                return_value=({"open_count": 0}, False)
            )
            self.async_fetch_seller_standards = AsyncMock(return_value={})
            self.async_fetch_feedback = AsyncMock(return_value={})
            self.async_fetch_messages = AsyncMock(return_value=({}, False))

    client = Client()

    async def run() -> dict[str, Any]:
        return await client.async_fetch_data(
            buying_enabled=False,
            selling_enabled=False,
            analytics_enabled=False,
            fulfillment_enabled=True,
            ending_soon_threshold_seconds=3600,
            granted_scopes=join_scopes(
                (CORE_SCOPE, SCOPE_FULFILLMENT_READONLY, SCOPE_PAYMENT_DISPUTE)
            ),
        )

    payload = asyncio.run(run())
    assert client.async_fetch_orders.await_count == 1
    assert client.async_fetch_payment_disputes.await_count == 1
    assert payload["seller_ops"]["orders"]["awaiting_shipment"] == 1
    assert "orders_missing_scope" not in payload["partial_failures"]


def test_seller_ops_missing_scope_skips_fetch() -> None:
    """Enabled seller-ops without scopes soft-fail and skip API calls."""

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
            self.async_fetch_orders = AsyncMock()
            self.async_fetch_payment_disputes = AsyncMock()
            self.async_fetch_feedback = AsyncMock()

    client = Client()

    async def run() -> dict[str, Any]:
        return await client.async_fetch_data(
            buying_enabled=False,
            selling_enabled=False,
            analytics_enabled=False,
            fulfillment_enabled=True,
            feedback_enabled=True,
            ending_soon_threshold_seconds=3600,
            granted_scopes=CORE_SCOPE,
        )

    payload = asyncio.run(run())
    assert client.async_fetch_orders.await_count == 0
    assert client.async_fetch_feedback.await_count == 0
    assert "orders_missing_scope" in payload["partial_failures"]
    assert "feedback_missing_scope" in payload["partial_failures"]
    assert CONF_FEEDBACK_ENABLED in payload["missing_scopes"]
    assert CONF_FULFILLMENT_ENABLED in payload["missing_scope_features"]
