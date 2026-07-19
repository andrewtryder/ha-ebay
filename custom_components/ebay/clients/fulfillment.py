"""Fulfillment (orders + payment disputes) client mixin."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..seller_ops import (
    DISPUTES_MAX_PAGES,
    DISPUTES_PAGE_LIMIT,
    ORDERS_LOOKBACK_DAYS,
    ORDERS_MAX_PAGES,
    ORDERS_PAGE_LIMIT,
    encode_creation_date_filter,
    encode_fulfillment_status_filter,
    normalize_dispute,
    normalize_order,
    summarize_disputes,
    summarize_orders,
)


class FulfillmentClientMixin:
    """Orders and payment-dispute fetchers."""

    _endpoints: Any
    site_id: str

    async def async_fetch_orders(
        self,
    ) -> tuple[dict[str, Any], bool]:
        """Fetch open and recent orders; return summary and truncation flag."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=ORDERS_LOOKBACK_DAYS)
        filters = [
            encode_fulfillment_status_filter(["NOT_STARTED", "IN_PROGRESS"]),
            encode_creation_date_filter(start, now),
        ]
        merged: dict[str, dict[str, Any]] = {}
        truncated = False
        for filter_value in filters:
            offset = 0
            page = 0
            while page < ORDERS_MAX_PAGES:
                page += 1
                payload = await self._async_rest_get(
                    f"{self._endpoints.fulfillment}/order",
                    params={
                        "filter": filter_value,
                        "limit": str(ORDERS_PAGE_LIMIT),
                        "offset": str(offset),
                    },
                    partial_category="orders",
                )
                orders = payload.get("orders") or []
                for raw in orders:
                    normalized = normalize_order(raw, now=now)
                    if normalized.get("order_id"):
                        merged[normalized["order_id"]] = normalized
                total = payload.get("total")
                offset += ORDERS_PAGE_LIMIT
                if not orders:
                    break
                if total is not None:
                    try:
                        if offset >= int(total):
                            break
                    except (TypeError, ValueError):
                        pass
                elif len(orders) < ORDERS_PAGE_LIMIT:
                    break
            else:
                truncated = True
        return summarize_orders(list(merged.values())), truncated

    async def async_fetch_payment_disputes(self) -> tuple[dict[str, Any], bool]:
        """Fetch open payment dispute summaries."""
        merged: dict[str, dict[str, Any]] = {}
        truncated = False
        offset = 0
        for page in range(DISPUTES_MAX_PAGES):
            payload = await self._async_rest_get(
                f"{self._endpoints.payment_disputes}/payment_dispute_summary",
                params=[
                    ("limit", str(DISPUTES_PAGE_LIMIT)),
                    ("offset", str(offset)),
                    ("payment_dispute_status", "OPEN"),
                    ("payment_dispute_status", "ACTION_NEEDED"),
                ],
                partial_category="payment_disputes",
            )
            summaries = payload.get("paymentDisputeSummaries") or []
            for raw in summaries:
                normalized = normalize_dispute(raw)
                if normalized.get("dispute_id"):
                    status = str(normalized.get("status") or "").upper()
                    if status in {"OPEN", "ACTION_NEEDED", "WAITING_SELLER"}:
                        merged[normalized["dispute_id"]] = normalized
            offset += DISPUTES_PAGE_LIMIT
            total = payload.get("total")
            if not summaries:
                break
            if total is not None:
                try:
                    if offset >= int(total):
                        break
                except (TypeError, ValueError):
                    pass
            elif len(summaries) < DISPUTES_PAGE_LIMIT:
                break
            if page == DISPUTES_MAX_PAGES - 1:
                truncated = True
        return summarize_disputes(list(merged.values())), truncated
