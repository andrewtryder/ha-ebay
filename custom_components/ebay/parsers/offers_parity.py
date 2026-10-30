"""Read-only Best Offers and Negotiation History XML vs. GraphQL API contract and parity helpers.

Used by ``scripts/validate_offers_parity.py`` and unit tests in Milestone 6 to
compare Trading XML ``GetBestOffers`` (seller) and ``GetMyeBayBuying`` ``BestOfferList`` (buyer)
with candidate eBay GraphQL ``sellerNegotiationHistory`` and ``buyerNegotiationHistory``
without exposing credentials, private pricing data, or private account details.
"""

from __future__ import annotations

import hashlib
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
        "password",
        "secret",
        "user_email",
        "phone",
        "title",
        "listing_title",
    }
)

_EBAY_TOKEN_PATTERN = re.compile(
    r"(Bearer\s+[^\s\"']+|v\^1\.1#[^\s\"']+|refresh_token=[^\s&\"']+)", re.IGNORECASE
)
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")


MAX_OFFERS_PAGES = 25


def validate_max_pages(max_pages: int) -> int:
    """Validate that max_pages is an integer between 1 and 25 inclusive.

    Raises ValueError if max_pages is not an integer or is out of range [1, 25].
    """
    if not isinstance(max_pages, int) or isinstance(max_pages, bool):
        raise ValueError(
            f"max_pages must be an integer, got {type(max_pages).__name__}"
        )
    if max_pages < 1 or max_pages > MAX_OFFERS_PAGES:
        raise ValueError(
            f"max_pages must be between 1 and {MAX_OFFERS_PAGES}, got {max_pages}"
        )
    return max_pages


def pseudonymize_identifier(val: str, prefix: str = "id") -> str:
    """Return a deterministic, privacy-preserving 12-char SHA-256 pseudonym."""
    if not val:
        return ""
    h = hashlib.sha256(val.strip().encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{h}"


_CURRENCY_AMOUNT_PATTERN = re.compile(
    r"(?:\b[A-Z]{3}\s*|\$|£|€|¥)\s*\d+(?:\.\d{1,4})?\b|\b\d+(?:\.\d{1,4})?\s*[A-Z]{3}\b|\b\d+\.\d{2}\b",
    re.IGNORECASE,
)
_NUMERIC_ID_PATTERN = re.compile(r"\b\d{9,19}\b")
_CREDENTIAL_KV_PATTERN = re.compile(
    r"(client_secret|client_id|refresh_token|password|secret|runame|token|auth_code|authorization|access_token)=([^&\s\"']+)",
    re.IGNORECASE,
)


def sanitize_diagnostic_string(val: str) -> str:
    """Sanitize arbitrary strings, redacting auth tokens, emails, prices, IDs, and sensitive patterns."""
    if not isinstance(val, str):
        return str(val)
    # Redact credentials and query params
    text = _CREDENTIAL_KV_PATTERN.sub(r"\1=[REDACTED]", val)
    # Redact eBay access/refresh tokens
    text = _EBAY_TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)
    # Redact email addresses
    text = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    # Redact currency amounts and prices (e.g. USD 85.37, $85.37, 85.37)
    text = _CURRENCY_AMOUNT_PATTERN.sub("[REDACTED_AMOUNT]", text)
    # Redact long numeric identifiers (e.g. 123456789012)
    text = _NUMERIC_ID_PATTERN.sub("[REDACTED_ID]", text)
    # Neutralize HTML tags and script injection
    text = re.sub(r"<[^>]*>", "[REDACTED_TAG]", text)
    # Neutralize markdown links [text](url) -> [text]
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"[\1]", text)
    # Replace markdown table pipes so table syntax cannot be broken
    text = text.replace("|", "-")
    return text


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
        "HANDSHAKE_ONLY",
        "COUNT_ONLY_QUERY",
        "COUNT_ONLY_OPERATION",
        "MISSING_PAGINATION_METADATA",
        "MALFORMED_PAGE_INFO",
        "TOTAL_RECORDS_MISMATCH",
        "MISSING_NEGOTIATION_ID",
        "DUPLICATE_OFFER_ID",
        "DUPLICATE_OFFER_ID_ACROSS_PAGES",
        "ROLE_MISMATCH",
        "NO_PAGES_PROVIDED",
        "MALFORMED_PAGE",
        "PAGINATION_CYCLE_DETECTED",
        "PAGES_AFTER_END_DECLARED",
        "CONTRADICTORY_PAGINATION_TOTALS",
        "MAX_PAGES_EXCEEDED",
        "PENDING_CONTINUATION",
        "UNVERIFIED_COLLECTION_CENSUS",
        "XML_ACK_FAILURE",
        "XML_ACK_PARTIAL_FAILURE",
        "XML_ACK_MISSING",
        "XML_ACK_UNKNOWN",
        "INVALID_XML_ROOT",
        "EMPTY_XML_PAYLOAD",
        "XML_PARSE_ERROR",
        "MISSING_REQUIRED_CONTAINER",
        "PAGINATION_SEQUENCE_ERROR",
        "INVENTORY_OFFER_MISMATCH",
        "ELIGIBLE_ITEMS_MISMATCH",
        "UNKNOWN_ERROR",
        "GRAPHQL_ERROR",
        "MISSING_DATA",
        "NO_OFFERS_CONTAINER",
    }
)

