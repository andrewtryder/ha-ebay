"""Sell Finances client mixin."""

from __future__ import annotations

from typing import Any

from ..seller_ops import empty_seller_ops, parse_last_payout, parse_seller_funds_summary
from .errors import EbayPartialFailure


class FinancesClientMixin:
    """Seller funds summary and last-payout fetchers."""

    _endpoints: Any

    async def async_fetch_finances(self) -> dict[str, Any]:
        """Fetch seller funds summary plus a stub last-payout record.

        Some marketplaces require digital request signing for Finances; those
        are capability-gated before this method is called.
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
            # Funds summary alone is still useful when payout listing fails.
            pass
        return finances
