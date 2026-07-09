"""Read-only eBay API client and parsers."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import xml.etree.ElementTree as ET

import aiohttp

from .const import COMPATIBILITY_LEVEL, EBAY_XML_NS, ENV_SANDBOX, NS

_LOGGER = logging.getLogger(__name__)

READ_ONLY_CALL_NAME = "GetMyeBayBuying"
SELLING_CALL_NAME = "GetMyeBaySelling"
SELLER_LIST_CALL_NAME = "GetSellerList"
BEST_OFFERS_CALL_NAME = "GetBestOffers"

DEFAULT_SCOPE = (
    "https://api.ebay.com/oauth/api_scope "
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"
)


class EbayError(Exception):
    """Base eBay integration error."""


class EbayAuthError(EbayError):
    """Authentication or authorization failed."""


class EbayApiError(EbayError):
    """eBay API returned an error."""


class EbayPartialFailure(EbayError):
    """Optional eBay API section failed."""


class EbayParseError(EbayError):
    """eBay response could not be parsed."""


@dataclass(slots=True)
class EbayEndpoints:
    """Endpoint set for an eBay environment."""

    trading: str
    token: str
    authorize: str
    analytics: str


def endpoints_for(environment: str) -> EbayEndpoints:
    """Return endpoint URLs for production or sandbox."""
    if environment == ENV_SANDBOX:
        return EbayEndpoints(
            trading="https://api.sandbox.ebay.com/ws/api.dll",
            token="https://api.sandbox.ebay.com/identity/v1/oauth2/token",
            authorize="https://auth.sandbox.ebay.com/oauth2/authorize",
            analytics="https://api.sandbox.ebay.com/sell/analytics/v1/traffic_report",
        )
    return EbayEndpoints(
        trading="https://api.ebay.com/ws/api.dll",
        token="https://api.ebay.com/identity/v1/oauth2/token",
        authorize="https://auth.ebay.com/oauth2/authorize",
        analytics="https://api.ebay.com/sell/analytics/v1/traffic_report",
    )


def build_consent_url(
    environment: str,
    client_id: str,
    runame: str,
    state: str,
    scope: str = DEFAULT_SCOPE,
) -> str:
    """Build an eBay OAuth consent URL."""
    endpoints = endpoints_for(environment)
    return (
        f"{endpoints.authorize}?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": runame,
                "response_type": "code",
                "scope": scope,
                "state": state,
            }
        )
    )


def extract_authorization_code(value: str) -> str:
    """Extract an OAuth code from a pasted code or final callback URL."""
    value = value.strip()
    parsed = urlparse(value)
    if parsed.query:
        query = parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        if code:
            return code
    return value


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {basic}"


def _text(parent: ET.Element | None, path: str) -> str | None:
    if parent is None:
        return None
    value = parent.findtext(path, namespaces=NS)
    return value.strip() if value else None


def _first_text(parent: ET.Element | None, paths: list[str]) -> str | None:
    for path in paths:
        value = _text(parent, path)
        if value:
            return value
    return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    lowered = str(value).strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def _money(item: ET.Element, path: str) -> tuple[float | None, str | None]:
    node = item.find(path, namespaces=NS)
    if node is None or node.text is None:
        return None, None
    return _to_float(node.text), node.attrib.get("currencyID")


def ebay_time(value: datetime) -> str:
    """Format a datetime for eBay Trading API XML."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_ebay_time(value: str | None) -> datetime | None:
    """Parse an eBay timestamp."""
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def seconds_until(value: datetime | str | None) -> int | None:
    """Return seconds until a datetime, never below zero."""
    end_time = parse_ebay_time(value) if isinstance(value, str) else value
    if not end_time:
        return None
    return max(0, int((end_time - datetime.now(timezone.utc)).total_seconds()))


