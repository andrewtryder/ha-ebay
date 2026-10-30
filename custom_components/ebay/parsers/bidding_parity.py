"""Read-only Auction and Bidding XML vs. GraphQL contract and parity comparison helpers.

Used by ``scripts/validate_bidding_parity.py`` and unit tests in Milestone 4 to
compare Trading XML ``GetMyeBayBuying`` (``BidList``) results with candidate or
live eBay GraphQL ``userBiddingActivity`` responses without exposing
credentials, private bid amounts, or private account data.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
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
    _VALIDATION_ERROR_CODES,
    _parse_graphql_definitions,
    _strip_graphql_strings_and_comments,
    is_graphql_permission_error,
    is_graphql_rate_limit_error,
    is_graphql_token_expired,
    is_graphql_validation_error,
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

MONITORED_BIDDING_FIELDS: tuple[str, ...] = (
    "bidding_count",
    "item_id",
    "title",
    "current_price",
    "currency",
    "max_bid",
    "winning",
    "bid_count",
    "end_time",
    "listing_type",
    "url",
    "image",
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
        "api_key",
        "apikey",
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

ALLOWED_SYMBOLIC_ERROR_CODES: frozenset[str] = frozenset(
    {
        "VALIDATION_ERROR",
        "GRAPHQL_VALIDATION_FAILED",
        "GRAPHQL_VALIDATION_ERROR",
        "FIELD_UNDEFINED",
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
        "1005",
        "1006",
        "1007",
        "1008",
        "1009",
        "11001",
        "11002",
        "12001",
        "12002",
        "17",
        "931",
        "21916984",
        "21916985",
        "21917053",
    }
)

ALLOWED_ERROR_CATEGORIES: frozenset[str] = frozenset(
    {
        "APPLICATION",
        "BUSINESS",
        "REQUEST",
        "SYSTEM",
    }
)

ALLOWED_ERROR_DOMAINS: frozenset[str] = frozenset(
    {
        "BUY_AUCTION",
        "BUY_BIDDING",
        "BUY_OFFER",
        "BUY_WATCHLIST",
        "CATALOG",
        "COMMERCE",
        "IDENTITY",
        "INVENTORY",
        "MARKETPLACE",
        "OAUTH",
        "ORDER",
        "SECURITY",
        "USER",
    }
)

CANONICAL_GRAPHQL_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "__typename",
        "userBiddingActivity",
        "biddingActivity",
        "userBidding",
        "bidding",
        "auctionBidding",
        "itemBiddingActivity",
        "userWatchList",
        "viewer",
        "user",
        "me",
    }
)

CANONICAL_BIDDING_CONTAINER_KEYS: frozenset[str] = frozenset(
    {
        "userBiddingActivity",
        "biddingActivity",
        "userBidding",
        "bidding",
        "auctionBidding",
        "itemBiddingActivity",
    }
)

CANONICAL_BIDDING_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "total",
        "biddingCount",
        "activeBiddingCount",
        "totalCount",
        "count",
        "offset",
        "limit",
        "items",
        "bids",
        "biddingActivity",
        "nodes",
        "edges",
        "pageInfo",
        "itemId",
        "legacyItemId",
        "listingId",
        "id",
        "title",
        "currentPrice",
        "price",
        "maxBid",
        "buyerMaxBid",
        "maxAmount",
        "userMaxBid",
        "winning",
        "isWinning",
        "highBidder",
        "bidCount",
        "itemEndDate",
        "endTime",
        "endDate",
        "itemWebUrl",
        "itemUrl",
        "url",
        "image",
        "imageUrl",
        "galleryUrl",
        "formatType",
        "listingType",
        "buyingOptions",
        "biddingStatus",
        "auctionStatus",
        "status",
    }
)

CANONICAL_BIDDING_OPERATION_NAMES: frozenset[str] = frozenset(
    {
        "Handshake",
        "GetBiddingTotal",
        "GetBiddingCountAlt",
        "GetUserBiddingActivity",
        "GetUserBiddingActivityAlt",
        "GetItemBiddingActivity",
        "OfflineFixture",
    }
)

CANONICAL_BIDDING_PROBE_KEYS: frozenset[str] = frozenset(
    {
        "typename_handshake",
        "user_bidding_activity_total",
        "user_bidding_activity_count_alt",
        "user_bidding_activity_paginated",
        "user_bidding_activity_bids_alt",
        "item_bidding_activity_probe",
        "offline_fixture",
        "custom_query_file",
    }
)

REDACTED_ERROR_CODE = "UNKNOWN_CODE"
REDACTED_CATEGORY = "UNKNOWN_CATEGORY"
REDACTED_DOMAIN = "UNKNOWN_DOMAIN"


def sanitize_graphql_operation_name(name: Any) -> str | None:
    """Sanitize a GraphQL operation name against canonical allowlists.

    Preserves canonical built-in operation names and replaces custom,
    unknown, or caller-controlled operation names (which may contain private
    account handles or identifiers) with '[CUSTOM_OPERATION]'.
    """
    if name is None or isinstance(name, bool):
        return None
    raw = str(name).strip()
    if not raw or raw in ("-", "None"):
        return None
    if raw in CANONICAL_BIDDING_OPERATION_NAMES or raw == "[CUSTOM_OPERATION]":
        return raw
    return "[CUSTOM_OPERATION]"


def sanitize_bidding_probe_key(key: Any) -> str:
    """Sanitize a probe key against canonical allowlists."""
    if key is None or isinstance(key, bool):
        return "[CUSTOM_PROBE]"
    raw = str(key).strip()
    if not raw or raw in ("-", "None"):
        return "[CUSTOM_PROBE]"
    if raw in CANONICAL_BIDDING_PROBE_KEYS:
        return raw
    return "[CUSTOM_PROBE]"


def sanitize_graphql_field_name(
    name: Any,
    *,
    kind: str = "field",
) -> str:
    """Sanitize a GraphQL field or container name against canonical allowlists.

    Replaces unknown caller aliases or arbitrary property names with generic
    placeholders like '[ALIASED_FIELD]' or '[ALIASED_CONTAINER]' to prevent
    private account handles or identifiers from leaking through aliases.
    """
    if name is None or isinstance(name, bool):
        return "[UNKNOWN]"
    raw = str(name).strip()
    if not raw:
        return "[UNKNOWN]"
    if kind == "top_level":
        if raw in CANONICAL_GRAPHQL_TOP_LEVEL_KEYS:
            return raw
        return "[ALIASED_TOP_LEVEL_FIELD]"
    if kind == "container":
        if raw in CANONICAL_BIDDING_CONTAINER_KEYS:
            return raw
        return "[ALIASED_CONTAINER]"
    if kind == "field":
        if raw in CANONICAL_BIDDING_FIELD_NAMES:
            return raw
        return "[ALIASED_FIELD]"
    return "[ALIASED_FIELD]"


def sanitize_diagnostic_token(value: Any, *, kind: str = "code") -> str | None:
    """Classify and return a privacy-safe error code, category, or domain."""
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    upper = raw.upper()

    if kind in ("code", "error_code"):
        if upper in ALLOWED_SYMBOLIC_ERROR_CODES:
            return upper
        if raw in ALLOWED_NUMERIC_ERROR_CODES:
            return raw
        return REDACTED_ERROR_CODE

    if kind == "category":
        if upper in ALLOWED_ERROR_CATEGORIES:
            return upper
        return REDACTED_CATEGORY

    if kind == "domain":
        if upper in ALLOWED_ERROR_DOMAINS:
            return upper
        return REDACTED_DOMAIN

    return REDACTED_ERROR_CODE


@dataclass(frozen=True)
class BiddingContractProbe:
    """Definition of a strictly read-only GraphQL Auction/Bidding contract probe."""

    key: str
    description: str
    query: str
    operation_name: str | None = None
    variables: dict[str, Any] | None = None


BIDDING_CONTRACT_PROBES: tuple[BiddingContractProbe, ...] = (
    BiddingContractProbe(
        key="typename_handshake",
        description="Minimal GraphQL root __typename handshake",
        query="query Handshake { __typename }",
        operation_name="Handshake",
    ),
    BiddingContractProbe(
        key="user_bidding_activity_total",
        description="Candidate userBiddingActivity total count probe",
        query="query GetBiddingTotal { userBiddingActivity { total } }",
        operation_name="GetBiddingTotal",
    ),
    BiddingContractProbe(
        key="user_bidding_activity_count_alt",
        description="Candidate userBiddingActivity biddingCount/activeCount probe",
        query=(
            "query GetBiddingCountAlt { "
            "userBiddingActivity { biddingCount activeBiddingCount } "
            "}"
        ),
        operation_name="GetBiddingCountAlt",
    ),
    BiddingContractProbe(
        key="user_bidding_activity_paginated",
        description="Candidate userBiddingActivity(limit, offset) with items[] fields",
        query=(
            "query GetUserBiddingActivity($limit: Int, $offset: Int) {\n"
            "  userBiddingActivity(limit: $limit, offset: $offset) {\n"
            "    total\n"
            "    items {\n"
            "      itemId\n"
            "      title\n"
            "      currentPrice {\n"
            "        value\n"
            "        currency\n"
            "      }\n"
            "      maxBid {\n"
            "        value\n"
            "        currency\n"
            "      }\n"
            "      winning\n"
            "      bidCount\n"
            "      itemEndDate\n"
            "      itemWebUrl\n"
            "      image {\n"
            "        imageUrl\n"
            "      }\n"
            "      formatType\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetUserBiddingActivity",
        variables={"limit": 100, "offset": 0},
    ),
    BiddingContractProbe(
        key="user_bidding_activity_bids_alt",
        description="Candidate userBiddingActivity with bids[] collection",
        query=(
            "query GetUserBiddingActivityAlt {\n"
            "  userBiddingActivity {\n"
            "    total\n"
            "    offset\n"
            "    limit\n"
            "    bids {\n"
            "      itemId\n"
            "      title\n"
            "      currentPrice {\n"
            "        value\n"
            "        currency\n"
            "      }\n"
            "      maxBid {\n"
            "        value\n"
            "        currency\n"
            "      }\n"
            "      highBidder\n"
            "      image {\n"
            "        imageUrl\n"
            "      }\n"
            "      status\n"
            "      endDate\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetUserBiddingActivityAlt",
    ),
    BiddingContractProbe(
        key="item_bidding_activity_probe",
        description="Supplementary itemBiddingActivity(itemId) probe",
        query=(
            "query GetItemBiddingActivity($itemId: String!) {\n"
            "  itemBiddingActivity(itemId: $itemId) {\n"
            "    itemId\n"
            "    currentPrice {\n"
            "      value\n"
            "      currency\n"
            "    }\n"
            "    bidCount\n"
            "    highBidder\n"
            "  }\n"
            "}"
        ),
        operation_name="GetItemBiddingActivity",
        variables={"itemId": "000000000000"},
    ),
)


def infer_and_validate_query_operation(
    query: str,
    operation_name: str | None = None,
) -> str | None:
    """Validate a read-only query and return its inferred operation name."""
    validate_read_only_query(query, operation_name=operation_name)
    cleaned = _strip_graphql_strings_and_comments(query).strip()
    defs = _parse_graphql_definitions(cleaned)
    query_ops = [(t, n) for t, n in defs if t == "query"]
    if not query_ops:
        return operation_name
    _, op_name = query_ops[0]
    return op_name or operation_name


def validate_all_bidding_contract_probes() -> None:
    """Verify every built-in contract probe passes strict read-only validation."""
    for probe in BIDDING_CONTRACT_PROBES:
        infer_and_validate_query_operation(
            probe.query,
            operation_name=probe.operation_name,
        )


def _extract_declared_variables(query: str) -> dict[str, tuple[str, str | None]]:
    """Extract variable declarations: ``{name: (type_name, default_raw)}``."""
    cleaned = _strip_graphql_strings_and_comments(query)
    paren_start = cleaned.find("(")
    brace_start = cleaned.find("{")
    if paren_start == -1 or (brace_start != -1 and paren_start > brace_start):
        return {}
    paren_depth = 0
    paren_slice: list[str] = []
    for c in cleaned[paren_start:]:
        if c == "(":
            paren_depth += 1
            if paren_depth > 1:
                paren_slice.append(c)
        elif c == ")":
            paren_depth -= 1
            if paren_depth == 0:
                break
            paren_slice.append(c)
        else:
            paren_slice.append(c)
    decl_str = f"({''.join(paren_slice)})"
    declared: dict[str, tuple[str, str | None]] = {}
    for match in _VARIABLE_DECL_RE.finditer(decl_str):
        name = match.group(1)
        type_name = match.group(2).strip()
        default_val = match.group(3).strip() if match.group(3) else None
        declared[name] = (type_name, default_val)
    return declared


def parse_query_variables(
    query: str,
    raw_variables: Mapping[str, Any] | None = None,
    *,
    default_limit: int = 100,
    default_offset: int = 0,
) -> dict[str, Any] | None:
    """Return validated GraphQL variables aligned with query declarations."""
    declared = _extract_declared_variables(query)
    if not declared:
        if raw_variables:
            return dict(raw_variables)
        return None

    result: dict[str, Any] = {}
    passed = dict(raw_variables or {})
    for var_name, (type_name, default_raw) in declared.items():
        if var_name in passed:
            result[var_name] = passed[var_name]
            continue
        is_required = type_name.endswith("!")
        base_type = type_name.rstrip("!").strip()
        lower_name = var_name.lower()
        if "limit" in lower_name and base_type in ("Int", "Int!"):
            result[var_name] = default_limit
        elif "offset" in lower_name and base_type in ("Int", "Int!"):
            result[var_name] = default_offset
        elif default_raw is not None:
            try:
                result[var_name] = json.loads(default_raw)
            except json.JSONDecodeError:
                result[var_name] = default_raw
        elif not is_required:
            continue
        else:
            result[var_name] = None
    return result


def summarize_bidding_probe_exception(exc: BaseException) -> dict[str, Any]:
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

        if isinstance(raw_exc_errors, (str, Mapping)):
            raw_error_iterable: list[Any] = [raw_exc_errors]
        elif isinstance(raw_exc_errors, Iterable):
            raw_error_iterable = list(raw_exc_errors)
        else:
            raw_error_iterable = []

        for err in raw_error_iterable:
            if not isinstance(err, dict):
                unrecognized_code_count += 1
                if "MALFORMED_ERROR" not in error_codes:
                    error_codes.append("MALFORMED_ERROR")
                sanitized_errors.append(
                    {
                        "code": "MALFORMED_ERROR",
                        "error_code": "MALFORMED_ERROR",
                        "error_id": None,
                        "category": None,
                        "domain": None,
                    }
                )
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

        is_validation = bool(raw_upper_codes & _VALIDATION_ERROR_CODES) or any(
            is_graphql_validation_error(e) for e in raw_err_dicts
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

        if is_perm:
            failure_class = "permission_or_scope_error"
        elif is_expired:
            failure_class = "auth_error"
        elif is_rate_limit:
            failure_class = "rate_limit"
        elif is_validation:
            failure_class = "validation_error"
        elif inferred_status == 400:
            failure_class = "bad_request"
        else:
            failure_class = "graphql_error"

        return {
            "error_type": type(exc).__name__,
            "failure_class": failure_class,
            "http_status": inferred_status,
            "error_count": len(sanitized_errors),
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
    elif http_status == 400:
        failure_class = "bad_request"
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


def normalize_bidding_item_id(raw_id: Any) -> tuple[str, str]:
    """Normalize a legacy or modern composite eBay item ID to ``(canonical_id, format)``."""
    if raw_id is None:
        return "", "missing"
    text = str(raw_id).strip()
    if not text:
        return "", "empty"
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


def _to_bool_or_none(value: Any) -> bool | None:
    """Parse a boolean value or return None if absent or indeterminate."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "winning", "high_bidder"):
        return True
    if text in ("false", "0", "no", "outbid", "losing"):
        return False
    return None


