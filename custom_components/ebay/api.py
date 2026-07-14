"""Read-only eBay API client and parsers."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
import xml.etree.ElementTree as ET

import aiohttp

from .const import (
    COMPATIBILITY_LEVEL,
    CONF_ANALYTICS_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SELLING_ENABLED,
    EBAY_XML_NS,
    ENV_SANDBOX,
    NS,
    SECTION_ANALYTICS,
    SECTION_BUYING,
    SECTION_FEEDBACK,
    SECTION_FULFILLMENT,
    SECTION_MESSAGES,
    SECTION_SELLER_STANDARDS,
    SECTION_SELLING,
)
from .oauth_errors import (
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
    oauth_token_error,
    oauth_token_request_error,
    oauth_token_transient_error,
)
from .seller_ops import (
    CONVERSATIONS_MAX_PAGES,
    CONVERSATIONS_PAGE_LIMIT,
    DISPUTES_MAX_PAGES,
    DISPUTES_PAGE_LIMIT,
    ORDERS_LOOKBACK_DAYS,
    ORDERS_MAX_PAGES,
    ORDERS_PAGE_LIMIT,
    empty_seller_ops,
    encode_creation_date_filter,
    encode_fulfillment_status_filter,
    is_known_site_id,
    marketplace_id_for_site,
    normalize_conversation,
    normalize_dispute,
    normalize_order,
    parse_awaiting_feedback_count,
    parse_customer_service_metric,
    parse_feedback_items,
    parse_feedback_summary,
    parse_seller_standards_profiles,
    summarize_disputes,
    summarize_messages,
    summarize_orders,
)

_LOGGER = logging.getLogger(__name__)

READ_ONLY_CALL_NAME = "GetMyeBayBuying"
SELLING_CALL_NAME = "GetMyeBaySelling"
SELLER_LIST_CALL_NAME = "GetSellerList"
BEST_OFFERS_CALL_NAME = "GetBestOffers"
BEST_OFFERS_ENTRIES_PER_PAGE = 200
BEST_OFFERS_MAX_PAGES = 25

CORE_SCOPE = "https://api.ebay.com/oauth/api_scope"
SCOPE_ANALYTICS_READONLY = (
    "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"
)
SCOPE_FULFILLMENT_READONLY = (
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
)
SCOPE_PAYMENT_DISPUTE = "https://api.ebay.com/oauth/api_scope/sell.payment.dispute"
SCOPE_FEEDBACK = "https://api.ebay.com/oauth/api_scope/commerce.feedback"
SCOPE_MESSAGE = "https://api.ebay.com/oauth/api_scope/commerce.message"

MODULE_SCOPES: dict[str, tuple[str, ...]] = {
    CONF_ANALYTICS_ENABLED: (SCOPE_ANALYTICS_READONLY,),
    CONF_SELLER_STANDARDS_ENABLED: (SCOPE_ANALYTICS_READONLY,),
    CONF_FULFILLMENT_ENABLED: (SCOPE_FULFILLMENT_READONLY, SCOPE_PAYMENT_DISPUTE),
    CONF_FEEDBACK_ENABLED: (SCOPE_FEEDBACK,),
    CONF_MESSAGES_ENABLED: (SCOPE_MESSAGE,),
}

ALL_OPTIONAL_SCOPES = (
    SCOPE_ANALYTICS_READONLY,
    SCOPE_FULFILLMENT_READONLY,
    SCOPE_PAYMENT_DISPUTE,
    SCOPE_FEEDBACK,
    SCOPE_MESSAGE,
)


def parse_scope_set(scopes: str | None) -> set[str]:
    """Parse a space-delimited OAuth scope string into a set."""
    if not scopes:
        return set()
    return {scope for scope in scopes.split() if scope}


def join_scopes(scopes: list[str] | tuple[str, ...] | set[str]) -> str:
    """Join scopes into a stable space-delimited string."""
    if isinstance(scopes, set):
        ordered = sorted(scopes)
    else:
        ordered = list(dict.fromkeys(scopes))
    return " ".join(ordered)


def scopes_for_options(options: dict[str, Any]) -> str:
    """Return the OAuth scope string required by enabled feature options."""
    scopes: list[str] = [CORE_SCOPE]
    for option_key, option_scopes in MODULE_SCOPES.items():
        if not options.get(option_key):
            continue
        # Analytics is only useful/scheduled when Selling is also enabled.
        if option_key == CONF_ANALYTICS_ENABLED and not options.get(
            CONF_SELLING_ENABLED
        ):
            continue
        scopes.extend(option_scopes)
    return join_scopes(scopes)


def missing_scopes_for_options(
    granted: str | set[str] | None, options: dict[str, Any]
) -> dict[str, list[str]]:
    """Return enabled modules whose required scopes are not all granted."""
    granted_set = granted if isinstance(granted, set) else parse_scope_set(granted)
    missing: dict[str, list[str]] = {}
    for option_key, option_scopes in MODULE_SCOPES.items():
        if not options.get(option_key):
            continue
        if option_key == CONF_ANALYTICS_ENABLED and not options.get(
            CONF_SELLING_ENABLED
        ):
            continue
        absent = [scope for scope in option_scopes if scope not in granted_set]
        if absent:
            missing[option_key] = absent
    return missing


def resolve_granted_scopes(requested: str, token: dict[str, Any] | None = None) -> str:
    """Prefer scopes returned by eBay; otherwise store the requested set."""
    if token:
        returned = token.get("scope")
        if isinstance(returned, str) and returned.strip():
            return join_scopes(parse_scope_set(returned))
    return join_scopes(parse_scope_set(requested))


def has_core_scope(scopes: str | None) -> bool:
    """Return True when the stored grant includes the core Trading API scope."""
    return CORE_SCOPE in parse_scope_set(scopes)


# Full union retained for docs/tests; routine authorize uses CORE_SCOPE or
# scopes_for_options().
DEFAULT_SCOPE = join_scopes((CORE_SCOPE, *ALL_OPTIONAL_SCOPES))

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
    analytics_base: str
    fulfillment: str
    payment_disputes: str
    feedback: str
    messages: str


def endpoints_for(environment: str) -> EbayEndpoints:
    """Return endpoint URLs for production or sandbox."""
    if environment == ENV_SANDBOX:
        return EbayEndpoints(
            trading="https://api.sandbox.ebay.com/ws/api.dll",
            token="https://api.sandbox.ebay.com/identity/v1/oauth2/token",
            authorize="https://auth.sandbox.ebay.com/oauth2/authorize",
            analytics="https://api.sandbox.ebay.com/sell/analytics/v1/traffic_report",
            analytics_base="https://api.sandbox.ebay.com/sell/analytics/v1",
            fulfillment="https://api.sandbox.ebay.com/sell/fulfillment/v1",
            payment_disputes="https://apiz.sandbox.ebay.com/sell/fulfillment/v1",
            feedback="https://api.sandbox.ebay.com/commerce/feedback/v1",
            messages="https://api.sandbox.ebay.com/commerce/message/v1",
        )
    return EbayEndpoints(
        trading="https://api.ebay.com/ws/api.dll",
        token="https://api.ebay.com/identity/v1/oauth2/token",
        authorize="https://auth.ebay.com/oauth2/authorize",
        analytics="https://api.ebay.com/sell/analytics/v1/traffic_report",
        analytics_base="https://api.ebay.com/sell/analytics/v1",
        fulfillment="https://api.ebay.com/sell/fulfillment/v1",
        payment_disputes="https://apiz.ebay.com/sell/fulfillment/v1",
        feedback="https://api.ebay.com/commerce/feedback/v1",
        messages="https://api.ebay.com/commerce/message/v1",
    )


def build_consent_url(
    environment: str,
    client_id: str,
    runame: str,
    state: str,
    scope: str = CORE_SCOPE,
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


def chunk_item_ids(
    item_ids: list[str], batch_size: int = ANALYTICS_BATCH_SIZE
) -> list[list[str]]:
    """Split item IDs into bounded Analytics request batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    return [
        item_ids[index : index + batch_size]
        for index in range(0, len(item_ids), batch_size)
    ]


