"""Read-only Watchlist XML vs. GraphQL contract and parity comparison helpers.

Used by ``scripts/validate_watchlist_parity.py`` and unit tests in Milestone 1 to
compare Trading XML ``GetMyeBayBuying`` (``WatchList``) results with candidate
or live eBay GraphQL ``userWatchList`` responses without exposing credentials
or private account data.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import aiohttp

from ..clients.errors import (
    EbayApiError,
    EbayAuthError,
    EbayAuthTransientError,
    EbayError,
    EbayGraphQLError,
    EbayParseError,
)
from ..clients.graphql import (
    _parse_graphql_definitions,
    _strip_graphql_strings_and_comments,
    is_graphql_permission_error,
    is_graphql_rate_limit_error,
    is_graphql_token_expired,
    validate_read_only_query,
)
from .xml import (
    MAX_PAGES,
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
        "auth_code",
        "token",
        "raw_xml",
        "raw_graphql",
    }
)

_EBAY_TOKEN_PATTERN = re.compile(
    r"(Bearer\s+[^\s\"']+|v\^1\.1#[^\s\"']+)", re.IGNORECASE
)
_EBAY_IMAGE_ASSET_PATTERN = re.compile(r"/images/g/([^/]+)/s-l\d+\.[a-zA-Z0-9]+$")
_VARIABLE_DECL_RE = re.compile(
    r"\$([_A-Za-z][_0-9A-Za-z]*)\s*:\s*([^,=)]+?)(?:\s*=\s*([^,)]+))?(?=[,)])"
)

_VALIDATION_ERROR_CODES: frozenset[str] = frozenset(
    {
        "VALIDATION_ERROR",
        "GRAPHQL_VALIDATION_FAILED",
        "INVALID_QUERY",
        "BAD_USER_INPUT",
        "GRAPHQL_PARSE_FAILED",
    }
)

ALLOWED_SYMBOLIC_ERROR_CODES: frozenset[str] = frozenset(
    {
        "VALIDATION_ERROR",
        "GRAPHQL_VALIDATION_FAILED",
        "GRAPHQL_PARSE_FAILED",
        "INVALID_QUERY",
        "BAD_USER_INPUT",
        "PERSISTED_QUERY_NOT_FOUND",
        "PERSISTED_QUERY_NOT_SUPPORTED",
        "OPERATION_RESOLUTION_FAILURE",
        "INTROSPECTION_DISABLED",
        "MALFORMED_ERROR",
        "UNAUTHENTICATED",
        "UNAUTHORIZED",
        "INVALID_TOKEN",
        "TOKEN_EXPIRED",
        "INVALID_ACCESS_TOKEN",
        "ACCESS_DENIED",
        "FORBIDDEN",
        "INSUFFICIENT_SCOPE",
        "MISSING_SCOPE",
        "NOT_AUTHORIZED",
        "RATE_LIMITED",
        "TOO_MANY_REQUESTS",
        "RATE_LIMIT_EXCEEDED",
        "THROTTLED",
        "INTERNAL_SERVER_ERROR",
        "SERVICE_UNAVAILABLE",
        "SYSTEM_ERROR",
        "DOWNSTREAM_SERVICE_ERROR",
        "GATEWAY_TIMEOUT",
        "BAD_GATEWAY",
    }
)

ALLOWED_NUMERIC_ERROR_CODES: frozenset[str] = frozenset(
    {
        "1001",
        "1002",
        "1003",
        "1004",
        "1100",
        "2001",
        "2002",
        "2003",
    }
)

ALLOWED_DIAGNOSTIC_CATEGORIES: frozenset[str] = frozenset(
    {
        "REQUEST",
        "BUSINESS",
        "APPLICATION",
        "SYSTEM",
        "AUTH",
        "AUTHENTICATION",
        "AUTHORIZATION",
    }
)

ALLOWED_DIAGNOSTIC_DOMAINS: frozenset[str] = frozenset(
    {
        "BUY_WATCHLIST",
        "BUY_BIDDING",
        "BUY_OFFER",
        "BUY_BROWSE",
        "SELL_LISTING",
        "SELL_FULFILLMENT",
        "SELL_ACCOUNT",
        "SELL_ANALYTICS",
        "SELL_FINANCES",
        "SECURITY",
        "OAUTH",
        "API_GRAPHQL",
        "GRAPHQL",
        "SYSTEM",
        "REQUEST",
    }
)

REDACTED_ERROR_CODE = "UNKNOWN_CODE"
REDACTED_CATEGORY = "UNKNOWN_CATEGORY"
REDACTED_DOMAIN = "UNKNOWN_DOMAIN"


@dataclass(frozen=True)
class WatchlistContractProbe:
    """Definition of a strictly read-only GraphQL contract probe."""

    key: str
    description: str
    query: str
    operation_name: str | None = None
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
        infer_and_validate_query_operation(
            probe.query,
            operation_name=probe.operation_name,
            variables=probe.variables,
        )


def infer_and_validate_query_operation(
    query: str,
    *,
    operation_name: str | None = None,
    variables: Mapping[str, Any] | None = None,
) -> str | None:
    """Validate a read-only GraphQL query, its operation name, and required variables.

    If ``operation_name`` is ``None``, infers the single query operation name
    declared in the document (or ``None`` for an anonymous query).
    """
    validate_read_only_query(query, operation_name=operation_name)
    if variables is not None and not isinstance(variables, Mapping):
        raise ValueError("GraphQL variables must be a JSON object (mapping)")

    cleaned = _strip_graphql_strings_and_comments(query).strip()
    defs = _parse_graphql_definitions(cleaned)
    query_ops = [name for def_type, name in defs if def_type == "query"]
    resolved_operation_name = (
        operation_name if operation_name is not None else query_ops[0]
    )

    # Check non-null variable definitions on the query operation header
    header_part = cleaned.split("{", 1)[0]
    if "(" in header_part and ")" in header_part:
        var_block = header_part[header_part.find("(") : header_part.rfind(")") + 1]
        for match in _VARIABLE_DECL_RE.finditer(var_block):
            var_name = match.group(1)
            var_type = match.group(2).strip()
            default_val = match.group(3)
            if (
                var_type.endswith("!")
                and default_val is None
                and (
                    not isinstance(variables, Mapping)
                    or variables.get(var_name) is None
                )
            ):
                raise ValueError(
                    f"GraphQL query requires non-null variable '${var_name}' "
                    f"({var_type}); provide it via --variables-file"
                )

    return resolved_operation_name


def sanitize_diagnostic_token(
    value: Any,
    *,
    kind: str = "code",
) -> str | None:
    """Validate a diagnostic code, category, or domain against strict allowlists.

    Preserves only explicitly recognized symbolic eBay/GraphQL error codes,
    documented numeric error codes, and known categories/domains. Any non-empty
    unrecognized value is replaced with a generic label (``UNKNOWN_CODE``,
    ``UNKNOWN_CATEGORY``, or ``UNKNOWN_DOMAIN``) so account handles, numeric
    item IDs, or secret-like strings can never leak into shareable reports.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    if _EBAY_TOKEN_PATTERN.search(text):
        if kind == "category":
            return REDACTED_CATEGORY
        if kind == "domain":
            return REDACTED_DOMAIN
        return REDACTED_ERROR_CODE

    upper = text.upper()
    if kind == "category":
        return upper if upper in ALLOWED_DIAGNOSTIC_CATEGORIES else REDACTED_CATEGORY
    if kind == "domain":
        return upper if upper in ALLOWED_DIAGNOSTIC_DOMAINS else REDACTED_DOMAIN

    if upper in ALLOWED_SYMBOLIC_ERROR_CODES:
        return upper
    if text in ALLOWED_NUMERIC_ERROR_CODES:
        return text
    return REDACTED_ERROR_CODE


