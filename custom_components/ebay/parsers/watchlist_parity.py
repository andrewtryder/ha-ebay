"""Read-only Watchlist XML vs. GraphQL contract and parity comparison helpers.

Used by ``scripts/validate_watchlist_parity.py`` and unit tests in Milestone 1 to
compare Trading XML ``GetMyeBayBuying`` (``WatchList``) results with candidate
or live eBay GraphQL ``userWatchList`` responses without exposing credentials
or private account data.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from ..clients.graphql import validate_read_only_query
from .xml import (
    _to_float,
    _to_int,
    format_seconds,
    parse_ebay_time,
    seconds_until,
)

PARITY_EXACT_MATCH = "exact_match"
PARITY_EQUIVALENT_NORMALIZED = "equivalent_after_normalization"
PARITY_DIFFERENT_SEMANTICS = "different_semantics"
PARITY_MISSING_FIELD = "missing_field"
PARITY_UNVERIFIED = "unverified"
PARITY_UNSUPPORTED = "unsupported"

PARITY_LABELS: dict[str, str] = {
    PARITY_EXACT_MATCH: "Exact match",
    PARITY_EQUIVALENT_NORMALIZED: "Equivalent after normalization",
    PARITY_DIFFERENT_SEMANTICS: "Different semantics",
    PARITY_MISSING_FIELD: "Missing field",
    PARITY_UNVERIFIED: "Unverified",
    PARITY_UNSUPPORTED: "Unsupported",
}

MONITORED_WATCHLIST_FIELDS: tuple[str, ...] = (
    "watched_count",
    "item_id",
    "title",
    "current_price",
    "currency",
    "listing_type",
    "end_time",
    "url",
    "image",
    "bid_count",
)

FORBIDDEN_REPORT_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "client_id",
        "runame",
        "authorization",
        "authorization_code",
        "code",
        "token",
        "raw_xml",
        "raw_graphql",
    }
)

_EBAY_TOKEN_PATTERN = re.compile(
    r"(Bearer\s+[^\s\"']+|v\^1\.1#[^\s\"']+)", re.IGNORECASE
)
_EBAY_IMAGE_ASSET_PATTERN = re.compile(r"/images/g/([^/]+)/s-l\d+\.[a-zA-Z0-9]+$")


@dataclass(frozen=True)
class WatchlistContractProbe:
    """Definition of a strictly read-only GraphQL contract probe."""

    key: str
    description: str
    query: str
    operation_name: str
    variables: dict[str, Any] | None = None


WATCHLIST_CONTRACT_PROBES: tuple[WatchlistContractProbe, ...] = (
    WatchlistContractProbe(
        key="typename_handshake",
        description="Minimal GraphQL root __typename handshake",
        query="query Handshake { __typename }",
        operation_name="Handshake",
    ),
    WatchlistContractProbe(
        key="user_watchlist_total",
        description="Candidate userWatchList total count probe",
        query="query GetWatchListTotal { userWatchList { total } }",
        operation_name="GetWatchListTotal",
    ),
    WatchlistContractProbe(
        key="user_watchlist_count_alt",
        description="Candidate userWatchList watchListCount/watchListMaximum probe",
        query=(
            "query GetWatchListCountAlt { "
            "userWatchList { watchListCount watchListMaximum } "
            "}"
        ),
        operation_name="GetWatchListCountAlt",
    ),
    WatchlistContractProbe(
        key="user_watchlist_items_paginated",
        description="Candidate userWatchList(limit, offset) with items[] fields",
        query=(
            "query GetUserWatchList($limit: Int, $offset: Int) {\n"
            "  userWatchList(limit: $limit, offset: $offset) {\n"
            "    total\n"
            "    items {\n"
            "      itemId\n"
            "      title\n"
            "      price {\n"
            "        value\n"
            "        currency\n"
            "      }\n"
            "      itemEndDate\n"
            "      itemWebUrl\n"
            "      image {\n"
            "        imageUrl\n"
            "      }\n"
            "      formatType\n"
            "      bidCount\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetUserWatchList",
        variables={"limit": 100, "offset": 0},
    ),
    WatchlistContractProbe(
        key="user_watchlist_watchlist_items_alt",
        description="Candidate userWatchList with watchlistItems[] collection",
        query=(
            "query GetUserWatchListAlt {\n"
            "  userWatchList {\n"
            "    total\n"
            "    offset\n"
            "    limit\n"
            "    watchlistItems {\n"
            "      itemId\n"
            "      title\n"
            "      price {\n"
            "        value\n"
            "        currency\n"
            "      }\n"
            "      image {\n"
            "        imageUrl\n"
            "      }\n"
            "      status\n"
            "      creationDate\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetUserWatchListAlt",
    ),
)


def validate_all_contract_probes() -> None:
    """Verify every built-in contract probe passes strict read-only validation."""
    for probe in WATCHLIST_CONTRACT_PROBES:
        validate_read_only_query(probe.query, operation_name=probe.operation_name)


def normalize_watchlist_item_id(raw_id: Any) -> tuple[str | None, str]:
    """Normalize an eBay item identifier and classify its source format.

    Returns ``(normalized_item_id, id_format)`` where ``id_format`` is one of
    ``"legacy_numeric"``, ``"composite_v1"``, ``"opaque_string"``, or ``"missing"``.
    """
    if raw_id is None:
        return None, "missing"
    text = str(raw_id).strip()
    if not text:
        return None, "missing"
    if text.startswith("v1|"):
        parts = text.split("|")
        if len(parts) >= 2 and parts[1].strip():
            return parts[1].strip(), "composite_v1"
    if text.isdigit():
        return text, "legacy_numeric"
    return text, "opaque_string"


def normalize_listing_type_category(value: Any) -> str | None:
    """Map Trading XML or GraphQL listing format values to a canonical category."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        lowered_items = [str(item).strip().lower() for item in value if item]
        if any("auction" in item or item == "chinese" for item in lowered_items):
            return "auction"
        if any(
            "fixed" in item or "buy_it_now" in item or "store" in item
            for item in lowered_items
        ):
            return "fixed_price"
        return lowered_items[0] if lowered_items else None
    text = str(value).strip().lower()
    if not text:
        return None
    if "auction" in text or text in {"chinese", "dutch", "live"}:
        return "auction"
    if "fixed" in text or text in {"storesfixedprice", "fixedpriceitem", "buy_it_now"}:
        return "fixed_price"
    return text


