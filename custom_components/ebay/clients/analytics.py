"""Sell Analytics traffic-report client mixin."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import time
from typing import Any

import aiohttp

from ..parsers.trading_items import analytics_views_by_item_id
from ..parsers.xml import _duration_ms, _safe_exception_context, chunk_item_ids
from .errors import (
    EbayApiError,
    EbayAuthError,
    EbayError,
    EbayParseError,
    EbayPartialFailure,
)

_LOGGER = logging.getLogger(__name__)


class AnalyticsClientMixin:
    """Sell Analytics traffic report batching."""

    _session: aiohttp.ClientSession
    _endpoints: Any

    async def async_get_traffic_report(self, item_ids: list[str]) -> dict[str, Any]:
        """Fetch optional Sell Analytics traffic report for one ID batch."""
        if not item_ids:
            return {}
        for attempt in range(2):
            try:
                return await self._async_get_traffic_report_once(item_ids)
            except EbayAuthError:
                self._clear_access_token()
                if attempt == 0:
                    continue
                raise
        raise EbayAuthError("eBay Sell Analytics auth failed")

    async def _async_get_traffic_report_once(
        self, item_ids: list[str]
    ) -> dict[str, Any]:
        """Fetch one Sell Analytics traffic report without auth retry."""
        access_token = await self.async_get_access_token()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=30)
        listing_ids = "{" + "|".join(item_ids) + "}"
        request_start = time.monotonic()
        try:
            async with self._session.get(
                self._endpoints.analytics,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                params={
                    "dimension": "LISTING",
                    "filter": f"listing_ids:{listing_ids},date_range:[{start:%Y%m%d}..{now:%Y%m%d}]",
                    "metric": "LISTING_VIEWS_TOTAL",
                    "sort": "LISTING_VIEWS_TOTAL",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 401:
                    _LOGGER.debug(
                        "Sell Analytics call failed status=%s item_count=%s duration_ms=%.1f",
                        response.status,
                        len(item_ids),
                        _duration_ms(request_start),
                    )
                    raise EbayAuthError(
                        f"eBay Sell Analytics auth failed with HTTP {response.status}"
                    )
                if response.status in {400, 403}:
                    _LOGGER.debug(
                        "Sell Analytics call failed status=%s item_count=%s duration_ms=%.1f",
                        response.status,
                        len(item_ids),
                        _duration_ms(request_start),
                    )
                    raise EbayPartialFailure("analytics_views")
                if response.status >= 400:
                    _LOGGER.debug(
                        "Sell Analytics call failed status=%s item_count=%s duration_ms=%.1f",
                        response.status,
                        len(item_ids),
                        _duration_ms(request_start),
                    )
                    raise EbayApiError(
                        f"eBay Sell Analytics failed with HTTP {response.status}"
                    )
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                    _LOGGER.debug(
                        "Sell Analytics parse failed status=%s item_count=%s duration_ms=%.1f",
                        response.status,
                        len(item_ids),
                        _duration_ms(request_start),
                    )
                    raise EbayParseError(
                        "Could not parse eBay Sell Analytics JSON"
                    ) from exc
                _LOGGER.debug(
                    "Sell Analytics call completed status=%s item_count=%s duration_ms=%.1f",
                    response.status,
                    len(item_ids),
                    _duration_ms(request_start),
                )
                return payload
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise EbayPartialFailure("analytics_views") from exc

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