def summarize_probe_exception(exc: BaseException) -> dict[str, Any]:
    """Return a sanitized, message-free diagnostic summary for an API/probe failure."""
    raw_exc_errors = getattr(exc, "errors", None)
    if isinstance(exc, EbayGraphQLError) or (
        isinstance(exc, EbayAuthError) and bool(raw_exc_errors)
    ):
        error_codes: list[str] = []
        categories: list[str] = []
        domains: list[str] = []
        sanitized_errors: list[dict[str, str | None]] = []
        raw_err_dicts: list[dict[str, Any]] = []
        status_codes: list[int] = []
        unrecognized_code_count = 0
        unrecognized_category_count = 0
        unrecognized_domain_count = 0
        raw_upper_codes: set[str] = set()

        for err in raw_exc_errors or []:
            if not isinstance(err, dict):
                continue
            raw_err_dicts.append(err)
            ext = (
                err.get("extensions") if isinstance(err.get("extensions"), dict) else {}
            )
            raw_code = (
                err.get("code") if err.get("code") is not None else ext.get("code")
            )
            raw_error_id = (
                err.get("error_id")
                if err.get("error_id") is not None
                else (
                    err.get("errorId")
                    if err.get("errorId") is not None
                    else ext.get("errorId")
                )
            )
            raw_error_code = (
                err.get("error_code")
                or ext.get("errorCode")
                or raw_code
                or raw_error_id
            )
            raw_category = err.get("category") or ext.get("category")
            raw_domain = err.get("domain") or ext.get("domain")
            raw_status = (
                err.get("status_code")
                if err.get("status_code") is not None
                else ext.get("statusCode")
            )
            if isinstance(raw_status, int):
                status_codes.append(raw_status)

            for raw_candidate in (raw_error_code, raw_code, raw_error_id):
                if raw_candidate is not None and not isinstance(raw_candidate, bool):
                    raw_upper_codes.add(str(raw_candidate).strip().upper())

            code_tok = sanitize_diagnostic_token(raw_code, kind="code")
            err_code_tok = (
                sanitize_diagnostic_token(raw_error_code, kind="code") or code_tok
            )
            err_id_tok = sanitize_diagnostic_token(raw_error_id, kind="code")
            cat_tok = sanitize_diagnostic_token(raw_category, kind="category")
            dom_tok = sanitize_diagnostic_token(raw_domain, kind="domain")

            if REDACTED_ERROR_CODE in (code_tok, err_code_tok, err_id_tok):
                unrecognized_code_count += 1
            if cat_tok == REDACTED_CATEGORY:
                unrecognized_category_count += 1
            if dom_tok == REDACTED_DOMAIN:
                unrecognized_domain_count += 1

            for candidate_code in (err_code_tok, code_tok, err_id_tok):
                if candidate_code and candidate_code not in error_codes:
                    error_codes.append(candidate_code)
            if cat_tok and cat_tok not in categories:
                categories.append(cat_tok)
            if dom_tok and dom_tok not in domains:
                domains.append(dom_tok)

            sanitized_errors.append(
                {
                    "code": code_tok,
                    "error_code": err_code_tok,
                    "error_id": err_id_tok,
                    "category": cat_tok,
                    "domain": dom_tok,
                }
            )

        exc_status = getattr(exc, "status", None)
        inferred_status = (
            exc_status
            if isinstance(exc_status, int)
            else (status_codes[0] if status_codes else 200)
        )

        is_validation = (
            bool(raw_upper_codes & _VALIDATION_ERROR_CODES) or inferred_status == 400
        )
        is_perm = (
            bool(getattr(exc, "is_permission_error", False))
            or any(is_graphql_permission_error(e) for e in raw_err_dicts)
            or inferred_status == 403
        )
        is_expired = (
            bool(
                getattr(exc, "is_expired", False)
                and not getattr(exc, "is_permission_error", False)
            )
            or any(is_graphql_token_expired(e) for e in raw_err_dicts)
            or inferred_status == 401
        )
        is_rate_limit = (
            any(is_graphql_rate_limit_error(e) for e in raw_err_dicts)
            or inferred_status == 429
        )

        if is_validation:
            failure_class = "validation_error"
        elif is_perm:
            failure_class = "permission_or_scope_error"
        elif is_expired:
            failure_class = "auth_error"
        elif is_rate_limit:
            failure_class = "rate_limit"
        else:
            failure_class = "graphql_error"

        return {
            "error_type": type(exc).__name__,
            "failure_class": failure_class,
            "http_status": inferred_status,
            "error_count": len(raw_err_dicts),
            "unrecognized_code_count": unrecognized_code_count,
            "unrecognized_category_count": unrecognized_category_count,
            "unrecognized_domain_count": unrecognized_domain_count,
            "code": error_codes[0] if error_codes else None,
            "error_code": error_codes[0] if error_codes else None,
            "category": categories[0] if categories else None,
            "domain": domains[0] if domains else None,
            "error_codes": error_codes,
            "categories": categories,
            "domains": domains,
            "errors": sanitized_errors,
            "is_validation_error": is_validation,
            "is_auth_error": is_perm or is_expired,
            "is_scope_or_permission_error": is_perm,
            "is_rate_limit": is_rate_limit,
        }

    http_status = getattr(exc, "status", None)
    if not isinstance(http_status, int):
        http_status = None

    is_perm = bool(getattr(exc, "is_permission_error", False)) or http_status == 403
    is_auth = isinstance(exc, (EbayAuthError, EbayAuthTransientError)) or (
        http_status in (401, 403)
    )
    exc_text_lower = str(exc).lower() if isinstance(exc, EbayApiError) else ""
    is_rate_limit = (
        http_status == 429 or "rate limit" in exc_text_lower or "429" in exc_text_lower
    )
    if http_status is None and "429" in exc_text_lower:
        http_status = 429
    elif http_status is None and is_perm:
        http_status = 403

    if is_perm:
        failure_class = "permission_or_scope_error"
    elif isinstance(exc, EbayAuthTransientError):
        failure_class = "auth_transient_error"
    elif is_auth:
        failure_class = "auth_error"
    elif is_rate_limit:
        failure_class = "rate_limit"
    elif isinstance(
        exc,
        (EbayParseError, ET.ParseError, json.JSONDecodeError, ValueError, TypeError),
    ):
        failure_class = "parse_error"
    elif isinstance(exc, (aiohttp.ClientError, TimeoutError)):
        failure_class = "transport_error"
    elif isinstance(exc, EbayError):
        failure_class = "api_error"
    else:
        failure_class = "unexpected_error"

    return {
        "error_type": type(exc).__name__,
        "failure_class": failure_class,
        "http_status": http_status,
        "error_count": 1,
        "unrecognized_code_count": 0,
        "unrecognized_category_count": 0,
        "unrecognized_domain_count": 0,
        "code": None,
        "error_code": None,
        "category": None,
        "domain": None,
        "error_codes": [],
        "categories": [],
        "domains": [],
        "errors": [],
        "is_validation_error": False,
        "is_auth_error": is_auth,
        "is_scope_or_permission_error": is_perm,
        "is_rate_limit": is_rate_limit,
    }


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