def normalize_graphql_bidding_item(raw_item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a single candidate GraphQL bidding activity item for parity comparison."""
    raw_id = (
        raw_item.get("itemId")
        or raw_item.get("legacyItemId")
        or raw_item.get("listingId")
        or raw_item.get("id")
    )
    item_id, id_format = normalize_bidding_item_id(raw_id)

    title_raw = raw_item.get("title")
    title = (
        str(title_raw).strip()
        if isinstance(title_raw, str) and title_raw.strip()
        else None
    )

    price_node = raw_item.get("currentPrice") or raw_item.get("price")
    price, currency, price_was_string = _extract_money(price_node)

    max_bid_node = (
        raw_item.get("maxBid")
        or raw_item.get("buyerMaxBid")
        or raw_item.get("maxAmount")
        or raw_item.get("userMaxBid")
    )
    max_bid, max_bid_currency, max_bid_was_string = _extract_money(max_bid_node)
    if currency is None and max_bid_currency is not None:
        currency = max_bid_currency

    winning_raw = (
        raw_item.get("winning")
        if "winning" in raw_item
        else (
            raw_item.get("isWinning")
            if "isWinning" in raw_item
            else raw_item.get("highBidder")
        )
    )
    winning = _to_bool_or_none(winning_raw)

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

    raw_status = (
        raw_item.get("biddingStatus")
        or raw_item.get("auctionStatus")
        or raw_item.get("status")
    )
    status_str = (
        str(raw_status).strip().lower()
        if isinstance(raw_status, str) and raw_status.strip()
        else None
    )

    return {
        "kind": "bidding",
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
        "max_bid": max_bid,
        "max_bid_was_string": max_bid_was_string,
        "winning": winning,
        "currency": currency,
        "bid_count": bid_count,
        "bid_count_field_present": bid_count_present,
        "status": status_str,
        "url": url,
        "image": image,
    }


def _extract_raw_bidding_items_from_container(
    container: Mapping[str, Any],
) -> tuple[str | None, list[Any] | None]:
    """Return ``(items_field_name, raw_items_list)`` from a bidding mapping."""
    for candidate_items_key in ("items", "bids", "biddingActivity", "nodes"):
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


def _find_bidding_container(data: Mapping[str, Any]) -> tuple[str | None, Any]:
    """Find the top-level or nested container mapping in a GraphQL response."""
    for top_key in (
        "userBiddingActivity",
        "biddingActivity",
        "userBidding",
        "bidding",
        "auctionBidding",
        "itemBiddingActivity",
    ):
        if top_key in data:
            return top_key, data[top_key]
    if len(data) == 1:
        only_key = next(iter(data.keys()))
        sanitized_key = sanitize_graphql_field_name(only_key, kind="container")
        return sanitized_key, data[only_key]
    return None, None


def merge_graphql_bidding_pages(
    pages: Sequence[Mapping[str, Any]],
    *,
    max_pages: int = MAX_PAGES,
) -> dict[str, Any]:
    """Merge up to ``max_pages`` candidate GraphQL bidding responses."""
    if not pages:
        return {
            "items_by_id": {},
            "reported_total": None,
            "raw_item_count": 0,
            "total_field_name": None,
            "items_field_name": None,
            "items_list_present": False,
            "composite_id_count": 0,
            "duplicate_id_count": 0,
            "missing_id_count": 0,
            "page_count": 0,
            "graphql_truncated": False,
            "graphql_complete": False,
            "pagination_status": "empty",
            "container_keys": [],
        }

    bounded_pages = list(pages[:max_pages])
    normalized_by_id: dict[str, dict[str, Any]] = {}
    composite_id_count = 0
    duplicate_id_count = 0
    missing_id_count = 0
    raw_item_count = 0
    reported_total: int | None = None
    total_field: str | None = None
    items_field: str | None = None
    items_list_present = False
    container_keys_set: set[str] = set()
    last_has_next_page = False
    max_pages_reached = len(pages) > max_pages

    for page_idx, page in enumerate(bounded_pages):
        if not isinstance(page, Mapping):
            continue
        container_key, container = _find_bidding_container(page)
        if container_key:
            container_keys_set.add(
                sanitize_graphql_field_name(container_key, kind="container")
            )
        if not isinstance(container, Mapping):
            continue

        for total_cand_key in (
            "total",
            "biddingCount",
            "activeBiddingCount",
            "totalCount",
            "count",
        ):
            if total_cand_key in container and reported_total is None:
                reported_total = _to_int(container.get(total_cand_key))
                total_field = sanitize_graphql_field_name(total_cand_key, kind="field")
                break

        page_info = container.get("pageInfo")
        if isinstance(page_info, Mapping):
            last_has_next_page = bool(page_info.get("hasNextPage"))

        field_name, raw_items = _extract_raw_bidding_items_from_container(container)
        if field_name:
            items_field = sanitize_graphql_field_name(field_name, kind="field")
        if raw_items is not None:
            items_list_present = True
            for raw_item in raw_items:
                raw_item_count += 1
                if not isinstance(raw_item, Mapping):
                    missing_id_count += 1
                    continue
                normalized = normalize_graphql_bidding_item(raw_item)
                item_id = normalized["item_id"]
                if not item_id:
                    missing_id_count += 1
                    continue
                if normalized["id_format"] == "composite_v1":
                    composite_id_count += 1
                if item_id in normalized_by_id:
                    duplicate_id_count += 1
                else:
                    normalized_by_id[item_id] = normalized

    unique_count = len(normalized_by_id)

    if not items_list_present and reported_total is not None:
        graphql_truncated = False
        graphql_complete = False
        pagination_status = "metadata_only"
    elif not items_list_present:
        graphql_truncated = False
        graphql_complete = False
        pagination_status = "no_items_list"
    elif last_has_next_page and (reported_total == 0 or raw_item_count == 0):
        graphql_truncated = True
        graphql_complete = False
        pagination_status = (
            "truncated" if reported_total is None else "inconsistent_total"
        )
    elif reported_total == 0:
        if raw_item_count == 0 and not last_has_next_page and not max_pages_reached:
            graphql_truncated = False
            graphql_complete = True
            pagination_status = "complete"
        else:
            graphql_truncated = False
            graphql_complete = False
            pagination_status = "inconsistent_total"
    elif reported_total is not None and reported_total < 0:
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


def normalize_graphql_bidding_payload(
    data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize a single GraphQL ``data`` payload containing bidding activity."""
    if not isinstance(data, Mapping):
        raise TypeError("GraphQL Bidding data must be a mapping")
    return merge_graphql_bidding_pages([data])


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


def _compare_single_bidding_field(
    field: str,
    xml_items: Mapping[str, Mapping[str, Any]],
    gql_items: Mapping[str, Mapping[str, Any]],
    common_ids: list[str],
    *,
    collection_complete: bool = True,
) -> dict[str, Any]:
    """Compare one monitored item field across matched bidding items."""
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
            "notes": "No common bidding items available to compare this field.",
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

        if field in ("current_price", "max_bid"):
            if abs(float(xml_val) - float(gql_val)) <= 1e-6:
                if (field == "current_price" and gql_item.get("price_was_string")) or (
                    field == "max_bid" and gql_item.get("max_bid_was_string")
                ):
                    normalized_count += 1
                else:
                    exact_count += 1
            else:
                mismatch_count += 1
        elif field == "winning":
            xml_bool = bool(xml_val)
            gql_bool = bool(gql_val)
            if xml_bool == gql_bool:
                if isinstance(xml_val, bool) and isinstance(gql_val, bool):
                    exact_count += 1
                else:
                    normalized_count += 1
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
        coverage = "full"
    else:
        coverage = "partial"

    if not collection_complete:
        classification = PARITY_UNVERIFIED
        if mismatch_count > 0:
            notes = (
                f"Observed {mismatch_count} value differences among compared sample items, "
                "but collection completeness is not established or response contains errors; "
                "field parity unverified."
            )
        elif both_present_count == 0:
            if xml_present_count > 0 and gql_present_count == 0:
                notes = (
                    "Field was absent across compared candidate items, but collection completeness "
                    "is not established or response contains errors; field parity unverified."
                )
            elif xml_present_count == 0 and gql_present_count > 0:
                notes = (
                    "Field was present in candidate items but absent in XML sample, and collection "
                    "completeness is not established; field parity unverified."
                )
            else:
                notes = (
                    "No compared items populated this field in either source and collection "
                    "completeness is not established; field parity unverified."
                )
        elif both_absent_count > 0:
            notes = (
                f"Matched on {both_present_count} sample items ({both_absent_count} null on both sides), "
                "but collection completeness is not established or response contains errors; "
                "field parity unverified."
            )
        else:
            notes = (
                f"Matched on all {both_present_count} compared sample items, but "
                "full Bidding collection is truncated, contains errors, or is unreconciled."
            )
    elif both_present_count == 0:
        if xml_present_count > 0 and gql_present_count == 0:
            classification = PARITY_MISSING_FIELD
            notes = "Field is present in Trading XML but absent across candidate GraphQL records."
        elif xml_present_count == 0 and gql_present_count > 0:
            classification = PARITY_UNSUPPORTED
            notes = "Field is present in candidate GraphQL records but unsupported in Trading XML."
        elif both_absent_count == total:
            classification = PARITY_UNVERIFIED
            notes = "Field was null/omitted on both sides across all compared items."
        else:
            classification = PARITY_UNVERIFIED
            notes = "No common items populated this field in either source."
    elif mismatch_count > 0:
        classification = PARITY_DIFFERENT_SEMANTICS
        notes = (
            f"Values differed on {mismatch_count} of {both_present_count} "
            "items with data on both sides."
        )
    elif both_absent_count > 0:
        classification = PARITY_UNVERIFIED
        notes = (
            f"Matched on {both_present_count} of {total} items, but "
            f"{both_absent_count} items were null in both sources "
            "(partial coverage; full parity unverified)."
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


def _unverified_bidding_fields_report(
    *,
    reason: str,
    xml_bidding_count: int | None = None,
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
                1 if field == "bidding_count" and xml_bidding_count is not None else 0
            ),
            "graphql_present_count": (
                graphql_present_for_count if field == "bidding_count" else 0
            ),
            "notes": reason,
        }
        for field in MONITORED_BIDDING_FIELDS
    }