def _extract_money(node: Any) -> tuple[float | None, str | None, bool]:
    """Return ``(amount, currency, was_string_value)`` from a GraphQL price dict."""
    if not isinstance(node, Mapping):
        return None, None, False
    raw_value = node.get("value")
    if raw_value is None:
        raw_value = node.get("amount")
    was_string = isinstance(raw_value, str)
    amount = _to_float(raw_value)
    raw_currency = node.get("currency") or node.get("currencyCode")
    currency = (
        str(raw_currency).strip().upper()
        if isinstance(raw_currency, str) and raw_currency.strip()
        else None
    )
    return amount, currency, was_string


def normalize_graphql_watchlist_item(raw_item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a single candidate GraphQL watchlist item for parity comparison."""
    raw_id = (
        raw_item.get("itemId")
        or raw_item.get("legacyItemId")
        or raw_item.get("listingId")
        or raw_item.get("id")
    )
    item_id, id_format = normalize_watchlist_item_id(raw_id)

    title_raw = raw_item.get("title")
    title = (
        str(title_raw).strip()
        if isinstance(title_raw, str) and title_raw.strip()
        else None
    )

    price_node = raw_item.get("price") or raw_item.get("currentPrice")
    price, currency, price_was_string = _extract_money(price_node)

    raw_listing_type = (
        raw_item.get("formatType")
        or raw_item.get("listingType")
        or raw_item.get("buyingOptions")
    )
    if isinstance(raw_listing_type, list):
        listing_type = ", ".join(str(x) for x in raw_listing_type if x) or None
    elif isinstance(raw_listing_type, str) and raw_listing_type.strip():
        listing_type = raw_listing_type.strip()
    else:
        listing_type = None

    raw_end_time = (
        raw_item.get("itemEndDate")
        or raw_item.get("endTime")
        or raw_item.get("endDate")
    )
    end_time_str = (
        str(raw_end_time).strip()
        if isinstance(raw_end_time, str) and raw_end_time.strip()
        else None
    )
    end_time = parse_ebay_time(end_time_str)
    seconds_left = seconds_until(end_time)

    raw_url = (
        raw_item.get("itemWebUrl") or raw_item.get("itemUrl") or raw_item.get("url")
    )
    url = str(raw_url).strip() if isinstance(raw_url, str) and raw_url.strip() else None

    image_node = raw_item.get("image")
    if isinstance(image_node, Mapping):
        raw_image = image_node.get("imageUrl") or image_node.get("url")
    else:
        raw_image = (
            raw_item.get("imageUrl")
            or raw_item.get("galleryUrl")
            or (image_node if isinstance(image_node, str) else None)
        )
    image = (
        str(raw_image).strip()
        if isinstance(raw_image, str) and raw_image.strip()
        else None
    )

    bid_count_present = "bidCount" in raw_item or "bids" in raw_item
    raw_bids = (
        raw_item.get("bidCount") if "bidCount" in raw_item else raw_item.get("bids")
    )
    bid_count = _to_int(raw_bids)

    return {
        "kind": "watched",
        "item_id": item_id,
        "raw_item_id": str(raw_id).strip() if raw_id is not None else None,
        "id_format": id_format,
        "title": title,
        "listing_type": listing_type,
        "listing_type_category": normalize_listing_type_category(raw_listing_type),
        "end_time": end_time,
        "raw_end_time": end_time_str,
        "seconds_left": seconds_left,
        "time_left": format_seconds(seconds_left),
        "current_price": price,
        "price_was_string": price_was_string,
        "currency": currency,
        "bid_count": bid_count,
        "bid_count_field_present": bid_count_present,
        "url": url,
        "image": image,
    }


def normalize_graphql_watchlist_payload(
    data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize a GraphQL ``data`` payload containing ``userWatchList``."""
    if not isinstance(data, Mapping):
        raise TypeError("GraphQL Watchlist data must be a mapping")

    container = data.get("userWatchList")
    if not isinstance(container, Mapping):
        raise ValueError("GraphQL payload is missing 'userWatchList' object")  # noqa: TRY004

    total_field: str | None = None
    reported_total: int | None = None
    for candidate_total_key in ("total", "watchListCount", "totalCount"):
        if candidate_total_key in container:
            total_field = candidate_total_key
            reported_total = _to_int(container.get(candidate_total_key))
            break

    items_field: str | None = None
    raw_items: list[Any] | None = None
    for candidate_items_key in ("items", "watchlistItems", "nodes"):
        candidate_val = container.get(candidate_items_key)
        if isinstance(candidate_val, list):
            items_field = candidate_items_key
            raw_items = candidate_val
            break

    if raw_items is None and isinstance(container.get("edges"), list):
        items_field = "edges"
        raw_items = [
            edge.get("node")
            for edge in container["edges"]
            if isinstance(edge, Mapping) and isinstance(edge.get("node"), Mapping)
        ]

    normalized_by_id: dict[str, dict[str, Any]] = {}
    composite_id_count = 0
    duplicate_id_count = 0

    for entry in raw_items or []:
        if not isinstance(entry, Mapping):
            continue
        normalized = normalize_graphql_watchlist_item(entry)
        item_id = normalized.get("item_id")
        if not item_id:
            continue
        if normalized.get("id_format") == "composite_v1":
            composite_id_count += 1
        if item_id in normalized_by_id:
            duplicate_id_count += 1
        normalized_by_id[item_id] = normalized

    return {
        "items_by_id": normalized_by_id,
        "reported_total": reported_total,
        "total_field_name": total_field,
        "items_field_name": items_field,
        "items_list_present": raw_items is not None,
        "composite_id_count": composite_id_count,
        "duplicate_id_count": duplicate_id_count,
        "container_keys": sorted(str(k) for k in container),
    }


def _urls_equivalent(url_a: str, url_b: str, item_id: str) -> bool:
    """Return whether two eBay item URLs refer to the same canonical item."""
    if url_a == url_b:
        return True
    parsed_a = urlparse(url_a)
    parsed_b = urlparse(url_b)
    if not parsed_a.netloc or not parsed_b.netloc:
        return False
    host_a = parsed_a.netloc.lower().removeprefix("www.")
    host_b = parsed_b.netloc.lower().removeprefix("www.")
    if host_a != host_b:
        return False
    if item_id and f"/{item_id}" in parsed_a.path and f"/{item_id}" in parsed_b.path:
        return True
    return parsed_a.path.rstrip("/") == parsed_b.path.rstrip("/")


def _images_equivalent(img_a: str, img_b: str) -> bool:
    """Return whether two eBay image URLs refer to the same image asset."""
    if img_a == img_b:
        return True
    parsed_a = urlparse(img_a)
    parsed_b = urlparse(img_b)
    match_a = _EBAY_IMAGE_ASSET_PATTERN.search(parsed_a.path)
    match_b = _EBAY_IMAGE_ASSET_PATTERN.search(parsed_b.path)
    if match_a and match_b and match_a.group(1) == match_b.group(1):
        return True
    return (
        parsed_a.netloc.lower() == parsed_b.netloc.lower()
        and parsed_a.path == parsed_b.path
    )


def _compare_single_field(
    field: str,
    xml_items: Mapping[str, Mapping[str, Any]],
    gql_items: Mapping[str, Mapping[str, Any]],
    common_ids: list[str],
) -> dict[str, Any]:
    """Compare one monitored item field across all matched watched items."""
    if not common_ids:
        return {
            "field": field,
            "classification": PARITY_UNVERIFIED,
            "classification_label": PARITY_LABELS[PARITY_UNVERIFIED],
            "compared_count": 0,
            "exact_match_count": 0,
            "normalized_match_count": 0,
            "mismatch_count": 0,
            "xml_present_count": 0,
            "graphql_present_count": 0,
            "notes": "No common watched items available to compare this field.",
        }

    exact_count = 0
    normalized_count = 0
    mismatch_count = 0
    xml_present_count = 0
    gql_present_count = 0

    for item_id in common_ids:
        xml_item = xml_items[item_id]
        gql_item = gql_items[item_id]
        xml_val = xml_item.get(field)
        gql_val = gql_item.get(field)

        if xml_val is not None:
            xml_present_count += 1
        if gql_val is not None:
            gql_present_count += 1

        if field == "item_id":
            raw_gql_id = gql_item.get("raw_item_id")
            if raw_gql_id == item_id:
                exact_count += 1
            elif gql_item.get("item_id") == item_id:
                normalized_count += 1
            else:
                mismatch_count += 1
            continue

        if xml_val is None and gql_val is None:
            exact_count += 1
            continue

        if xml_val is None or gql_val is None:
            mismatch_count += 1
            continue

        if field == "current_price":
            if abs(float(xml_val) - float(gql_val)) <= 1e-6:
                if gql_item.get("price_was_string"):
                    normalized_count += 1
                else:
                    exact_count += 1
            else:
                mismatch_count += 1
        elif field == "currency":
            if str(xml_val) == str(gql_val):
                exact_count += 1
            elif str(xml_val).strip().upper() == str(gql_val).strip().upper():
                normalized_count += 1
            else:
                mismatch_count += 1
        elif field == "listing_type":
            if str(xml_val) == str(gql_val):
                exact_count += 1
            elif normalize_listing_type_category(
                xml_val
            ) == normalize_listing_type_category(gql_val):
                normalized_count += 1
            else:
                mismatch_count += 1
        elif field == "end_time":
            xml_dt = (
                xml_val
                if isinstance(xml_val, datetime)
                else parse_ebay_time(str(xml_val))
            )
            gql_dt = (
                gql_val
                if isinstance(gql_val, datetime)
                else parse_ebay_time(str(gql_val))
            )
            if xml_dt is not None and gql_dt is not None:
                delta_seconds = abs((xml_dt - gql_dt).total_seconds())
                if delta_seconds <= 1.0:
                    normalized_count += 1
                else:
                    mismatch_count += 1
            else:
                mismatch_count += 1
        elif field == "url":
            if str(xml_val) == str(gql_val):
                exact_count += 1
            elif _urls_equivalent(str(xml_val), str(gql_val), item_id):
                normalized_count += 1
            else:
                mismatch_count += 1
        elif field == "image":
            if str(xml_val) == str(gql_val):
                exact_count += 1
            elif _images_equivalent(str(xml_val), str(gql_val)):
                normalized_count += 1
            else:
                mismatch_count += 1
        elif field == "bid_count":
            if int(xml_val) == int(gql_val):
                exact_count += 1
            else:
                mismatch_count += 1
        else:
            if xml_val == gql_val:
                exact_count += 1
            elif str(xml_val).strip() == str(gql_val).strip():
                normalized_count += 1
            else:
                mismatch_count += 1

    total = len(common_ids)
    if xml_present_count == 0 and gql_present_count == 0:
        classification = PARITY_UNVERIFIED
        notes = "Field was null in both XML and GraphQL across all compared items."
    elif xml_present_count > 0 and gql_present_count == 0:
        classification = PARITY_MISSING_FIELD
        notes = "Field is populated in Trading XML but missing/null in GraphQL."
    elif mismatch_count == 0 and normalized_count == 0:
        classification = PARITY_EXACT_MATCH
        notes = "All compared items matched identically."
    elif mismatch_count == 0 and normalized_count > 0:
        classification = PARITY_EQUIVALENT_NORMALIZED
        notes = "All compared items matched after canonical normalization."
    else:
        classification = PARITY_DIFFERENT_SEMANTICS
        notes = (
            f"{mismatch_count} of {total} compared items differed between "
            "Trading XML and GraphQL."
        )

    return {
        "field": field,
        "classification": classification,
        "classification_label": PARITY_LABELS[classification],
        "compared_count": total,
        "exact_match_count": exact_count,
        "normalized_match_count": normalized_count,
        "mismatch_count": mismatch_count,
        "xml_present_count": xml_present_count,
        "graphql_present_count": gql_present_count,
        "notes": notes,
    }


def compare_watchlist_snapshots(
    xml_watched: Mapping[str, Mapping[str, Any]],
    graphql_payload: Mapping[str, Any] | None,
    *,
    graphql_error_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare normalized XML ``watched`` items against a GraphQL Watchlist payload.

    Returns a sanitized aggregate comparison dictionary with zero private item IDs,
    titles, prices, or URLs.
    """
    xml_count = len(xml_watched)

    if graphql_payload is None:
        fields_report = {
            field: {
                "field": field,
                "classification": PARITY_UNVERIFIED,
                "classification_label": PARITY_LABELS[PARITY_UNVERIFIED],
                "compared_count": 0,
                "exact_match_count": 0,
                "normalized_match_count": 0,
                "mismatch_count": 0,
                "xml_present_count": xml_count if field == "watched_count" else 0,
                "graphql_present_count": 0,
                "notes": (
                    "GraphQL Watchlist response unavailable; contract remains "
                    "unverified."
                ),
            }
            for field in MONITORED_WATCHLIST_FIELDS
        }
        return {
            "status": "unverified",
            "xml_watched_count": xml_count,
            "graphql_watched_count": None,
            "graphql_reported_total": None,
            "common_items_count": 0,
            "xml_only_count": xml_count,
            "graphql_only_count": 0,
            "composite_ids_normalized_count": 0,
            "duplicate_ids_count": 0,
            "graphql_schema_metadata": None,
            "graphql_error_summary": dict(graphql_error_summary)
            if graphql_error_summary
            else None,
            "fields": fields_report,
        }

    normalized_gql = normalize_graphql_watchlist_payload(graphql_payload)
    gql_items = normalized_gql["items_by_id"]
    gql_count = len(gql_items)
    reported_total = normalized_gql["reported_total"]

    xml_ids = set(xml_watched.keys())
    gql_ids = set(gql_items.keys())
    common_ids = sorted(xml_ids & gql_ids)
    xml_only_count = len(xml_ids - gql_ids)
    gql_only_count = len(gql_ids - xml_ids)

    if not normalized_gql["items_list_present"]:
        count_classification = (
            PARITY_EXACT_MATCH
            if reported_total is not None and reported_total == xml_count
            else PARITY_UNVERIFIED
        )
        count_notes = "GraphQL response returned count metadata only (no items array)."
    elif xml_count == gql_count and xml_only_count == 0 and gql_only_count == 0:
        count_classification = PARITY_EXACT_MATCH
        count_notes = "XML and GraphQL returned the exact same watched item set."
    elif reported_total == xml_count and gql_count != xml_count:
        count_classification = PARITY_EQUIVALENT_NORMALIZED
        count_notes = (
            "Reported total matches XML count, but page size differed from "
            "fetched item count."
        )
    else:
        count_classification = PARITY_DIFFERENT_SEMANTICS
        count_notes = (
            f"XML returned {xml_count} items; GraphQL returned {gql_count} items "
            f"(reported_total={reported_total})."
        )

    fields_report: dict[str, dict[str, Any]] = {
        "watched_count": {
            "field": "watched_count",
            "classification": count_classification,
            "classification_label": PARITY_LABELS[count_classification],
            "compared_count": 1,
            "exact_match_count": 1 if count_classification == PARITY_EXACT_MATCH else 0,
            "normalized_match_count": 1
            if count_classification == PARITY_EQUIVALENT_NORMALIZED
            else 0,
            "mismatch_count": 1
            if count_classification == PARITY_DIFFERENT_SEMANTICS
            else 0,
            "xml_present_count": 1,
            "graphql_present_count": 1
            if (normalized_gql["items_list_present"] or reported_total is not None)
            else 0,
            "notes": count_notes,
        }
    }

    for field in MONITORED_WATCHLIST_FIELDS:
        if field == "watched_count":
            continue
        fields_report[field] = _compare_single_field(
            field, xml_watched, gql_items, common_ids
        )

    return {
        "status": "compared"
        if common_ids or (xml_count == 0 and gql_count == 0)
        else "partial",
        "xml_watched_count": xml_count,
        "graphql_watched_count": gql_count,
        "graphql_reported_total": reported_total,
        "common_items_count": len(common_ids),
        "xml_only_count": xml_only_count,
        "graphql_only_count": gql_only_count,
        "composite_ids_normalized_count": normalized_gql["composite_id_count"],
        "duplicate_ids_count": normalized_gql["duplicate_id_count"],
        "graphql_schema_metadata": {
            "container_keys": normalized_gql["container_keys"],
            "total_field_name": normalized_gql["total_field_name"],
            "items_field_name": normalized_gql["items_field_name"],
        },
        "graphql_error_summary": dict(graphql_error_summary)
        if graphql_error_summary
        else None,
        "fields": fields_report,
    }


def sanitize_parity_report(
    report: Mapping[str, Any],
    *,
    forbidden_values: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a deep-copied report with secrets and private strings redacted."""
    secret_tokens = [
        str(val).strip()
        for val in forbidden_values
        if isinstance(val, str) and len(str(val).strip()) >= 4
    ]

    def _clean_str(text: str) -> str:
        cleaned = _EBAY_TOKEN_PATTERN.sub("[REDACTED]", text)
        for token in secret_tokens:
            if token in cleaned:
                cleaned = cleaned.replace(token, "[REDACTED]")
        return cleaned

    def _walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            result: dict[str, Any] = {}
            for key, val in node.items():
                key_str = str(key)
                if key_str.lower() in FORBIDDEN_REPORT_KEYS:
                    result[key_str] = "[REDACTED]"
                else:
                    result[key_str] = _walk(val)
            return result
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, tuple):
            return [_walk(item) for item in node]
        if isinstance(node, str):
            return _clean_str(node)
        return node

    sanitized = _walk(report)
    return dict(sanitized) if isinstance(sanitized, Mapping) else {}


def format_parity_report_markdown(report: Mapping[str, Any]) -> str:
    """Format a sanitized Watchlist parity report as Markdown."""
    lines: list[str] = [
        "# eBay Watchlist XML vs. GraphQL Parity Report",
        "",
        f"- **Mode**: `{report.get('mode', 'unknown')}`",
        f"- **Environment**: `{report.get('environment', 'unknown')}`",
        f"- **Site ID / Marketplace**: `{report.get('site_id', '0')}` (`{report.get('marketplace_id', 'EBAY_US')}`)",
        f"- **Comparison Status**: `{parity_status(report)}`",
        "",
        "## Collection Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Trading XML Watched Items | `{report.get('xml_watched_count')}` |",
        f"| GraphQL Watched Items | `{report.get('graphql_watched_count')}` |",
        f"| GraphQL Reported Total | `{report.get('graphql_reported_total')}` |",
        f"| Common Matched Item IDs | `{report.get('common_items_count')}` |",
        f"| XML-Only Item IDs | `{report.get('xml_only_count')}` |",
        f"| GraphQL-Only Item IDs | `{report.get('graphql_only_count')}` |",
        f"| Composite `v1|...|...` IDs Normalized | `{report.get('composite_ids_normalized_count')}` |",
        "",
    ]

    probes = report.get("probes")
    if isinstance(probes, list) and probes:
        lines.extend(
            [
                "## Contract Probes",
                "",
                "| Probe | Outcome | Details |",
                "|---|---|---|",
            ]
        )
        for probe in probes:
            if not isinstance(probe, Mapping):
                continue
            lines.append(
                f"| `{probe.get('key')}` | `{probe.get('outcome')}` | {probe.get('summary', '')} |"
            )
        lines.append("")

    fields = report.get("fields")
    if isinstance(fields, Mapping) and fields:
        lines.extend(
            [
                "## Field-Level Parity Matrix",
                "",
                "| Field | Classification | Compared | Exact | Normalized | Mismatched | XML Present | GraphQL Present | Notes |",
                "|---|---|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for field_name in MONITORED_WATCHLIST_FIELDS:
            row = fields.get(field_name)
            if not isinstance(row, Mapping):
                continue
            lines.append(
                f"| `{field_name}` | **{row.get('classification_label')}** (`{row.get('classification')}`) "
                f"| {row.get('compared_count', 0)} "
                f"| {row.get('exact_match_count', 0)} "
                f"| {row.get('normalized_match_count', 0)} "
                f"| {row.get('mismatch_count', 0)} "
                f"| {row.get('xml_present_count', 0)} "
                f"| {row.get('graphql_present_count', 0)} "
                f"| {row.get('notes', '')} |"
            )
        lines.append("")

    return "\n".join(lines)


def parity_status(report: Mapping[str, Any]) -> str:
    """Return the top-level status string from a parity report."""
    return str(report.get("status") or "unverified")