GENERIC_DIAGNOSTIC_MESSAGES: dict[str, str] = {
    "ACCESS_DENIED": "Access denied; authorization check failed",
    "BAD_GATEWAY": "Bad gateway encountered communicating with remote service",
    "BAD_USER_INPUT": "Invalid argument or input parameter provided",
    "CONTRADICTORY_PAGINATION_TOTALS": "Declared pagination totals changed across pages",
    "COUNT_ONLY_OPERATION": "Count-only operation does not retrieve negotiation records",
    "COUNT_ONLY_QUERY": "Count-only operation does not retrieve negotiation records",
    "DOWNSTREAM_SERVICE_ERROR": "Downstream service error encountered",
    "DUPLICATE_OFFER_ID": "Duplicate offer identifier encountered within page",
    "DUPLICATE_OFFER_ID_ACROSS_PAGES": "Duplicate offer identifier encountered across pages",
    "ELIGIBLE_ITEMS_MISMATCH": "Eligible items container does not match negotiation records",
    "EMPTY_XML_PAYLOAD": "Trading XML payload is empty or whitespace",
    "FIELD_UNDEFINED": "Field undefined in GraphQL schema",
    "FORBIDDEN": "Access forbidden; insufficient permissions or scope",
    "GATEWAY_TIMEOUT": "Gateway timeout communicating with remote service",
    "GRAPHQL_ERROR": "GraphQL service returned an error condition",
    "GRAPHQL_PARSE_FAILED": "Failed to parse GraphQL document",
    "GRAPHQL_VALIDATION_ERROR": "GraphQL query validation failed against schema",
    "GRAPHQL_VALIDATION_FAILED": "GraphQL query validation failed against schema",
    "HANDSHAKE_ONLY": "Transport connectivity handshake only; no negotiation records retrieved",
    "INSUFFICIENT_SCOPE": "Insufficient OAuth scope for requested resource",
    "INTERNAL_SERVER_ERROR": "Internal server error encountered",
    "INTROSPECTION_DISABLED": "GraphQL schema introspection is disabled",
    "INVALID_ACCESS_TOKEN": "Access token is invalid or expired",
    "INVALID_QUERY": "Invalid GraphQL query document",
    "INVALID_TOKEN": "Access token is invalid",
    "INVALID_XML_ROOT": "Trading XML root element does not match expected operation",
    "INVENTORY_OFFER_MISMATCH": "Detected REST Sell Inventory payload; inventory offers are not negotiation records",
    "ITEM_NOT_FOUND": "Item identifier was not found",
    "LISTING_NOT_FOUND": "Listing identifier was not found",
    "MALFORMED_ERROR": "Malformed error object encountered in response",
    "MALFORMED_PAGE": "Response page is not a valid structured payload",
    "MALFORMED_PAGE_INFO": "Pagination pageInfo is malformed or non-boolean",
    "MAX_PAGES_EXCEEDED": "Total pages exceeded maximum allowed limit of 25",
    "MISSING_DATA": "Response missing expected data field",
    "MISSING_NEGOTIATION_ID": "Negotiation record missing required identifier",
    "MISSING_PAGINATION_METADATA": "Response lacks required pagination metadata",
    "MISSING_REQUIRED_CONTAINER": "Missing required data container in response",
    "MISSING_SCOPE": "Missing required OAuth scope",
    "NEGOTIATION_NOT_FOUND": "Negotiation identifier was not found",
    "NO_OFFERS_CONTAINER": "No negotiation container found in GraphQL data",
    "NO_PAGES_PROVIDED": "No response pages provided for collection",
    "NOT_AUTHORIZED": "Not authorized to access requested resource",
    "OFFER_NOT_FOUND": "Offer identifier was not found",
    "OPERATION_RESOLUTION_FAILURE": "Unable to resolve GraphQL operation",
    "PAGES_AFTER_END_DECLARED": "Extra page received after final page was declared",
    "PAGINATION_CYCLE_DETECTED": "Duplicate page payload detected during pagination",
    "PAGINATION_SEQUENCE_ERROR": "Pagination page numbers out of sequence",
    "PENDING_CONTINUATION": "Final page has pending continuation; collection incomplete",
    "PERSISTED_QUERY_NOT_FOUND": "Persisted query identifier not found",
    "PERSISTED_QUERY_NOT_SUPPORTED": "Persisted queries not supported by endpoint",
    "RATE_LIMITED": "Request rate limit exceeded",
    "RATE_LIMIT_EXCEEDED": "Request rate limit exceeded",
    "ROLE_MISMATCH": "Negotiation record role does not match expected role",
    "SERVICE_UNAVAILABLE": "Remote service is temporarily unavailable",
    "SYSTEM_ERROR": "System error encountered during operation",
    "THROTTLED": "Request throttled by remote service",
    "TOKEN_EXPIRED": "OAuth access token has expired",
    "TOO_MANY_REQUESTS": "Too many requests; rate limited by server",
    "TOTAL_RECORDS_MISMATCH": "Total collected records does not match declared total count",
    "UNAUTHENTICATED": "Request unauthenticated; credentials missing or invalid",
    "UNAUTHORIZED": "Request unauthorized; invalid or expired credentials",
    "UNKNOWN_ERROR": "An unclassified or unverified error occurred",
    "UNVERIFIED_COLLECTION_CENSUS": "No declared total count available to verify census completeness",
    "VALIDATION_ERROR": "GraphQL query validation failed against schema",
    "XML_ACK_FAILURE": "Trading XML returned failure response",
    "XML_ACK_MISSING": "Trading XML returned missing or unrecognized Ack value",
    "XML_ACK_PARTIAL_FAILURE": "Trading XML returned partial failure response",
    "XML_ACK_UNKNOWN": "Trading XML returned unknown Ack value",
    "XML_PARSE_ERROR": "Failed to parse XML response payload",
}

GENERIC_FIELD_PARITY_NOTES: dict[str, str] = {
    PARITY_EXACT_MATCH: "Exact match between baseline and candidate",
    PARITY_EQUIVALENT_NORMALIZED: "Equivalent values after normalization",
    PARITY_DIFFERENT_SEMANTICS: "Value or semantic difference between baseline and candidate",
    PARITY_MISSING_FIELD: "Field present in baseline but missing in candidate",
    PARITY_UNVERIFIED: "Field unverified or collection incomplete",
    PARITY_UNSUPPORTED: "Field not supported by candidate API",
}

SAFE_FIELD_NOTE_EXACT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "Snapshot is incomplete, truncated, or contains errors; fail-closed",
        "Verified empty collection; no records available to evaluate record-level field",
    }
)

_SAFE_FIELD_NOTE_PATTERNS = (
    re.compile(r"^All \d+ negotiation IDs matched exactly$"),
    re.compile(
        r"^No matching negotiation IDs between XML \(\d+\) and candidate \(\d+\)$"
    ),
    re.compile(r"^Identity mismatch: \d+ missing in candidate$"),
    re.compile(r"^Identity mismatch: \d+ extra in candidate$"),
    re.compile(
        r"^Identity mismatch: \d+ missing in candidate; \d+ extra in candidate$"
    ),
    re.compile(r"^Evaluated across \d+ matched negotiations$"),
)

_ITEM_PSEUDONYM_RE = re.compile(r"^item_[0-9a-f]{12}$")


def _is_safe_item_pseudonym(key: str) -> bool:
    """Validate that key matches genuine item pseudonym format ^item_[0-9a-f]{12}$ and is not a raw numeric ID."""
    if not _ITEM_PSEUDONYM_RE.fullmatch(key):
        return False
    suffix = key[5:]
    if suffix.isdigit():
        return False
    return True


