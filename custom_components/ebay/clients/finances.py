"""Sell Finances client mixin."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..seller_ops import (
    empty_seller_ops,
    finances_date_range_filter,
    merge_failed_payout_stats,
    parse_last_payout,
    parse_payout_status_page,
    parse_payout_summary,
    parse_seller_funds_summary,
    parse_transaction_summary,
    utc_day_bounds,
    utc_month_bounds,
    utc_rolling_days_bounds,
)
from .errors import EbayPartialFailure

_FAILED_PAYOUT_STATUS_FILTERS = (
    ("RETRYABLE_FAILED", "failed"),
    ("TERMINAL_FAILED", "failed"),
    ("REVERSED", "returned"),
)


class FinancesClientMixin:
    """Seller funds, payouts, and transaction-summary fetchers."""

    _endpoints: Any

    async def async_fetch_finances(self) -> dict[str, Any]:
        """Fetch seller funds, payout health, and period summaries.

        Some marketplaces require digital request signing for Finances; those
        are capability-gated before this method is called. Subcalls soft-fail
        so funds summary remains useful when payout/transaction endpoints fail.
        """
        finances = empty_seller_ops()["finances"]
        try:
            funds_payload = await self._async_rest_get(
                f"{self._endpoints.finances}/seller_funds_summary",
                partial_category="finances",
                allow_404=True,
            )
            finances.update(parse_seller_funds_summary(funds_payload))
        except EbayPartialFailure:
            raise

        try:
            payout_payload = await self._async_rest_get(
                f"{self._endpoints.finances}/payout",
                params={"limit": "1"},
                partial_category="finances",
                allow_404=True,
            )
            finances.update(parse_last_payout(payout_payload))
        except EbayPartialFailure:
            pass

        await self._async_fetch_failed_payouts(finances)
        await self._async_fetch_period_summaries(finances)
        return finances

    async def _async_fetch_failed_payouts(self, finances: dict[str, Any]) -> None:
        """Soft-fetch failed/returned payout counts and newest failure date."""
        parts: list[dict[str, Any]] = []
        for status, kind in _FAILED_PAYOUT_STATUS_FILTERS:
            try:
                payload = await self._async_rest_get(
                    f"{self._endpoints.finances}/payout",
                    params={
                        "limit": "20",
                        "filter": f"payoutStatus:{{{status}}}",
                    },
                    partial_category="finances",
                    allow_404=True,
                )
                parts.append(parse_payout_status_page(payload, status_kind=kind))
            except EbayPartialFailure:
                continue
        if parts:
            finances.update(merge_failed_payout_stats(*parts))

    async def _async_fetch_period_summaries(self, finances: dict[str, Any]) -> None:
        """Soft-fetch rolling/day/month transaction and payout summaries."""
        now = datetime.now(timezone.utc)
        windows = (
            ("recent", *utc_rolling_days_bounds(30, now)),
            ("daily", *utc_day_bounds(now)),
            ("monthly", *utc_month_bounds(now)),
        )
        for prefix, start, end in windows:
            tx_filter = "transactionStatus:{PAYOUT}," + finances_date_range_filter(
                "transactionDate", start, end
            )
            try:
                payload = await self._async_rest_get(
                    f"{self._endpoints.finances}/transaction_summary",
                    params={"filter": tx_filter},
                    partial_category="finances",
                    allow_404=True,
                )
                parsed = parse_transaction_summary(payload)
                finances[f"{prefix}_fees"] = parsed.get("fees")
                finances[f"{prefix}_refunds"] = parsed.get("refunds")
                finances[f"{prefix}_net"] = parsed.get("net")
                if finances.get("funds_currency") is None and parsed.get("currency"):
                    finances["funds_currency"] = parsed["currency"]
            except EbayPartialFailure:
                pass

            if prefix == "recent":
                continue
            payout_filter = finances_date_range_filter("payoutDate", start, end)
            try:
                payload = await self._async_rest_get(
                    f"{self._endpoints.finances}/payout_summary",
                    params={"filter": payout_filter},
                    partial_category="finances",
                    allow_404=True,
                )
                parsed = parse_payout_summary(payload)
                finances[f"{prefix}_payout_total"] = parsed.get("payout_total")
                if finances.get("funds_currency") is None and parsed.get("currency"):
                    finances["funds_currency"] = parsed["currency"]
            except EbayPartialFailure:
                pass