def _duration_ms(start: float) -> float:
    """Return elapsed monotonic time in milliseconds."""
    return (time.monotonic() - start) * 1000


def _safe_exception_context(exc: BaseException) -> str:
    """Return safe exception class and message context for debug logs."""
    detail = str(exc)
    if not detail:
        return type(exc).__name__
    return f"{type(exc).__name__}: {detail}"


def _page_number_from_xml(xml_body: str) -> int | None:
    """Extract a Trading API page number from generated XML without logging XML."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError:
        return None
    return _to_int(_text(root, ".//e:PageNumber"))


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


SOLD_UNSOLD_DURATION_DAYS = 60
SOLD_UNSOLD_REFRESH_HOURS = 6


def _parse_optional_datetime(value: Any) -> datetime | None:
    """Parse an optional datetime or ISO string."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _should_fetch_sold_unsold(
    previous_selling: dict[str, Any],
    current_selling: dict[str, Any],
    last_fetched: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether SoldList/UnsoldList classification should be refreshed."""
    if set(previous_selling) - set(current_selling):
        return True
    if last_fetched is None:
        return True
    stamp = now or datetime.now(timezone.utc)
    return stamp - last_fetched >= timedelta(hours=SOLD_UNSOLD_REFRESH_HOURS)


def build_get_my_ebay_selling_list_xml(
    list_name: str,
    *,
    entries_per_page: int = 100,
    page: int = 1,
    duration_in_days: int = SOLD_UNSOLD_DURATION_DAYS,
) -> str:
    """Build GetMyeBaySelling XML for one SoldList or UnsoldList page."""
    if list_name not in {"SoldList", "UnsoldList"}:
        raise ValueError(f"Unsupported selling list: {list_name}")
    sold_block = (
        f"""  <SoldList>
    <Include>true</Include>
    <DurationInDays>{duration_in_days}</DurationInDays>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </SoldList>"""
        if list_name == "SoldList"
        else "  <SoldList><Include>false</Include></SoldList>"
    )
    unsold_block = (
        f"""  <UnsoldList>
    <Include>true</Include>
    <DurationInDays>{duration_in_days}</DurationInDays>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </UnsoldList>"""
        if list_name == "UnsoldList"
        else "  <UnsoldList><Include>false</Include></UnsoldList>"
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <ActiveList><Include>false</Include></ActiveList>
  <ScheduledList><Include>false</Include></ScheduledList>
{sold_block}
{unsold_block}
</GetMyeBaySellingRequest>
"""