def format_seconds(seconds: int | None) -> str | None:
    """Return a human readable duration."""
    if seconds is None:
        return None
    if seconds <= 0:
        return "ended"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _base_item(item: ET.Element, kind: str) -> dict[str, Any]:
    end_time_text = _first_text(item, ["e:ListingDetails/e:EndTime", "e:EndTime"])
    end_time = parse_ebay_time(end_time_text)
    seconds_left = seconds_until(end_time)
    price, currency = _money(item, "e:SellingStatus/e:CurrentPrice")
    if price is None:
        price, currency = _money(item, "e:StartPrice")

    return {
        "kind": kind,
        "item_id": _text(item, "e:ItemID"),
        "title": _text(item, "e:Title"),
        "listing_type": _text(item, "e:ListingType"),
        "end_time": end_time,
        "seconds_left": seconds_left,
        "time_left": format_seconds(seconds_left),
        "current_price": price,
        "currency": currency,
        "bid_count": _to_int(_text(item, "e:SellingStatus/e:BidCount")),
        "watchers": None,
        "views": None,
        "offers": None,
        "questions": None,
        "quantity": None,
        "quantity_available": None,
        "quantity_sold": None,
        "max_bid": None,
        "winning": None,
        "url": _first_text(
            item,
            [
                "e:ListingDetails/e:ViewItemURL",
                "e:ListingDetails/e:ViewItemURLForNaturalSearch",
                "e:ViewItemURL",
            ],
        ),
        "image": _first_text(item, ["e:PictureDetails/e:GalleryURL", "e:GalleryURL"]),
    }


def parse_buying_item(item: ET.Element, kind: str) -> dict[str, Any]:
    """Parse a WatchList or BidList item."""
    data = _base_item(item, kind)
    if kind == "bidding":
        max_bid, _ = _money(item, "e:BiddingDetails/e:MaxBid")
        data["max_bid"] = max_bid
        data["winning"] = _to_bool(_text(item, "e:BiddingDetails/e:Winning"))
    return data


def parse_selling_item(item: ET.Element) -> dict[str, Any]:
    """Parse an active selling item."""
    data = _base_item(item, "selling")
    data.update(
        {
            "quantity": _to_int(_text(item, "e:Quantity")),
            "quantity_available": _to_int(_text(item, "e:QuantityAvailable")),
            "quantity_sold": _to_int(_text(item, "e:SellingStatus/e:QuantitySold")),
            "views": _to_int(_text(item, "e:HitCount")),
            "watchers": _to_int(_text(item, "e:WatchCount")),
            "questions": _to_int(_text(item, "e:QuestionCount")),
            "offers": _to_int(_text(item, "e:BestOfferDetails/e:BestOfferCount")),
        }
    )
    return data


def parse_container(root: ET.Element, container_name: str, kind: str) -> list[dict[str, Any]]:
    """Parse a buying response container."""
    container = root.find(f"e:{container_name}", namespaces=NS)
    if container is None:
        return []
    return [
        parse_buying_item(item, kind)
        for item in container.findall("e:ItemArray/e:Item", namespaces=NS)
        if _text(item, "e:ItemID")
    ]


def parse_selling_container(root: ET.Element) -> list[dict[str, Any]]:
    """Parse the active selling response container."""
    container = root.find("e:ActiveList", namespaces=NS)
    if container is None:
        return []
    return [
        parse_selling_item(item)
        for item in container.findall("e:ItemArray/e:Item", namespaces=NS)
        if _text(item, "e:ItemID")
    ]


def seller_list_views_by_item_id(root: ET.Element) -> dict[str, int]:
    """Parse GetSellerList hit counts."""
    views: dict[str, int] = {}
    for item in root.findall("e:ItemArray/e:Item", namespaces=NS):
        item_id = _text(item, "e:ItemID")
        hit_count = _to_int(_text(item, "e:HitCount"))
        if item_id and hit_count is not None:
            views[item_id] = hit_count
    return views


def analytics_views_by_item_id(payload: dict[str, Any]) -> dict[str, int]:
    """Parse Sell Analytics traffic report views."""
    views: dict[str, int] = {}
    for record in payload.get("records", []):
        dimension_values = record.get("dimensionValues") or []
        metric_values = record.get("metricValues") or []
        if not dimension_values or not metric_values:
            continue
        item_id = str(dimension_values[0].get("value") or "")
        value = _to_int(metric_values[0].get("value"))
        applicable = metric_values[0].get("applicable", True)
        if item_id and applicable and value is not None:
            views[item_id] = value
    return views