def _resolve_safe_field_comparison_note(status: str, raw_note: Any) -> str:
    """Resolve a safe field comparison note using strict allowlists and generic fallbacks."""
    if raw_note is not None and isinstance(raw_note, str):
        cleaned_note = raw_note.strip()
        if cleaned_note in SAFE_FIELD_NOTE_EXACT_ALLOWLIST:
            return cleaned_note
        for pat in _SAFE_FIELD_NOTE_PATTERNS:
            if pat.fullmatch(cleaned_note):
                return cleaned_note
    return GENERIC_FIELD_PARITY_NOTES.get(status, "Field comparison evaluated")


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
    raw_status: str | None = None
    price: float | None = None
    currency: str | None = None
    quantity: int | None = None
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
    is_truncated: bool = False
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
        cleaned = root_or_xml.strip()
        if not cleaned:
            return OfferCollectionResult(
                negotiations=(),
                total_count=None,
                active_offers_by_item_id={},
                total_active_offers=0,
                is_complete=False,
                is_truncated=False,
                errors=(
                    {
                        "code": "EMPTY_XML_PAYLOAD",
                        "diagnostic": "Empty XML content provided",
                    },
                ),
            )
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            return OfferCollectionResult(
                negotiations=(),
                total_count=None,
                active_offers_by_item_id={},
                total_active_offers=0,
                is_complete=False,
                is_truncated=False,
                errors=(
                    {
                        "code": "XML_PARSE_ERROR",
                        "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["XML_PARSE_ERROR"],
                    },
                ),
            )
    else:
        root = root_or_xml

    negotiations: list[NormalizedNegotiation] = []
    active_offers_by_item: dict[str, int] = {}
    seen_offer_ids: set[str] = set()
    errors: list[dict[str, Any]] = []

    # Verify response root tag
    if not (
        root.tag.endswith("GetBestOffersResponse")
        or root.tag == "GetBestOffersResponse"
    ):
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "INVALID_XML_ROOT",
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["INVALID_XML_ROOT"],
                },
            ),
        )

    # Check Ack status
    ack = _text(root, "e:Ack")
    if ack == "Failure":
        errors.append(
            {
                "code": "XML_ACK_FAILURE",
                "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["XML_ACK_FAILURE"],
            }
        )
    elif ack == "PartialFailure":
        errors.append(
            {
                "code": "XML_ACK_PARTIAL_FAILURE",
                "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["XML_ACK_PARTIAL_FAILURE"],
            }
        )
    elif ack not in ("Success", "Warning"):
        errors.append(
            {
                "code": "XML_ACK_MISSING",
                "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["XML_ACK_MISSING"],
            }
        )

    # Check for XML errors
    for err in root.findall("e:Errors", namespaces=NS):
        code = _text(err, "e:ErrorCode")
        severity = _text(err, "e:SeverityCode")
        if code:
            token_code = sanitize_diagnostic_token(code, kind="error_code")
            errors.append(
                {
                    "code": token_code,
                    "severity": sanitize_diagnostic_token(severity, kind="category"),
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES.get(
                        token_code,
                        f"eBay error code {token_code}"
                        if token_code in ALLOWED_NUMERIC_ERROR_CODES
                        else "Trading XML reported an error condition",
                    ),
                }
            )

    pag_result = root.find("e:PaginationResult", namespaces=NS)
    has_pagination = pag_result is not None
    total_pages = _to_int(_text(root, "e:PaginationResult/e:TotalNumberOfPages"))
    total_entries = _to_int(_text(root, "e:PaginationResult/e:TotalNumberOfEntries"))
    page_number = _to_int(_text(root, "e:PaginationResult/e:PageNumber")) or _to_int(
        _text(root, "e:PageNumber")
    )

    if not has_pagination or total_pages is None or total_entries is None:
        errors.append(
            {
                "code": "MISSING_PAGINATION_METADATA",
                "diagnostic": "Missing or incomplete PaginationResult in Trading XML response",
            }
        )

    item_array = root.find("e:ItemBestOffersArray", namespaces=NS)
    if item_array is None and total_entries is not None and total_entries > 0:
        errors.append(
            {
                "code": "MISSING_REQUIRED_CONTAINER",
                "diagnostic": "Missing ItemBestOffersArray container despite positive TotalNumberOfEntries",
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

            if not offer_id:
                errors.append(
                    {
                        "code": "MISSING_NEGOTIATION_ID",
                        "diagnostic": "Trading XML BestOffer missing required BestOfferID",
                    }
                )
            elif offer_id in seen_offer_ids:
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
    is_truncated = total_pages is not None and total_pages > 1
    if total_entries is not None and (total_pages is None or total_pages == 1):
        if total_entries != len(negotiations):
            errors.append(
                {
                    "code": "TOTAL_RECORDS_MISMATCH",
                    "diagnostic": f"Declared total entries ({total_entries}) does not match retrieved count ({len(negotiations)})",
                }
            )

    has_blocking_errors = any(
        e.get("code")
        in (
            "XML_ACK_FAILURE",
            "XML_ACK_PARTIAL_FAILURE",
            "XML_ACK_MISSING",
            "INVALID_XML_ROOT",
            "XML_PARSE_ERROR",
            "MISSING_REQUIRED_CONTAINER",
            "MISSING_PAGINATION_METADATA",
        )
        for e in errors
    )
    is_complete = (
        not has_blocking_errors
        and len(errors) == 0
        and not is_truncated
        and has_pagination
        and total_pages == 1
        and (total_entries is not None and total_entries == len(negotiations))
    )

    return OfferCollectionResult(
        negotiations=tuple(negotiations),
        total_count=total_entries if total_entries is not None else len(negotiations),
        active_offers_by_item_id=active_offers_by_item,
        total_active_offers=total_active,
        is_complete=is_complete,
        is_truncated=is_truncated,
        errors=tuple(errors),
        diagnostics={
            "total_pages": total_pages,
            "total_entries": total_entries,
            "page_number": page_number,
            "has_pagination": has_pagination,
        },
    )


def merge_trading_xml_seller_offers_pages(
    pages: Sequence[ET.Element | str],
    max_pages: int = 25,
) -> OfferCollectionResult:
    """Merge multiple pages of Trading XML GetBestOffers responses fail-closed."""
    max_pages = validate_max_pages(max_pages)
    if not pages:
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "NO_PAGES_PROVIDED",
                    "diagnostic": "No XML pages provided for merging",
                },
            ),
        )

    all_negotiations: list[NormalizedNegotiation] = []
    active_offers_by_item: dict[str, int] = {}
    seen_offer_ids: set[str] = set()
    errors: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()

    declared_total_pages: int | None = None
    declared_total_entries: int | None = None
    is_truncated = False
    pages_after_end = False

    for page_idx, page in enumerate(pages[:max_pages]):
        # Fingerprint for cycle detection
        page_str = (
            page
            if isinstance(page, str)
            else ET.tostring(page, encoding="utf-8").decode("utf-8")
        )
        h = hashlib.sha256(page_str.strip().encode("utf-8")).hexdigest()
        if h in seen_fingerprints and len(page_str) > 50:
            errors.append(
                {
                    "code": "PAGINATION_CYCLE_DETECTED",
                    "diagnostic": f"Duplicate XML page detected at page {page_idx + 1}",
                }
            )
            is_truncated = True
            break
        seen_fingerprints.add(h)

        if declared_total_pages is not None and page_idx >= declared_total_pages:
            pages_after_end = True
            errors.append(
                {
                    "code": "PAGES_AFTER_END_DECLARED",
                    "diagnostic": f"Received extra XML page {page_idx + 1} after declared terminal page {declared_total_pages}",
                }
            )

        page_res = normalize_trading_xml_seller_offers(page)
        # Propagate page errors except single-page TOTAL_RECORDS_MISMATCH (evaluated on merged total)
        for err in page_res.errors:
            if err.get("code") != "TOTAL_RECORDS_MISMATCH":
                errors.append(err)

        diag = page_res.diagnostics or {}
        p_total_pages = diag.get("total_pages")
        p_total_entries = diag.get("total_entries")
        p_page_num = diag.get("page_number")

        if p_total_pages is None or p_total_entries is None:
            errors.append(
                {
                    "code": "MISSING_PAGINATION_METADATA",
                    "diagnostic": f"XML page {page_idx + 1} missing required PaginationResult metadata",
                }
            )

        if declared_total_pages is None and p_total_pages is not None:
            declared_total_pages = p_total_pages
        elif (
            declared_total_pages is not None
            and p_total_pages is not None
            and declared_total_pages != p_total_pages
        ):
            errors.append(
                {
                    "code": "CONTRADICTORY_PAGINATION_TOTALS",
                    "diagnostic": f"Declared total pages changed from {declared_total_pages} to {p_total_pages}",
                }
            )

        if declared_total_entries is None and p_total_entries is not None:
            declared_total_entries = p_total_entries
        elif (
            declared_total_entries is not None
            and p_total_entries is not None
            and declared_total_entries != p_total_entries
        ):
            errors.append(
                {
                    "code": "CONTRADICTORY_PAGINATION_TOTALS",
                    "diagnostic": f"Declared total entries changed from {declared_total_entries} to {p_total_entries}",
                }
            )

        if p_page_num is not None and p_page_num != page_idx + 1:
            errors.append(
                {
                    "code": "PAGINATION_SEQUENCE_ERROR",
                    "diagnostic": f"Expected page {page_idx + 1} but received page {p_page_num}",
                }
            )

        for neg in page_res.negotiations:
            if not neg.negotiation_id:
                errors.append(
                    {
                        "code": "MISSING_NEGOTIATION_ID",
                        "diagnostic": f"Offer on page {page_idx + 1} missing required identifier",
                    }
                )
            elif neg.negotiation_id in seen_offer_ids:
                errors.append(
                    {
                        "code": "DUPLICATE_OFFER_ID_ACROSS_PAGES",
                        "diagnostic": f"Offer ID repeated across XML pages: {len(seen_offer_ids) + 1}",
                    }
                )
            if neg.negotiation_id:
                seen_offer_ids.add(neg.negotiation_id)

            all_negotiations.append(neg)

        for item_id, count in page_res.active_offers_by_item_id.items():
            active_offers_by_item[item_id] = (
                active_offers_by_item.get(item_id, 0) + count
            )

    if len(pages) > max_pages:
        is_truncated = True
        errors.append(
            {
                "code": "MAX_PAGES_EXCEEDED",
                "diagnostic": f"Total XML pages {len(pages)} exceeded limit {max_pages}",
            }
        )

    if declared_total_pages is not None and len(pages) < declared_total_pages:
        is_truncated = True
        errors.append(
            {
                "code": "PENDING_CONTINUATION",
                "diagnostic": f"Retrieved {len(pages)} of {declared_total_pages} declared XML pages",
            }
        )
    elif declared_total_pages is None:
        errors.append(
            {
                "code": "UNVERIFIED_COLLECTION_CENSUS",
                "diagnostic": "No declared total pages available to verify XML pagination completeness",
            }
        )

    if (
        declared_total_entries is not None
        and len(all_negotiations) != declared_total_entries
    ):
        errors.append(
            {
                "code": "TOTAL_RECORDS_MISMATCH",
                "diagnostic": f"Collected {len(all_negotiations)} offers but declared total is {declared_total_entries}",
            }
        )
    elif declared_total_entries is None:
        errors.append(
            {
                "code": "UNVERIFIED_COLLECTION_CENSUS",
                "diagnostic": "No declared total entries available to verify XML census completeness",
            }
        )

    is_complete = (
        len(errors) == 0
        and not is_truncated
        and not pages_after_end
        and declared_total_pages is not None
        and len(pages) == declared_total_pages
        and declared_total_entries is not None
        and len(all_negotiations) == declared_total_entries
    )

    return OfferCollectionResult(
        negotiations=tuple(all_negotiations),
        total_count=declared_total_entries
        if declared_total_entries is not None
        else len(all_negotiations),
        active_offers_by_item_id=active_offers_by_item,
        total_active_offers=sum(active_offers_by_item.values()),
        is_complete=is_complete,
        is_truncated=is_truncated,
        errors=tuple(errors),
        diagnostics={
            "page_count": min(len(pages), max_pages),
            "total_pages": declared_total_pages,
            "total_entries": declared_total_entries,
        },
    )


