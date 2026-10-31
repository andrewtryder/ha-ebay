"""Trading API client mixin (call + buying/selling fetchers)."""

from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp

from ..const import COMPATIBILITY_LEVEL
from ..parsers.payload import _dict_by_item_id
from ..parsers.trading_builders import (
    READ_ONLY_CALL_NAME,
    SELLER_LIST_CALL_NAME,
    SELLING_CALL_NAME,
    build_get_my_ebay_buying_xml,
    build_get_my_ebay_selling_list_xml,
    build_get_my_ebay_selling_xml,
    build_get_seller_list_xml,
    parse_selling_list_item_ids,
)
from ..parsers.trading_items import (
    parse_container,
    parse_selling_container,
    seller_list_views_by_item_id,
)
from ..parsers.xml import (
    MAX_PAGES,
    _auth_like_error,
    _duration_ms,
    _page_number_from_xml,
    _rate_limit_error,
    _reported_total_pages,
    _text,
    extract_trading_warnings,
)
from .errors import EbayApiError, EbayAuthError, EbayParseError
from .resilience import (
    BoundedRetryPolicy,
    EndpointFamily,
    RateLimitTracker,
    parse_retry_after,
)

_LOGGER = logging.getLogger(__name__)


class TradingClientMixin:
    """Trading API calls and buying/selling page fetchers."""

    _session: aiohttp.ClientSession
    site_id: str
    _endpoints: Any
    _api_warnings: list[dict[str, str | None]]

    @property
    def rate_limit_tracker(self) -> RateLimitTracker:
        """Return the shared or instance RateLimitTracker."""
        if not hasattr(self, "_rate_limit_tracker") or self._rate_limit_tracker is None:
            self._rate_limit_tracker = RateLimitTracker()
        return self._rate_limit_tracker

    async def async_call_trading_api(self, call_name: str, xml_body: str) -> ET.Element:
        """Call a read-only Trading API operation with bounded retries and token refresh."""
        policy = getattr(self, "retry_policy", None) or BoundedRetryPolicy()
        max_attempts = policy.max_retries + 1
        deadline = time.monotonic() + policy.total_timeout
        auth_refreshed = False
        self._last_retry_count = 0

        for attempt in range(max_attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise EbayApiError(
                    f"eBay Trading API request budget exhausted for {call_name}",
                    status=504,
                )
            if attempt > 0:
                self._last_retry_count = getattr(self, "_last_retry_count", 0) + 1

            try:
                return await self._async_call_trading_api_once(
                    call_name, xml_body, deadline=deadline
                )
            except EbayAuthError as exc:
                if not auth_refreshed and (attempt + 1 < max_attempts):
                    auth_refreshed = True
                    rejected_token = getattr(exc, "failed_token", None) or getattr(
                        self, "_access_token", None
                    )
                    await self.async_refresh_access_token(failed_token=rejected_token)
                    if time.monotonic() < deadline:
                        continue
                raise
            except EbayApiError as exc:
                if (
                    exc.status in {500, 502, 503, 504}
                    and (attempt + 1 < max_attempts)
                    and (time.monotonic() < deadline)
                ):
                    delay = policy.compute_backoff(attempt)
                    if time.monotonic() + delay < deadline:
                        _LOGGER.debug(
                            "Trading API transient 5xx call=%s status=%d attempt=%d retrying in %.2fs",
                            call_name,
                            exc.status,
                            attempt + 1,
                            delay,
                        )
                        await policy.async_wait(delay)
                        continue
                raise
            except (asyncio.CancelledError, EbayParseError):
                raise
            except Exception as exc:
                if policy.is_transient_exception(exc):
                    if (attempt + 1 < max_attempts) and (time.monotonic() < deadline):
                        delay = policy.compute_backoff(attempt)
                        if time.monotonic() + delay < deadline:
                            _LOGGER.debug(
                                "Trading API transient transport error call=%s exc=%s attempt=%d retrying in %.2fs",
                                call_name,
                                type(exc).__name__,
                                attempt + 1,
                                delay,
                            )
                            await policy.async_wait(delay)
                            continue
                raise EbayApiError("eBay Trading API request failed") from exc
        raise EbayApiError(
            f"eBay Trading API request budget exhausted for {call_name}",
            status=504,
        )

    async def _async_call_trading_api_once(
        self, call_name: str, xml_body: str, deadline: float | None = None
    ) -> ET.Element:
        """Call a Trading API operation once."""
        client_id = getattr(self, "client_id", None)
        tracker = self.rate_limit_tracker
        cooldown = tracker.cooldown_remaining(EndpointFamily.TRADING, client_id)
        if cooldown > 2.0:
            raise EbayApiError(
                f"eBay Trading API in cooldown for {call_name} (cooldown_remaining={cooldown:.1f}s)",
                status=429,
            )
        elif cooldown > 0.0:
            await asyncio.sleep(cooldown)

        page = _page_number_from_xml(xml_body)
        start = time.monotonic()
        remaining_budget = (
            (deadline - time.monotonic()) if deadline is not None else 30.0
        )
        if remaining_budget <= 0.0:
            raise EbayApiError(
                f"eBay Trading API request timed out for {call_name}",
                status=504,
            )

        attempt_timeout = aiohttp.ClientTimeout(
            total=max(0.1, min(30.0, remaining_budget))
        )
        access_token = await self.async_get_access_token()
        headers = {
            "Content-Type": "text/xml;charset=UTF-8",
            "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": self.site_id,
            "X-EBAY-API-IAF-TOKEN": access_token,
        }

        try:
            async with self._session.post(
                self._endpoints.trading,
                headers=headers,
                data=xml_body.encode("utf-8"),
                timeout=attempt_timeout,
            ) as response:
                body = await response.text()
                if response.status == 401:
                    _LOGGER.debug(
                        "Trading API call failed call=%s page=%s status=401 duration_ms=%.1f",
                        call_name,
                        page,
                        _duration_ms(start),
                    )
                    raise EbayAuthError(
                        "eBay Trading API auth failed with HTTP 401",
                        failed_token=access_token,
                    )
                if response.status == 403:
                    _LOGGER.debug(
                        "Trading API call forbidden call=%s page=%s status=403 duration_ms=%.1f",
                        call_name,
                        page,
                        _duration_ms(start),
                    )
                    raise EbayApiError(
                        "eBay Trading API forbidden with HTTP 403",
                        status=403,
                    )
                if response.status == 429:
                    retry_after_hdr = response.headers.get("Retry-After")
                    delay = parse_retry_after(retry_after_hdr, default_delay=60.0)
                    tracker.record_throttled(EndpointFamily.TRADING, delay, client_id)
                    _LOGGER.debug(
                        "Trading API call rate-limited call=%s status=429 delay=%.1fs duration_ms=%.1f",
                        call_name,
                        delay,
                        _duration_ms(start),
                    )
                    raise EbayApiError(
                        f"eBay Trading API failed with HTTP {response.status}",
                        status=429,
                    )
                if response.status >= 400:
                    _LOGGER.debug(
                        "Trading API call failed call=%s page=%s status=%s duration_ms=%.1f",
                        call_name,
                        page,
                        response.status,
                        _duration_ms(start),
                    )
                    raise EbayApiError(
                        f"eBay Trading API failed with HTTP {response.status}",
                        status=response.status,
                    )
        except (EbayAuthError, EbayApiError, asyncio.CancelledError):
            raise
        except Exception as exc:
            policy = getattr(self, "retry_policy", None) or BoundedRetryPolicy()
            if policy.is_transient_exception(exc):
                raise
            raise EbayApiError("eBay Trading API request failed") from exc

        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            _LOGGER.debug(
                "Trading API parse failed call=%s page=%s duration_ms=%.1f",
                call_name,
                page,
                _duration_ms(start),
            )
            raise EbayParseError("Could not parse eBay Trading API XML") from exc
        ack = _text(root, "e:Ack")
        if ack not in {"Success", "Warning"}:
            _LOGGER.debug(
                "Trading API call failed call=%s page=%s ack=%s duration_ms=%.1f",
                call_name,
                page,
                ack,
                _duration_ms(start),
            )
            if _rate_limit_error(root):
                tracker.record_throttled(EndpointFamily.TRADING, 60.0, client_id)
                raise EbayApiError(
                    f"eBay Trading API returned Ack={ack!r} (rate limited)",
                    status=429,
                )
            if _auth_like_error(root):
                raise EbayAuthError(
                    f"eBay Trading API returned Ack={ack!r}",
                    failed_token=access_token,
                )
            raise EbayApiError(f"eBay Trading API returned Ack={ack!r}")
        if ack == "Warning":
            self._api_warnings.extend(extract_trading_warnings(root))
        _LOGGER.debug(
            "Trading API call completed call=%s page=%s status=%s ack=%s duration_ms=%.1f",
            call_name,
            page,
            response.status,
            ack,
            _duration_ms(start),
        )
        return root

    async def _fetch_buying_pages(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], bool, bool]:
        """Fetch all configured buying pages up to MAX_PAGES."""
        watched_items: list[dict[str, Any]] = []
        bidding_items: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        watched_truncated = False
        bidding_truncated = False
        while page <= total_pages:
            root = await self.async_call_trading_api(
                READ_ONLY_CALL_NAME, build_get_my_ebay_buying_xml(page=page)
            )
            watched_page = parse_container(root, "WatchList", "watched")
            bidding_page = parse_container(root, "BidList", "bidding")
            watched_items.extend(watched_page)
            bidding_items.extend(bidding_page)
            watch_pages = _reported_total_pages(root, "WatchList")
            bid_pages = _reported_total_pages(root, "BidList")
            watched_truncated = watched_truncated or watch_pages > MAX_PAGES
            bidding_truncated = bidding_truncated or bid_pages > MAX_PAGES
            total_pages = max(
                total_pages,
                min(watch_pages, MAX_PAGES),
                min(bid_pages, MAX_PAGES),
            )
            _LOGGER.debug(
                "Fetched buying page page=%s watched_count=%s bidding_count=%s total_pages=%s watched_truncated=%s bidding_truncated=%s",
                page,
                len(watched_page),
                len(bidding_page),
                total_pages,
                watched_truncated,
                bidding_truncated,
            )
            page += 1
        return (
            _dict_by_item_id(watched_items),
            _dict_by_item_id(bidding_items),
            watched_truncated,
            bidding_truncated,
        )

    async def _fetch_selling_pages(self) -> tuple[dict[str, dict[str, Any]], bool]:
        """Fetch active selling pages up to MAX_PAGES."""
        selling_items: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        truncated = False
        while page <= total_pages:
            root = await self.async_call_trading_api(
                SELLING_CALL_NAME, build_get_my_ebay_selling_xml(page=page)
            )
            selling_page = parse_selling_container(root)
            selling_items.extend(selling_page)
            active_pages = _reported_total_pages(root, "ActiveList")
            truncated = truncated or active_pages > MAX_PAGES
            total_pages = max(total_pages, min(active_pages, MAX_PAGES))
            _LOGGER.debug(
                "Fetched selling page page=%s selling_count=%s total_pages=%s truncated=%s",
                page,
                len(selling_page),
                total_pages,
                truncated,
            )
            page += 1
        return _dict_by_item_id(selling_items), truncated

    async def _fetch_selling_list_pages(self, list_name: str) -> tuple[list[str], bool]:
        """Fetch one SoldList or UnsoldList across pages up to MAX_PAGES."""
        item_ids: list[str] = []
        page = 1
        total_pages = 1
        truncated = False
        while page <= total_pages:
            root = await self.async_call_trading_api(
                SELLING_CALL_NAME,
                build_get_my_ebay_selling_list_xml(list_name, page=page),
            )
            page_ids = parse_selling_list_item_ids(root, list_name)
            item_ids.extend(page_ids)
            reported_pages = _reported_total_pages(root, list_name)
            truncated = truncated or reported_pages > MAX_PAGES
            total_pages = max(total_pages, min(reported_pages, MAX_PAGES))
            _LOGGER.debug(
                "Fetched %s page page=%s item_count=%s total_pages=%s truncated=%s",
                list_name,
                page,
                len(page_ids),
                total_pages,
                truncated,
            )
            page += 1
        return item_ids, truncated

    async def async_fetch_sold_unsold_classification(
        self,
    ) -> tuple[dict[str, str], int, int, bool]:
        """Fetch SoldList/UnsoldList classification and counts.

        Returns (classification, sold_count, unsold_count, truncated).
        """
        unsold_ids, unsold_truncated = await self._fetch_selling_list_pages(
            "UnsoldList"
        )
        sold_ids, sold_truncated = await self._fetch_selling_list_pages("SoldList")
        classified: dict[str, str] = {item_id: "unsold" for item_id in unsold_ids}
        for item_id in sold_ids:
            classified[item_id] = "sold"
        return (
            classified,
            len(set(sold_ids)),
            len(set(unsold_ids)),
            sold_truncated or unsold_truncated,
        )

    async def _fetch_seller_list_views(self) -> tuple[dict[str, int], bool]:
        """Fetch GetSellerList view counts across pages up to MAX_PAGES."""
        views: dict[str, int] = {}
        page = 1
        total_pages = 1
        truncated = False
        while page <= total_pages:
            root = await self.async_call_trading_api(
                SELLER_LIST_CALL_NAME, build_get_seller_list_xml(page=page)
            )
            page_views = seller_list_views_by_item_id(root)
            views.update(page_views)
            seller_list_pages = _reported_total_pages(root)
            truncated = truncated or seller_list_pages > MAX_PAGES
            total_pages = max(total_pages, min(seller_list_pages, MAX_PAGES))
            _LOGGER.debug(
                "Fetched seller-list views page=%s view_count=%s total_pages=%s truncated=%s",
                page,
                len(page_views),
                total_pages,
                truncated,
            )
            page += 1
        return views, truncated