def active_offers_by_item_id(root: ET.Element) -> dict[str, int]:
    """Parse active best offers by item id."""
    offers: dict[str, int] = {}
    for item_offers in root.findall("e:ItemBestOffersArray/e:ItemBestOffers", namespaces=NS):
        role = _text(item_offers, "e:Role")
        if role and role != "Seller":
            continue
        item_id = _text(item_offers, "e:Item/e:ItemID")
        if not item_id:
            continue
        count = 0
        for offer in item_offers.findall("e:BestOfferArray/e:BestOffer", namespaces=NS):
            status = _text(offer, "e:Status")
            if status in {None, "Active", "Countered", "Pending"}:
                count += 1
        offers[item_id] = offers.get(item_id, 0) + count
    return offers


def build_get_my_ebay_buying_xml(entries_per_page: int = 100, page: int = 1) -> str:
    """Build GetMyeBayBuying XML."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingRequest xmlns="{EBAY_XML_NS}">
  <Version>{COMPATIBILITY_LEVEL}</Version>
  <WatchList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
    <Sort>EndTime</Sort>
  </WatchList>
  <BidList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
    <Sort>EndTime</Sort>
  </BidList>
</GetMyeBayBuyingRequest>
"""


def build_get_my_ebay_selling_xml(entries_per_page: int = 100, page: int = 1) -> str:
    """Build GetMyeBaySelling XML."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <ActiveList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
    <Sort>EndTime</Sort>
  </ActiveList>
  <ScheduledList><Include>false</Include></ScheduledList>
  <SoldList><Include>false</Include></SoldList>
  <UnsoldList><Include>false</Include></UnsoldList>
  <SellingSummary><Include>true</Include></SellingSummary>
</GetMyeBaySellingRequest>
"""


def build_get_my_ebay_selling_classification_xml(
    entries_per_page: int = 100, page: int = 1
) -> str:
    """Build a read-only sold/unsold lookup request for future classification."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <ActiveList><Include>false</Include></ActiveList>
  <ScheduledList><Include>false</Include></ScheduledList>
  <SoldList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </SoldList>
  <UnsoldList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </UnsoldList>
</GetMyeBaySellingRequest>
"""


def parse_sold_unsold_classification(root: ET.Element) -> dict[str, str]:
    """Parse explicit sold/unsold item classification when that lookup is enabled."""
    classified: dict[str, str] = {}
    for container_name, status in (("SoldList", "sold"), ("UnsoldList", "unsold")):
        container = root.find(f"e:{container_name}", namespaces=NS)
        if container is None:
            continue
        for item in container.findall("e:ItemArray/e:Item", namespaces=NS):
            item_id = _text(item, "e:ItemID")
            if item_id:
                classified[item_id] = status
    return classified


def build_get_seller_list_xml() -> str:
    """Build GetSellerList XML for active listings."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=120)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetSellerListRequest xmlns="{EBAY_XML_NS}">
  <GranularityLevel>Fine</GranularityLevel>
  <EndTimeFrom>{ebay_time(now)}</EndTimeFrom>
  <EndTimeTo>{ebay_time(end)}</EndTimeTo>
  <Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>1</PageNumber></Pagination>
</GetSellerListRequest>
"""


def build_get_best_offers_xml() -> str:
    """Build GetBestOffers XML."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <BestOfferStatus>Active</BestOfferStatus>
</GetBestOffersRequest>
"""


def _dict_by_item_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["item_id"]): item for item in items if item.get("item_id")}