def normalize_trading_xml_buyer_offers(
    root_or_xml: ET.Element | str,
) -> OfferCollectionResult:
    """Parse legacy Trading XML GetMyeBayBuying (BestOfferList) into canonical OfferCollectionResult."""
    if isinstance(root_or_xml, str):
        cleaned = root_or_xml.strip()
        if not cleaned:
            return OfferCollectionResult(
                negotiations=(),
                total_count=None,
                active_offers_by_item_id={},
                total_active_offers=0,
                is_complete=False,
                is_truncated=False,
                errors=(
                    {
                        "code": "EMPTY_XML_PAYLOAD",
                        "diagnostic": "Empty XML content provided",
                    },
                ),
            )
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            return OfferCollectionResult(
                negotiations=(),
                total_count=None,
                active_offers_by_item_id={},
                total_active_offers=0,
                is_complete=False,
                is_truncated=False,
                errors=(
                    {
                        "code": "XML_PARSE_ERROR",
                        "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["XML_PARSE_ERROR"],
                    },
                ),
            )
    else:
        root = root_or_xml

    negotiations: list[NormalizedNegotiation] = []
    active_offers_by_item: dict[str, int] = {}
    seen_offer_ids: set[str] = set()
    errors: list[dict[str, Any]] = []

    # Verify response root tag
    if not (
        root.tag.endswith("GetMyeBayBuyingResponse")
        or root.tag == "GetMyeBayBuyingResponse"
    ):
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "INVALID_XML_ROOT",
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["INVALID_XML_ROOT"],
                },
            ),
        )

    # Check Ack status
    ack = _text(root, "e:Ack")
    if ack == "Failure":
        errors.append(
            {
                "code": "XML_ACK_FAILURE",
                "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["XML_ACK_FAILURE"],
            }
        )
    elif ack == "PartialFailure":
        errors.append(
            {
                "code": "XML_ACK_PARTIAL_FAILURE",
                "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["XML_ACK_PARTIAL_FAILURE"],
            }
        )
    elif ack not in ("Success", "Warning"):
        errors.append(
            {
                "code": "XML_ACK_MISSING",
                "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["XML_ACK_MISSING"],
            }
        )

    for err in root.findall("e:Errors", namespaces=NS):
        code = _text(err, "e:ErrorCode")
        severity = _text(err, "e:SeverityCode")
        if code:
            token_code = sanitize_diagnostic_token(code, kind="error_code")
            errors.append(
                {
                    "code": token_code,
                    "severity": sanitize_diagnostic_token(severity, kind="category"),
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES.get(
                        token_code,
                        f"eBay error code {token_code}"
                        if token_code in ALLOWED_NUMERIC_ERROR_CODES
                        else "Trading XML reported an error condition",
                    ),
                }
            )

    container = root.find("e:BestOfferList", namespaces=NS)
    if container is None:
        errors.append(
            {
                "code": "MISSING_REQUIRED_CONTAINER",
                "diagnostic": "Missing BestOfferList container in GetMyeBayBuyingResponse",
            }
        )
    else:
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

                if not offer_id:
                    errors.append(
                        {
                            "code": "MISSING_NEGOTIATION_ID",
                            "diagnostic": "Buyer BestOffer missing required BestOfferID",
                        }
                    )
                elif offer_id in seen_offer_ids:
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
    has_pagination = False
    total_pages: int | None = None
    total_entries: int | None = None
    page_number: int | None = None

    if container is not None:
        pag_result = container.find("e:PaginationResult", namespaces=NS)
        has_pagination = pag_result is not None
        total_pages = _to_int(
            _text(container, "e:PaginationResult/e:TotalNumberOfPages")
        )
        total_entries = _to_int(
            _text(container, "e:PaginationResult/e:TotalNumberOfEntries")
        )
        page_number = _to_int(
            _text(container, "e:PaginationResult/e:PageNumber")
        ) or _to_int(_text(container, "e:PageNumber"))

    if not has_pagination or total_pages is None or total_entries is None:
        errors.append(
            {
                "code": "MISSING_PAGINATION_METADATA",
                "diagnostic": "Missing or incomplete PaginationResult in BestOfferList",
            }
        )

    is_truncated = total_pages is not None and total_pages > 1
    if total_entries is not None and (total_pages is None or total_pages == 1):
        if total_entries != len(negotiations):
            errors.append(
                {
                    "code": "TOTAL_RECORDS_MISMATCH",
                    "diagnostic": f"Declared total entries ({total_entries}) does not match retrieved count ({len(negotiations)})",
                }
            )

    has_blocking_errors = any(
        e.get("code")
        in (
            "XML_ACK_FAILURE",
            "XML_ACK_PARTIAL_FAILURE",
            "XML_ACK_MISSING",
            "INVALID_XML_ROOT",
            "XML_PARSE_ERROR",
            "MISSING_REQUIRED_CONTAINER",
            "MISSING_PAGINATION_METADATA",
        )
        for e in errors
    )
    is_complete = (
        not has_blocking_errors
        and len(errors) == 0
        and not is_truncated
        and has_pagination
        and total_pages == 1
        and (total_entries is not None and total_entries == len(negotiations))
    )

    return OfferCollectionResult(
        negotiations=tuple(negotiations),
        total_count=total_entries if total_entries is not None else len(negotiations),
        active_offers_by_item_id=active_offers_by_item,
        total_active_offers=total_active,
        is_complete=is_complete,
        is_truncated=is_truncated,
        errors=tuple(errors),
        diagnostics={
            "total_pages": total_pages,
            "total_entries": total_entries,
            "page_number": page_number,
            "has_pagination": has_pagination,
        },
    )


