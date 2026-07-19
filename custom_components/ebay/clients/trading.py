"""Trading API client mixin (call + buying/selling fetchers)."""

from __future__ import annotations

import logging
import time
from typing import Any
import xml.etree.ElementTree as ET

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
    _reported_total_pages,
    _text,
    extract_trading_warnings,
)
from .errors import EbayApiError, EbayAuthError, EbayParseError

_LOGGER = logging.getLogger(__name__)


class TradingClientMixin:
    """Trading API calls and buying/selling page fetchers."""

    _session: aiohttp.ClientSession
    site_id: str
    _endpoints: Any
    _api_warnings: list[dict[str, str | None]]

    async def async_call_trading_api(self, call_name: str, xml_body: str) -> ET.Element:
        """Call a read-only Trading API operation."""
        for attempt in range(2):
            try:
                return await self._async_call_trading_api_once(call_name, xml_body)
            except EbayAuthError:
                self._clear_access_token()
                if attempt == 0:
                    continue
                raise
        raise EbayAuthError("eBay Trading API auth failed")

    async def _async_call_trading_api_once(
        self, call_name: str, xml_body: str
    ) -> ET.Element:
        """Call a Trading API operation once."""
        access_token = await self.async_get_access_token()
        page = _page_number_from_xml(xml_body)
        headers = {
            "Content-Type": "text/xml;charset=UTF-8",
            "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": self.site_id,
            "X-EBAY-API-IAF-TOKEN": access_token,
        }
        start = time.monotonic()
        try:
            async with self._session.post(
                self._endpoints.trading,
                headers=headers,
                data=xml_body.encode("utf-8"),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                body = await response.text()
                if response.status in {401, 403}:
                    _LOGGER.debug(
                        "Trading API call failed call=%s page=%s status=%s duration_ms=%.1f",
                        call_name,
                        page,
                        response.status,
                        _duration_ms(start),
                    )
                    raise EbayAuthError(
                        f"eBay Trading API auth failed with HTTP {response.status}"
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
                        f"eBay Trading API failed with HTTP {response.status}"
                    )
        except (aiohttp.ClientError, TimeoutError) as exc:
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
            if _auth_like_error(root):
                raise EbayAuthError(f"eBay Trading API returned Ack={ack!r}")
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