def build_get_my_ebay_selling_classification_xml(
    entries_per_page: int = 100,
    page: int = 1,
    *,
    duration_in_days: int = SOLD_UNSOLD_DURATION_DAYS,
) -> str:
    """Build a combined sold/unsold lookup request (single page of each list)."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <ActiveList><Include>false</Include></ActiveList>
  <ScheduledList><Include>false</Include></ScheduledList>
  <SoldList>
    <Include>true</Include>
    <DurationInDays>{duration_in_days}</DurationInDays>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </SoldList>
  <UnsoldList>
    <Include>true</Include>
    <DurationInDays>{duration_in_days}</DurationInDays>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </UnsoldList>
</GetMyeBaySellingRequest>
"""


def parse_sold_unsold_classification(root: ET.Element) -> dict[str, str]:
    """Parse sold/unsold ItemIDs from a GetMyeBaySelling response.

    Unsold is applied first, then sold, so SoldList wins if an ItemID appears in both.
    """
    classified: dict[str, str] = {}
    for container_name, status in (("UnsoldList", "unsold"), ("SoldList", "sold")):
        container = root.find(f"e:{container_name}", namespaces=NS)
        if container is None:
            continue
        for item in container.findall("e:ItemArray/e:Item", namespaces=NS):
            item_id = _text(item, "e:ItemID")
            if item_id:
                classified[item_id] = status
    return classified