def summarize_payload(
    watched: dict[str, dict[str, Any]],
    bidding: dict[str, dict[str, Any]],
    selling: dict[str, dict[str, Any]],
    ending_soon_threshold_seconds: int,
) -> dict[str, Any]:
    """Build bounded summary metrics and attributes."""
    def under_threshold(item: dict[str, Any]) -> bool:
        seconds_left = item.get("seconds_left")
        return seconds_left is not None and seconds_left <= ending_soon_threshold_seconds

    selling_items = list(selling.values())
    watched_items = list(watched.values())
    items_with_bids = [item for item in selling_items if (item.get("bid_count") or 0) > 0]
    items_with_offers = [item for item in selling_items if (item.get("offers") or 0) > 0]
    items_with_questions = [item for item in selling_items if (item.get("questions") or 0) > 0]
    selling_ending = [item for item in selling_items if under_threshold(item)]
    watched_ending = [item for item in watched_items if under_threshold(item)]

    def small(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "item_id": item.get("item_id"),
            "title": item.get("title"),
            "price": item.get("current_price"),
            "currency": item.get("currency"),
            "end_time": item.get("end_time").isoformat() if item.get("end_time") else None,
            "seconds_left": item.get("seconds_left"),
            "url": item.get("url"),
        }

    highest_watchers = max(selling_items, key=lambda item: item.get("watchers") or -1, default=None)
    highest_views = max(selling_items, key=lambda item: item.get("views") or -1, default=None)
    return {
        "active_selling_items": len(selling),
        "watched_items": len(watched),
        "bidding_items": len(bidding),
        "selling_total_bids": sum(item.get("bid_count") or 0 for item in selling_items),
        "selling_total_offers": sum(item.get("offers") or 0 for item in selling_items),
        "selling_total_sold": sum(item.get("quantity_sold") or 0 for item in selling_items),
        "selling_total_watchers": sum(item.get("watchers") or 0 for item in selling_items),
        "selling_total_views": sum(item.get("views") or 0 for item in selling_items),
        "watched_ending_soon": len(watched_ending),
        "selling_ending_soon": len(selling_ending),
        "items_ending_soon": [small(item) for item in (watched_ending + selling_ending)[:10]],
        "items_with_offers": [small(item) for item in items_with_offers[:10]],
        "items_with_bids": [small(item) for item in items_with_bids[:10]],
        "items_with_questions": [small(item) for item in items_with_questions[:10]],
        "highest_watcher_count_item": small(highest_watchers) if highest_watchers else None,
        "highest_view_count_item": small(highest_views) if highest_views else None,
    }