def _extract_raw_items_from_container(
    container: Mapping[str, Any],
) -> tuple[str | None, list[Any] | None]:
    """Return ``(items_field_name, raw_items_list)`` from a ``userWatchList`` mapping."""
    for candidate_items_key in ("items", "watchlistItems", "nodes"):
        candidate_val = container.get(candidate_items_key)
        if isinstance(candidate_val, list):
            return candidate_items_key, candidate_val

    if isinstance(container.get("edges"), list):
        raw_edges = container["edges"]
        extracted: list[Any] = [
            edge.get("node") if isinstance(edge, Mapping) else None
            for edge in raw_edges
        ]
        return "edges", extracted

    return None, None


def _extract_has_next_page(container: Mapping[str, Any]) -> bool:
    """Return True if the container explicitly signals more pages exist."""
    if container.get("hasNextPage") is True or container.get("hasMore") is True:
        return True
    page_info = container.get("pageInfo")
    return bool(isinstance(page_info, Mapping) and page_info.get("hasNextPage") is True)


def merge_graphql_watchlist_pages(
    pages: Sequence[Mapping[str, Any]],
    *,
    max_pages: int = MAX_PAGES,
) -> dict[str, Any]:
    """Normalize and merge one or more GraphQL ``userWatchList`` page payloads.

    Tracks reported totals, raw vs. deduplicated item counts, missing/invalid item
    identities, and truncation so partial pages are never mistaken for complete
    collections.
    """
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)) or not pages:
        raise TypeError("GraphQL Watchlist pages must be a non-empty sequence")

    max_pages_reached = len(pages) > max_pages
    bounded_pages = list(pages[:max_pages])

    total_field: str | None = None
    reported_total: int | None = None
    inconsistent_total = False
    items_field: str | None = None
    items_list_present = False
    raw_item_count = 0
    composite_id_count = 0
    duplicate_id_count = 0
    missing_id_count = 0
    normalized_by_id: dict[str, dict[str, Any]] = {}
    container_keys_set: set[str] = set()
    last_has_next_page = False

    for page_data in bounded_pages:
        if not isinstance(page_data, Mapping):
            raise TypeError("GraphQL Watchlist data must be a mapping")
        container = page_data.get("userWatchList")
        if not isinstance(container, Mapping):
            raise ValueError("GraphQL payload is missing 'userWatchList' object")  # noqa: TRY004

        container_keys_set.update(str(k) for k in container)
        last_has_next_page = _extract_has_next_page(container)

        for candidate_total_key in ("total", "watchListCount", "totalCount"):
            if candidate_total_key in container:
                page_total = _to_int(container.get(candidate_total_key))
                if total_field is None:
                    total_field = candidate_total_key
                    reported_total = page_total
                elif page_total != reported_total:
                    inconsistent_total = True
                break

        page_items_field, raw_items = _extract_raw_items_from_container(container)
        if raw_items is not None:
            items_list_present = True
            if items_field is None:
                items_field = page_items_field
            raw_item_count += len(raw_items)
            for entry in raw_items:
                if not isinstance(entry, Mapping):
                    missing_id_count += 1
                    continue
                normalized = normalize_graphql_watchlist_item(entry)
                item_id = normalized.get("item_id")
                if not item_id:
                    missing_id_count += 1
                    continue
                if normalized.get("id_format") == "composite_v1":
                    composite_id_count += 1
                if item_id in normalized_by_id:
                    duplicate_id_count += 1
                normalized_by_id[item_id] = normalized

    unique_count = len(normalized_by_id)

    if not items_list_present:
        graphql_truncated = bool(reported_total is not None and reported_total > 0)
        graphql_complete = False
        pagination_status = "count_only"
    elif inconsistent_total:
        graphql_truncated = False
        graphql_complete = False
        pagination_status = "inconsistent_total"
    elif reported_total is None:
        graphql_truncated = bool(last_has_next_page or max_pages_reached)
        graphql_complete = False
        pagination_status = "truncated" if graphql_truncated else "unverified"
    elif max_pages_reached or last_has_next_page or raw_item_count < reported_total:
        graphql_truncated = True
        graphql_complete = False
        pagination_status = "truncated"
    elif duplicate_id_count > 0 or missing_id_count > 0:
        graphql_truncated = unique_count < reported_total
        graphql_complete = False
        pagination_status = "incomplete_identity"
    elif raw_item_count != reported_total or unique_count != reported_total:
        graphql_truncated = False
        graphql_complete = False
        pagination_status = "inconsistent_total"
    else:
        graphql_truncated = False
        graphql_complete = True
        pagination_status = "complete"

    return {
        "items_by_id": normalized_by_id,
        "reported_total": reported_total,
        "raw_item_count": raw_item_count,
        "total_field_name": total_field,
        "items_field_name": items_field,
        "items_list_present": items_list_present,
        "composite_id_count": composite_id_count,
        "duplicate_id_count": duplicate_id_count,
        "missing_id_count": missing_id_count,
        "page_count": len(bounded_pages),
        "graphql_truncated": graphql_truncated,
        "graphql_complete": graphql_complete,
        "pagination_status": pagination_status,
        "container_keys": sorted(container_keys_set),
    }


