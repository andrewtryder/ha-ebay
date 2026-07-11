"""Read-only eBay API client and parsers."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import xml.etree.ElementTree as ET

import aiohttp

from .const import COMPATIBILITY_LEVEL, EBAY_XML_NS, ENV_SANDBOX, NS
from .oauth_errors import (
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)

_LOGGER = logging.getLogger(__name__)

READ_ONLY_CALL_NAME = "GetMyeBayBuying"
SELLING_CALL_NAME = "GetMyeBaySelling"
SELLER_LIST_CALL_NAME = "GetSellerList"
BEST_OFFERS_CALL_NAME = "GetBestOffers"
BEST_OFFERS_ENTRIES_PER_PAGE = 200
BEST_OFFERS_MAX_PAGES = 25

DEFAULT_SCOPE = (
    "https://api.ebay.com/oauth/api_scope "
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"
)
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
MAX_PAGES = 20
ANALYTICS_BATCH_SIZE = 100
_WARNING_MESSAGE_MAX_LEN = 240
API_WARNINGS_STATE_ATTR_MAX = 10


class EbayError(Exception):
    """Base eBay integration error."""


class EbayAuthError(EbayError):
    """Authentication or authorization failed."""


class EbayAuthTransientError(EbayError):
    """Authentication refresh failed temporarily."""


class EbayApiError(EbayError):
    """eBay API returned an error."""


class EbayPartialFailure(EbayError):
    """Optional eBay API section failed."""


class EbayParseError(EbayError):
    """eBay response could not be parsed."""


@dataclass
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
    return f"{endpoints.authorize}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": runame,
            "response_type": "code",
            "scope": scope,
            "state": state,
        }
    )


def extract_authorization_code(value: str) -> str:
    """Extract an OAuth code from a pasted code or final callback URL."""
    value = value.strip()
    params = extract_oauth_callback_params(value)
    code = params.get("code", [None])[0]
    if code:
        return code
    return value


def extract_oauth_callback_params(value: str) -> dict[str, list[str]]:
    """Extract OAuth query parameters from a pasted callback URL."""
    parsed = urlparse(value.strip())
    if parsed.query:
        query = parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        if code:
            return query
    return {}


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {basic}"


def _raise_token_request_error(status: int, error_code: str | None) -> None:
    """Raise the OAuth exception matching an eBay token failure."""
    if (
        status == HTTPStatus.TOO_MANY_REQUESTS
        or status >= HTTPStatus.INTERNAL_SERVER_ERROR
    ):
        raise OAuth2TokenRequestTransientError(
            f"eBay OAuth token request failed temporarily with HTTP {status}"
        )
    if status < HTTPStatus.INTERNAL_SERVER_ERROR:
        if error_code in {
            "invalid_grant",
            "invalid_client",
            "invalid_request",
            "unauthorized_client",
            "access_denied",
        } or status in {
            HTTPStatus.BAD_REQUEST,
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
        }:
            raise OAuth2TokenRequestReauthError(
                f"eBay OAuth token request requires reauthorization; HTTP {status}"
            )
    raise OAuth2TokenRequestError(f"eBay OAuth token request failed with HTTP {status}")


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


def _total_pages(root: ET.Element, container_name: str | None = None) -> int:
    """Parse PaginationResult.TotalNumberOfPages, capped for safety."""
    return min(_reported_total_pages(root, container_name), MAX_PAGES)


def _reported_total_pages(root: ET.Element, container_name: str | None = None) -> int:
    """Parse PaginationResult.TotalNumberOfPages without applying local caps."""
    parent = root.find(f"e:{container_name}", namespaces=NS) if container_name else root
    total = _to_int(_text(parent, "e:PaginationResult/e:TotalNumberOfPages"))
    if total is None or total < 1:
        return 1
    return total


def _auth_like_error(root: ET.Element) -> bool:
    """Return whether a Trading API XML failure looks token-related."""
    for error in root.findall("e:Errors", namespaces=NS):
        code = _text(error, "e:ErrorCode")
        fields = [
            code,
            _text(error, "e:ShortMessage"),
            _text(error, "e:LongMessage"),
            _text(error, "e:SeverityCode"),
        ]
        haystack = " ".join(value for value in fields if value).lower()
        if code in {"931", "932", "16110", "17470"}:
            return True
        if any(
            marker in haystack
            for marker in (
                "auth",
                "token",
                "oauth",
                "credential",
                "access denied",
                "not authorized",
            )
        ):
            return True
    return False


def _sanitize_warning_text(value: str | None) -> str | None:
    """Truncate and redact credential-like substrings from Trading API warnings."""
    if not value:
        return None
    sanitized = value
    for marker in ("Bearer ", "token=", "refresh_token", "access_token"):
        if marker.lower() in sanitized.lower():
            sanitized = "[redacted]"
            break
    if len(sanitized) > _WARNING_MESSAGE_MAX_LEN:
        sanitized = f"{sanitized[:_WARNING_MESSAGE_MAX_LEN]}…"
    return sanitized


def extract_trading_warnings(root: ET.Element) -> list[dict[str, str | None]]:
    """Return sanitized Trading API warning entries from an Ack=Warning response."""
    warnings: list[dict[str, str | None]] = []
    for error in root.findall("e:Errors", namespaces=NS):
        severity = (_text(error, "e:SeverityCode") or "").lower()
        if severity and severity != "warning":
            continue
        warnings.append(
            {
                "code": _text(error, "e:ErrorCode"),
                "severity": _text(error, "e:SeverityCode"),
                "short_message": _sanitize_warning_text(_text(error, "e:ShortMessage")),
                "long_message": _sanitize_warning_text(_text(error, "e:LongMessage")),
            }
        )
    return warnings


def dedupe_trading_warnings(
    warnings: list[dict[str, str | None]],
) -> list[dict[str, str | None]]:
    """Deduplicate Trading API warnings by code and message text."""
    deduped: list[dict[str, str | None]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for warning in warnings:
        key = (
            warning.get("code"),
            warning.get("short_message"),
            warning.get("long_message"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(warning)
    return deduped


def chunk_item_ids(item_ids: list[str], batch_size: int = ANALYTICS_BATCH_SIZE) -> list[list[str]]:
    """Split item IDs into bounded Analytics request batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    return [item_ids[index : index + batch_size] for index in range(0, len(item_ids), batch_size)]


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
        "analytics_views_30d": None,
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


