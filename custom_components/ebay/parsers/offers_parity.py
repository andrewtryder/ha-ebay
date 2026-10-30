"""Read-only Best Offers and Negotiation History XML vs. GraphQL API contract and parity helpers.

Used by ``scripts/validate_offers_parity.py`` and unit tests in Milestone 6 to
compare Trading XML ``GetBestOffers`` (seller) and ``GetMyeBayBuying`` ``BestOfferList`` (buyer)
with candidate eBay GraphQL ``sellerNegotiationHistory`` and ``buyerNegotiationHistory``
without exposing credentials, private pricing data, or private account details.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import aiohttp

from ..clients.errors import (
    EbayApiError,
    EbayAuthError,
    EbayAuthTransientError,
    EbayGraphQLError,
)
from ..clients.graphql import (
    _parse_graphql_definitions,
    _strip_graphql_strings_and_comments,
    is_graphql_permission_error,
    is_graphql_rate_limit_error,
    is_graphql_token_expired,
    is_graphql_validation_error,
    validate_read_only_query,
)
from ..const import COMPATIBILITY_LEVEL, EBAY_XML_NS, NS
from .xml import (
    _money,
    _text,
    _to_float,
    _to_int,
    parse_ebay_time,
)

# ---------------------------------------------------------------------------
# Parity taxonomy constants (consistent across Milestones 1, 4, 5, 6)
# ---------------------------------------------------------------------------
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

SELLER_MONITORED_OFFERS_FIELDS: tuple[str, ...] = (
    "selling_total_offers",
    "items_with_offers",
    "active_offers_by_item_id",
    "negotiation_id",
    "item_id",
    "role",
    "status",
    "price",
    "currency",
    "quantity",
    "created_at",
    "expires_at",
    "counteroffer_price",
)

BUYER_MONITORED_OFFERS_FIELDS: tuple[str, ...] = (
    "buyer_total_offers",
    "items_with_offers",
    "active_offers_by_item_id",
    "negotiation_id",
    "item_id",
    "role",
    "status",
    "price",
    "currency",
    "quantity",
    "created_at",
    "expires_at",
    "counteroffer_price",
)

MONITORED_OFFERS_FIELDS: tuple[str, ...] = SELLER_MONITORED_OFFERS_FIELDS

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
        "message",
        "buyer_message",
        "seller_message",
        "buyer_username",
        "seller_username",
        "buyer_id",
        "seller_id",
        "feedback_score",
        "email",
    }
)

_EBAY_TOKEN_PATTERN = re.compile(
    r"(Bearer\s+[^\s\"']+|v\^1\.1#[^\s\"']+)", re.IGNORECASE
)
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
        "ITEM_NOT_FOUND",
        "LISTING_NOT_FOUND",
        "OFFER_NOT_FOUND",
        "NEGOTIATION_NOT_FOUND",
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
        "25001",
        "25002",
        "25003",
        "25004",
        "25005",
        "25006",
        "25007",
        "25008",
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
        "LISTING",
        "MARKETPLACE",
        "NEGOTIATION",
        "OAUTH",
        "ORDER",
        "SECURITY",
        "SELL_INVENTORY",
        "SELL_NEGOTIATION",
        "SELL_LISTING",
        "SELLER",
        "USER",
    }
)

CANONICAL_GRAPHQL_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "__typename",
        "sellerNegotiationHistory",
        "sellerNegotiations",
        "buyerNegotiationHistory",
        "buyerNegotiations",
        "negotiationHistory",
        "userNegotiations",
        "viewer",
        "user",
        "me",
    }
)

CANONICAL_OFFERS_CONTAINER_KEYS: frozenset[str] = frozenset(
    {
        "sellerNegotiationHistory",
        "sellerNegotiations",
        "buyerNegotiationHistory",
        "buyerNegotiations",
        "negotiationHistory",
        "userNegotiations",
    }
)

CANONICAL_OFFERS_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "total",
        "totalCount",
        "activeCount",
        "activeOfferCount",
        "count",
        "offset",
        "limit",
        "size",
        "pageInfo",
        "hasNextPage",
        "endCursor",
        "items",
        "negotiations",
        "offers",
        "records",
        "nodes",
        "edges",
        "node",
        "negotiationId",
        "offerId",
        "bestOfferId",
        "id",
        "itemId",
        "legacyItemId",
        "listingId",
        "sku",
        "role",
        "initiatorRole",
        "status",
        "offerStatus",
        "currentOffer",
        "counterOffer",
        "price",
        "amount",
        "value",
        "currency",
        "currencyCode",
        "quantity",
        "creationDate",
        "created",
        "creationTime",
        "expirationDate",
        "expires",
        "expirationTime",
        "updatedDate",
        "updated",
        "buyer",
        "seller",
        "message",
    }
)

CANONICAL_OFFERS_OPERATION_NAMES: frozenset[str] = frozenset(
    {
        "Handshake",
        "GetSellerNegotiationsTotal",
        "GetSellerNegotiationsCountAlt",
        "GetSellerNegotiationHistoryPaginated",
        "GetSellerNegotiationsAlternative",
        "GetBuyerNegotiationsTotal",
        "GetBuyerNegotiationHistoryPaginated",
        "GetBuyerNegotiationsAlternative",
        "OfflineFixture",
    }
)

CANONICAL_OFFERS_PROBE_KEYS: frozenset[str] = frozenset(
    {
        "typename_handshake",
        "seller_negotiation_total",
        "seller_negotiation_count_alt",
        "seller_negotiation_paginated",
        "seller_negotiation_alt_fields",
        "buyer_negotiation_total",
        "buyer_negotiation_paginated",
        "buyer_negotiation_alt_fields",
        "offline_fixture",
        "custom_query_file",
    }
)

REDACTED_ERROR_CODE = "UNKNOWN_CODE"
REDACTED_CATEGORY = "UNKNOWN_CATEGORY"
REDACTED_DOMAIN = "UNKNOWN_DOMAIN"


# ---------------------------------------------------------------------------
# Sanitization and safety helpers
# ---------------------------------------------------------------------------
def sanitize_graphql_operation_name(name: Any) -> str | None:
    """Sanitize a GraphQL operation name against canonical allowlists."""
    if name is None or isinstance(name, bool):
        return None
    raw = str(name).strip()
    if not raw or raw in ("-", "None"):
        return None
    if raw in CANONICAL_OFFERS_OPERATION_NAMES or raw == "[CUSTOM_OPERATION]":
        return raw
    return "[CUSTOM_OPERATION]"


def sanitize_offers_probe_key(key: Any) -> str:
    """Sanitize a probe key against canonical allowlists."""
    if key is None or isinstance(key, bool):
        return "[CUSTOM_PROBE]"
    raw = str(key).strip()
    if not raw or raw in ("-", "None"):
        return "[CUSTOM_PROBE]"
    if raw in CANONICAL_OFFERS_PROBE_KEYS:
        return raw
    return "[CUSTOM_PROBE]"


def sanitize_graphql_field_name(
    name: Any,
    *,
    kind: str = "field",
) -> str:
    """Sanitize a GraphQL field or container name against canonical allowlists."""
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
        if raw in CANONICAL_OFFERS_CONTAINER_KEYS:
            return raw
        return "[ALIASED_CONTAINER]"
    if raw in CANONICAL_OFFERS_FIELD_NAMES:
        return raw
    return "[ALIASED_FIELD]"


def sanitize_diagnostic_token(token: Any, *, kind: str = "error_code") -> str:
    """Sanitize an error code, category, or domain against strict allowlists."""
    if token is None or isinstance(token, bool):
        return REDACTED_ERROR_CODE
    raw = str(token).strip()
    if not raw:
        return REDACTED_ERROR_CODE

    upper = raw.upper()
    if kind == "error_code":
        if upper in ALLOWED_SYMBOLIC_ERROR_CODES or raw in ALLOWED_NUMERIC_ERROR_CODES:
            return upper if upper in ALLOWED_SYMBOLIC_ERROR_CODES else raw
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


# ---------------------------------------------------------------------------
# Enums and Dataclasses
# ---------------------------------------------------------------------------
class NegotiationRole(StrEnum):
    """Role of the user in a Best Offer negotiation."""

    SELLER = "seller"
    BUYER = "buyer"


class NegotiationStatus(StrEnum):
    """Normalized negotiation / offer status."""

    ACTIVE = "active"
    PENDING = "pending"
    COUNTERED = "countered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


ACTIVE_NEGOTIATION_STATUSES: frozenset[NegotiationStatus] = frozenset(
    {
        NegotiationStatus.ACTIVE,
        NegotiationStatus.PENDING,
        NegotiationStatus.COUNTERED,
    }
)


@dataclass(frozen=True)
class NormalizedNegotiation:
    """Immutable normalized Best Offer or negotiation record."""

    negotiation_id: str | None
    item_id: str | None
    role: NegotiationRole
    status: NegotiationStatus
    raw_status: str | None
    price: float | None
    currency: str | None
    quantity: int | None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    updated_at: datetime | None = None
    counteroffer_price: float | None = None
    counteroffer_currency: str | None = None
    counteroffer_sequence: int | None = None
    initiator_role: str | None = None
    provider: str = "unknown"
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OfferCollectionResult:
    """Normalized collection of offers/negotiations with aggregate counts."""

    negotiations: tuple[NormalizedNegotiation, ...]
    total_count: int | None
    active_offers_by_item_id: dict[str, int]
    total_active_offers: int
    is_complete: bool
    is_truncated: bool
    errors: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OffersContractProbe:
    """Definition of a strictly read-only GraphQL Offers contract probe."""

    key: str
    role: str  # "seller" or "buyer"
    description: str
    query: str
    operation_name: str | None = None
    variables: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Built-in strictly read-only Offers GraphQL contract probes
# ---------------------------------------------------------------------------
OFFERS_CONTRACT_PROBES: tuple[OffersContractProbe, ...] = (
    OffersContractProbe(
        key="typename_handshake",
        role="seller",
        description="Minimal GraphQL root __typename handshake",
        query="query Handshake { __typename }",
        operation_name="Handshake",
    ),
    OffersContractProbe(
        key="seller_negotiation_total",
        role="seller",
        description="Candidate sellerNegotiationHistory total count probe",
        query="query GetSellerNegotiationsTotal { sellerNegotiationHistory { total } }",
        operation_name="GetSellerNegotiationsTotal",
    ),
    OffersContractProbe(
        key="seller_negotiation_count_alt",
        role="seller",
        description="Candidate sellerNegotiationHistory totalCount/activeCount probe",
        query=(
            "query GetSellerNegotiationsCountAlt { "
            "sellerNegotiationHistory { totalCount activeCount } "
            "}"
        ),
        operation_name="GetSellerNegotiationsCountAlt",
    ),
    OffersContractProbe(
        key="seller_negotiation_paginated",
        role="seller",
        description="Candidate sellerNegotiationHistory(limit, offset) with negotiations[] fields",
        query=(
            "query GetSellerNegotiationHistoryPaginated($limit: Int, $offset: Int) {\n"
            "  sellerNegotiationHistory(limit: $limit, offset: $offset) {\n"
            "    total\n"
            "    negotiations {\n"
            "      negotiationId\n"
            "      legacyItemId\n"
            "      itemId\n"
            "      role\n"
            "      status\n"
            "      currentOffer {\n"
            "        offerId\n"
            "        price {\n"
            "          value\n"
            "          currency\n"
            "        }\n"
            "        quantity\n"
            "        creationDate\n"
            "        expirationDate\n"
            "        initiatorRole\n"
            "      }\n"
            "      counterOffer {\n"
            "        offerId\n"
            "        price {\n"
            "          value\n"
            "          currency\n"
            "        }\n"
            "      }\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetSellerNegotiationHistoryPaginated",
        variables={"limit": 50, "offset": 0},
    ),
    OffersContractProbe(
        key="seller_negotiation_alt_fields",
        role="seller",
        description="Candidate sellerNegotiations with alternative field names",
        query=(
            "query GetSellerNegotiationsAlternative {\n"
            "  sellerNegotiations {\n"
            "    totalCount\n"
            "    offers {\n"
            "      offerId\n"
            "      listingId\n"
            "      status\n"
            "      amount {\n"
            "        amount\n"
            "        currencyCode\n"
            "      }\n"
            "      quantity\n"
            "      created\n"
            "      expires\n"
            "      role\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetSellerNegotiationsAlternative",
    ),
    OffersContractProbe(
        key="buyer_negotiation_total",
        role="buyer",
        description="Candidate buyerNegotiationHistory total count probe",
        query="query GetBuyerNegotiationsTotal { buyerNegotiationHistory { total } }",
        operation_name="GetBuyerNegotiationsTotal",
    ),
    OffersContractProbe(
        key="buyer_negotiation_paginated",
        role="buyer",
        description="Candidate buyerNegotiationHistory(limit, offset) with negotiations[] fields",
        query=(
            "query GetBuyerNegotiationHistoryPaginated($limit: Int, $offset: Int) {\n"
            "  buyerNegotiationHistory(limit: $limit, offset: $offset) {\n"
            "    total\n"
            "    negotiations {\n"
            "      negotiationId\n"
            "      legacyItemId\n"
            "      itemId\n"
            "      role\n"
            "      status\n"
            "      currentOffer {\n"
            "        offerId\n"
            "        price {\n"
            "          value\n"
            "          currency\n"
            "        }\n"
            "        quantity\n"
            "        creationDate\n"
            "        expirationDate\n"
            "        initiatorRole\n"
            "      }\n"
            "      counterOffer {\n"
            "        offerId\n"
            "        price {\n"
            "          value\n"
            "          currency\n"
            "        }\n"
            "      }\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetBuyerNegotiationHistoryPaginated",
        variables={"limit": 50, "offset": 0},
    ),
    OffersContractProbe(
        key="buyer_negotiation_alt_fields",
        role="buyer",
        description="Candidate buyerNegotiations with alternative field names",
        query=(
            "query GetBuyerNegotiationsAlternative {\n"
            "  buyerNegotiations {\n"
            "    totalCount\n"
            "    offers {\n"
            "      offerId\n"
            "      listingId\n"
            "      status\n"
            "      amount {\n"
            "        amount\n"
            "        currencyCode\n"
            "      }\n"
            "      quantity\n"
            "      created\n"
            "      expires\n"
            "      role\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetBuyerNegotiationsAlternative",
    ),
)


# ---------------------------------------------------------------------------
# Query parsing, validation, and probe execution helpers
# ---------------------------------------------------------------------------
def infer_and_validate_query_operation(
    query: str,
    *,
    operation_name: str | None = None,
) -> str | None:
    """Validate that query is strictly read-only and return its operation name."""
    try:
        validate_read_only_query(query, operation_name=operation_name)
    except ValueError as exc:
        raise EbayGraphQLError(f"Query validation failed: {exc}") from exc
    cleaned = _strip_graphql_strings_and_comments(query).strip()
    defs = _parse_graphql_definitions(cleaned)
    if not defs:
        raise EbayGraphQLError("Empty GraphQL query definition")
    for kind, name in defs:
        if kind != "query":
            raise EbayGraphQLError(f"Forbidden GraphQL operation type: {kind}")
        if operation_name and name == operation_name:
            return name
    return defs[0][1]


def validate_all_offers_contract_probes() -> None:
    """Verify all built-in offers probes pass read-only validation."""
    for probe in OFFERS_CONTRACT_PROBES:
        infer_and_validate_query_operation(
            probe.query, operation_name=probe.operation_name
        )


def _extract_declared_variables(query: str) -> dict[str, str]:
    """Extract variable names and type declarations from the first operation."""
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
    declared: dict[str, str] = {}
    for match in _VARIABLE_DECL_RE.finditer(decl_str):
        name = match.group(1).strip()
        type_str = match.group(2).strip()
        declared[name] = type_str
    return declared


def parse_query_variables(query: str, raw_vars: str | None) -> dict[str, Any]:
    """Parse JSON variables string and validate against query variable declarations."""
    declared = _extract_declared_variables(query)
    parsed: dict[str, Any] = {}
    if raw_vars:
        try:
            loaded = json.loads(raw_vars)
            if not isinstance(loaded, dict):
                raise ValueError("GraphQL variables must be a JSON object")
            parsed = loaded
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in --variables: {exc}") from exc

    for name, type_decl in declared.items():
        is_required = type_decl.endswith("!")
        if is_required and name not in parsed:
            base_type = type_decl.rstrip("!")
            if base_type == "Int":
                parsed[name] = 50 if name == "limit" else 0
            elif base_type == "String":
                parsed[name] = "0"
            elif base_type == "Boolean":
                parsed[name] = False
    return parsed


def summarize_offers_probe_exception(exc: Exception) -> dict[str, Any]:
    """Produce a sanitized summary of an exception encountered during probe execution."""
    if isinstance(exc, EbayGraphQLError):
        raw_errors = getattr(exc, "errors", None) or []
        error_codes: list[str] = []
        err_dicts: list[dict[str, Any]] = []
        for err in raw_errors:
            if isinstance(err, Mapping):
                err_dicts.append(dict(err))
                ext = (
                    err.get("extensions")
                    if isinstance(err.get("extensions"), Mapping)
                    else {}
                )
                code = ext.get("code") or ext.get("errorCode") or err.get("code")
                if code:
                    error_codes.append(
                        sanitize_diagnostic_token(code, kind="error_code")
                    )
            elif isinstance(err, str):
                error_codes.append(sanitize_diagnostic_token(err, kind="error_code"))
        return {
            "status": "error",
            "error_type": "EbayGraphQLError",
            "error_codes": error_codes,
            "is_permission_error": any(
                is_graphql_permission_error(e) for e in err_dicts
            ),
            "is_token_expired": any(is_graphql_token_expired(e) for e in err_dicts),
            "is_rate_limit": any(is_graphql_rate_limit_error(e) for e in err_dicts),
            "is_validation_error": any(
                is_graphql_validation_error(e) for e in err_dicts
            ),
        }
    if isinstance(exc, EbayAuthTransientError):
        return {
            "status": "transient_auth_error",
            "error_type": "EbayAuthTransientError",
            "is_permission_error": False,
            "is_token_expired": True,
            "is_rate_limit": False,
            "is_validation_error": False,
        }
    if isinstance(exc, EbayAuthError):
        return {
            "status": "auth_error",
            "error_type": "EbayAuthError",
            "is_permission_error": True,
            "is_token_expired": False,
            "is_rate_limit": False,
            "is_validation_error": False,
        }
    if isinstance(exc, EbayApiError):
        status_val = getattr(exc, "status", getattr(exc, "status_code", None))
        return {
            "status": "api_error",
            "error_type": "EbayApiError",
            "http_status": status_val,
            "is_permission_error": False,
            "is_token_expired": False,
            "is_rate_limit": status_val == 429,
            "is_validation_error": False,
        }
    if isinstance(exc, (aiohttp.ClientError, TimeoutError)):
        return {
            "status": "network_error",
            "error_type": type(exc).__name__,
            "is_permission_error": False,
            "is_token_expired": False,
            "is_rate_limit": False,
            "is_validation_error": False,
        }
    return {
        "status": "unexpected_error",
        "error_type": type(exc).__name__,
        "is_permission_error": False,
        "is_token_expired": False,
        "is_rate_limit": False,
        "is_validation_error": False,
    }


# ---------------------------------------------------------------------------
# Normalization functions for Status, Money, Dates, and IDs
# ---------------------------------------------------------------------------
def normalize_negotiation_status(raw_status: Any) -> NegotiationStatus:
    """Map raw status string from XML or GraphQL to NegotiationStatus."""
    if raw_status is None:
        return NegotiationStatus.UNKNOWN
    cleaned = str(raw_status).strip().upper()
    if not cleaned:
        return NegotiationStatus.UNKNOWN

    if cleaned in {"ACTIVE", "OPEN"}:
        return NegotiationStatus.ACTIVE
    if cleaned in {
        "PENDING",
        "PENDINGBUYERPAYMENT",
        "PENDINGBUYERCONFIRMATION",
        "AWAITING_RESPONSE",
    }:
        return NegotiationStatus.PENDING
    if cleaned in {"COUNTERED", "CUSTOMCODE", "COUNTER_OFFERED"}:
        return NegotiationStatus.COUNTERED
    if cleaned in {"ACCEPTED", "AGREED", "OFFER_ACCEPTED"}:
        return NegotiationStatus.ACCEPTED
    if cleaned in {"DECLINED", "REJECTED", "OFFER_DECLINED"}:
        return NegotiationStatus.DECLINED
    if cleaned in {"EXPIRED", "OFFER_EXPIRED"}:
        return NegotiationStatus.EXPIRED
    if cleaned in {
        "CANCELLED",
        "CANCELED",
        "ADMINENDED",
        "RETRACTED",
        "OFFER_RETRACTED",
    }:
        return NegotiationStatus.CANCELLED

    return NegotiationStatus.UNKNOWN


def normalize_item_id(raw: Any) -> str | None:
    """Normalize an ItemID, legacyItemId, or listingId to string or None."""
    if raw is None or isinstance(raw, bool):
        return None
    cleaned = str(raw).strip()
    return cleaned if cleaned else None


def _extract_money(container: Any) -> tuple[float | None, str | None]:
    """Extract (amount, currency) from diverse money dicts or values."""
    if not isinstance(container, Mapping):
        return None, None
    raw_val = (
        container.get("value")
        if "value" in container
        else container.get("amount")
        if "amount" in container
        else container.get("price")
    )
    val = _to_float(raw_val)

    raw_curr = (
        container.get("currency")
        if "currency" in container
        else container.get("currencyCode")
    )
    curr: str | None = None
    if raw_curr is not None and not isinstance(raw_curr, bool):
        curr_str = str(raw_curr).strip()
        if curr_str:
            curr = curr_str.upper()

    return val, curr


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Parse ISO 8601 or eBay datetime string to timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc)
            if value.tzinfo
            else value.replace(tzinfo=timezone.utc)
        )
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    # Try parsing via parse_ebay_time first (handles eBay formats)
    parsed = parse_ebay_time(cleaned)
    if parsed is not None:
        return (
            parsed.astimezone(timezone.utc)
            if parsed.tzinfo
            else parsed.replace(tzinfo=timezone.utc)
        )
    try:
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return (
            dt.astimezone(timezone.utc)
            if dt.tzinfo
            else dt.replace(tzinfo=timezone.utc)
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# API Contract distinction helpers (REST Inventory vs. Negotiation vs. Offers)
# ---------------------------------------------------------------------------
def is_inventory_offer_payload(payload: Mapping[str, Any]) -> bool:
    """Check if payload represents a REST Sell Inventory listing-offer object.

    Inventory offer objects contain SKU, pricingSummary, format, and marketplaceId
    rather than buyer/seller Best Offer negotiations.
    """
    if not isinstance(payload, Mapping):
        return False
    keys = set(payload.keys())
    if "sku" in keys and ("pricingSummary" in keys or "listingPolicies" in keys):
        return True
    if "offers" in payload and isinstance(payload["offers"], list):
        first = payload["offers"][0] if payload["offers"] else None
        if isinstance(first, Mapping) and "sku" in first and "pricingSummary" in first:
            return True
    return False


def is_eligible_items_payload(payload: Mapping[str, Any]) -> bool:
    """Check if payload represents a REST Sell Negotiation find_eligible_items response.

    The find_eligible_items endpoint returns listings eligible for seller discounts,
    NOT Best Offer negotiation history.
    """
    if not isinstance(payload, Mapping):
        return False
    return "eligibleItems" in payload or "eligibleItemCount" in payload


# ---------------------------------------------------------------------------
# Legacy Trading XML Normalization (Seller & Buyer)
# ---------------------------------------------------------------------------
def normalize_trading_xml_seller_offers(
    root_or_xml: ET.Element | str,
) -> OfferCollectionResult:
    """Parse legacy Trading XML GetBestOffersResponse into canonical OfferCollectionResult.

    Produces active_offers_by_item_id that is 100% mathematically and semantically
    identical to ``active_offers_by_item_id`` in ``trading_items.py``.
    """
    if isinstance(root_or_xml, str):
        root = ET.fromstring(root_or_xml)
    else:
        root = root_or_xml

    negotiations: list[NormalizedNegotiation] = []
    active_offers_by_item: dict[str, int] = {}
    seen_offer_ids: set[str] = set()
    errors: list[dict[str, Any]] = []

    # Check for XML errors
    for err in root.findall("e:Errors", namespaces=NS):
        code = _text(err, "e:ErrorCode")
        severity = _text(err, "e:SeverityCode")
        if code:
            errors.append(
                {
                    "code": sanitize_diagnostic_token(code, kind="error_code"),
                    "severity": severity,
                }
            )

    for item_offers in root.findall(
        "e:ItemBestOffersArray/e:ItemBestOffers", namespaces=NS
    ):
        role_str = _text(item_offers, "e:Role")
        if role_str and role_str != "Seller":
            continue

        item_id = _text(item_offers, "e:Item/e:ItemID")
        if not item_id:
            continue

        item_active_count = 0
        for offer_el in item_offers.findall(
            "e:BestOfferArray/e:BestOffer", namespaces=NS
        ):
            offer_id = _text(offer_el, "e:BestOfferID")
            raw_status = _text(offer_el, "e:Status")
            status = normalize_negotiation_status(raw_status)

            # Match exact active counting rule from trading_items.py:
            # if status in {None, "Active", "Countered", "Pending"}
            if raw_status in {None, "Active", "Countered", "Pending"}:
                item_active_count += 1

            price, currency = _money(offer_el, "e:Price")
            qty = _to_int(_text(offer_el, "e:Quantity"))
            created = _parse_iso_datetime(_text(offer_el, "e:CreationTime"))
            expires = _parse_iso_datetime(_text(offer_el, "e:ExpirationTime"))

            # Counteroffer if present
            counter_price, counter_curr = _money(offer_el, "e:CounterOfferPrice")

            if offer_id and offer_id in seen_offer_ids:
                errors.append(
                    {
                        "code": "DUPLICATE_OFFER_ID",
                        "diagnostic": f"Duplicate offer ID encountered: {len(seen_offer_ids) + 1}",
                    }
                )
            if offer_id:
                seen_offer_ids.add(offer_id)

            negotiations.append(
                NormalizedNegotiation(
                    negotiation_id=offer_id,
                    item_id=item_id,
                    role=NegotiationRole.SELLER,
                    status=status,
                    raw_status=raw_status,
                    price=price,
                    currency=currency,
                    quantity=qty,
                    created_at=created,
                    expires_at=expires,
                    counteroffer_price=counter_price,
                    counteroffer_currency=counter_curr,
                    initiator_role="buyer",
                    provider="trading_xml",
                    provenance={"operation": "GetBestOffers", "role": "Seller"},
                )
            )

        active_offers_by_item[item_id] = (
            active_offers_by_item.get(item_id, 0) + item_active_count
        )

    total_active = sum(active_offers_by_item.values())
    total_pages = _to_int(_text(root, "e:PaginationResult/e:TotalNumberOfPages")) or 1
    total_entries = _to_int(_text(root, "e:PaginationResult/e:TotalNumberOfEntries"))

    return OfferCollectionResult(
        negotiations=tuple(negotiations),
        total_count=total_entries if total_entries is not None else len(negotiations),
        active_offers_by_item_id=active_offers_by_item,
        total_active_offers=total_active,
        is_complete=len(errors) == 0,
        is_truncated=total_pages > 1,
        errors=tuple(errors),
        diagnostics={"total_pages": total_pages, "total_entries": total_entries},
    )


def normalize_trading_xml_buyer_offers(
    root_or_xml: ET.Element | str,
) -> OfferCollectionResult:
    """Parse legacy Trading XML GetMyeBayBuying (BestOfferList) into canonical OfferCollectionResult."""
    if isinstance(root_or_xml, str):
        root = ET.fromstring(root_or_xml)
    else:
        root = root_or_xml

    negotiations: list[NormalizedNegotiation] = []
    active_offers_by_item: dict[str, int] = {}
    seen_offer_ids: set[str] = set()
    errors: list[dict[str, Any]] = []

    for err in root.findall("e:Errors", namespaces=NS):
        code = _text(err, "e:ErrorCode")
        severity = _text(err, "e:SeverityCode")
        if code:
            errors.append(
                {
                    "code": sanitize_diagnostic_token(code, kind="error_code"),
                    "severity": severity,
                }
            )

    container = root.find("e:BestOfferList", namespaces=NS)
    if container is not None:
        for item in container.findall("e:ItemArray/e:Item", namespaces=NS):
            item_id = _text(item, "e:ItemID")
            if not item_id:
                continue

            item_active_count = 0
            # Check for BestOffer elements directly or inside BestOfferDetails
            offers = item.findall("e:BestOfferDetails/e:BestOffer", namespaces=NS)
            if not offers:
                offers = item.findall("e:BestOfferArray/e:BestOffer", namespaces=NS)

            for offer_el in offers:
                offer_id = _text(offer_el, "e:BestOfferID")
                raw_status = _text(offer_el, "e:Status")
                status = normalize_negotiation_status(raw_status)

                if status in ACTIVE_NEGOTIATION_STATUSES:
                    item_active_count += 1

                price, currency = _money(offer_el, "e:Price")
                qty = _to_int(_text(offer_el, "e:Quantity"))
                created = _parse_iso_datetime(_text(offer_el, "e:CreationTime"))
                expires = _parse_iso_datetime(_text(offer_el, "e:ExpirationTime"))

                if offer_id and offer_id in seen_offer_ids:
                    errors.append(
                        {
                            "code": "DUPLICATE_OFFER_ID",
                            "diagnostic": f"Duplicate offer ID encountered: {len(seen_offer_ids) + 1}",
                        }
                    )
                if offer_id:
                    seen_offer_ids.add(offer_id)

                negotiations.append(
                    NormalizedNegotiation(
                        negotiation_id=offer_id,
                        item_id=item_id,
                        role=NegotiationRole.BUYER,
                        status=status,
                        raw_status=raw_status,
                        price=price,
                        currency=currency,
                        quantity=qty,
                        created_at=created,
                        expires_at=expires,
                        initiator_role="buyer",
                        provider="trading_xml",
                        provenance={
                            "operation": "GetMyeBayBuying",
                            "container": "BestOfferList",
                        },
                    )
                )

            active_offers_by_item[item_id] = (
                active_offers_by_item.get(item_id, 0) + item_active_count
            )

    total_active = sum(active_offers_by_item.values())
    total_pages = 1
    total_entries = None
    if container is not None:
        total_pages = (
            _to_int(_text(container, "e:PaginationResult/e:TotalNumberOfPages")) or 1
        )
        total_entries = _to_int(
            _text(container, "e:PaginationResult/e:TotalNumberOfEntries")
        )

    return OfferCollectionResult(
        negotiations=tuple(negotiations),
        total_count=total_entries if total_entries is not None else len(negotiations),
        active_offers_by_item_id=active_offers_by_item,
        total_active_offers=total_active,
        is_complete=len(errors) == 0,
        is_truncated=total_pages > 1,
        errors=tuple(errors),
        diagnostics={"total_pages": total_pages, "total_entries": total_entries},
    )


def build_get_my_ebay_buying_best_offers_xml(
    entries_per_page: int = 100, page: int = 1
) -> str:
    """Build isolated GetMyeBayBuying XML requesting BestOfferList for the validation harness."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingRequest xmlns="{EBAY_XML_NS}">
  <Version>{COMPATIBILITY_LEVEL}</Version>
  <BestOfferList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
    <Sort>EndTime</Sort>
  </BestOfferList>