def normalize_graphql_watchlist_payload(
    data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize a single GraphQL ``data`` payload containing ``userWatchList``."""
    if not isinstance(data, Mapping):
        raise TypeError("GraphQL Watchlist data must be a mapping")
    return merge_graphql_watchlist_pages([data])


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
    *,
    collection_complete: bool = True,
) -> dict[str, Any]:
    """Compare one monitored item field across matched watched items.

    Tracks null-on-both-sides separately from exact matches and downgrades
    definitive parity conclusions when coverage or collection pagination is
    incomplete.
    """
    if not common_ids:
        return {
            "field": field,
            "classification": PARITY_UNVERIFIED,
            "classification_label": PARITY_LABELS[PARITY_UNVERIFIED],
            "coverage": "none",
            "compared_count": 0,
            "both_present_count": 0,
            "both_absent_count": 0,
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
    both_present_count = 0
    both_absent_count = 0
    xml_present_count = 0
    gql_present_count = 0

    for item_id in common_ids:
        xml_item = xml_items[item_id]
        gql_item = gql_items[item_id]

        if field == "item_id":
            xml_present_count += 1
            gql_present_count += 1
            both_present_count += 1
            raw_gql_id = gql_item.get("raw_item_id")
            if raw_gql_id == item_id:
                exact_count += 1
            elif gql_item.get("item_id") == item_id:
                normalized_count += 1
            else:
                mismatch_count += 1
            continue

        xml_val = xml_item.get(field)
        gql_val = gql_item.get(field)

        if xml_val is not None:
            xml_present_count += 1
        if gql_val is not None:
            gql_present_count += 1

        if xml_val is None and gql_val is None:
            both_absent_count += 1
            continue

        if xml_val is None or gql_val is None:
            mismatch_count += 1
            continue

        both_present_count += 1

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
    if both_present_count == 0:
        coverage = "none"
    elif both_present_count == total:
        coverage = "complete"
    else:
        coverage = "partial"

    if xml_present_count == 0 and gql_present_count == 0:
        classification = PARITY_UNVERIFIED
        notes = "Field was null in both XML and GraphQL across all compared items."
    elif xml_present_count > 0 and gql_present_count == 0:
        classification = PARITY_MISSING_FIELD
        notes = "Field is populated in Trading XML but missing/null in GraphQL."
    elif mismatch_count > 0:
        classification = PARITY_DIFFERENT_SEMANTICS
        notes = (
            f"{mismatch_count} of {total} compared items differed between "
            "Trading XML and GraphQL."
        )
    elif both_absent_count > 0:
        classification = PARITY_UNVERIFIED
        notes = (
            f"Matched on {both_present_count} of {total} items, but "
            f"{both_absent_count} items were null in both sources "
            "(partial coverage; full parity unverified)."
        )
    elif not collection_complete:
        classification = PARITY_UNVERIFIED
        notes = (
            f"Matched on all {both_present_count} compared sample items, but "
            "full Watchlist collection is truncated or unreconciled."
        )
    elif normalized_count == 0:
        classification = PARITY_EXACT_MATCH
        notes = "All compared items had non-null values and matched identically."
    else:
        classification = PARITY_EQUIVALENT_NORMALIZED
        notes = "All compared items had non-null values and matched after canonical normalization."

    return {
        "field": field,
        "classification": classification,
        "classification_label": PARITY_LABELS[classification],
        "coverage": coverage,
        "compared_count": total,
        "both_present_count": both_present_count,
        "both_absent_count": both_absent_count,
        "exact_match_count": exact_count,
        "normalized_match_count": normalized_count,
        "mismatch_count": mismatch_count,
        "xml_present_count": xml_present_count,
        "graphql_present_count": gql_present_count,
        "notes": notes,
    }


def _unverified_fields_report(
    *,
    reason: str,
    xml_watched_count: int | None = None,
    graphql_present_for_count: int = 0,
) -> dict[str, dict[str, Any]]:
    """Build an all-unverified field report dictionary when a source is unavailable."""
    return {
        field: {
            "field": field,
            "classification": PARITY_UNVERIFIED,
            "classification_label": PARITY_LABELS[PARITY_UNVERIFIED],
            "coverage": "none",
            "compared_count": 0,
            "both_present_count": 0,
            "both_absent_count": 0,
            "exact_match_count": 0,
            "normalized_match_count": 0,
            "mismatch_count": 0,
            "xml_present_count": (
                1 if field == "watched_count" and xml_watched_count is not None else 0
            ),
            "graphql_present_count": (
                graphql_present_for_count if field == "watched_count" else 0
            ),
            "notes": reason,
        }
        for field in MONITORED_WATCHLIST_FIELDS
    }


def compare_watchlist_snapshots(
    xml_watched: Mapping[str, Mapping[str, Any]] | None,
    graphql_payload: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    xml_available: bool = True,
    xml_truncated: bool = False,
    xml_error_summary: Mapping[str, Any] | None = None,
    graphql_error_summary: Mapping[str, Any] | None = None,
    max_pages: int = MAX_PAGES,
) -> dict[str, Any]:
    """Compare normalized XML ``watched`` items against a GraphQL Watchlist payload.

    Returns a sanitized aggregate comparison dictionary with zero private item IDs,
    titles, prices, or URLs. Fails closed when either source is unavailable,
    truncated, or has incomplete pagination/identity reconciliation.
    """
    normalized_gql: dict[str, Any] | None = None
    if graphql_payload is not None:
        if isinstance(graphql_payload, Mapping):
            normalized_gql = merge_graphql_watchlist_pages(
                [graphql_payload], max_pages=max_pages
            )
        else:
            normalized_gql = merge_graphql_watchlist_pages(
                graphql_payload, max_pages=max_pages
            )

    if not xml_available or xml_watched is None:
        gql_items = normalized_gql["items_by_id"] if normalized_gql else {}
        gql_count = len(gql_items) if normalized_gql else None
        reported_total = normalized_gql["reported_total"] if normalized_gql else None
        gql_present_count = (
            1
            if normalized_gql
            and (normalized_gql["items_list_present"] or reported_total is not None)
            else 0
        )
        return {
            "status": "unverified",
            "xml_available": False,
            "xml_truncated": xml_truncated,
            "xml_error_summary": dict(xml_error_summary) if xml_error_summary else None,
            "xml_watched_count": None,
            "graphql_watched_count": gql_count,
            "graphql_raw_items_count": (
                normalized_gql["raw_item_count"] if normalized_gql else None
            ),
            "graphql_reported_total": reported_total,
            "graphql_complete": (
                normalized_gql["graphql_complete"] if normalized_gql else False
            ),
            "graphql_truncated": (
                normalized_gql["graphql_truncated"] if normalized_gql else False
            ),
            "pagination_status": (
                normalized_gql["pagination_status"] if normalized_gql else "unverified"
            ),
            "common_items_count": 0,
            "xml_only_count": None,
            "graphql_only_count": gql_count if gql_count is not None else 0,
            "composite_ids_normalized_count": (
                normalized_gql["composite_id_count"] if normalized_gql else 0
            ),
            "duplicate_ids_count": (
                normalized_gql["duplicate_id_count"] if normalized_gql else 0
            ),
            "missing_ids_count": (
                normalized_gql["missing_id_count"] if normalized_gql else 0
            ),
            "graphql_schema_metadata": (
                {
                    "container_keys": normalized_gql["container_keys"],
                    "total_field_name": normalized_gql["total_field_name"],
                    "items_field_name": normalized_gql["items_field_name"],
                    "page_count": normalized_gql["page_count"],
                }
                if normalized_gql
                else None
            ),
            "graphql_error_summary": dict(graphql_error_summary)
            if graphql_error_summary
            else None,
            "fields": _unverified_fields_report(
                reason=(
                    "Trading XML WatchList response unavailable; parity comparison "
                    "could not be performed."
                ),
                xml_watched_count=None,
                graphql_present_for_count=gql_present_count,
            ),
        }

    xml_count = len(xml_watched)

    if normalized_gql is None:
        return {
            "status": "unverified",
            "xml_available": True,
            "xml_truncated": xml_truncated,
            "xml_error_summary": dict(xml_error_summary) if xml_error_summary else None,
            "xml_watched_count": xml_count,
            "graphql_watched_count": None,
            "graphql_raw_items_count": None,
            "graphql_reported_total": None,
            "graphql_complete": False,
            "graphql_truncated": False,
            "pagination_status": "unverified",
            "common_items_count": 0,
            "xml_only_count": xml_count,
            "graphql_only_count": 0,
            "composite_ids_normalized_count": 0,
            "duplicate_ids_count": 0,
            "missing_ids_count": 0,
            "graphql_schema_metadata": None,
            "graphql_error_summary": dict(graphql_error_summary)
            if graphql_error_summary
            else None,
            "fields": _unverified_fields_report(
                reason=(
                    "GraphQL Watchlist response unavailable; contract remains "
                    "unverified."
                ),
                xml_watched_count=xml_count,
            ),
        }

    gql_items = normalized_gql["items_by_id"]
    gql_count = len(gql_items)
    raw_item_count = normalized_gql["raw_item_count"]
    reported_total = normalized_gql["reported_total"]
    graphql_complete = normalized_gql["graphql_complete"]
    graphql_truncated = normalized_gql["graphql_truncated"]
    pagination_status = normalized_gql["pagination_status"]
    duplicate_id_count = normalized_gql["duplicate_id_count"]
    missing_id_count = normalized_gql["missing_id_count"]

    xml_ids = set(xml_watched.keys())
    gql_ids = set(gql_items.keys())
    common_ids = sorted(xml_ids & gql_ids)
    xml_only_count = len(xml_ids - gql_ids)
    gql_only_count = len(gql_ids - xml_ids)

    collection_reconciled = (
        not xml_truncated
        and graphql_complete
        and not graphql_truncated
        and duplicate_id_count == 0
        and missing_id_count == 0
        and xml_only_count == 0
        and gql_only_count == 0
        and xml_count == gql_count
    )

    if not normalized_gql["items_list_present"]:
        if (
            reported_total is not None
            and reported_total != xml_count
            and not xml_truncated
        ):
            count_classification = PARITY_DIFFERENT_SEMANTICS
            count_notes = (
                f"GraphQL count-only response reported total={reported_total}, "
                f"which differs from Trading XML count={xml_count}."
            )
        else:
            count_classification = PARITY_UNVERIFIED
            count_notes = (
                "GraphQL response returned count metadata only (no items array); "
                "item-level collection completeness is unverified."
            )
    elif collection_reconciled:
        count_classification = PARITY_EXACT_MATCH
        count_notes = (
            "XML and GraphQL returned the exact same complete watched item set."
        )
    elif xml_truncated:
        count_classification = PARITY_UNVERIFIED
        count_notes = (
            "Trading XML WatchList was truncated at MAX_PAGES; full collection "
            "parity cannot be verified."
        )
    elif graphql_truncated:
        count_classification = PARITY_UNVERIFIED
        count_notes = (
            f"GraphQL Watchlist response is truncated/partial "
            f"(retrieved {gql_count} unique items of reported_total={reported_total}); "
            "full collection parity is inconclusive until all pages are retrieved."
        )
    elif not graphql_complete:
        if (
            reported_total is not None
            and reported_total != xml_count
            and duplicate_id_count == 0
            and missing_id_count == 0
        ):
            count_classification = PARITY_DIFFERENT_SEMANTICS
            count_notes = (
                f"XML returned {xml_count} items; GraphQL returned {gql_count} items "
                f"(reported_total={reported_total}, pagination_status={pagination_status})."
            )
        else:
            count_classification = PARITY_UNVERIFIED
            count_notes = (
                f"GraphQL pagination/completeness is '{pagination_status}' "
                f"(retrieved={gql_count}, raw={raw_item_count}, "
                f"reported_total={reported_total}, duplicates={duplicate_id_count}, "
                f"missing_ids={missing_id_count}); full parity is inconclusive."
            )
    else:
        count_classification = PARITY_DIFFERENT_SEMANTICS
        count_notes = (
            f"XML returned {xml_count} items; GraphQL returned {gql_count} items "
            f"(reported_total={reported_total}, xml_only={xml_only_count}, "
            f"graphql_only={gql_only_count})."
        )

    gql_count_present = (
        1 if (normalized_gql["items_list_present"] or reported_total is not None) else 0
    )
    fields_report: dict[str, dict[str, Any]] = {
        "watched_count": {
            "field": "watched_count",
            "classification": count_classification,
            "classification_label": PARITY_LABELS[count_classification],
            "coverage": "complete"
            if collection_reconciled
            else ("partial" if gql_count_present else "none"),
            "compared_count": 1,
            "both_present_count": 1 if gql_count_present else 0,
            "both_absent_count": 0,
            "exact_match_count": 1 if count_classification == PARITY_EXACT_MATCH else 0,
            "normalized_match_count": 1
            if count_classification == PARITY_EQUIVALENT_NORMALIZED
            else 0,
            "mismatch_count": 1
            if count_classification == PARITY_DIFFERENT_SEMANTICS
            else 0,
            "xml_present_count": 1,
            "graphql_present_count": gql_count_present,
            "notes": count_notes,
        }
    }

    for field in MONITORED_WATCHLIST_FIELDS:
        if field == "watched_count":
            continue
        fields_report[field] = _compare_single_field(
            field,
            xml_watched,
            gql_items,
            common_ids,
            collection_complete=collection_reconciled,
        )

    if not normalized_gql["items_list_present"]:
        overall_status = "unverified"
    elif collection_reconciled:
        overall_status = "compared"
    else:
        overall_status = "inconclusive"

    return {
        "status": overall_status,
        "xml_available": True,
        "xml_truncated": xml_truncated,
        "xml_error_summary": dict(xml_error_summary) if xml_error_summary else None,
        "xml_watched_count": xml_count,
        "graphql_watched_count": gql_count,
        "graphql_raw_items_count": raw_item_count,
        "graphql_reported_total": reported_total,
        "graphql_complete": graphql_complete,
        "graphql_truncated": graphql_truncated,
        "pagination_status": pagination_status,
        "common_items_count": len(common_ids),
        "xml_only_count": xml_only_count,
        "graphql_only_count": gql_only_count,
        "composite_ids_normalized_count": normalized_gql["composite_id_count"],
        "duplicate_ids_count": duplicate_id_count,
        "missing_ids_count": missing_id_count,
        "graphql_schema_metadata": {
            "container_keys": normalized_gql["container_keys"],
            "total_field_name": normalized_gql["total_field_name"],
            "items_field_name": normalized_gql["items_field_name"],
            "page_count": normalized_gql["page_count"],
        },
        "graphql_error_summary": dict(graphql_error_summary)
        if graphql_error_summary
        else None,
        "fields": fields_report,
    }


def rank_graphql_probe_candidate(
    merged_preview: Mapping[str, Any],
    *,
    is_custom: bool = False,
) -> tuple[int, int, int, int, int, int]:
    """Score a normalized GraphQL probe result for parity comparison selection.

    Selection priority (highest tuple wins):
    1. Completeness and pagination validity (``complete`` > ``truncated`` > ``unverified`` > ``incomplete_identity`` > ``count_only``).
    2. Presence of usable item records (non-empty ``items_by_id`` > empty ``items`` list > count-only).
    3. Available monitored field coverage across retrieved items (0..9 fields populated).
    4. Number of unique valid item records retrieved.
    5. Presence of reported total count metadata.
    6. Custom vs. built-in probe preference (only when evidence quality is comparable).
    """
    pagination_status = str(merged_preview.get("pagination_status") or "")
    completeness_rank_map = {
        "complete": 4,
        "truncated": 3,
        "unverified": 2,
        "incomplete_identity": 1,
        "inconsistent_total": 1,
        "count_only": 0,
    }
    completeness_tier = completeness_rank_map.get(pagination_status, 0)

    items_by_id = merged_preview.get("items_by_id")
    unique_items = items_by_id if isinstance(items_by_id, Mapping) else {}
    items_list_present = bool(merged_preview.get("items_list_present"))
    if items_list_present and len(unique_items) > 0:
        items_tier = 2
    elif items_list_present:
        items_tier = 1
    else:
        items_tier = 0

    item_fields = tuple(f for f in MONITORED_WATCHLIST_FIELDS if f != "watched_count")
    populated_fields_count = 0
    if unique_items:
        for field_name in item_fields:
            if any(
                isinstance(item, Mapping) and item.get(field_name) is not None
                for item in unique_items.values()
            ):
                populated_fields_count += 1

    has_reported_total = 1 if merged_preview.get("reported_total") is not None else 0
    custom_tiebreaker = 1 if is_custom else 0

    return (
        completeness_tier,
        items_tier,
        populated_fields_count,
        len(unique_items),
        has_reported_total,
        custom_tiebreaker,
    )


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
                key_lower = key_str.lower()
                if key_lower in FORBIDDEN_REPORT_KEYS:
                    result[key_str] = "[REDACTED]"
                elif (
                    key_lower in {"code", "error_code", "error_id"} and val is not None
                ):
                    result[key_str] = sanitize_diagnostic_token(val, kind="code")
                elif key_lower == "domain" and val is not None:
                    result[key_str] = sanitize_diagnostic_token(val, kind="domain")
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
        f"- **Pagination / Completeness Status**: `{report.get('pagination_status', 'unverified')}`",
    ]
    if report.get("selected_probe_key"):
        lines.append(
            f"- **Selected Parity Probe**: `{report.get('selected_probe_key')}`"
        )
    lines.extend(
        [
            "",
            "## Collection Summary",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Trading XML Available | `{report.get('xml_available', True)}` |",
            f"| Trading XML Truncated | `{report.get('xml_truncated', False)}` |",
            f"| Trading XML Watched Items | `{report.get('xml_watched_count')}` |",
            f"| GraphQL Unique Watched Items | `{report.get('graphql_watched_count')}` |",
            f"| GraphQL Raw Items Retrieved | `{report.get('graphql_raw_items_count')}` |",
            f"| GraphQL Reported Total | `{report.get('graphql_reported_total')}` |",
            f"| GraphQL Complete | `{report.get('graphql_complete', False)}` |",
            f"| GraphQL Truncated | `{report.get('graphql_truncated', False)}` |",
            f"| Common Matched Item IDs | `{report.get('common_items_count')}` |",
            f"| XML-Only Item IDs | `{report.get('xml_only_count')}` |",
            f"| GraphQL-Only Item IDs | `{report.get('graphql_only_count')}` |",
            f"| Composite `v1|...|...` IDs Normalized | `{report.get('composite_ids_normalized_count')}` |",
            f"| Duplicate GraphQL Item IDs | `{report.get('duplicate_ids_count', 0)}` |",
            f"| Missing / Invalid GraphQL Item IDs | `{report.get('missing_ids_count', 0)}` |",
            "",
        ]
    )

    op_warnings = report.get("operational_warnings")
    if isinstance(op_warnings, list) and op_warnings:
        lines.extend(["## Operational Warnings", ""])
        for warning in op_warnings:
            lines.append(f"- {warning}")
        lines.append("")

    xml_err = report.get("xml_error_summary")
    if isinstance(xml_err, Mapping) and xml_err:
        lines.extend(
            [
                "## Trading XML Diagnostic Summary",
                "",
                f"- **Error Type**: `{xml_err.get('error_type')}`",
                f"- **Failure Class**: `{xml_err.get('failure_class')}`",
                f"- **HTTP Status**: `{xml_err.get('http_status')}`",
                "",
            ]
        )

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
                "| Field | Classification | Coverage | Compared | Both Present | Both Absent | Exact | Normalized | Mismatched | XML Present | GraphQL Present | Notes |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for field_name in MONITORED_WATCHLIST_FIELDS:
            row = fields.get(field_name)
            if not isinstance(row, Mapping):
                continue
            lines.append(
                f"| `{field_name}` | **{row.get('classification_label')}** (`{row.get('classification')}`) "
                f"| `{row.get('coverage', 'none')}` "
                f"| {row.get('compared_count', 0)} "
                f"| {row.get('both_present_count', 0)} "
                f"| {row.get('both_absent_count', 0)} "
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
