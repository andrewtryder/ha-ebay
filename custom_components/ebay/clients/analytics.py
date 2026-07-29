"""Sell Analytics traffic-report client mixin."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..parsers.trading_items import analytics_views_by_item_id
from ..parsers.xml import _safe_exception_context, chunk_item_ids
from .errors import EbayAuthError, EbayError

_LOGGER = logging.getLogger(__name__)

_ANALYTICS_TZ = ZoneInfo("America/Los_Angeles")


def analytics_date_range_filter(*, now: datetime | None = None, days: int = 30) -> str:
    """Build a Pacific calendar date_range filter for traffic_report."""
    end = (now or datetime.now(_ANALYTICS_TZ)).astimezone(_ANALYTICS_TZ)
    start = end - timedelta(days=days)
    return f"date_range:[{start:%Y%m%d}..{end:%Y%m%d}]"


class AnalyticsClientMixin:
    """Sell Analytics traffic report batching."""

    _session: aiohttp.ClientSession
    _endpoints: Any

    async def async_get_traffic_report(self, item_ids: list[str]) -> dict[str, Any]:
        """Fetch optional Sell Analytics traffic report for one ID batch."""
        if not item_ids:
            return {}
        listing_ids = "{" + "|".join(item_ids) + "}"
        return await self._async_rest_get(
            self._endpoints.analytics,
            params={
                "dimension": "LISTING",
                "filter": f"listing_ids:{listing_ids},{analytics_date_range_filter()}",
                "metric": "LISTING_VIEWS_TOTAL",
                "sort": "LISTING_VIEWS_TOTAL",
            },
            partial_category="analytics_views",
            operation="analytics.traffic_report",
        )

    async def async_fetch_analytics_views(
        self, item_ids: list[str]
    ) -> tuple[dict[str, int], bool]:
        """Fetch 30-day Analytics views in batches; merge successes on partial failure."""
        if not item_ids:
            return {}, False
        merged: dict[str, int] = {}
        partial_failure = False
        batches = chunk_item_ids(item_ids)
        for batch_number, batch in enumerate(batches, start=1):
            _LOGGER.debug(
                "Fetching Sell Analytics batch batch=%s item_count=%s",
                batch_number,
                len(batch),
            )
            try:
                payload = await self.async_get_traffic_report(batch)
            except EbayAuthError:
                raise
            except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug(
                    "Sell Analytics batch failed batch=%s item_count=%s error=%s",
                    batch_number,
                    len(batch),
                    _safe_exception_context(exc),
                )
                partial_failure = True
                continue
            batch_views = analytics_views_by_item_id(payload)
            merged.update(batch_views)
            _LOGGER.debug(
                "Sell Analytics batch completed batch=%s item_count=%s view_count=%s",
                batch_number,
                len(batch),
                len(batch_views),
            )
        return merged, partial_failure