</GetMyeBayBuyingRequest>
"""


# ---------------------------------------------------------------------------
# Candidate GraphQL Offer API Normalization (Seller & Buyer)
# ---------------------------------------------------------------------------
def _find_offers_container(
    data: Mapping[str, Any],
) -> tuple[str | None, Mapping[str, Any] | Sequence[Any] | None]:
    """Find the negotiation container in a GraphQL response data dict."""
    for key in (
        "sellerNegotiationHistory",
        "sellerNegotiations",
        "buyerNegotiationHistory",
        "buyerNegotiations",
        "negotiationHistory",
        "userNegotiations",
    ):
        if key in data:
            return key, data[key]
    return None, None


def normalize_graphql_negotiation_item(
    raw: Mapping[str, Any],
    default_role: NegotiationRole = NegotiationRole.SELLER,
) -> NormalizedNegotiation:
    """Normalize a single GraphQL negotiation/offer item."""
    # Negotiation / Offer ID
    neg_id: str | None = None
    for id_key in ("negotiationId", "offerId", "bestOfferId", "id"):
        if id_key in raw and raw[id_key] is not None:
            neg_id = str(raw[id_key]).strip()
            break

    # Item ID
    item_id: str | None = None
    for item_key in ("legacyItemId", "itemId", "listingId"):
        if item_key in raw and raw[item_key] is not None:
            item_id = normalize_item_id(raw[item_key])
            break

    # Role
    role = default_role
    if "role" in raw and raw["role"] is not None:
        role_str = str(raw["role"]).strip().lower()
        if role_str in ("seller", "buyer"):
            role = NegotiationRole(role_str)

    # Status
    raw_status = raw.get("status") or raw.get("offerStatus")
    status = normalize_negotiation_status(raw_status)

    # Current offer details (nested or flat)
    current_offer = raw.get("currentOffer")
    if not isinstance(current_offer, Mapping):
        current_offer = raw

    if neg_id is None and "offerId" in current_offer:
        neg_id = str(current_offer["offerId"]).strip()

    price, currency = _extract_money(
        current_offer.get("price") or current_offer.get("amount") or current_offer
    )
    quantity = _to_int(current_offer.get("quantity"))

    created = _parse_iso_datetime(
        current_offer.get("creationDate")
        or current_offer.get("created")
        or current_offer.get("creationTime")
        or raw.get("creationDate")
    )
    expires = _parse_iso_datetime(
        current_offer.get("expirationDate")
        or current_offer.get("expires")
        or current_offer.get("expirationTime")
        or raw.get("expirationDate")
    )
    updated = _parse_iso_datetime(
        current_offer.get("updatedDate")
        or current_offer.get("updated")
        or raw.get("updatedDate")
    )

    initiator = current_offer.get("initiatorRole") or raw.get("initiatorRole")
    initiator_str = str(initiator).strip().lower() if initiator else None

    # Counteroffer
    counter = raw.get("counterOffer")
    counter_price: float | None = None
    counter_curr: str | None = None
    if isinstance(counter, Mapping):
        counter_price, counter_curr = _extract_money(counter.get("price") or counter)

    return NormalizedNegotiation(
        negotiation_id=neg_id,
        item_id=item_id,
        role=role,
        status=status,
        raw_status=str(raw_status) if raw_status is not None else None,
        price=price,
        currency=currency,
        quantity=quantity,
        created_at=created,
        expires_at=expires,
        updated_at=updated,
        counteroffer_price=counter_price,
        counteroffer_currency=counter_curr,
        initiator_role=initiator_str,
        provider="graphql",
        provenance={"raw_keys": sorted(str(k) for k in raw.keys())},
    )


def normalize_graphql_offers_payload(
    payload: Mapping[str, Any],
    *,
    role: NegotiationRole = NegotiationRole.SELLER,
) -> OfferCollectionResult:
    """Normalize a candidate GraphQL response into an OfferCollectionResult.

    Handles HTTP 200 responses with ``errors[]`` fail-closed by recording
    sanitized error codes and setting ``is_complete = False``.
    """
    if not isinstance(payload, Mapping):
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "MALFORMED_PAYLOAD",
                    "diagnostic": "GraphQL payload must be a JSON object",
                },
            ),
        )

    # Detect REST inventory vs negotiation misconfiguration
    if is_inventory_offer_payload(payload):
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "INVENTORY_OFFER_MISMATCH",
                    "diagnostic": "Detected REST Sell Inventory payload; inventory offers are not negotiation records",
                },
            ),
        )

    if is_eligible_items_payload(payload):
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "ELIGIBLE_ITEMS_MISMATCH",
                    "diagnostic": "Detected find_eligible_items payload; discount eligibility is not negotiation history",
                },
            ),
        )

    errors: list[dict[str, Any]] = []
    raw_errors = payload.get("errors")
    if isinstance(raw_errors, Sequence):
        for err in raw_errors:
            if isinstance(err, Mapping):
                ext = (
                    err.get("extensions")
                    if isinstance(err.get("extensions"), Mapping)
                    else {}
                )
                code = (
                    ext.get("code")
                    or ext.get("errorCode")
                    or err.get("code")
                    or "GRAPHQL_ERROR"
                )
                errors.append(
                    {
                        "code": sanitize_diagnostic_token(code, kind="error_code"),
                        "category": sanitize_diagnostic_token(
                            ext.get("category"), kind="category"
                        ),
                        "domain": sanitize_diagnostic_token(
                            ext.get("domain"), kind="domain"
                        ),
                    }
                )

    data = payload.get("data")
    if not isinstance(data, Mapping):
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=tuple(errors)
            if errors
            else (
                {
                    "code": "MISSING_DATA",
                    "diagnostic": "Missing data block in GraphQL payload",
                },
            ),
        )

    container_key, container = _find_offers_container(data)
    if container is None:
        # Check __typename handshake probe
        if "__typename" in data:
            return OfferCollectionResult(
                negotiations=(),
                total_count=0,
                active_offers_by_item_id={},
                total_active_offers=0,
                is_complete=len(errors) == 0,
                is_truncated=False,
                errors=tuple(errors),
                diagnostics={"handshake_typename": str(data["__typename"])},
            )
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=tuple(errors)
            if errors
            else (
                {
                    "code": "NO_OFFERS_CONTAINER",
                    "diagnostic": "No negotiation container found in GraphQL data",
                },
            ),
        )

    total_count: int | None = None
    items_raw: list[Any] = []
    page_info: dict[str, Any] = {}

    if isinstance(container, Mapping):
        # Extract total / counts
        for count_key in ("total", "totalCount", "activeCount", "count"):
            if count_key in container and container[count_key] is not None:
                val = _to_int(container[count_key])
                if val is not None:
                    total_count = val
                    break

        # Extract items / negotiations array
        for arr_key in ("negotiations", "offers", "items", "records", "nodes", "edges"):
            if arr_key in container and isinstance(container[arr_key], Sequence):
                items_raw = list(container[arr_key])
                break

        if isinstance(container.get("pageInfo"), Mapping):
            page_info = dict(container["pageInfo"])
    elif isinstance(container, Sequence):
        items_raw = list(container)
        total_count = len(items_raw)

    negotiations: list[NormalizedNegotiation] = []
    active_offers_by_item: dict[str, int] = {}
    seen_offer_ids: set[str] = set()

    for raw_item in items_raw:
        if not isinstance(raw_item, Mapping):
            continue
        # If edges format: {node: {...}}
        item_dict = (
            raw_item.get("node")
            if "node" in raw_item and isinstance(raw_item["node"], Mapping)
            else raw_item
        )

        normalized = normalize_graphql_negotiation_item(item_dict, default_role=role)
        if normalized.negotiation_id:
            if normalized.negotiation_id in seen_offer_ids:
                errors.append(
                    {
                        "code": "DUPLICATE_OFFER_ID",
                        "diagnostic": f"Duplicate offer ID encountered: {len(seen_offer_ids) + 1}",
                    }
                )
            seen_offer_ids.add(normalized.negotiation_id)

        negotiations.append(normalized)

        if normalized.item_id:
            if normalized.item_id not in active_offers_by_item:
                active_offers_by_item[normalized.item_id] = 0
            if normalized.status in ACTIVE_NEGOTIATION_STATUSES:
                active_offers_by_item[normalized.item_id] += 1

    has_next = page_info.get("hasNextPage") is True
    is_complete = len(errors) == 0 and not has_next

    return OfferCollectionResult(
        negotiations=tuple(negotiations),
        total_count=total_count if total_count is not None else len(negotiations),
        active_offers_by_item_id=active_offers_by_item,
        total_active_offers=sum(active_offers_by_item.values()),
        is_complete=is_complete,
        is_truncated=has_next,
        errors=tuple(errors),
        diagnostics={
            "container": container_key,
            "has_next_page": has_next,
            "page_info": page_info,
        },
    )


def merge_graphql_offers_pages(
    pages: Sequence[Mapping[str, Any]],
    *,
    role: NegotiationRole = NegotiationRole.SELLER,
    max_pages: int = 25,
) -> OfferCollectionResult:
    """Merge multiple pages of GraphQL offers responses fail-closed."""
    if not pages:
        return OfferCollectionResult(
            negotiations=(),
            total_count=0,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=True,
            is_truncated=False,
        )

    all_negotiations: list[NormalizedNegotiation] = []
    active_offers_by_item: dict[str, int] = {}
    seen_offer_ids: set[str] = set()
    errors: list[dict[str, Any]] = []
    seen_page_fingerprints: set[str] = set()

    declared_total: int | None = None
    is_truncated = False
    pages_after_end = False
    declared_end_reached = False

    for page_idx, page in enumerate(pages[:max_pages]):
        # Fingerprint for cycle/loop detection
        raw_items_str = json.dumps(page.get("data") or {}, sort_keys=True)
        if raw_items_str in seen_page_fingerprints and len(raw_items_str) > 20:
            errors.append(
                {
                    "code": "PAGINATION_CYCLE_DETECTED",
                    "diagnostic": f"Duplicate page payload detected at page {page_idx + 1}",
                }
            )
            is_truncated = True
            break
        seen_page_fingerprints.add(raw_items_str)

        if declared_end_reached:
            pages_after_end = True
            errors.append(
                {
                    "code": "PAGES_AFTER_END_DECLARED",
                    "diagnostic": f"Received extra page {page_idx + 1} after hasNextPage was false",
                }
            )

        page_res = normalize_graphql_offers_payload(page, role=role)
        errors.extend(page_res.errors)

        if declared_total is None and page_res.total_count is not None:
            declared_total = page_res.total_count
        elif (
            declared_total is not None
            and page_res.total_count is not None
            and declared_total != page_res.total_count
        ):
            errors.append(
                {
                    "code": "CONTRADICTORY_PAGINATION_TOTALS",
                    "diagnostic": f"Declared total changed from {declared_total} to {page_res.total_count}",
                }
            )

        for neg in page_res.negotiations:
            if neg.negotiation_id and neg.negotiation_id in seen_offer_ids:
                errors.append(
                    {
                        "code": "DUPLICATE_OFFER_ID_ACROSS_PAGES",
                        "diagnostic": f"Offer ID repeated across pages: {len(seen_offer_ids) + 1}",
                    }
                )
            if neg.negotiation_id:
                seen_offer_ids.add(neg.negotiation_id)

            all_negotiations.append(neg)

            if neg.item_id:
                if neg.item_id not in active_offers_by_item:
                    active_offers_by_item[neg.item_id] = 0
                if neg.status in ACTIVE_NEGOTIATION_STATUSES:
                    active_offers_by_item[neg.item_id] += 1

        if not page_res.diagnostics.get("has_next_page", True):
            declared_end_reached = True

    if len(pages) > max_pages:
        is_truncated = True
        errors.append(
            {
                "code": "MAX_PAGES_EXCEEDED",
                "diagnostic": f"Total pages {len(pages)} exceeded limit {max_pages}",
            }
        )

    is_complete = len(errors) == 0 and not is_truncated and not pages_after_end

    return OfferCollectionResult(
        negotiations=tuple(all_negotiations),
        total_count=declared_total
        if declared_total is not None
        else len(all_negotiations),
        active_offers_by_item_id=active_offers_by_item,
        total_active_offers=sum(active_offers_by_item.values()),
        is_complete=is_complete,
        is_truncated=is_truncated,
        errors=tuple(errors),
        diagnostics={
            "page_count": min(len(pages), max_pages),
            "declared_end_reached": declared_end_reached,
        },
    )


# ---------------------------------------------------------------------------
# Parity Comparison Engine (XML vs. Candidate GraphQL)
# ---------------------------------------------------------------------------
def _compare_single_field(
    field_name: str,
    xml_val: Any,
    cand_val: Any,
) -> tuple[str, str | None]:
    """Compare a single field value between XML and Candidate, returning (parity_state, note)."""
    if xml_val is None and cand_val is None:
        return PARITY_EXACT_MATCH, None

    if xml_val is not None and cand_val is None:
        return (
            PARITY_MISSING_FIELD,
            "Field present in XML baseline but missing from candidate",
        )

    if xml_val is None and cand_val is not None:
        return (
            PARITY_UNVERIFIED,
            "Field present in candidate but not monitored in XML baseline",
        )

    if field_name in (
        "selling_total_offers",
        "buyer_total_offers",
        "quantity",
        "items_with_offers",
    ):
        if int(xml_val) == int(cand_val):
            return PARITY_EXACT_MATCH, None
        return (
            PARITY_DIFFERENT_SEMANTICS,
            f"Integer mismatch: baseline={xml_val}, candidate={cand_val}",
        )

    if field_name == "active_offers_by_item_id":
        if isinstance(xml_val, Mapping) and isinstance(cand_val, Mapping):
            if dict(xml_val) == dict(cand_val):
                return PARITY_EXACT_MATCH, None
            all_keys = set(xml_val.keys()) | set(cand_val.keys())
            diff_keys = [k for k in all_keys if xml_val.get(k) != cand_val.get(k)]
            return (
                PARITY_DIFFERENT_SEMANTICS,
                f"Active offer counts differ for {len(diff_keys)} items",
            )
        return PARITY_DIFFERENT_SEMANTICS, "Type mismatch on active_offers_by_item_id"

    if field_name in ("price", "counteroffer_price"):
        f_xml = _to_float(xml_val)
        f_cand = _to_float(cand_val)
        if f_xml is not None and f_cand is not None:
            if abs(f_xml - f_cand) < 0.001:
                return PARITY_EXACT_MATCH, None
            return (
                PARITY_DIFFERENT_SEMANTICS,
                f"Price mismatch: baseline={f_xml}, candidate={f_cand}",
            )
        return PARITY_DIFFERENT_SEMANTICS, "Numeric parse failure on price"

    if field_name == "currency":
        if str(xml_val).upper() == str(cand_val).upper():
            return PARITY_EXACT_MATCH, None
        return (
            PARITY_DIFFERENT_SEMANTICS,
            f"Currency mismatch: baseline={xml_val}, candidate={cand_val}",
        )

    if field_name == "status":
        s_xml = normalize_negotiation_status(xml_val)
        s_cand = normalize_negotiation_status(cand_val)
        if s_xml == s_cand:
            return PARITY_EQUIVALENT_NORMALIZED, None
        return (
            PARITY_DIFFERENT_SEMANTICS,
            f"Status mismatch: baseline={s_xml}, candidate={s_cand}",
        )

    if field_name in ("created_at", "expires_at"):
        dt_xml = _parse_iso_datetime(xml_val)
        dt_cand = _parse_iso_datetime(cand_val)
        if dt_xml is not None and dt_cand is not None:
            if abs((dt_xml - dt_cand).total_seconds()) < 2.0:
                return PARITY_EXACT_MATCH, None
            return (
                PARITY_DIFFERENT_SEMANTICS,
                f"Timestamp mismatch: baseline={dt_xml}, candidate={dt_cand}",
            )
        return PARITY_DIFFERENT_SEMANTICS, "Date parse failure"

    if str(xml_val).strip() == str(cand_val).strip():
        return PARITY_EXACT_MATCH, None

    return (
        PARITY_DIFFERENT_SEMANTICS,
        f"Value mismatch: baseline={xml_val}, candidate={cand_val}",
    )


def compare_offers_snapshots(
    xml_snapshot: OfferCollectionResult,
    candidate_snapshot: OfferCollectionResult,
    *,
    role: NegotiationRole = NegotiationRole.SELLER,
) -> dict[str, Any]:
    """Compare XML baseline vs Candidate offers snapshot across all monitored fields fail-closed."""
    monitored_fields = (
        SELLER_MONITORED_OFFERS_FIELDS
        if role == NegotiationRole.SELLER
        else BUYER_MONITORED_OFFERS_FIELDS
    )

    field_comparisons: dict[str, dict[str, Any]] = {}
    counts = {
        PARITY_EXACT_MATCH: 0,
        PARITY_EQUIVALENT_NORMALIZED: 0,
        PARITY_DIFFERENT_SEMANTICS: 0,
        PARITY_MISSING_FIELD: 0,
        PARITY_UNVERIFIED: 0,
        PARITY_UNSUPPORTED: 0,
    }

    # If either snapshot is incomplete or has errors, fail-closed: all monitored fields unverified
    is_inconclusive = (
        not xml_snapshot.is_complete
        or not candidate_snapshot.is_complete
        or xml_snapshot.is_truncated
        or candidate_snapshot.is_truncated
        or len(xml_snapshot.errors) > 0
        or len(candidate_snapshot.errors) > 0
    )

    if is_inconclusive:
        for fld in monitored_fields:
            field_comparisons[fld] = {
                "status": PARITY_UNVERIFIED,
                "label": PARITY_LABELS[PARITY_UNVERIFIED],
                "note": "Snapshot is incomplete, truncated, or contains errors; fail-closed",
            }
            counts[PARITY_UNVERIFIED] += 1

        return {
            "role": str(role),
            "overall_status": "inconclusive",
            "is_inconclusive": True,
            "field_counts": counts,
            "field_comparisons": field_comparisons,
            "baseline_summary": {
                "total_active_offers": xml_snapshot.total_active_offers,
                "items_with_offers": len(xml_snapshot.active_offers_by_item_id),
                "negotiations_count": len(xml_snapshot.negotiations),
            },
            "candidate_summary": {
                "total_active_offers": candidate_snapshot.total_active_offers,
                "items_with_offers": len(candidate_snapshot.active_offers_by_item_id),
                "negotiations_count": len(candidate_snapshot.negotiations),
            },
            "errors": [
                *xml_snapshot.errors,
                *candidate_snapshot.errors,
            ],
        }

    # 1. Compare aggregate fields
    total_key = (
        "selling_total_offers"
        if role == NegotiationRole.SELLER
        else "buyer_total_offers"
    )
    st, note = _compare_single_field(
        total_key,
        xml_snapshot.total_active_offers,
        candidate_snapshot.total_active_offers,
    )
    field_comparisons[total_key] = {
        "status": st,
        "label": PARITY_LABELS[st],
        "note": note,
    }
    counts[st] += 1

    st, note = _compare_single_field(
        "items_with_offers",
        len(xml_snapshot.active_offers_by_item_id),
        len(candidate_snapshot.active_offers_by_item_id),
    )
    field_comparisons["items_with_offers"] = {
        "status": st,
        "label": PARITY_LABELS[st],
        "note": note,
    }
    counts[st] += 1

    st, note = _compare_single_field(
        "active_offers_by_item_id",
        xml_snapshot.active_offers_by_item_id,
        candidate_snapshot.active_offers_by_item_id,
    )
    field_comparisons["active_offers_by_item_id"] = {
        "status": st,
        "label": PARITY_LABELS[st],
        "note": note,
    }
    counts[st] += 1

    # 2. Compare record-level fields across matched negotiations
    xml_by_id = {
        n.negotiation_id: n for n in xml_snapshot.negotiations if n.negotiation_id
    }
    cand_by_id = {
        n.negotiation_id: n for n in candidate_snapshot.negotiations if n.negotiation_id
    }

    matched_ids = set(xml_by_id.keys()) & set(cand_by_id.keys())

    record_fields = (
        "negotiation_id",
        "item_id",
        "role",
        "status",
        "price",
        "currency",
        "quantity",
        "created_at",
        "expires_at",
        "counteroffer_price",
    )

    if not xml_snapshot.negotiations and not candidate_snapshot.negotiations:
        # Both empty - perfect match
        for fld in record_fields:
            field_comparisons[fld] = {
                "status": PARITY_EXACT_MATCH,
                "label": PARITY_LABELS[PARITY_EXACT_MATCH],
                "note": "Empty collection in both snapshots",
            }
            counts[PARITY_EXACT_MATCH] += 1
    elif not matched_ids:
        for fld in record_fields:
            field_comparisons[fld] = {
                "status": PARITY_MISSING_FIELD,
                "label": PARITY_LABELS[PARITY_MISSING_FIELD],
                "note": f"No matching negotiation IDs between XML ({len(xml_by_id)}) and candidate ({len(cand_by_id)})",
            }
            counts[PARITY_MISSING_FIELD] += 1
    else:
        # Aggregate field states across all matched records
        for fld in record_fields:
            field_states: list[str] = []
            for mid in matched_ids:
                x_neg = xml_by_id[mid]
                c_neg = cand_by_id[mid]
                x_val = getattr(x_neg, fld, None)
                c_val = getattr(c_neg, fld, None)
                st, _ = _compare_single_field(fld, x_val, c_val)
                field_states.append(st)

            # Determine aggregate status for field
            if all(s == PARITY_EXACT_MATCH for s in field_states):
                overall_fld = PARITY_EXACT_MATCH
            elif all(
                s in (PARITY_EXACT_MATCH, PARITY_EQUIVALENT_NORMALIZED)
                for s in field_states
            ):
                overall_fld = PARITY_EQUIVALENT_NORMALIZED
            elif any(s == PARITY_DIFFERENT_SEMANTICS for s in field_states):
                overall_fld = PARITY_DIFFERENT_SEMANTICS
            elif any(s == PARITY_MISSING_FIELD for s in field_states):
                overall_fld = PARITY_MISSING_FIELD
            else:
                overall_fld = PARITY_UNVERIFIED

            field_comparisons[fld] = {
                "status": overall_fld,
                "label": PARITY_LABELS[overall_fld],
                "note": f"Evaluated across {len(matched_ids)} matched negotiations",
            }
            counts[overall_fld] += 1

    overall_parity = (
        "pass"
        if counts[PARITY_DIFFERENT_SEMANTICS] == 0
        and counts[PARITY_MISSING_FIELD] == 0
        and counts[PARITY_UNVERIFIED] == 0
        else "fail"
    )

    return {
        "role": str(role),
        "overall_status": overall_parity,
        "is_inconclusive": False,
        "field_counts": counts,
        "field_comparisons": field_comparisons,
        "baseline_summary": {
            "total_active_offers": xml_snapshot.total_active_offers,
            "items_with_offers": len(xml_snapshot.active_offers_by_item_id),
            "negotiations_count": len(xml_snapshot.negotiations),
        },
        "candidate_summary": {
            "total_active_offers": candidate_snapshot.total_active_offers,
            "items_with_offers": len(candidate_snapshot.active_offers_by_item_id),
            "negotiations_count": len(candidate_snapshot.negotiations),
        },
        "errors": [],
    }


def rank_offers_probe_candidate(result: dict[str, Any]) -> int:
    """Rank a candidate probe result (higher score is better)."""
    if result.get("overall_status") == "inconclusive":
        return -100
    counts = result.get("field_counts", {})
    score = (
        counts.get(PARITY_EXACT_MATCH, 0) * 10
        + counts.get(PARITY_EQUIVALENT_NORMALIZED, 0) * 8
        - counts.get(PARITY_DIFFERENT_SEMANTICS, 0) * 15
        - counts.get(PARITY_MISSING_FIELD, 0) * 10
        - counts.get(PARITY_UNVERIFIED, 0) * 5
    )
    return score


# ---------------------------------------------------------------------------
# Privacy protection and report sanitization
# ---------------------------------------------------------------------------
def sanitize_offers_parity_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize report dictionary ensuring zero sensitive tokens or private IDs leak."""
    sanitized: dict[str, Any] = {}
    for key, value in report.items():
        if key in FORBIDDEN_REPORT_KEYS:
            continue
        if isinstance(value, Mapping):
            sanitized[key] = sanitize_offers_parity_report(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            sanitized[key] = [
                sanitize_offers_parity_report(v) if isinstance(v, Mapping) else v
                for v in value
            ]
        elif isinstance(value, str):
            # Check for token leak
            redacted = _EBAY_TOKEN_PATTERN.sub("[REDACTED_TOKEN]", value)
            sanitized[key] = redacted
        else:
            sanitized[key] = value
    return sanitized


def render_offers_parity_markdown(report: Mapping[str, Any]) -> str:
    """Render a sanitized markdown summary of the parity comparison."""
    sanitized = sanitize_offers_parity_report(report)
    role = sanitized.get("role", "seller")
    status = sanitized.get("overall_status", "unknown").upper()
    counts = sanitized.get("field_counts", {})
    comparisons = sanitized.get("field_comparisons", {})

    lines: list[str] = [
        f"# Best Offers & Negotiation History Parity Report ({role.title()})",
        "",
        f"**Overall Status:** `{status}`",
        f"**Inconclusive:** `{sanitized.get('is_inconclusive', False)}`",
        "",
        "## Summary Counts",
        "",
        f"- Exact Match: {counts.get(PARITY_EXACT_MATCH, 0)}",
        f"- Equivalent after Normalization: {counts.get(PARITY_EQUIVALENT_NORMALIZED, 0)}",
        f"- Different Semantics: {counts.get(PARITY_DIFFERENT_SEMANTICS, 0)}",
        f"- Missing Field: {counts.get(PARITY_MISSING_FIELD, 0)}",
        f"- Unverified: {counts.get(PARITY_UNVERIFIED, 0)}",
        f"- Unsupported: {counts.get(PARITY_UNSUPPORTED, 0)}",
        "",
        "## Field Comparison Matrix",
        "",
        "| Field | Status | Note |",
        "|---|---|---|",
    ]

    for fld, info in sorted(comparisons.items()):
        lbl = info.get("label", info.get("status", "Unknown"))
        note = info.get("note") or ""
        lines.append(f"| `{fld}` | {lbl} | {note} |")

    errors = sanitized.get("errors", [])
    if errors:
        lines.extend(["", "## Diagnostic Errors", ""])
        for err in errors:
            code = err.get("code", "ERROR")
            diag = err.get("diagnostic", "")
            lines.append(f"- `{code}`: {diag}")

    return "\n".join(lines)


def render_offers_parity_json(report: Mapping[str, Any]) -> str:
    """Render a sanitized, deterministic JSON report."""
    sanitized = sanitize_offers_parity_report(report)
    return json.dumps(sanitized, indent=2, sort_keys=True)