class EbayApiClient:
    """Async read-only eBay API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        environment: str,
        client_id: str,
        client_secret: str,
        runame: str,
        refresh_token: str | None,
        site_id: str,
    ) -> None:
        self._session = session
        self.environment = environment
        self.client_id = client_id
        self._client_secret = client_secret
        self.runame = runame
        self.refresh_token = refresh_token
        self.site_id = site_id
        self._endpoints = endpoints_for(environment)
        self._access_token: str | None = None

    async def async_exchange_authorization_code(self, code: str) -> dict[str, Any]:
        """Exchange an authorization code for OAuth tokens."""
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": extract_authorization_code(code),
                "redirect_uri": self.runame,
            }
        )
        if not payload.get("access_token") or not payload.get("refresh_token"):
            raise EbayAuthError("OAuth response did not include access and refresh tokens")
        self._access_token = payload["access_token"]
        self.refresh_token = payload["refresh_token"]
        return payload

    async def async_get_access_token(self) -> str:
        """Return a fresh access token."""
        if self._access_token:
            return self._access_token
        if not self.refresh_token:
            raise EbayAuthError("Missing eBay refresh token")
        payload = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }
        )
        access_token = payload.get("access_token")
        if not access_token:
            raise EbayAuthError("OAuth refresh response did not include access token")
        self._access_token = access_token
        return access_token

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        headers = {
            "Authorization": _basic_auth_header(self.client_id, self._client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with self._session.post(
            self._endpoints.token,
            headers=headers,
            data=data,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                raise EbayAuthError(f"eBay OAuth request failed with HTTP {response.status}")
            return payload

    async def async_call_trading_api(self, call_name: str, xml_body: str) -> ET.Element:
        """Call a read-only Trading API operation."""
        access_token = await self.async_get_access_token()
        headers = {
            "Content-Type": "text/xml;charset=UTF-8",
            "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": self.site_id,
            "X-EBAY-API-IAF-TOKEN": access_token,
        }
        async with self._session.post(
            self._endpoints.trading,
            headers=headers,
            data=xml_body.encode("utf-8"),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            body = await response.text()
            if response.status in {401, 403}:
                self._access_token = None
                raise EbayAuthError(f"eBay Trading API auth failed with HTTP {response.status}")
            if response.status >= 400:
                raise EbayApiError(f"eBay Trading API failed with HTTP {response.status}")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise EbayParseError("Could not parse eBay Trading API XML") from exc
        ack = _text(root, "e:Ack")
        if ack not in {"Success", "Warning"}:
            raise EbayApiError(f"eBay Trading API returned Ack={ack!r}")
        return root

    async def async_get_traffic_report(self, item_ids: list[str]) -> dict[str, Any]:
        """Fetch optional Sell Analytics traffic report."""
        if not item_ids:
            return {}
        access_token = await self.async_get_access_token()
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=30)
        listing_ids = "{" + "|".join(item_ids) + "}"
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
            if response.status in {400, 401, 403}:
                raise EbayPartialFailure("analytics_views")
            if response.status >= 400:
                raise EbayApiError(f"eBay Sell Analytics failed with HTTP {response.status}")
            return await response.json(content_type=None)

    async def async_fetch_data(
        self,
        *,
        buying_enabled: bool,
        selling_enabled: bool,
        analytics_enabled: bool,
        ending_soon_threshold_seconds: int,
    ) -> dict[str, Any]:
        """Fetch and normalize all configured read-only telemetry."""
        partial_failures: list[str] = []
        watched: dict[str, dict[str, Any]] = {}
        bidding: dict[str, dict[str, Any]] = {}
        selling: dict[str, dict[str, Any]] = {}

        if buying_enabled:
            root = await self.async_call_trading_api(
                READ_ONLY_CALL_NAME, build_get_my_ebay_buying_xml()
            )
            watched = _dict_by_item_id(parse_container(root, "WatchList", "watched"))
            bidding = _dict_by_item_id(parse_container(root, "BidList", "bidding"))

        if selling_enabled:
            root = await self.async_call_trading_api(
                SELLING_CALL_NAME, build_get_my_ebay_selling_xml()
            )
            selling = _dict_by_item_id(parse_selling_container(root))
            try:
                seller_list = await self.async_call_trading_api(
                    SELLER_LIST_CALL_NAME, build_get_seller_list_xml()
                )
                for item_id, views in seller_list_views_by_item_id(seller_list).items():
                    if item_id in selling:
                        selling[item_id]["views"] = views
            except EbayError as exc:
                _LOGGER.debug("Optional GetSellerList views failed: %s", type(exc).__name__)
                partial_failures.append("seller_list_views")

            try:
                offers = await self.async_call_trading_api(
                    BEST_OFFERS_CALL_NAME, build_get_best_offers_xml()
                )
                for item_id, count in active_offers_by_item_id(offers).items():
                    if item_id in selling:
                        selling[item_id]["offers"] = count
            except EbayError as exc:
                _LOGGER.debug("Optional GetBestOffers failed: %s", type(exc).__name__)
                partial_failures.append("active_offers")

            if analytics_enabled:
                try:
                    analytics = await self.async_get_traffic_report(list(selling))
                    for item_id, views in analytics_views_by_item_id(analytics).items():
                        if item_id in selling:
                            selling[item_id]["views"] = views
                except EbayPartialFailure:
                    partial_failures.append("analytics_views")

        now = datetime.now(timezone.utc)
        summary = summarize_payload(
            watched, bidding, selling, ending_soon_threshold_seconds
        )
        return {
            "watched": watched,
            "bidding": bidding,
            "selling": selling,
            "summary": summary,
            "last_update": now,
            "last_successful_update": now,
            "partial_failures": partial_failures,
        }