def parse_selling_list_item_ids(root: ET.Element, list_name: str) -> list[str]:
    """Return ItemIDs from one SoldList or UnsoldList container."""
    container = root.find(f"e:{list_name}", namespaces=NS)
    if container is None:
        return []
    item_ids: list[str] = []
    for item in container.findall("e:ItemArray/e:Item", namespaces=NS):
        item_id = _text(item, "e:ItemID")
        if item_id:
            item_ids.append(item_id)
    return item_ids


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
        return (
            seconds_left is not None
            and 0 < seconds_left <= ending_soon_threshold_seconds
        )

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
                    raise oauth_token_request_error(
                        f"eBay OAuth response was not valid JSON; HTTP {response.status}",
                        status=response.status,
                        token_url=self._endpoints.token,
                    ) from exc
                if response.status >= 400:
                    error_code = (
                        payload.get("error") if isinstance(payload, dict) else None
                    )
                    raise oauth_token_error(
                        response.status,
                        error_code=error_code,
                        token_url=self._endpoints.token,
                    )
                return payload
        except OAuth2TokenRequestError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise oauth_token_transient_error(
                "eBay OAuth token request failed temporarily",
                token_url=self._endpoints.token,
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

    async def _async_rest_get(
        self,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        partial_category: str,
        allow_404: bool = False,
    ) -> dict[str, Any]:
        """GET a JSON REST resource with auth retry."""
        for attempt in range(2):
            try:
                return await self._async_rest_get_once(
                    url,
                    params=params,
                    partial_category=partial_category,
                    allow_404=allow_404,
                )
            except EbayAuthError:
                self._clear_access_token()
                if attempt == 0:
                    continue
                raise
        raise EbayAuthError(f"eBay REST auth failed for {partial_category}")

    async def _async_rest_get_once(
        self,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, str]] | None = None,
        partial_category: str,
        allow_404: bool = False,
    ) -> dict[str, Any]:
        """GET a JSON REST resource without auth retry."""
        access_token = await self.async_get_access_token()
        request_start = time.monotonic()
        try:
            async with self._session.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                params=params or {},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 401:
                    raise EbayAuthError(
                        f"eBay REST auth failed with HTTP {response.status}"
                    )
                if allow_404 and response.status == 404:
                    return {}
                if response.status in {400, 403, 404}:
                    raise EbayPartialFailure(partial_category)
                if response.status >= 400:
                    raise EbayApiError(
                        f"eBay REST call failed with HTTP {response.status}"
                    )
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                    raise EbayParseError(
                        f"Could not parse eBay REST JSON for {partial_category}"
                    ) from exc
                _LOGGER.debug(
                    "REST GET completed category=%s status=%s duration_ms=%.1f",
                    partial_category,
                    response.status,
                    _duration_ms(request_start),
                )
                return payload if isinstance(payload, dict) else {}
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise EbayPartialFailure(partial_category) from exc

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

    async def async_fetch_seller_standards(self) -> dict[str, Any]:
        """Fetch seller standards profile plus INR/INAD customer-service metrics."""
        standards = empty_seller_ops()["standards"]
        profiles = await self._async_rest_get(
            f"{self._endpoints.analytics_base}/seller_standards_profile",
            partial_category="seller_standards",
            allow_404=True,
        )
        parsed = parse_seller_standards_profiles(profiles)
        standards.update(parsed)

        marketplace = marketplace_id_for_site(self.site_id)
        if marketplace is None:
            _LOGGER.debug(
                "Skipping seller standards marketplace metrics for unknown site_id=%s",
                self.site_id,
            )
            return standards
        for metric_type, rating_key, flag_key in (
            (
                "ITEM_NOT_RECEIVED",
                "item_not_received_rating",
                "item_not_received_above_benchmark",
            ),
            (
                "ITEM_NOT_AS_DESCRIBED",
                "item_not_as_described_rating",
                "item_not_as_described_above_benchmark",
            ),
        ):
            try:
                metric_payload = await self._async_rest_get(
                    (
                        f"{self._endpoints.analytics_base}/customer_service_metric/"
                        f"{metric_type}/CURRENT"
                    ),
                    params={"evaluation_marketplace_id": marketplace},
                    partial_category="seller_standards",
                    allow_404=True,
                )
            except EbayPartialFailure:
                continue
            metric = parse_customer_service_metric(metric_payload)
            standards[rating_key] = metric.get("rating")
            standards[flag_key] = bool(metric.get("above_peer_benchmark"))
            if metric.get("above_peer_benchmark"):
                standards["at_risk"] = True
        return standards

    async def async_fetch_feedback(self) -> dict[str, Any]:
        """Fetch feedback summary, recent counts, and awaiting-feedback count."""
        feedback = empty_seller_ops()["feedback"]
        try:
            summary_payload = await self._async_rest_get(
                f"{self._endpoints.feedback}/feedback_rating_summary",
                params={
                    "filter": "ratingType:OVERALL_EXPERIENCE,lookbackPeriodInDays:30",
                },
                partial_category="feedback",
                allow_404=True,
            )
            feedback.update(parse_feedback_summary(summary_payload))
        except EbayPartialFailure:
            raise

        try:
            items_payload = await self._async_rest_get(
                f"{self._endpoints.feedback}/feedback",
                params={
                    "filter": "feedback_type:FEEDBACK_RECEIVED,period:30,role:SELLER",
                    "limit": "50",
                    "offset": "0",
                },
                partial_category="feedback",
                allow_404=True,
            )
            counts = parse_feedback_items(items_payload)
            # Prefer explicit item counts when summary left zeros.
            if (
                counts["recent_positive"]
                or counts["recent_neutral"]
                or counts["recent_negative"]
            ):
                feedback.update(counts)
        except EbayPartialFailure:
            pass

        try:
            awaiting_payload = await self._async_rest_get(
                f"{self._endpoints.feedback}/awaiting_feedback",
                params={"limit": "50", "offset": "0"},
                partial_category="feedback",
                allow_404=True,
            )
            feedback["awaiting_count"] = parse_awaiting_feedback_count(awaiting_payload)
        except EbayPartialFailure:
            pass
        return feedback

    async def async_fetch_messages(self) -> tuple[dict[str, Any], bool]:
        """Fetch member conversation metadata aggregates."""
        merged: dict[str, dict[str, Any]] = {}
        truncated = False
        now = datetime.now(timezone.utc)
        offset = 0
        for page in range(CONVERSATIONS_MAX_PAGES):
            payload = await self._async_rest_get(
                f"{self._endpoints.messages}/conversation",
                params={
                    "conversation_type": "FROM_MEMBERS",
                    "limit": str(CONVERSATIONS_PAGE_LIMIT),
                    "offset": str(offset),
                },
                partial_category="messages",
            )
            conversations = payload.get("conversations") or []
            for raw in conversations:
                normalized = normalize_conversation(raw)
                if normalized.get("conversation_id"):
                    merged[normalized["conversation_id"]] = normalized
            offset += CONVERSATIONS_PAGE_LIMIT
            total = payload.get("total")
            if not conversations:
                break
            if total is not None:
                try:
                    if offset >= int(total):
                        break
                except (TypeError, ValueError):
                    pass
            elif len(conversations) < CONVERSATIONS_PAGE_LIMIT:
                break
            if page == CONVERSATIONS_MAX_PAGES - 1:
                truncated = True
        return summarize_messages(list(merged.values()), now=now), truncated

    async def async_fetch_data(
        self,
        *,
        buying_enabled: bool,
        selling_enabled: bool,
        analytics_enabled: bool,
        fulfillment_enabled: bool = False,
        seller_standards_enabled: bool = False,
        feedback_enabled: bool = False,
        messages_enabled: bool = False,
        ending_soon_threshold_seconds: int,
        granted_scopes: str | None = None,
        sections: set[str] | None = None,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch and normalize configured read-only telemetry.

        When ``sections`` is None, all enabled sections are fetched (full refresh).
        Otherwise only the requested sections are refreshed and merged into
        ``previous`` (or an empty shell).
        """
        enabled_sections = enabled_sections_for_options(
            buying_enabled=buying_enabled,
            selling_enabled=selling_enabled,
            analytics_enabled=analytics_enabled,
            fulfillment_enabled=fulfillment_enabled,
            seller_standards_enabled=seller_standards_enabled,
            feedback_enabled=feedback_enabled,
            messages_enabled=messages_enabled,
        )
        due = enabled_sections if sections is None else (sections & enabled_sections)
        return await self.async_fetch_sections(
            due,
            previous=previous,
            buying_enabled=buying_enabled,
            selling_enabled=selling_enabled,
            analytics_enabled=analytics_enabled,
            fulfillment_enabled=fulfillment_enabled,
            seller_standards_enabled=seller_standards_enabled,
            feedback_enabled=feedback_enabled,
            messages_enabled=messages_enabled,
            ending_soon_threshold_seconds=ending_soon_threshold_seconds,
            granted_scopes=granted_scopes,
        )

    async def async_fetch_sections(
        self,
        sections: set[str],
        *,
        previous: dict[str, Any] | None = None,
        buying_enabled: bool,
        selling_enabled: bool,
        analytics_enabled: bool,
        fulfillment_enabled: bool = False,
        seller_standards_enabled: bool = False,
        feedback_enabled: bool = False,
        messages_enabled: bool = False,
        ending_soon_threshold_seconds: int,
        granted_scopes: str | None = None,
    ) -> dict[str, Any]:
        """Fetch due sections and merge into a central coordinator payload."""
        refresh_start = time.monotonic()
        _LOGGER.debug(
            "eBay API section refresh started sections=%s buying_enabled=%s selling_enabled=%s analytics_enabled=%s fulfillment_enabled=%s seller_standards_enabled=%s feedback_enabled=%s messages_enabled=%s",
            sorted(sections),
            buying_enabled,
            selling_enabled,
            analytics_enabled,
            fulfillment_enabled,
            seller_standards_enabled,
            feedback_enabled,
            messages_enabled,
        )
        base = _payload_shell(previous)
        watched: dict[str, dict[str, Any]] = dict(base["watched"])
        bidding: dict[str, dict[str, Any]] = dict(base["bidding"])
        selling: dict[str, dict[str, Any]] = {
            item_id: dict(item) for item_id, item in base["selling"].items()
        }
        selling_classification: dict[str, str] = dict(base["selling_classification"])
        seller_ops = _copy_seller_ops(base["seller_ops"])
        section_last_fetched: dict[str, datetime] = dict(
            base.get("section_last_fetched") or {}
        )
        section_last_attempt: dict[str, datetime] = dict(
            base.get("section_last_attempt") or {}
        )
        section_last_success: dict[str, datetime] = dict(
            base.get("section_last_success") or section_last_fetched
        )
        partial_failures = [
            category
            for category in base.get("partial_failures") or []
            if not any(
                category in PARTIAL_FAILURE_CATEGORIES_BY_SECTION.get(section, ())
                for section in sections
            )
            and not (
                category == "trading_warning"
                and (SECTION_BUYING in sections or SECTION_SELLING in sections)
            )
        ]
        truncated_collections = dict(base.get("truncated_collections") or {})
        for section in sections:
            for key in TRUNCATED_KEYS_BY_SECTION.get(section, ()):
                truncated_collections[key] = False

        feature_options = {
            CONF_ANALYTICS_ENABLED: analytics_enabled,
            CONF_FULFILLMENT_ENABLED: fulfillment_enabled,
            CONF_SELLER_STANDARDS_ENABLED: seller_standards_enabled,
            CONF_FEEDBACK_ENABLED: feedback_enabled,
            CONF_MESSAGES_ENABLED: messages_enabled,
        }
        missing_by_feature = missing_scopes_for_options(granted_scopes, feature_options)
        missing_scope_features = sorted(missing_by_feature)

        trading_sections = SECTION_BUYING in sections or SECTION_SELLING in sections
        if trading_sections:
            self._api_warnings = []

        sold_items_count = base["summary"].get("sold_items_count")
        unsold_items_count = base["summary"].get("unsold_items_count")
        sold_unsold_last_fetched = _parse_optional_datetime(
            base.get("sold_unsold_last_fetched")
            or (base.get("summary") or {}).get("sold_unsold_last_fetched")
        )

        if SECTION_BUYING in sections and buying_enabled:
            (
                watched,
                bidding,
                watched_truncated,
                bidding_truncated,
            ) = await self._fetch_buying_pages()
            truncated_collections["watched"] = watched_truncated
            truncated_collections["bidding"] = bidding_truncated
            if watched_truncated or bidding_truncated:
                partial_failures.append("buying_truncated")
            _stamp_section_attempt(
                SECTION_BUYING,
                success=True,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_SELLING in sections and selling_enabled:
            previous_selling = dict(base["selling"])
            selling, selling_truncated = await self._fetch_selling_pages()
            truncated_collections["selling"] = selling_truncated
            if selling_truncated:
                partial_failures.append("selling_truncated")
            fetch_now = datetime.now(timezone.utc)
            if _should_fetch_sold_unsold(
                previous_selling,
                selling,
                sold_unsold_last_fetched,
                now=fetch_now,
            ):
                try:
                    (
                        selling_classification,
                        sold_items_count,
                        unsold_items_count,
                        sold_unsold_truncated,
                    ) = await self.async_fetch_sold_unsold_classification()
                    truncated_collections["sold_unsold"] = sold_unsold_truncated
                    if sold_unsold_truncated:
                        partial_failures.append("sold_unsold_truncated")
                    sold_unsold_last_fetched = fetch_now
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional sold/unsold classification failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("sold_unsold_classification")
                    selling_classification = {}
                    sold_items_count = None
                    unsold_items_count = None
                    truncated_collections["sold_unsold"] = True
            try:
                (
                    seller_list_views,
                    seller_list_views_truncated,
                ) = await self._fetch_seller_list_views()
                for item_id, views in seller_list_views.items():
                    if item_id in selling:
                        selling[item_id]["views"] = views
                truncated_collections["seller_list_views"] = seller_list_views_truncated
                if seller_list_views_truncated:
                    partial_failures.append("seller_list_views_truncated")
            except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug(
                    "Optional GetSellerList views failed error=%s",
                    _safe_exception_context(exc),
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
                    page_offers = active_offers_by_item_id(offers)
                    for item_id, count in page_offers.items():
                        if item_id in selling:
                            active_offer_counts[item_id] = (
                                active_offer_counts.get(item_id, 0) + count
                            )
                    _LOGGER.debug(
                        "Fetched active offers page=%s offer_item_count=%s total_pages=%s",
                        page,
                        len(page_offers),
                        total_pages,
                    )
                    page += 1
                for item_id, count in active_offer_counts.items():
                    selling[item_id]["offers"] = count
                active_offers_truncated = total_pages > BEST_OFFERS_MAX_PAGES
                truncated_collections["active_offers"] = active_offers_truncated
                if active_offers_truncated:
                    partial_failures.append("active_offers_truncated")
            except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.debug(
                    "Optional GetBestOffers failed error=%s",
                    _safe_exception_context(exc),
                )
                partial_failures.append("active_offers")
            _stamp_section_attempt(
                SECTION_SELLING,
                success=True,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_ANALYTICS in sections and analytics_enabled and selling_enabled:
            analytics_ok = True
            if CONF_ANALYTICS_ENABLED in missing_by_feature:
                partial_failures.append("analytics_views_missing_scope")
                analytics_ok = False
            elif not is_known_site_id(self.site_id):
                _LOGGER.debug(
                    "Skipping analytics views for unknown site_id=%s",
                    self.site_id,
                )
                partial_failures.append("analytics_views")
                analytics_ok = False
            else:
                try:
                    (
                        views_by_id,
                        analytics_partial,
                    ) = await self.async_fetch_analytics_views(list(selling))
                    for item_id, views in views_by_id.items():
                        if item_id in selling:
                            selling[item_id]["analytics_views_30d"] = views
                    if analytics_partial:
                        partial_failures.append("analytics_views")
                        analytics_ok = False
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional analytics views failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("analytics_views")
                    analytics_ok = False
            _stamp_section_attempt(
                SECTION_ANALYTICS,
                success=analytics_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_FULFILLMENT in sections and fulfillment_enabled:
            fulfillment_ok = True
            if CONF_FULFILLMENT_ENABLED in missing_by_feature:
                partial_failures.append("orders_missing_scope")
                partial_failures.append("payment_disputes_missing_scope")
                fulfillment_ok = False
            else:
                try:
                    orders_summary, orders_truncated = await self.async_fetch_orders()
                    seller_ops["orders"] = orders_summary
                    truncated_collections["orders"] = orders_truncated
                    if orders_truncated:
                        partial_failures.append("orders_truncated")
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional orders fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("orders")
                    fulfillment_ok = False
                try:
                    (
                        disputes_summary,
                        disputes_truncated,
                    ) = await self.async_fetch_payment_disputes()
                    seller_ops["disputes"] = disputes_summary
                    truncated_collections["payment_disputes"] = disputes_truncated
                    if disputes_truncated:
                        partial_failures.append("payment_disputes_truncated")
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional payment disputes fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("payment_disputes")
                    fulfillment_ok = False
            _stamp_section_attempt(
                SECTION_FULFILLMENT,
                success=fulfillment_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_SELLER_STANDARDS in sections and seller_standards_enabled:
            standards_ok = True
            if CONF_SELLER_STANDARDS_ENABLED in missing_by_feature:
                partial_failures.append("seller_standards_missing_scope")
                standards_ok = False
            else:
                try:
                    seller_ops["standards"] = await self.async_fetch_seller_standards()
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional seller standards fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("seller_standards")
                    standards_ok = False
            _stamp_section_attempt(
                SECTION_SELLER_STANDARDS,
                success=standards_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_FEEDBACK in sections and feedback_enabled:
            feedback_ok = True
            if CONF_FEEDBACK_ENABLED in missing_by_feature:
                partial_failures.append("feedback_missing_scope")
                feedback_ok = False
            else:
                try:
                    seller_ops["feedback"] = await self.async_fetch_feedback()
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional feedback fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("feedback")
                    feedback_ok = False
            _stamp_section_attempt(
                SECTION_FEEDBACK,
                success=feedback_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if SECTION_MESSAGES in sections and messages_enabled:
            messages_ok = True
            if CONF_MESSAGES_ENABLED in missing_by_feature:
                partial_failures.append("messages_missing_scope")
                messages_ok = False
            else:
                try:
                    (
                        messages_summary,
                        messages_truncated,
                    ) = await self.async_fetch_messages()
                    seller_ops["messages"] = messages_summary
                    truncated_collections["messages"] = messages_truncated
                    if messages_truncated:
                        partial_failures.append("messages_truncated")
                except EbayAuthError:
                    raise
                except (EbayError, aiohttp.ClientError, TimeoutError) as exc:
                    _LOGGER.debug(
                        "Optional messages fetch failed error=%s",
                        _safe_exception_context(exc),
                    )
                    partial_failures.append("messages")
                    messages_ok = False
            _stamp_section_attempt(
                SECTION_MESSAGES,
                success=messages_ok,
                section_last_attempt=section_last_attempt,
                section_last_success=section_last_success,
                section_last_fetched=section_last_fetched,
            )

        if trading_sections:
            api_warnings = dedupe_trading_warnings(self._api_warnings)
        else:
            api_warnings = list(base.get("api_warnings") or [])
        if api_warnings and "trading_warning" not in partial_failures:
            partial_failures.append("trading_warning")

        # Keep missing-scope markers accurate even when a section was skipped.
        for feature_key, category in (
            (CONF_ANALYTICS_ENABLED, "analytics_views_missing_scope"),
            (CONF_FULFILLMENT_ENABLED, "orders_missing_scope"),
            (CONF_FULFILLMENT_ENABLED, "payment_disputes_missing_scope"),
            (CONF_SELLER_STANDARDS_ENABLED, "seller_standards_missing_scope"),
            (CONF_FEEDBACK_ENABLED, "feedback_missing_scope"),
            (CONF_MESSAGES_ENABLED, "messages_missing_scope"),
        ):
            if feature_key in missing_by_feature:
                if category not in partial_failures:
                    partial_failures.append(category)
            elif category in partial_failures:
                partial_failures = [c for c in partial_failures if c != category]

        now = datetime.now(timezone.utc)
        summary = summarize_payload(
            watched, bidding, selling, ending_soon_threshold_seconds
        )
        summary.update(_seller_ops_summary_keys(seller_ops))
        summary["sold_items_count"] = sold_items_count
        summary["unsold_items_count"] = unsold_items_count
        summary["sold_unsold_window_days"] = SOLD_UNSOLD_DURATION_DAYS
        summary["sold_unsold_last_fetched"] = (
            sold_unsold_last_fetched.isoformat()
            if isinstance(sold_unsold_last_fetched, datetime)
            else sold_unsold_last_fetched
        )
        summary["missing_scopes"] = missing_by_feature
        summary["missing_scope_features"] = missing_scope_features
        summary["section_last_fetched"] = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in section_last_fetched.items()
        }
        summary["section_last_attempt"] = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in section_last_attempt.items()
        }
        summary["section_last_success"] = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in section_last_success.items()
        }
        sold_item_ids = sorted(
            item_id
            for item_id, status in selling_classification.items()
            if status == "sold"
        )
        unsold_item_ids = sorted(
            item_id
            for item_id, status in selling_classification.items()
            if status == "unsold"
        )
        _LOGGER.debug(
            "eBay API section refresh completed sections=%s watched_count=%s bidding_count=%s selling_count=%s partial_failures=%s truncated_collections=%s duration_ms=%.1f",
            sorted(sections),
            len(watched),
            len(bidding),
            len(selling),
            partial_failures,
            truncated_collections,
            _duration_ms(refresh_start),
        )
        return {
            "watched": watched,
            "bidding": bidding,
            "selling": selling,
            "seller_ops": seller_ops,
            "selling_classification": selling_classification,
            "sold_item_ids": sold_item_ids,
            "unsold_item_ids": unsold_item_ids,
            "summary": summary,
            "last_update": now,
            "last_successful_update": now,
            "partial_failures": partial_failures,
            "truncated_collections": truncated_collections,
            "api_warnings": api_warnings,
            "missing_scopes": missing_by_feature,
            "missing_scope_features": missing_scope_features,
            "section_last_fetched": section_last_fetched,
            "section_last_attempt": section_last_attempt,
            "section_last_success": section_last_success,
            "sold_unsold_last_fetched": sold_unsold_last_fetched,
            "refreshed_sections": sorted(sections),
        }


PARTIAL_FAILURE_CATEGORIES_BY_SECTION: dict[str, frozenset[str]] = {
    SECTION_BUYING: frozenset({"buying_truncated"}),
    SECTION_SELLING: frozenset(
        {
            "selling_truncated",
            "sold_unsold_truncated",
            "sold_unsold_classification",
            "seller_list_views_truncated",
            "seller_list_views",
            "active_offers_truncated",
            "active_offers",
        }
    ),
    SECTION_ANALYTICS: frozenset({"analytics_views_missing_scope", "analytics_views"}),
    SECTION_FULFILLMENT: frozenset(
        {
            "orders_missing_scope",
            "payment_disputes_missing_scope",
            "orders_truncated",
            "orders",
            "payment_disputes_truncated",
            "payment_disputes",
        }
    ),
    SECTION_SELLER_STANDARDS: frozenset(
        {"seller_standards_missing_scope", "seller_standards"}
    ),
    SECTION_FEEDBACK: frozenset({"feedback_missing_scope", "feedback"}),
    SECTION_MESSAGES: frozenset(
        {"messages_missing_scope", "messages_truncated", "messages"}
    ),
}

TRUNCATED_KEYS_BY_SECTION: dict[str, tuple[str, ...]] = {
    SECTION_BUYING: ("watched", "bidding"),
    SECTION_SELLING: ("selling", "seller_list_views", "active_offers", "sold_unsold"),
    SECTION_FULFILLMENT: ("orders", "payment_disputes"),
    SECTION_MESSAGES: ("messages",),
}

_EMPTY_TRUNCATED_COLLECTIONS = {
    "watched": False,
    "bidding": False,
    "selling": False,
    "seller_list_views": False,
    "active_offers": False,
    "orders": False,
    "payment_disputes": False,
    "messages": False,
    "sold_unsold": False,
}


def enabled_sections_for_options(
    *,
    buying_enabled: bool,
    selling_enabled: bool,
    analytics_enabled: bool,
    fulfillment_enabled: bool,
    seller_standards_enabled: bool,
    feedback_enabled: bool,
    messages_enabled: bool,
) -> set[str]:
    """Return payload sections that should be fetched for the given options."""
    sections: set[str] = set()
    if buying_enabled:
        sections.add(SECTION_BUYING)
    if selling_enabled:
        sections.add(SECTION_SELLING)
    if analytics_enabled and selling_enabled:
        sections.add(SECTION_ANALYTICS)
    if fulfillment_enabled:
        sections.add(SECTION_FULFILLMENT)
    if seller_standards_enabled:
        sections.add(SECTION_SELLER_STANDARDS)
    if feedback_enabled:
        sections.add(SECTION_FEEDBACK)
    if messages_enabled:
        sections.add(SECTION_MESSAGES)
    return sections


def _copy_seller_ops(seller_ops: dict[str, Any]) -> dict[str, Any]:
    """Shallow-copy seller-ops sections so merges do not mutate the previous payload."""
    empty = empty_seller_ops()
    copied: dict[str, Any] = {}
    for key, default in empty.items():
        value = seller_ops.get(key)
        if not isinstance(value, dict):
            copied[key] = dict(default)
            continue
        section = dict(value)
        if "by_id" in default:
            section["by_id"] = dict(value.get("by_id") or {})
        copied[key] = section
    return copied


def _stamp_section_attempt(
    section: str,
    *,
    success: bool,
    section_last_attempt: dict[str, datetime],
    section_last_success: dict[str, datetime],
    section_last_fetched: dict[str, datetime],
    when: datetime | None = None,
) -> None:
    """Record a section attempt; stamp success/fetched only when successful."""
    stamp = when or datetime.now(timezone.utc)
    section_last_attempt[section] = stamp
    if success:
        section_last_success[section] = stamp
        section_last_fetched[section] = stamp


def _parse_section_dt_map(raw: dict[str, Any] | None) -> dict[str, datetime]:
    """Parse a section→datetime map from previous payloads."""
    result: dict[str, datetime] = {}
    for key, value in (raw or {}).items():
        if isinstance(value, datetime):
            result[key] = value
        elif isinstance(value, str):
            try:
                result[key] = datetime.fromisoformat(value)
            except ValueError:
                continue
    return result


def _payload_shell(previous: dict[str, Any] | None) -> dict[str, Any]:
    """Return a mutable payload shell seeded from a previous coordinator payload."""
    if not previous:
        return {
            "watched": {},
            "bidding": {},
            "selling": {},
            "seller_ops": empty_seller_ops(),
            "selling_classification": {},
            "sold_item_ids": [],
            "unsold_item_ids": [],
            "summary": {},
            "partial_failures": [],
            "truncated_collections": dict(_EMPTY_TRUNCATED_COLLECTIONS),
            "api_warnings": [],
            "missing_scopes": {},
            "missing_scope_features": [],
            "section_last_fetched": {},
            "section_last_attempt": {},
            "section_last_success": {},
            "sold_unsold_last_fetched": None,
        }
    section_last_fetched = _parse_section_dt_map(previous.get("section_last_fetched"))
    section_last_attempt = _parse_section_dt_map(previous.get("section_last_attempt"))
    section_last_success = _parse_section_dt_map(previous.get("section_last_success"))
    if not section_last_success and section_last_fetched:
        section_last_success = dict(section_last_fetched)
    if not section_last_attempt and section_last_fetched:
        section_last_attempt = dict(section_last_fetched)
    truncated = dict(_EMPTY_TRUNCATED_COLLECTIONS)
    truncated.update(previous.get("truncated_collections") or {})
    return {
        "watched": previous.get("watched") or {},
        "bidding": previous.get("bidding") or {},
        "selling": previous.get("selling") or {},
        "seller_ops": previous.get("seller_ops") or empty_seller_ops(),
        "selling_classification": previous.get("selling_classification") or {},
        "sold_item_ids": previous.get("sold_item_ids") or [],
        "unsold_item_ids": previous.get("unsold_item_ids") or [],
        "summary": previous.get("summary") or {},
        "partial_failures": list(previous.get("partial_failures") or []),
        "truncated_collections": truncated,
        "api_warnings": list(previous.get("api_warnings") or []),
        "missing_scopes": previous.get("missing_scopes") or {},
        "missing_scope_features": list(previous.get("missing_scope_features") or []),
        "section_last_fetched": section_last_fetched,
        "section_last_attempt": section_last_attempt,
        "section_last_success": section_last_success,
        "sold_unsold_last_fetched": previous.get("sold_unsold_last_fetched")
        or (previous.get("summary") or {}).get("sold_unsold_last_fetched"),
    }


def _seller_ops_summary_keys(seller_ops: dict[str, Any]) -> dict[str, Any]:
    """Flatten seller-ops aggregates into summary sensor keys."""
    orders = seller_ops.get("orders") or {}
    disputes = seller_ops.get("disputes") or {}
    standards = seller_ops.get("standards") or {}
    feedback = seller_ops.get("feedback") or {}
    messages = seller_ops.get("messages") or {}
    return {
        "orders_awaiting_shipment": orders.get("awaiting_shipment"),
        "orders_shipping_today": orders.get("shipping_today"),
        "orders_overdue": orders.get("overdue"),
        "orders_shipped": orders.get("shipped"),
        "orders_partially_fulfilled": orders.get("partially_fulfilled"),
        "open_payment_disputes": disputes.get("open_count"),
        "seller_level": standards.get("seller_level"),
        "transaction_defect_rate": standards.get("transaction_defect_rate"),
        "late_shipment_rate": standards.get("late_shipment_rate"),
        "cases_closed_without_seller_resolution": standards.get(
            "cases_closed_without_seller_resolution"
        ),
        "item_not_received_metric": standards.get("item_not_received_rating"),
        "item_not_as_described_metric": standards.get("item_not_as_described_rating"),
        "seller_next_evaluation_date": standards.get("next_evaluation_date"),
        "seller_standard_at_risk": standards.get("at_risk"),
        "feedback_score": feedback.get("score"),
        "positive_feedback_percent": feedback.get("positive_percent"),
        "recent_positive_feedback": feedback.get("recent_positive"),
        "recent_neutral_feedback": feedback.get("recent_neutral"),
        "recent_negative_feedback": feedback.get("recent_negative"),
        "items_awaiting_feedback": feedback.get("awaiting_count"),
        "unread_conversations": messages.get("unread_count"),
        "buyer_question_conversations": messages.get("buyer_question_count"),
        "oldest_unanswered_message_hours": messages.get("oldest_unanswered_hours"),
    }