def merge_trading_xml_buyer_offers_pages(
    pages: Sequence[ET.Element | str],
    max_pages: int = 25,
) -> OfferCollectionResult:
    """Merge multiple pages of Trading XML GetMyeBayBuying responses fail-closed."""
    max_pages = validate_max_pages(max_pages)
    if not pages:
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "NO_PAGES_PROVIDED",
                    "diagnostic": "No XML pages provided for merging",
                },
            ),
        )

    all_negotiations: list[NormalizedNegotiation] = []
    active_offers_by_item: dict[str, int] = {}
    seen_offer_ids: set[str] = set()
    errors: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()

    declared_total_pages: int | None = None
    declared_total_entries: int | None = None
    is_truncated = False
    pages_after_end = False

    for page_idx, page in enumerate(pages[:max_pages]):
        page_str = (
            page
            if isinstance(page, str)
            else ET.tostring(page, encoding="utf-8").decode("utf-8")
        )
        h = hashlib.sha256(page_str.strip().encode("utf-8")).hexdigest()
        if h in seen_fingerprints and len(page_str) > 50:
            errors.append(
                {
                    "code": "PAGINATION_CYCLE_DETECTED",
                    "diagnostic": f"Duplicate XML page detected at page {page_idx + 1}",
                }
            )
            is_truncated = True
            break
        seen_fingerprints.add(h)

        if declared_total_pages is not None and page_idx >= declared_total_pages:
            pages_after_end = True
            errors.append(
                {
                    "code": "PAGES_AFTER_END_DECLARED",
                    "diagnostic": f"Received extra XML page {page_idx + 1} after declared terminal page {declared_total_pages}",
                }
            )

        page_res = normalize_trading_xml_buyer_offers(page)
        # Propagate page errors except single-page TOTAL_RECORDS_MISMATCH (evaluated on merged total)
        for err in page_res.errors:
            if err.get("code") != "TOTAL_RECORDS_MISMATCH":
                errors.append(err)

        diag = page_res.diagnostics or {}
        p_total_pages = diag.get("total_pages")
        p_total_entries = diag.get("total_entries")
        p_page_num = diag.get("page_number")

        if p_total_pages is None or p_total_entries is None:
            errors.append(
                {
                    "code": "MISSING_PAGINATION_METADATA",
                    "diagnostic": f"XML page {page_idx + 1} missing required PaginationResult metadata",
                }
            )

        if declared_total_pages is None and p_total_pages is not None:
            declared_total_pages = p_total_pages
        elif (
            declared_total_pages is not None
            and p_total_pages is not None
            and declared_total_pages != p_total_pages
        ):
            errors.append(
                {
                    "code": "CONTRADICTORY_PAGINATION_TOTALS",
                    "diagnostic": f"Declared total pages changed from {declared_total_pages} to {p_total_pages}",
                }
            )

        if declared_total_entries is None and p_total_entries is not None:
            declared_total_entries = p_total_entries
        elif (
            declared_total_entries is not None
            and p_total_entries is not None
            and declared_total_entries != p_total_entries
        ):
            errors.append(
                {
                    "code": "CONTRADICTORY_PAGINATION_TOTALS",
                    "diagnostic": f"Declared total entries changed from {declared_total_entries} to {p_total_entries}",
                }
            )

        if p_page_num is not None and p_page_num != page_idx + 1:
            errors.append(
                {
                    "code": "PAGINATION_SEQUENCE_ERROR",
                    "diagnostic": f"Expected page {page_idx + 1} but received page {p_page_num}",
                }
            )

        for neg in page_res.negotiations:
            if not neg.negotiation_id:
                errors.append(
                    {
                        "code": "MISSING_NEGOTIATION_ID",
                        "diagnostic": f"Buyer offer on page {page_idx + 1} missing required identifier",
                    }
                )
            elif neg.negotiation_id in seen_offer_ids:
                errors.append(
                    {
                        "code": "DUPLICATE_OFFER_ID_ACROSS_PAGES",
                        "diagnostic": f"Offer ID repeated across XML pages: {len(seen_offer_ids) + 1}",
                    }
                )
            if neg.negotiation_id:
                seen_offer_ids.add(neg.negotiation_id)

            all_negotiations.append(neg)

        for item_id, count in page_res.active_offers_by_item_id.items():
            active_offers_by_item[item_id] = (
                active_offers_by_item.get(item_id, 0) + count
            )

    if len(pages) > max_pages:
        is_truncated = True
        errors.append(
            {
                "code": "MAX_PAGES_EXCEEDED",
                "diagnostic": f"Total XML pages {len(pages)} exceeded limit {max_pages}",
            }
        )

    if declared_total_pages is not None and len(pages) < declared_total_pages:
        is_truncated = True
        errors.append(
            {
                "code": "PENDING_CONTINUATION",
                "diagnostic": f"Retrieved {len(pages)} of {declared_total_pages} declared XML pages",
            }
        )
    elif declared_total_pages is None:
        errors.append(
            {
                "code": "UNVERIFIED_COLLECTION_CENSUS",
                "diagnostic": "No declared total pages available to verify XML pagination completeness",
            }
        )

    if (
        declared_total_entries is not None
        and len(all_negotiations) != declared_total_entries
    ):
        errors.append(
            {
                "code": "TOTAL_RECORDS_MISMATCH",
                "diagnostic": f"Collected {len(all_negotiations)} buyer offers but PaginationResult declared {declared_total_entries}",
            }
        )
    elif declared_total_entries is None:
        errors.append(
            {
                "code": "UNVERIFIED_COLLECTION_CENSUS",
                "diagnostic": "No declared total entries available to verify XML census completeness",
            }
        )

    is_complete = (
        len(errors) == 0
        and not is_truncated
        and not pages_after_end
        and declared_total_pages is not None
        and len(pages) == declared_total_pages
        and declared_total_entries is not None
        and len(all_negotiations) == declared_total_entries
    )

    return OfferCollectionResult(
        negotiations=tuple(all_negotiations),
        total_count=declared_total_entries
        if declared_total_entries is not None
        else len(all_negotiations),
        active_offers_by_item_id=active_offers_by_item,
        total_active_offers=sum(active_offers_by_item.values()),
        is_complete=is_complete,
        is_truncated=is_truncated,
        errors=tuple(errors),
        diagnostics={
            "page_count": min(len(pages), max_pages),
            "total_pages": declared_total_pages,
            "total_entries": declared_total_entries,
        },
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
                total_count=None,
                active_offers_by_item_id={},
                total_active_offers=0,
                is_complete=False,
                is_truncated=False,
                errors=tuple(errors)
                + (
                    {
                        "code": "HANDSHAKE_ONLY",
                        "diagnostic": "Handshake verifies connectivity only, not a negotiation collection",
                    },
                ),
                diagnostics={
                    "handshake_typename": str(data["__typename"]),
                    "operation_kind": "handshake_only",
                },
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
    has_items_array = False

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
                has_items_array = True
                break

        if isinstance(container.get("pageInfo"), Mapping):
            page_info = dict(container["pageInfo"])
    elif isinstance(container, Sequence):
        items_raw = list(container)
        has_items_array = True
        total_count = len(items_raw)

    if not has_items_array:
        errors.append(
            {
                "code": "COUNT_ONLY_QUERY",
                "diagnostic": "Count-only operation does not retrieve negotiation records",
            }
        )

    has_page_info = (
        isinstance(container.get("pageInfo"), Mapping)
        if isinstance(container, Mapping)
        else False
    )
    has_next: bool = False
    raw_has_next: Any = None
    if has_page_info:
        raw_has_next = page_info.get("hasNextPage")
        if not isinstance(raw_has_next, bool):
            errors.append(
                {
                    "code": "MALFORMED_PAGE_INFO",
                    "diagnostic": "pageInfo.hasNextPage must be a boolean",
                }
            )
            has_next = True
        else:
            has_next = raw_has_next
    else:
        errors.append(
            {
                "code": "MISSING_PAGINATION_METADATA",
                "diagnostic": "Response lacks pageInfo pagination metadata",
            }
        )

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
        if not normalized.negotiation_id:
            errors.append(
                {
                    "code": "MISSING_NEGOTIATION_ID",
                    "diagnostic": "Negotiation item missing required identifier",
                }
            )
        elif normalized.negotiation_id in seen_offer_ids:
            errors.append(
                {
                    "code": "DUPLICATE_OFFER_ID",
                    "diagnostic": f"Duplicate offer ID encountered: {len(seen_offer_ids) + 1}",
                }
            )
        if normalized.negotiation_id:
            seen_offer_ids.add(normalized.negotiation_id)

        if normalized.role != role:
            errors.append(
                {
                    "code": "ROLE_MISMATCH",
                    "diagnostic": f"Negotiation item role {normalized.role} does not match expected {role}",
                }
            )

        negotiations.append(normalized)

        if normalized.item_id:
            if normalized.item_id not in active_offers_by_item:
                active_offers_by_item[normalized.item_id] = 0
            if normalized.status in ACTIVE_NEGOTIATION_STATUSES:
                active_offers_by_item[normalized.item_id] += 1

    if total_count is not None and not has_next:
        if total_count != len(negotiations):
            errors.append(
                {
                    "code": "TOTAL_RECORDS_MISMATCH",
                    "diagnostic": f"Declared total ({total_count}) does not match retrieved count ({len(negotiations)})",
                }
            )

    is_complete = (
        len(errors) == 0
        and has_page_info
        and (raw_has_next is False)
        and (total_count is not None and total_count == len(negotiations))
    )

    return OfferCollectionResult(
        negotiations=tuple(negotiations),
        total_count=total_count,
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
    max_pages = validate_max_pages(max_pages)
    if not pages:
        return OfferCollectionResult(
            negotiations=(),
            total_count=None,
            active_offers_by_item_id={},
            total_active_offers=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "NO_PAGES_PROVIDED",
                    "diagnostic": "No pages provided for merging",
                },
            ),
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
        if not isinstance(page, Mapping):
            errors.append(
                {
                    "code": "MALFORMED_PAGE",
                    "diagnostic": f"Page {page_idx + 1} is not a valid JSON object",
                }
            )
            continue

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
        # Propagate page errors, excluding single-page TOTAL_RECORDS_MISMATCH (evaluated on merged total)
        for err in page_res.errors:
            if err.get("code") != "TOTAL_RECORDS_MISMATCH":
                errors.append(err)

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
            if not neg.negotiation_id:
                errors.append(
                    {
                        "code": "MISSING_NEGOTIATION_ID",
                        "diagnostic": f"Negotiation item on page {page_idx + 1} missing required identifier",
                    }
                )
            elif neg.negotiation_id in seen_offer_ids:
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

        has_next_val = page_res.diagnostics.get("has_next_page")
        if has_next_val is False:
            declared_end_reached = True
        elif has_next_val is True:
            declared_end_reached = False

    if len(pages) > max_pages:
        is_truncated = True
        errors.append(
            {
                "code": "MAX_PAGES_EXCEEDED",
                "diagnostic": f"Total pages {len(pages)} exceeded limit {max_pages}",
            }
        )

    if not declared_end_reached:
        is_truncated = True
        errors.append(
            {
                "code": "PENDING_CONTINUATION",
                "diagnostic": "Final page has pending continuation (hasNextPage is not false)",
            }
        )

    if declared_total is not None and len(all_negotiations) != declared_total:
        errors.append(
            {
                "code": "TOTAL_RECORDS_MISMATCH",
                "diagnostic": f"Total collected records {len(all_negotiations)} does not match declared total {declared_total}",
            }
        )
    elif declared_total is None:
        errors.append(
            {
                "code": "UNVERIFIED_COLLECTION_CENSUS",
                "diagnostic": "No declared total count available to verify census completeness",
            }
        )

    is_complete = (
        len(errors) == 0
        and declared_end_reached
        and not is_truncated
        and not pages_after_end
        and (declared_total is not None and len(all_negotiations) == declared_total)
    )

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
        if field_name == "counteroffer_price":
            return (
                PARITY_EQUIVALENT_NORMALIZED,
                "No counteroffer present on either side",
            )
        return (
            PARITY_UNVERIFIED,
            f"Field '{field_name}' absent in both baselines",
        )

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

    xml_ids = set(xml_by_id.keys())
    cand_ids = set(cand_by_id.keys())
    matched_ids = xml_ids & cand_ids
    missing_in_candidate = xml_ids - cand_ids
    extra_in_candidate = cand_ids - xml_ids

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
        # Both verified empty collections - record-level fields cannot be verified without records
        for fld in record_fields:
            field_comparisons[fld] = {
                "status": PARITY_UNVERIFIED,
                "label": PARITY_LABELS[PARITY_UNVERIFIED],
                "note": "Verified empty collection; no records available to evaluate record-level field",
            }
            counts[PARITY_UNVERIFIED] += 1
        overall_parity = "inconclusive"
    else:
        # Check negotiation_id identity set match
        if xml_ids != cand_ids:
            st_id = (
                PARITY_MISSING_FIELD
                if (len(cand_ids) == 0 and len(xml_ids) > 0)
                else PARITY_DIFFERENT_SEMANTICS
            )
            notes_parts: list[str] = []
            if missing_in_candidate:
                notes_parts.append(f"{len(missing_in_candidate)} missing in candidate")
            if extra_in_candidate:
                notes_parts.append(f"{len(extra_in_candidate)} extra in candidate")
            note_str = "Identity mismatch: " + "; ".join(notes_parts)
            field_comparisons["negotiation_id"] = {
                "status": st_id,
                "label": PARITY_LABELS[st_id],
                "note": note_str,
            }
            counts[st_id] += 1
        else:
            field_comparisons["negotiation_id"] = {
                "status": PARITY_EXACT_MATCH,
                "label": PARITY_LABELS[PARITY_EXACT_MATCH],
                "note": f"All {len(matched_ids)} negotiation IDs matched exactly",
            }
            counts[PARITY_EXACT_MATCH] += 1

        other_record_fields = tuple(f for f in record_fields if f != "negotiation_id")
        if not matched_ids:
            for fld in other_record_fields:
                field_comparisons[fld] = {
                    "status": PARITY_MISSING_FIELD,
                    "label": PARITY_LABELS[PARITY_MISSING_FIELD],
                    "note": f"No matching negotiation IDs between XML ({len(xml_by_id)}) and candidate ({len(cand_by_id)})",
                }
                counts[PARITY_MISSING_FIELD] += 1
        else:
            for fld in other_record_fields:
                field_states: list[str] = []
                for mid in matched_ids:
                    x_neg = xml_by_id[mid]
                    c_neg = cand_by_id[mid]
                    x_val = getattr(x_neg, fld, None)
                    c_val = getattr(c_neg, fld, None)
                    st_fld, _ = _compare_single_field(fld, x_val, c_val)
                    field_states.append(st_fld)

                if any(s == PARITY_DIFFERENT_SEMANTICS for s in field_states):
                    overall_fld = PARITY_DIFFERENT_SEMANTICS
                elif any(s == PARITY_MISSING_FIELD for s in field_states):
                    overall_fld = PARITY_MISSING_FIELD
                elif any(s == PARITY_UNVERIFIED for s in field_states):
                    overall_fld = PARITY_UNVERIFIED
                elif all(s == PARITY_EXACT_MATCH for s in field_states):
                    overall_fld = PARITY_EXACT_MATCH
                elif all(
                    s in (PARITY_EXACT_MATCH, PARITY_EQUIVALENT_NORMALIZED)
                    for s in field_states
                ):
                    overall_fld = PARITY_EQUIVALENT_NORMALIZED
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
            if xml_ids == cand_ids
            and len(matched_ids) > 0
            and counts[PARITY_DIFFERENT_SEMANTICS] == 0
            and counts[PARITY_MISSING_FIELD] == 0
            and counts[PARITY_UNVERIFIED] == 0
            else "fail"
        )

    return {
        "role": str(role),
        "overall_status": overall_parity,
        "is_inconclusive": overall_parity == "inconclusive",
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
    if result.get("overall_status") in ("inconclusive", "fail"):
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
ALLOWED_REPORT_TOP_KEYS: frozenset[str] = frozenset(
    {
        "role",
        "overall_status",
        "is_inconclusive",
        "field_counts",
        "field_comparisons",
        "baseline_summary",
        "candidate_summary",
        "errors",
        "active_offers_by_item_id",
    }
)


def sanitize_offers_parity_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize report dictionary using a strict default-deny schema ensuring zero leaks."""
    if not isinstance(report, Mapping):
        return {}

    sanitized: dict[str, Any] = {}

    # 1. Role (strict enum whitelist)
    raw_role = str(report.get("role", "seller")).lower()
    sanitized["role"] = raw_role if raw_role in ("seller", "buyer") else "unknown"

    # 2. Overall status (strict enum whitelist)
    raw_status = str(report.get("overall_status", "inconclusive")).lower()
    sanitized["overall_status"] = (
        raw_status if raw_status in ("pass", "fail", "inconclusive") else "inconclusive"
    )

    # 3. is_inconclusive boolean flag
    sanitized["is_inconclusive"] = bool(
        report.get("is_inconclusive", sanitized["overall_status"] == "inconclusive")
    )

    # 4. Field counts (strict taxonomy keys, non-negative integer counts only)
    raw_counts = report.get("field_counts")
    sanitized_counts: dict[str, int] = {}
    if isinstance(raw_counts, Mapping):
        for tax_key in (
            PARITY_EXACT_MATCH,
            PARITY_EQUIVALENT_NORMALIZED,
            PARITY_DIFFERENT_SEMANTICS,
            PARITY_MISSING_FIELD,
            PARITY_UNVERIFIED,
            PARITY_UNSUPPORTED,
        ):
            if tax_key in raw_counts:
                val = raw_counts[tax_key]
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    sanitized_counts[tax_key] = max(0, int(val))
    sanitized["field_counts"] = sanitized_counts

    # 5. Field comparisons (strictly monitored fields only, safe keys, sanitized notes)
    raw_comps = report.get("field_comparisons")
    sanitized_comps: dict[str, dict[str, str]] = {}
    allowed_fields = set(SELLER_MONITORED_OFFERS_FIELDS) | set(
        BUYER_MONITORED_OFFERS_FIELDS
    )
    if isinstance(raw_comps, Mapping):
        for fld, fld_data in raw_comps.items():
            if fld in allowed_fields and isinstance(fld_data, Mapping):
                st = fld_data.get("status")
                if st not in PARITY_LABELS:
                    st = PARITY_UNVERIFIED
                lbl = PARITY_LABELS.get(st, "Unverified")
                clean_note = _resolve_safe_field_comparison_note(
                    st, fld_data.get("note")
                )
                sanitized_comps[fld] = {
                    "status": str(st),
                    "label": str(lbl),
                    "note": clean_note,
                }
    sanitized["field_comparisons"] = sanitized_comps

    # 6. Baseline and Candidate summaries (safe numeric counts only)
    for sum_key in ("baseline_summary", "candidate_summary"):
        raw_sum = report.get(sum_key)
        if isinstance(raw_sum, Mapping):
            sanitized[sum_key] = {
                "total_active_offers": max(
                    0, int(raw_sum.get("total_active_offers") or 0)
                ),
                "items_with_offers": max(0, int(raw_sum.get("items_with_offers") or 0)),
                "negotiations_count": max(
                    0, int(raw_sum.get("negotiations_count") or 0)
                ),
            }

    # 7. active_offers_by_item_id (strictly pseudonymized item IDs, integer counts)
    raw_items = report.get("active_offers_by_item_id")
    if isinstance(raw_items, Mapping):
        sanitized_items: dict[str, int] = {}
        for item_id, count in raw_items.items():
            key_str = str(item_id).strip()
            if not key_str:
                continue
            clean_key = (
                key_str
                if _is_safe_item_pseudonym(key_str)
                else pseudonymize_identifier(key_str, prefix="item")
            )
            try:
                clean_count = max(0, int(count))
            except (ValueError, TypeError):
                clean_count = 0
            sanitized_items[clean_key] = clean_count
        sanitized["active_offers_by_item_id"] = sanitized_items

    # 8. Errors (default-deny: symbolic codes only, safe generic diagnostics, no raw payloads)
    raw_errors = report.get("errors")
    sanitized_errors: list[dict[str, str]] = []
    if isinstance(raw_errors, Sequence) and not isinstance(raw_errors, (str, bytes)):
        for err in raw_errors:
            if not isinstance(err, Mapping):
                sanitized_errors.append(
                    {
                        "code": "MALFORMED_ERROR",
                        "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["MALFORMED_ERROR"],
                    }
                )
                continue
            raw_code = str(err.get("code") or "UNKNOWN_ERROR")
            token_code = sanitize_diagnostic_token(raw_code, kind="error_code")
            if (
                token_code not in ALLOWED_SYMBOLIC_ERROR_CODES
                and token_code not in ALLOWED_NUMERIC_ERROR_CODES
            ):
                token_code = "UNKNOWN_ERROR"

            clean_err: dict[str, str] = {"code": token_code}
            if "severity" in err:
                sev = str(err["severity"])
                if sev in ("Error", "Warning", "Info"):
                    clean_err["severity"] = sev

            if token_code in GENERIC_DIAGNOSTIC_MESSAGES:
                clean_err["diagnostic"] = GENERIC_DIAGNOSTIC_MESSAGES[token_code]
            elif token_code in ALLOWED_NUMERIC_ERROR_CODES:
                clean_err["diagnostic"] = f"eBay error code {token_code}"
            else:
                clean_err["diagnostic"] = "An error occurred during verification"

            sanitized_errors.append(clean_err)
    sanitized["errors"] = sanitized_errors

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
        note = (info.get("note") or "").replace("|", "-")
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