def parse_container(
    root: ET.Element, container_name: str, kind: str
) -> list[dict[str, Any]]:
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
    for item_offers in root.findall(
        "e:ItemBestOffersArray/e:ItemBestOffers", namespaces=NS
    ):
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


def pagination_total_pages(root: ET.Element) -> int:
    """Parse Trading API PaginationResult total pages."""
    total_pages = _to_int(_text(root, "e:PaginationResult/e:TotalNumberOfPages"))
    return max(1, total_pages or 1)


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


def build_get_seller_list_xml(entries_per_page: int = 200, page: int = 1) -> str:
    """Build GetSellerList XML for active listings."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=120)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetSellerListRequest xmlns="{EBAY_XML_NS}">
  <GranularityLevel>Fine</GranularityLevel>
  <EndTimeFrom>{ebay_time(now)}</EndTimeFrom>
  <EndTimeTo>{ebay_time(end)}</EndTimeTo>
  <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
</GetSellerListRequest>
"""


def build_get_best_offers_xml(
    entries_per_page: int = BEST_OFFERS_ENTRIES_PER_PAGE,
    page: int = 1,
) -> str:
    """Build GetBestOffers XML."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <BestOfferStatus>Active</BestOfferStatus>
  <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
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
        return seconds_left is not None and 0 < seconds_left <= ending_soon_threshold_seconds

    selling_items = list(selling.values())
    watched_items = list(watched.values())
    items_with_bids = [
        item for item in selling_items if (item.get("bid_count") or 0) > 0
    ]
    items_with_offers = [
        item for item in selling_items if (item.get("offers") or 0) > 0
    ]
    items_with_questions = [
        item for item in selling_items if (item.get("questions") or 0) > 0
    ]
    selling_ending = [item for item in selling_items if under_threshold(item)]
    watched_ending = [item for item in watched_items if under_threshold(item)]

    def small(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "item_id": item.get("item_id"),
            "title": item.get("title"),
            "price": item.get("current_price"),
            "currency": item.get("currency"),
            "end_time": item.get("end_time").isoformat()
            if item.get("end_time")
            else None,
            "seconds_left": item.get("seconds_left"),
            "url": item.get("url"),
        }

    highest_watchers = max(
        selling_items, key=lambda item: item.get("watchers") or -1, default=None
    )
    highest_views = max(
        selling_items, key=lambda item: item.get("views") or -1, default=None
    )
    return {
        "active_selling_items": len(selling),
        "watched_items": len(watched),
        "bidding_items": len(bidding),
        "selling_total_bids": sum(item.get("bid_count") or 0 for item in selling_items),
        "selling_total_offers": sum(item.get("offers") or 0 for item in selling_items),
        "active_listings_quantity_sold": sum(
            item.get("quantity_sold") or 0 for item in selling_items
        ),
        "selling_total_watchers": sum(
            item.get("watchers") or 0 for item in selling_items
        ),
        "selling_total_views": sum(item.get("views") or 0 for item in selling_items),
        "watched_ending_soon": len(watched_ending),
        "selling_ending_soon": len(selling_ending),
        "items_ending_soon": [
            small(item) for item in (watched_ending + selling_ending)[:10]
        ],
        "items_with_offers": [small(item) for item in items_with_offers[:10]],
        "items_with_bids": [small(item) for item in items_with_bids[:10]],
        "items_with_questions": [small(item) for item in items_with_questions[:10]],
        "highest_watcher_count_item": small(highest_watchers)
        if highest_watchers
        else None,
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
        oauth_session: Any | None = None,
    ) -> None:
        self._session = session
        self._oauth_session = oauth_session
        self.environment = environment
        self.client_id = client_id
        self._client_secret = client_secret
        self.runame = runame
        self.refresh_token = refresh_token
        self.site_id = site_id
        self._endpoints = endpoints_for(environment)
        self._access_token: str | None = None
        self._access_token_expires_at: datetime | None = None
        self._api_warnings: list[dict[str, str | None]] = []

    async def async_exchange_authorization_code(self, code: str) -> dict[str, Any]:
        """Exchange an authorization code for OAuth tokens."""
        try:
            payload = await self._token_request(
                {
                    "grant_type": "authorization_code",
                    "code": extract_authorization_code(code),
                    "redirect_uri": self.runame,
                }
            )
        except OAuth2TokenRequestError as exc:
            raise EbayAuthError("OAuth authorization code exchange failed") from exc
        if not payload.get("access_token") or not payload.get("refresh_token"):
            raise EbayAuthError(
                "OAuth response did not include access and refresh tokens"
            )
        self._store_access_token(payload)
        self.refresh_token = payload["refresh_token"]
        return payload

    async def async_get_access_token(self) -> str:
        """Return a fresh access token."""
        if self._oauth_session is not None:
            try:
                await self._oauth_session.async_ensure_token_valid()
            except OAuth2TokenRequestReauthError as exc:
                raise EbayAuthError("OAuth token refresh failed") from exc
            except (OAuth2TokenRequestTransientError, OAuth2TokenRequestError) as exc:
                raise EbayAuthTransientError(
                    "OAuth token refresh failed temporarily"
                ) from exc
            access_token = self._oauth_session.token.get("access_token")
            if not access_token:
                raise EbayAuthError("OAuth token data does not include access token")
            return access_token
        if self._access_token and not self._access_token_expiring():
            return self._access_token
        if not self.refresh_token:
            raise EbayAuthError("Missing eBay refresh token")
        try:
            payload = await self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                }
            )
        except OAuth2TokenRequestReauthError as exc:
            raise EbayAuthError("OAuth token refresh failed") from exc
        except (OAuth2TokenRequestTransientError, OAuth2TokenRequestError) as exc:
            raise EbayAuthTransientError(
                "OAuth token refresh failed temporarily"
            ) from exc
        access_token = payload.get("access_token")
        if not access_token:
            raise EbayAuthError("OAuth refresh response did not include access token")
        self._store_access_token(payload)
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        return access_token

    def _access_token_expiring(self) -> bool:
        """Return whether the cached access token is close to expiry."""
        if self._access_token_expires_at is None:
            return True
        return (
            datetime.now(timezone.utc) + TOKEN_REFRESH_MARGIN
            >= self._access_token_expires_at
        )

    def _store_access_token(self, payload: dict[str, Any]) -> None:
        """Cache an access token and its expiry."""
        self._access_token = payload["access_token"]
        expires_in = _to_int(payload.get("expires_in"))
        if expires_in is None:
            self._access_token_expires_at = datetime.now(timezone.utc)
            return
        self._access_token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

    def _clear_access_token(self) -> None:
        """Clear cached access token state."""
        self._access_token = None
        self._access_token_expires_at = None

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        headers = {
            "Authorization": _basic_auth_header(self.client_id, self._client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            async with self._session.post(
                self._endpoints.token,
                headers=headers,
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                    raise OAuth2TokenRequestError(
                        f"eBay OAuth response was not valid JSON; HTTP {response.status}"
                    ) from exc
                if response.status >= 400:
                    error_code = (
                        payload.get("error") if isinstance(payload, dict) else None
                    )
                    _raise_token_request_error(response.status, error_code)
                return payload
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise OAuth2TokenRequestTransientError(
                "eBay OAuth token request failed temporarily"
            ) from exc

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
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                body = await response.text()
                if response.status in {401, 403}:
                    raise EbayAuthError(
                        f"eBay Trading API auth failed with HTTP {response.status}"
                    )
                if response.status >= 400:
                    raise EbayApiError(
                        f"eBay Trading API failed with HTTP {response.status}"
                    )
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise EbayApiError("eBay Trading API request failed") from exc
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise EbayParseError("Could not parse eBay Trading API XML") from exc
        ack = _text(root, "e:Ack")
        if ack not in {"Success", "Warning"}:
            if _auth_like_error(root):
                raise EbayAuthError(f"eBay Trading API returned Ack={ack!r}")
            raise EbayApiError(f"eBay Trading API returned Ack={ack!r}")
        if ack == "Warning":
            self._api_warnings.extend(extract_trading_warnings(root))
        return root

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
                    raise EbayAuthError(
                        f"eBay Sell Analytics auth failed with HTTP {response.status}"
                    )
                if response.status in {400, 403}:
                    raise EbayPartialFailure("analytics_views")
                if response.status >= 400:
                    raise EbayApiError(
                        f"eBay Sell Analytics failed with HTTP {response.status}"
                    )
                try:
                    return await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                    raise EbayParseError(
                        "Could not parse eBay Sell Analytics JSON"
                    ) from exc
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
        for batch in chunk_item_ids(item_ids):
            try:
                payload = await self.async_get_traffic_report(batch)
            except EbayAuthError:
                raise
            except (EbayError, aiohttp.ClientError, TimeoutError):
                partial_failure = True
                continue
            merged.update(analytics_views_by_item_id(payload))
        return merged, partial_failure

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
            watched_items.extend(parse_container(root, "WatchList", "watched"))
            bidding_items.extend(parse_container(root, "BidList", "bidding"))
            watch_pages = _reported_total_pages(root, "WatchList")
            bid_pages = _reported_total_pages(root, "BidList")
            watched_truncated = watched_truncated or watch_pages > MAX_PAGES
            bidding_truncated = bidding_truncated or bid_pages > MAX_PAGES
            total_pages = max(
                total_pages,
                min(watch_pages, MAX_PAGES),
                min(bid_pages, MAX_PAGES),
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
            selling_items.extend(parse_selling_container(root))
            active_pages = _reported_total_pages(root, "ActiveList")
            truncated = truncated or active_pages > MAX_PAGES
            total_pages = max(total_pages, min(active_pages, MAX_PAGES))
            page += 1
        return _dict_by_item_id(selling_items), truncated

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
            views.update(seller_list_views_by_item_id(root))
            seller_list_pages = _reported_total_pages(root)
            truncated = truncated or seller_list_pages > MAX_PAGES
            total_pages = max(total_pages, min(seller_list_pages, MAX_PAGES))
            page += 1
        return views, truncated

    async def async_fetch_data(
        self,
        *,
        buying_enabled: bool,
        selling_enabled: bool,
        analytics_enabled: bool,
        ending_soon_threshold_seconds: int,
    ) -> dict[str, Any]:
        """Fetch and normalize all configured read-only telemetry."""
        self._api_warnings = []
        partial_failures: list[str] = []
        watched: dict[str, dict[str, Any]] = {}
        bidding: dict[str, dict[str, Any]] = {}
        selling: dict[str, dict[str, Any]] = {}
        watched_truncated = False
        bidding_truncated = False
        selling_truncated = False
        seller_list_views_truncated = False
        active_offers_truncated = False

        if buying_enabled:
            (
                watched,
                bidding,
                watched_truncated,
                bidding_truncated,
            ) = await self._fetch_buying_pages()
            if watched_truncated or bidding_truncated:
                partial_failures.append("buying_truncated")

        if selling_enabled:
            selling, selling_truncated = await self._fetch_selling_pages()
            if selling_truncated:
                partial_failures.append("selling_truncated")
            try:
                seller_list_views, seller_list_views_truncated = (
                    await self._fetch_seller_list_views()
                )
                for item_id, views in seller_list_views.items():
                    if item_id in selling:
                        selling[item_id]["views"] = views
                if seller_list_views_truncated:
                    partial_failures.append("seller_list_views_truncated")
            except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug(
                    "Optional GetSellerList views failed: %s", type(exc).__name__
                )
                partial_failures.append("seller_list_views")

            try:
                page = 1
                total_pages = 1
                active_offer_counts: dict[str, int] = {}
                while page <= min(total_pages, BEST_OFFERS_MAX_PAGES):
                    offers = await self.async_call_trading_api(
                        BEST_OFFERS_CALL_NAME, build_get_best_offers_xml(page=page)
                    )
                    total_pages = pagination_total_pages(offers)
                    for item_id, count in active_offers_by_item_id(offers).items():
                        if item_id in selling:
                            active_offer_counts[item_id] = (
                                active_offer_counts.get(item_id, 0) + count
                            )
                    page += 1
                for item_id, count in active_offer_counts.items():
                    selling[item_id]["offers"] = count
                if total_pages > BEST_OFFERS_MAX_PAGES:
                    active_offers_truncated = True
                    partial_failures.append("active_offers_truncated")
            except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug("Optional GetBestOffers failed: %s", type(exc).__name__)
                partial_failures.append("active_offers")

            if analytics_enabled:
                try:
                    views_by_id, analytics_partial = await self.async_fetch_analytics_views(
                        list(selling)
                    )
                    for item_id, views in views_by_id.items():
                        if item_id in selling:
                            selling[item_id]["analytics_views_30d"] = views
                    if analytics_partial:
                        partial_failures.append("analytics_views")
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional analytics views failed: %s", type(exc).__name__
                    )
                    partial_failures.append("analytics_views")

        api_warnings = dedupe_trading_warnings(self._api_warnings)
        if api_warnings and "trading_warning" not in partial_failures:
            partial_failures.append("trading_warning")

        truncated_collections = {
            "watched": watched_truncated,
            "bidding": bidding_truncated,
            "selling": selling_truncated,
            "seller_list_views": seller_list_views_truncated,
            "active_offers": active_offers_truncated,
        }

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
            "truncated_collections": truncated_collections,
            "api_warnings": api_warnings,
        }