def compare_bidding_snapshots(
    xml_bidding: Mapping[str, Mapping[str, Any]] | None,
    graphql_payload: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    xml_available: bool = True,
    xml_truncated: bool = False,
    xml_error_summary: Mapping[str, Any] | None = None,
    graphql_error_summary: Mapping[str, Any] | None = None,
    max_pages: int = MAX_PAGES,
) -> dict[str, Any]:
    """Compare normalized XML ``bidding`` items against a GraphQL Bidding payload.

    Returns a sanitized aggregate comparison dictionary with zero private item IDs,
    titles, prices, max bids, or URLs. Fails closed when either source is unavailable,
    truncated, or has incomplete pagination/identity reconciliation.
    """
    normalized_gql: dict[str, Any] | None = None
    if graphql_payload is not None:
        if isinstance(graphql_payload, Mapping):
            normalized_gql = merge_graphql_bidding_pages(
                [graphql_payload], max_pages=max_pages
            )
        else:
            normalized_gql = merge_graphql_bidding_pages(
                graphql_payload, max_pages=max_pages
            )

    has_gql_errors = bool(
        graphql_error_summary and (graphql_error_summary.get("error_count") or 0) > 0
    )
    is_scope_or_perm = bool(
        graphql_error_summary
        and (
            graphql_error_summary.get("is_scope_or_permission_error")
            or graphql_error_summary.get("http_status") in (401, 403)
        )
    )

    if not xml_available or xml_bidding is None:
        gql_items = normalized_gql["items_by_id"] if normalized_gql else {}
        gql_count = len(gql_items) if normalized_gql else None
        reported_total = normalized_gql["reported_total"] if normalized_gql else None
        gql_present_count = (
            1
            if normalized_gql
            and (normalized_gql["items_list_present"] or reported_total is not None)
            else 0
        )
        if is_scope_or_perm:
            gql_available: Any = "unauthorized"
        elif normalized_gql and not has_gql_errors:
            if normalized_gql.get("pagination_status") == "metadata_only":
                gql_available = "count_only"
            else:
                gql_available = True
        else:
            gql_available = False

        return {
            "status": "unverified",
            "xml_available": False,
            "graphql_available": gql_available,
            "xml_truncated": xml_truncated,
            "xml_error_summary": dict(xml_error_summary) if xml_error_summary else None,
            "xml_bidding_count": None,
            "graphql_bidding_count": gql_count,
            "graphql_raw_items_count": (
                normalized_gql["raw_item_count"] if normalized_gql else None
            ),
            "graphql_reported_total": reported_total,
            "graphql_complete": (
                normalized_gql["graphql_complete"] if normalized_gql else False
            )
            and not has_gql_errors,
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
            "fields": _unverified_bidding_fields_report(
                reason=(
                    "Trading XML BidList response unavailable; parity comparison "
                    "cannot establish baseline."
                ),
                xml_bidding_count=None,
                graphql_present_for_count=gql_present_count,
            ),
        }

    xml_count = len(xml_bidding)
    if normalized_gql is None:
        return {
            "status": "unverified",
            "xml_available": True,
            "graphql_available": "unauthorized" if is_scope_or_perm else False,
            "xml_truncated": xml_truncated,
            "xml_error_summary": dict(xml_error_summary) if xml_error_summary else None,
            "xml_bidding_count": xml_count,
            "graphql_bidding_count": None,
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
            "fields": _unverified_bidding_fields_report(
                reason=(
                    "GraphQL Auction/Bidding response unavailable or failed execution; "
                    "parity unverified."
                ),
                xml_bidding_count=xml_count,
                graphql_present_for_count=0,
            ),
        }

    gql_items = normalized_gql["items_by_id"]
    reported_total = normalized_gql["reported_total"]
    gql_count = len(gql_items)
    gql_complete = bool(normalized_gql["graphql_complete"]) and not has_gql_errors
    gql_truncated = bool(normalized_gql["graphql_truncated"])
    pagination_status = str(normalized_gql["pagination_status"])

    xml_ids = set(xml_bidding.keys())
    gql_ids = set(gql_items.keys())
    common_ids = sorted(xml_ids & gql_ids)
    xml_only = sorted(xml_ids - gql_ids)
    gql_only = sorted(gql_ids - xml_ids)

    fields_report: dict[str, dict[str, Any]] = {}
    collection_complete = (
        not xml_truncated
        and gql_complete
        and not gql_truncated
        and not xml_only
        and not gql_only
        and not has_gql_errors
    )

    for field in MONITORED_BIDDING_FIELDS:
        if field == "bidding_count":
            count_exact = xml_count == gql_count
            count_reported_exact = (
                reported_total is not None and xml_count == reported_total
            )
            if not collection_complete:
                classification = PARITY_UNVERIFIED
                notes = (
                    f"XML has {xml_count} items, GraphQL has {gql_count} items "
                    f"(reported total={reported_total}), but collection is truncated, "
                    "contains errors, or is unreconciled."
                )
            elif count_exact and (reported_total is None or count_reported_exact):
                classification = PARITY_EXACT_MATCH
                notes = (
                    f"Both sources resolved exactly {xml_count} active bidding items."
                )
            elif count_exact:
                classification = PARITY_EQUIVALENT_NORMALIZED
                notes = (
                    f"Retrieved items match ({xml_count}), but reported total "
                    f"differed ({reported_total})."
                )
            else:
                classification = PARITY_DIFFERENT_SEMANTICS
                notes = (
                    f"Active bidding count mismatch: XML={xml_count}, "
                    f"GraphQL={gql_count} (reported total={reported_total})."
                )

            fields_report["bidding_count"] = {
                "field": "bidding_count",
                "classification": classification,
                "classification_label": PARITY_LABELS[classification],
                "coverage": "full" if (xml_count > 0 or gql_count > 0) else "none",
                "compared_count": 1,
                "both_present_count": 1,
                "both_absent_count": 0,
                "exact_match_count": 1 if count_exact else 0,
                "normalized_match_count": 0,
                "mismatch_count": 0 if count_exact else 1,
                "xml_present_count": 1,
                "graphql_present_count": 1
                if reported_total is not None or gql_count > 0
                else 0,
                "notes": notes,
            }
            continue

        fields_report[field] = _compare_single_bidding_field(
            field,
            xml_bidding,
            gql_items,
            common_ids,
            collection_complete=collection_complete,
        )

    classifications = {f["classification"] for f in fields_report.values()}
    if (
        xml_truncated
        or gql_truncated
        or not gql_complete
        or bool(xml_only)
        or bool(gql_only)
        or has_gql_errors
    ):
        status = "inconclusive"
    elif PARITY_DIFFERENT_SEMANTICS in classifications:
        status = "semantic_differences"
    elif PARITY_MISSING_FIELD in classifications:
        status = "missing_fields"
    elif PARITY_UNVERIFIED in classifications:
        status = "unverified"
    elif classifications <= {PARITY_EXACT_MATCH, PARITY_EQUIVALENT_NORMALIZED}:
        status = "parity_verified"
    else:
        status = "inconclusive"

    if is_scope_or_perm:
        graphql_available_val: Any = "unauthorized"
    elif has_gql_errors:
        graphql_available_val = False
    elif pagination_status == "metadata_only" or (
        not normalized_gql["items_list_present"] and reported_total is not None
    ):
        graphql_available_val = "count_only"
    else:
        graphql_available_val = True

    return {
        "status": status,
        "xml_available": True,
        "graphql_available": graphql_available_val,
        "xml_truncated": xml_truncated,
        "xml_error_summary": dict(xml_error_summary) if xml_error_summary else None,
        "xml_bidding_count": xml_count,
        "graphql_bidding_count": gql_count,
        "graphql_raw_items_count": normalized_gql["raw_item_count"],
        "graphql_reported_total": reported_total,
        "graphql_complete": gql_complete,
        "graphql_truncated": gql_truncated,
        "pagination_status": pagination_status,
        "common_items_count": len(common_ids),
        "xml_only_count": len(xml_only),
        "graphql_only_count": len(gql_only),
        "composite_ids_normalized_count": normalized_gql["composite_id_count"],
        "duplicate_ids_count": normalized_gql["duplicate_id_count"],
        "missing_ids_count": normalized_gql["missing_id_count"],
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


def rank_bidding_probe_candidate(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    """Score a candidate GraphQL probe outcome for ranking comparison preference.

    Prioritizes complete collections > usable item records > valid identities
    > pagination quality > custom tie-breaker. Item-bearing responses (even
    unverified or truncated) must always outrank count-only / metadata-only probes.
    """
    outcome = candidate.get("outcome")
    if outcome == "success":
        outcome_rank = 3
    elif outcome == "empty":
        outcome_rank = 2
    elif outcome == "unsupported_shape":
        outcome_rank = 1
    else:
        outcome_rank = 0

    usable_items = int(candidate.get("graphql_bidding_count") or 0)
    has_items_list = bool(candidate.get("items_list_present"))
    raw_items = int(candidate.get("raw_items_count") or 0)
    reported_total = candidate.get("reported_total")
    has_total = 1 if reported_total is not None else 0
    pagination_status = candidate.get("pagination_status") or ""

    duplicate_penalty = -int(candidate.get("duplicate_ids_count") or 0)
    missing_penalty = -int(candidate.get("missing_ids_count") or 0)
    is_custom = 1 if candidate.get("is_custom") else 0

    # Evidence tier:
    # 4: Complete item collection (usable items match or verified empty items)
    # 3: Usable item records present (even if unverified, truncated, etc.)
    # 2: Items list present but empty
    # 1: Count-only / metadata-only response (reported_total present, no items)
    # 0: No items and no count
    if pagination_status == "complete" and (usable_items > 0 or has_items_list):
        evidence_tier = 4
    elif usable_items > 0:
        evidence_tier = 3
    elif has_items_list:
        evidence_tier = 2
    elif pagination_status == "metadata_only" or has_total:
        evidence_tier = 1
    else:
        evidence_tier = 0

    pagination_rank = {
        "complete": 4,
        "unverified": 3,
        "truncated": 2,
        "metadata_only": 1,
        "incomplete_identity": 0,
        "inconsistent_total": 0,
    }.get(pagination_status, 0)

    return (
        outcome_rank,
        evidence_tier,
        usable_items,
        duplicate_penalty,
        missing_penalty,
        pagination_rank,
        raw_items,
        has_total,
        is_custom,
    )


def sanitize_bidding_parity_report(
    node: Any,
    *,
    forbidden_values: Iterable[str] = (),
) -> Any:
    """Recursively scrub forbidden keys, OAuth tokens, and item details from reports."""
    secret_tokens = [
        str(val).strip()
        for val in forbidden_values
        if isinstance(val, str) and len(str(val).strip()) >= 4
    ]

    def _clean_str(text: str) -> str:
        cleaned = _EBAY_TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)
        for token in secret_tokens:
            if token in cleaned:
                cleaned = cleaned.replace(token, "[REDACTED]")
        return cleaned

    def _walk(item: Any) -> Any:
        if isinstance(item, Mapping):
            scrubbed: dict[str, Any] = {}
            for key, value in item.items():
                key_str = str(key)
                if key_str.lower() in FORBIDDEN_REPORT_KEYS:
                    continue
                if any(
                    part in key_str.lower()
                    for part in ("token", "secret", "password", "api_key", "apikey")
                ):
                    continue
                if key_str == "top_level_keys" and isinstance(value, (list, tuple)):
                    scrubbed[key_str] = [
                        sanitize_graphql_field_name(k, kind="top_level") for k in value
                    ]
                    continue
                if key_str == "container_keys" and isinstance(value, (list, tuple)):
                    scrubbed[key_str] = [
                        sanitize_graphql_field_name(k, kind="container") for k in value
                    ]
                    continue
                if key_str in ("total_field_name", "total_field") and isinstance(
                    value, str
                ):
                    scrubbed[key_str] = sanitize_graphql_field_name(value, kind="field")
                    continue
                if key_str in ("items_field_name", "items_field") and isinstance(
                    value, str
                ):
                    scrubbed[key_str] = sanitize_graphql_field_name(value, kind="field")
                    continue
                if key_str == "operation_name":
                    scrubbed[key_str] = (
                        sanitize_graphql_operation_name(value)
                        if value is not None
                        else None
                    )
                    continue
                if key_str in ("selected_probe_key", "probe_key"):
                    scrubbed[key_str] = (
                        sanitize_bidding_probe_key(value) if value is not None else None
                    )
                    continue
                if key_str == "key" and isinstance(value, str):
                    scrubbed[key_str] = sanitize_bidding_probe_key(value)
                    continue
                scrubbed[key_str] = _walk(value)
            return scrubbed
        if isinstance(item, (list, tuple)):
            return [_walk(elem) for elem in item]
        if isinstance(item, str):
            return _clean_str(item)
        return item

    return _walk(node)


def render_bidding_parity_markdown(report: Mapping[str, Any]) -> str:
    """Render a clean, sanitized, GitHub Flavored Markdown summary report."""
    clean = sanitize_bidding_parity_report(report)
    timestamp = clean.get("timestamp") or datetime.now(timezone.utc).isoformat()
    status = clean.get("status", "unverified")

    def _format_cell(val: Any) -> str:
        if val is None:
            return "-"
        return f"`{val}`"

    gql_count_display = (
        clean.get("graphql_bidding_count")
        if clean.get("graphql_bidding_count") is not None
        else "-"
    )
    xml_count_display = (
        clean.get("xml_bidding_count")
        if clean.get("xml_bidding_count") is not None
        else "-"
    )
    xml_truncated_display = (
        clean.get("xml_truncated") if clean.get("xml_truncated") is not None else "-"
    )
    gql_complete_display = (
        clean.get("graphql_complete")
        if clean.get("graphql_complete") is not None
        else "-"
    )
    pag_status_display = clean.get("pagination_status") or "-"

    lines: list[str] = [
        "# eBay Auction and Bidding API Contract and Parity Validation Report",
        "",
        f"- **Generated:** `{timestamp}`",
        f"- **Parity Status:** `{status}`",
        f"- **Environment:** `{clean.get('environment', 'unknown')}`",
        f"- **Marketplace Site ID:** `{clean.get('site_id', 'unknown')}`",
        "",
        "## Executive Summary",
        "",
        (
            f"- **Trading XML BidList Available:** `{clean.get('xml_available')}` "
            f"(Count: `{xml_count_display}`, Truncated: `{xml_truncated_display}`)"
        ),
        (
            f"- **GraphQL Bidding Available:** `{clean.get('graphql_available')}` "
            f"(Count: `{gql_count_display}`, Complete: `{gql_complete_display}`, "
            f"Pagination Status: `{pag_status_display}`)"
        ),
        (
            f"- **Common Reconciled Items:** `{clean.get('common_items_count', 0)}` "
            f"(XML-Only: `{clean.get('xml_only_count', 0)}`, GraphQL-Only: `{clean.get('graphql_only_count', 0)}`)"
        ),
        (
            f"- **Composite IDs Normalized:** `{clean.get('composite_ids_normalized_count', 0)}` "
            f"(Duplicate IDs: `{clean.get('duplicate_ids_count', 0)}`, Missing IDs: `{clean.get('missing_ids_count', 0)}`)"
        ),
        "",
    ]

    op_warnings = clean.get("operational_warnings")
    if isinstance(op_warnings, list) and op_warnings:
        lines.extend(["## Operational Warnings", ""])
        for warning in op_warnings:
            lines.append(f"- {warning}")
        lines.append("")

    selected_probe = clean.get("selected_probe")
    if selected_probe:
        rep_tot_str = (
            str(selected_probe.get("reported_total"))
            if selected_probe.get("reported_total") is not None
            else "-"
        )
        lines.extend(
            [
                "## Selected Contract Probe",
                "",
                f"- **Probe Key:** `{selected_probe.get('key') or '-'}`",
                f"- **Operation Name:** `{selected_probe.get('operation_name') or '-'}`",
                f"- **Outcome:** `{selected_probe.get('outcome') or '-'}`",
                f"- **Items Field:** `{selected_probe.get('items_field') or '-'}`",
                f"- **Total Field:** `{selected_probe.get('total_field') or '-'}`",
                f"- **Reported Total:** `{rep_tot_str}`",
                f"- **Pagination Status:** `{selected_probe.get('pagination_status') or '-'}`",
                "",
            ]
        )

    probes = clean.get("probes")
    if isinstance(probes, list) and probes:
        lines.extend(
            [
                "## Candidate Contract Probes Tested",
                "",
                "| Probe Key | Operation | Outcome | Total | Items Found | Pagination Status | Error / Failure |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for p in probes:
            err = p.get("error")
            err_label = (
                f"{err.get('failure_class')} (HTTP {err.get('http_status')})"
                if isinstance(err, dict)
                else "-"
            )
            op_label = _format_cell(p.get("operation_name"))
            tot_label = _format_cell(p.get("reported_total"))
            cnt_label = _format_cell(p.get("graphql_bidding_count"))
            pag_label = _format_cell(p.get("pagination_status"))
            lines.append(
                f"| `{p.get('key')}` | {op_label} | `{p.get('outcome')}` | "
                f"{tot_label} | {cnt_label} | "
                f"{pag_label} | `{err_label}` |"
            )
        lines.append("")

    fields = clean.get("fields")
    if isinstance(fields, dict) and fields:
        lines.extend(
            [
                "## Field-by-Field Parity Matrix",
                "",
                "| Field | Parity Classification | Coverage | Compared | Exact | Normalized | Mismatches | Both Null | Notes |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for field_name in MONITORED_BIDDING_FIELDS:
            f = fields.get(field_name) or {}
            lines.append(
                f"| `{field_name}` | **{f.get('classification_label', f.get('classification'))}** | "
                f"`{f.get('coverage')}` | {f.get('compared_count', 0)} | "
                f"{f.get('exact_match_count', 0)} | {f.get('normalized_match_count', 0)} | "
                f"{f.get('mismatch_count', 0)} | {f.get('both_absent_count', 0)} | "
                f"{f.get('notes', '')} |"
            )
        lines.append("")

    fields = clean.get("fields")
    if isinstance(fields, dict) and fields:
        lines.extend(
            [
                "## Field-by-Field Parity Matrix",
                "",
                "| Field | Parity Classification | Coverage | Compared | Exact | Normalized | Mismatches | Both Null | Notes |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for field_name in MONITORED_BIDDING_FIELDS:
            f = fields.get(field_name) or {}
            lines.append(
                f"| `{field_name}` | **{f.get('classification_label', f.get('classification'))}** | "
                f"`{f.get('coverage')}` | {f.get('compared_count', 0)} | "
                f"{f.get('exact_match_count', 0)} | {f.get('normalized_match_count', 0)} | "
                f"{f.get('mismatch_count', 0)} | {f.get('both_absent_count', 0)} | "
                f"{f.get('notes', '')} |"
            )
        lines.append("")

    xml_err = clean.get("xml_error_summary")
    if xml_err:
        lines.extend(
            [
                "## Trading XML Diagnostics",
                "",
                f"- **Error Type:** `{xml_err.get('error_type')}`",
                f"- **Failure Class:** `{xml_err.get('failure_class')}`",
                "",
            ]
        )

    gql_err = clean.get("graphql_error_summary")
    if gql_err:
        lines.extend(
            [
                "## GraphQL Diagnostics (Sanitized)",
                "",
                f"- **Error Type:** `{gql_err.get('error_type')}`",
                f"- **Failure Class:** `{gql_err.get('failure_class')}`",
                f"- **HTTP Status:** `{gql_err.get('http_status')}`",
                f"- **Primary Code:** `{gql_err.get('code')}`",
                f"- **Allowed Error Codes:** `{', '.join(gql_err.get('error_codes', [])) or 'None'}`",
                f"- **Categories:** `{', '.join(gql_err.get('categories', [])) or 'None'}`",
                f"- **Domains:** `{', '.join(gql_err.get('domains', [])) or 'None'}`",
                f"- **Unrecognized / Redacted Codes:** `{gql_err.get('unrecognized_code_count', 0)}`",
                "",
            ]
        )

    lines.extend(
        [
            "## Safety & Privacy Invariants",
            "",
            "- Strictly read-only monitoring; zero mutation queries or mutations dispatched.",
            "- Zero OAuth tokens, client secrets, user credentials, or account IDs included in this report.",
            "- Zero private item IDs, titles, prices, maximum bids, or URLs logged or saved.",
            "- Incomplete collections fail closed; Trading XML `GetMyeBayBuying` (`BidList`) remains authoritative.",
            "",
        ]
    )
    return "\n".join(lines)


def render_bidding_parity_json(report: Mapping[str, Any]) -> str:
    """Render a sanitized, formatted JSON representation of the parity report."""
    clean = sanitize_bidding_parity_report(report)
    return json.dumps(clean, indent=2, sort_keys=True, default=str)
