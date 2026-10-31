"""Read-only Sold and Unsold Listing Classification XML vs. Modern API contract and parity helpers.

Used by ``scripts/validate_sold_unsold_parity.py`` and unit tests in Milestone 7 to
compare legacy Trading XML ``GetMyeBaySelling`` (``SoldList`` and ``UnsoldList``)
with candidate modern sources (GraphQL ``sellerListingSearch`` with ended listings
and REST Sell Fulfillment API ``getOrders``) without exposing credentials,
private financial amounts, or private listing identifiers.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any


from ..clients.graphql import (
    _parse_graphql_definitions,
    _strip_graphql_strings_and_comments,
    validate_read_only_query,
)
from ..const import NS
from .xml import (
    _text,
    _to_int,
    parse_ebay_time,
)

# ---------------------------------------------------------------------------
# Parity taxonomy constants (consistent across Milestones 1, 4, 5, 6, 7)
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

MONITORED_CLASSIFICATION_FIELDS: tuple[str, ...] = (
    "sold_items_count",
    "unsold_items_count",
    "sold_item_ids",
    "unsold_item_ids",
    "selling_classification",
    "item_id",
    "lifecycle_state",
    "sale_classification",
    "quantity_sold",
    "ended_at",
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

MAX_CLASSIFICATION_PAGES = 25
SOLD_UNSOLD_DEFAULT_WINDOW_DAYS = 60


def validate_max_pages(max_pages: int) -> int:
    """Validate that max_pages is an integer between 1 and 25 inclusive.

    Raises ValueError if max_pages is not an integer or is out of range [1, 25].
    """
    if not isinstance(max_pages, int) or isinstance(max_pages, bool):
        raise ValueError(
            f"max_pages must be an integer, got {type(max_pages).__name__}"
        )
    if max_pages < 1 or max_pages > MAX_CLASSIFICATION_PAGES:
        raise ValueError(
            f"max_pages must be between 1 and {MAX_CLASSIFICATION_PAGES}, got {max_pages}"
        )
    return max_pages


def pseudonymize_identifier(val: str, prefix: str = "id") -> str:
    """Return a deterministic, privacy-preserving 12-char SHA-256 pseudonym."""
    if not val:
        return ""
    h = hashlib.sha256(f"ebay_parity_salt:{val.strip()}".encode("utf-8")).hexdigest()[
        :12
    ]
    return f"{prefix}_{h}"


_ITEM_PSEUDONYM_RE = re.compile(r"^item_[0-9a-f]{12}$")


def _is_safe_item_pseudonym(key: str) -> bool:
    """Validate that key matches genuine item pseudonym format ^item_[0-9a-f]{12}$ and is not a raw numeric ID."""
    if not _ITEM_PSEUDONYM_RE.fullmatch(key):
        return False
    suffix = key[5:]
    if suffix.isdigit():
        return False
    return True


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
    text = _CREDENTIAL_KV_PATTERN.sub(r"\1=[REDACTED]", val)
    text = _EBAY_TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)
    text = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    text = _CURRENCY_AMOUNT_PATTERN.sub("[REDACTED_AMOUNT]", text)
    text = _NUMERIC_ID_PATTERN.sub("[REDACTED_ID]", text)
    text = re.sub(r"<[^>]*>", "[REDACTED_TAG]", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"[\1]", text)
    text = text.replace("|", "-")
    return text


_VARIABLE_DECL_RE = re.compile(
    r"\$([_A-Za-z][_0-9A-Za-z]*)\s*:\s*([^,=)]+?)(?:\s*=\s*([^,)]+))?(?=[,)])"
)

ALLOWED_SYMBOLIC_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ACCESS_DENIED",
        "ACCOUNT_MISMATCH",
        "BAD_GATEWAY",
        "BAD_USER_INPUT",
        "CONTRADICTORY_PAGINATION_TOTALS",
        "COUNT_ONLY_OPERATION",
        "COUNT_ONLY_QUERY",
        "DOWNSTREAM_SERVICE_ERROR",
        "DUPLICATE_ITEM_ID",
        "DUPLICATE_ITEM_ID_ACROSS_PAGES",
        "DUPLICATE_ORDER_ID",
        "EMPTY_XML_PAYLOAD",
        "FIELD_UNDEFINED",
        "FORBIDDEN",
        "GATEWAY_TIMEOUT",
        "GRAPHQL_ERROR",
        "GRAPHQL_PARSE_FAILED",
        "GRAPHQL_VALIDATION_ERROR",
        "GRAPHQL_VALIDATION_FAILED",
        "HANDSHAKE_ONLY",
        "INSUFFICIENT_SCOPE",
        "INTERNAL_SERVER_ERROR",
        "INTROSPECTION_DISABLED",
        "INVALID_ACCESS_TOKEN",
        "INVALID_QUERY",
        "INVALID_TOKEN",
        "INVALID_XML_ROOT",
        "ITEM_NOT_FOUND",
        "MALFORMED_ERROR",
        "MALFORMED_ORDER",
        "MALFORMED_PAGE",
        "MALFORMED_PAGE_INFO",
        "MAX_PAGES_EXCEEDED",
        "MISSING_DATA",
        "MISSING_ITEM_ID",
        "MISSING_PAGINATION_METADATA",
        "MISSING_REQUIRED_CONTAINER",
        "MISSING_SCOPE",
        "NO_ITEMS_CONTAINER",
        "NO_PAGES_PROVIDED",
        "NOT_AUTHORIZED",
        "NOT_IMPLEMENTED",
        "OPERATION_RESOLUTION_FAILURE",
        "PAGES_AFTER_END_DECLARED",
        "PAGINATION_CYCLE_DETECTED",
        "PAGINATION_SEQUENCE_ERROR",
        "PENDING_CONTINUATION",
        "PERSISTED_QUERY_NOT_FOUND",
        "PERSISTED_QUERY_NOT_SUPPORTED",
        "RATE_LIMITED",
        "RATE_LIMIT_EXCEEDED",
        "SERVICE_UNAVAILABLE",
        "SYSTEM_ERROR",
        "THROTTLED",
        "TOKEN_EXPIRED",
        "TOO_MANY_REQUESTS",
        "TOTAL_RECORDS_MISMATCH",
        "UNAUTHENTICATED",
        "UNAUTHORIZED",
        "UNKNOWN_ERROR",
        "UNVERIFIED_COLLECTION_CENSUS",
        "VALIDATION_ERROR",
        "WINDOW_MISMATCH",
        "XML_ACK_FAILURE",
        "XML_ACK_MISSING",
        "XML_ACK_PARTIAL_FAILURE",
        "XML_ACK_UNKNOWN",
        "XML_PARSE_ERROR",
    }
)

GENERIC_DIAGNOSTIC_MESSAGES: dict[str, str] = {
    "ACCESS_DENIED": "Access denied; authorization check failed",
    "ACCOUNT_MISMATCH": "Account identifier mismatch between candidate sources",
    "BAD_GATEWAY": "Bad gateway encountered communicating with remote service",
    "BAD_USER_INPUT": "Invalid argument or input parameter provided",
    "CONTRADICTORY_PAGINATION_TOTALS": "Declared pagination totals changed across pages",
    "COUNT_ONLY_OPERATION": "Count-only operation does not retrieve classification records",
    "COUNT_ONLY_QUERY": "Count-only operation does not retrieve classification records",
    "DOWNSTREAM_SERVICE_ERROR": "Downstream service error encountered",
    "DUPLICATE_ITEM_ID": "Duplicate item identifier encountered within page",
    "DUPLICATE_ITEM_ID_ACROSS_PAGES": "Duplicate item identifier encountered across pages",
    "DUPLICATE_ORDER_ID": "Duplicate order identifier encountered in response",
    "EMPTY_XML_PAYLOAD": "Trading XML payload is empty or whitespace",
    "FIELD_UNDEFINED": "Field undefined in GraphQL schema",
    "FORBIDDEN": "Access forbidden; insufficient permissions or scope",
    "GATEWAY_TIMEOUT": "Gateway timeout communicating with remote service",
    "GRAPHQL_ERROR": "GraphQL service returned an error condition",
    "GRAPHQL_PARSE_FAILED": "Failed to parse GraphQL document",
    "GRAPHQL_VALIDATION_ERROR": "GraphQL query validation failed against schema",
    "GRAPHQL_VALIDATION_FAILED": "GraphQL query validation failed against schema",
    "HANDSHAKE_ONLY": "Transport connectivity handshake only; no classification records retrieved",
    "INSUFFICIENT_SCOPE": "Insufficient OAuth scope for requested resource",
    "INTERNAL_SERVER_ERROR": "Internal server error encountered",
    "INTROSPECTION_DISABLED": "GraphQL schema introspection is disabled",
    "INVALID_ACCESS_TOKEN": "Access token is invalid or expired",
    "INVALID_QUERY": "Invalid GraphQL query document",
    "INVALID_TOKEN": "Access token is invalid",
    "INVALID_XML_ROOT": "Trading XML root element does not match expected operation",
    "ITEM_NOT_FOUND": "Item identifier was not found",
    "MALFORMED_ERROR": "Malformed error object encountered in response",
    "MALFORMED_ORDER": "Malformed order or line item encountered in response",
    "MALFORMED_PAGE": "Response page is not a valid structured payload",
    "MALFORMED_PAGE_INFO": "Pagination pageInfo is malformed or non-boolean",
    "MAX_PAGES_EXCEEDED": "Total pages exceeded maximum allowed limit of 25",
    "MISSING_DATA": "Response missing expected data field",
    "MISSING_ITEM_ID": "Classification record missing required identifier",
    "MISSING_PAGINATION_METADATA": "Response lacks required pagination metadata",
    "MISSING_REQUIRED_CONTAINER": "Missing required data container in response",
    "MISSING_SCOPE": "Missing required OAuth scope",
    "NO_ITEMS_CONTAINER": "No listings or orders container found in payload",
    "NO_PAGES_PROVIDED": "No response pages provided for collection",
    "NOT_AUTHORIZED": "Not authorized to access requested resource",
    "NOT_IMPLEMENTED": "Live validation is not implemented because GraphQL sellerListingSearch schema entitlement is unverified. Use offline fixtures (--xml-fixture, --graphql-fixture, --orders-fixture) or --synthetic-demo.",
    "OPERATION_RESOLUTION_FAILURE": "Unable to resolve GraphQL operation",
    "PAGES_AFTER_END_DECLARED": "Extra page received after final page was declared",
    "PAGINATION_CYCLE_DETECTED": "Duplicate page payload detected during pagination",
    "PAGINATION_SEQUENCE_ERROR": "Pagination page numbers out of sequence",
    "PENDING_CONTINUATION": "Final page has pending continuation; collection incomplete",
    "PERSISTED_QUERY_NOT_FOUND": "Persisted query identifier not found",
    "PERSISTED_QUERY_NOT_SUPPORTED": "Persisted queries not supported by endpoint",
    "RATE_LIMITED": "Request rate limit exceeded",
    "RATE_LIMIT_EXCEEDED": "Request rate limit exceeded",
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
    "WINDOW_MISMATCH": "Time window mismatch between baseline and candidate sources",
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
        "Field not provided in baseline Trading XML",
        "Field absent in both baseline and candidate",
    }
)

_SAFE_FIELD_NOTE_PATTERNS = (
    re.compile(r"^All \d+ item IDs matched exactly$"),
    re.compile(r"^All \d+ sold item IDs matched exactly$"),
    re.compile(r"^All \d+ unsold item IDs matched exactly$"),
    re.compile(r"^No matching item IDs between XML \(\d+\) and candidate \(\d+\)$"),
    re.compile(r"^Identity mismatch: \d+ missing in candidate$"),
    re.compile(r"^Identity mismatch: \d+ extra in candidate$"),
    re.compile(
        r"^Identity mismatch: \d+ missing in candidate; \d+ extra in candidate$"
    ),
    re.compile(r"^Evaluated across \d+ matched listings$"),
)


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
        "COMMERCE",
        "IDENTITY",
        "ORDER",
        "FULFILLMENT",
        "INVENTORY",
        "LISTING",
    }
)


def sanitize_diagnostic_token(token: Any, kind: str = "error_code") -> str:
    """Sanitize symbolic tokens and error codes."""
    if token is None or isinstance(token, bool):
        return "UNKNOWN_ERROR" if kind == "error_code" else "UNKNOWN"
    s = str(token).strip()
    if not s:
        return "UNKNOWN_ERROR" if kind == "error_code" else "UNKNOWN"
    if kind == "error_code":
        norm = s.upper().replace(" ", "_").replace("-", "_")
        if norm in ALLOWED_SYMBOLIC_ERROR_CODES:
            return norm
        if s in ALLOWED_NUMERIC_ERROR_CODES:
            return s
        return "UNKNOWN_ERROR"
    if kind == "category":
        norm = s.upper().replace(" ", "_")
        return norm if norm in ALLOWED_ERROR_CATEGORIES else "SYSTEM"
    if kind == "domain":
        norm = s.upper().replace(" ", "_")
        return norm if norm in ALLOWED_ERROR_DOMAINS else "COMMERCE"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Strict Canonical Enums and Typed Models
# ---------------------------------------------------------------------------
class ListingLifecycleState(StrEnum):
    """Lifecycle state of an eBay listing (separate from sale outcome)."""

    ACTIVE = "active"
    SCHEDULED = "scheduled"
    ENDED = "ended"
    UNKNOWN = "unknown"


class SaleClassification(StrEnum):
    """Outcome classification of an ended or sold listing."""

    SOLD = "sold"
    UNSOLD = "unsold"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class ClassificationEvidence(StrEnum):
    """Confidence level of classification evidence."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class ClassificationSource(StrEnum):
    """Origin provider of classification data."""

    TRADING_XML = "trading_xml"
    GRAPHQL = "graphql"
    REST_FULFILLMENT = "rest_fulfillment"
    COMPOSITE = "composite"


@dataclass(frozen=True)
class NormalizedListingClassification:
    """Canonical representation of a single listing's lifecycle and sale classification."""

    item_id: str
    lifecycle_state: ListingLifecycleState
    sale_classification: SaleClassification
    source: ClassificationSource
    quantity_sold: int | None = None
    quantity_total: int | None = None
    order_ids: tuple[str, ...] = ()
    ended_at: datetime | None = None
    evidence_level: ClassificationEvidence = ClassificationEvidence.UNKNOWN
    notes: str | None = None


@dataclass(frozen=True)
class SoldUnsoldCollectionResult:
    """Normalized snapshot of sold and unsold listings across a time window."""

    listings: tuple[NormalizedListingClassification, ...] = ()
    sold_item_ids: frozenset[str] = frozenset()
    unsold_item_ids: frozenset[str] = frozenset()
    selling_classification: dict[str, str] = field(default_factory=dict)
    sold_items_count: int = 0
    unsold_items_count: int = 0
    is_complete: bool = False  # Fail-closed: default incomplete without positive proof
    is_truncated: bool = False
    total_count: int | None = None
    account_id: str | None = None
    account_verified: bool = False
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS
    window_verified: bool = False
    reconciliation_ready: bool = False
    errors: tuple[dict[str, Any], ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Candidate Contract Probes (Read-Only GraphQL)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SoldUnsoldContractProbe:
    """Definition of an isolated read-only GraphQL probe for classification research."""

    key: str
    description: str
    query: str
    operation_name: str
    variables: dict[str, Any] = field(default_factory=dict)


SOLD_UNSOLD_CONTRACT_PROBES: tuple[SoldUnsoldContractProbe, ...] = (
    SoldUnsoldContractProbe(
        key="typename_handshake",
        description="Minimal GraphQL root __typename handshake",
        query="query Handshake { __typename }",
        operation_name="Handshake",
    ),
    SoldUnsoldContractProbe(
        key="ended_listings_count",
        description="Candidate sellerListingSearch ended listings count probe",
        query="query GetEndedListingsCount { sellerListingSearch(status: [ENDED]) { total } }",
        operation_name="GetEndedListingsCount",
    ),
    SoldUnsoldContractProbe(
        key="ended_listings_paginated",
        description="Candidate sellerListingSearch(status: [ENDED], limit, offset) probe",
        query=(
            "query GetEndedListingsPaginated($limit: Int, $offset: Int) {\n"
            "  sellerListingSearch(status: [ENDED], limit: $limit, offset: $offset) {\n"
            "    total\n"
            "    items {\n"
            "      itemId\n"
            "      title\n"
            "      quantity\n"
            "      quantitySold\n"
            "      itemEndDate\n"
            "      formatType\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetEndedListingsPaginated",
        variables={"limit": 50, "offset": 0},
    ),
    SoldUnsoldContractProbe(
        key="ended_listings_alternative",
        description="Candidate sellerListings with status=ENDED alternative probe",
        query=(
            "query GetEndedListingsAlternative {\n"
            '  sellerListings(status: "ENDED") {\n'
            "    totalCount\n"
            "    listings {\n"
            "      listingId\n"
            "      soldQuantity\n"
            "      availableQuantity\n"
            "      endTime\n"
            "      status\n"
            "    }\n"
            "  }\n"
            "}"
        ),
        operation_name="GetEndedListingsAlternative",
    ),
)


def validate_all_sold_unsold_contract_probes() -> None:
    """Verify all built-in sold/unsold probes pass read-only validation."""
    for probe in SOLD_UNSOLD_CONTRACT_PROBES:
        cleaned = _strip_graphql_strings_and_comments(probe.query)
        validate_read_only_query(cleaned)
        defs = _parse_graphql_definitions(cleaned)
        if not defs:
            raise ValueError(f"Probe {probe.key} has no operations")


# ---------------------------------------------------------------------------
# Legacy Trading XML Normalization
# ---------------------------------------------------------------------------
def normalize_trading_xml_sold_unsold(
    root_or_xml: str | bytes | ET.Element,
    *,
    requested_list: str = "both",
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> SoldUnsoldCollectionResult:
    """Normalize a single-page Trading XML GetMyeBaySelling (SoldList/UnsoldList) response."""
    if isinstance(root_or_xml, (str, bytes)):
        cleaned = (
            root_or_xml.decode("utf-8", errors="replace")
            if isinstance(root_or_xml, bytes)
            else root_or_xml
        ).strip()
        if not cleaned:
            return SoldUnsoldCollectionResult(
                listings=(),
                sold_item_ids=frozenset(),
                unsold_item_ids=frozenset(),
                selling_classification={},
                sold_items_count=0,
                unsold_items_count=0,
                is_complete=False,
                is_truncated=False,
                errors=(
                    {
                        "code": "EMPTY_XML_PAYLOAD",
                        "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["EMPTY_XML_PAYLOAD"],
                    },
                ),
            )
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            return SoldUnsoldCollectionResult(
                listings=(),
                sold_item_ids=frozenset(),
                unsold_item_ids=frozenset(),
                selling_classification={},
                sold_items_count=0,
                unsold_items_count=0,
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

    errors: list[dict[str, Any]] = []

    # Verify response root tag
    if not (
        root.tag.endswith("GetMyeBaySellingResponse")
        or root.tag == "GetMyeBaySellingResponse"
    ):
        return SoldUnsoldCollectionResult(
            listings=(),
            sold_item_ids=frozenset(),
            unsold_item_ids=frozenset(),
            selling_classification={},
            sold_items_count=0,
            unsold_items_count=0,
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

    # Check XML errors
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

    req_target = (
        "SoldList"
        if str(requested_list).lower().strip() in ("sold", "soldlist")
        else (
            "UnsoldList"
            if str(requested_list).lower().strip() in ("unsold", "unsoldlist")
            else "both"
        )
    )

    sold_container = root.find("e:SoldList", namespaces=NS)
    unsold_container = root.find("e:UnsoldList", namespaces=NS)

    if req_target == "both":
        if sold_container is None:
            errors.append(
                {
                    "code": "MISSING_REQUIRED_CONTAINER",
                    "diagnostic": "Missing SoldList container in combined Trading XML response",
                }
            )
        if unsold_container is None:
            errors.append(
                {
                    "code": "MISSING_REQUIRED_CONTAINER",
                    "diagnostic": "Missing UnsoldList container in combined Trading XML response",
                }
            )
    elif req_target == "SoldList" and sold_container is None:
        errors.append(
            {
                "code": "MISSING_REQUIRED_CONTAINER",
                "diagnostic": "Missing SoldList container in Trading XML response",
            }
        )
    elif req_target == "UnsoldList" and unsold_container is None:
        errors.append(
            {
                "code": "MISSING_REQUIRED_CONTAINER",
                "diagnostic": "Missing UnsoldList container in Trading XML response",
            }
        )

    sold_ids_list: list[str] = []
    unsold_ids_list: list[str] = []
    seen_sold: set[str] = set()
    seen_unsold: set[str] = set()

    # Diagnostics metadata for pagination reconciliation
    diag_info: dict[str, Any] = {}
    is_truncated = False

    containers_to_check = []
    if req_target in ("both", "SoldList") and sold_container is not None:
        containers_to_check.append(
            (sold_container, sold_ids_list, seen_sold, "SoldList")
        )
    if req_target in ("both", "UnsoldList") and unsold_container is not None:
        containers_to_check.append(
            (unsold_container, unsold_ids_list, seen_unsold, "UnsoldList")
        )

    for container, target_list, seen_set, c_name in containers_to_check:
        pag = container.find("e:PaginationResult", namespaces=NS)
        total_p = None
        total_e = None
        if pag is None:
            errors.append(
                {
                    "code": "MISSING_PAGINATION_METADATA",
                    "diagnostic": f"Container {c_name} missing required PaginationResult metadata",
                }
            )
        else:
            total_p = _to_int(_text(pag, "e:TotalNumberOfPages"))
            total_e = _to_int(_text(pag, "e:TotalNumberOfEntries"))
            diag_info[f"{c_name}_total_pages"] = total_p
            diag_info[f"{c_name}_total_entries"] = total_e
            if total_p is None or total_e is None:
                errors.append(
                    {
                        "code": "MISSING_PAGINATION_METADATA",
                        "diagnostic": f"Container {c_name} PaginationResult missing page or entry counts",
                    }
                )
            elif total_p < 1 or total_e < 0:
                errors.append(
                    {
                        "code": "MALFORMED_PAGE",
                        "diagnostic": f"Container {c_name} invalid total pages {total_p} or entries {total_e}",
                    }
                )
            elif total_p > 1:
                is_truncated = True
                errors.append(
                    {
                        "code": "PENDING_CONTINUATION",
                        "diagnostic": f"Single page provided but container {c_name} reports {total_p} total pages",
                    }
                )

        for item in container.findall("e:ItemArray/e:Item", namespaces=NS):
            i_id = _text(item, "e:ItemID")
            if not i_id:
                errors.append(
                    {
                        "code": "MISSING_ITEM_ID",
                        "diagnostic": f"Item in {c_name} missing required identifier",
                    }
                )
            elif i_id in seen_set:
                errors.append(
                    {
                        "code": "DUPLICATE_ITEM_ID",
                        "diagnostic": f"Duplicate item ID in {c_name}: {i_id}",
                    }
                )
            if i_id:
                seen_set.add(i_id)
                target_list.append(i_id)

        if total_p == 1 and total_e is not None:
            if len(target_list) != total_e:
                errors.append(
                    {
                        "code": "TOTAL_RECORDS_MISMATCH",
                        "diagnostic": f"Container {c_name} collected {len(target_list)} items but declared total is {total_e}",
                    }
                )

    # Apply established production precedence:
    # Unsold IDs first, then Sold IDs overwrite ("sold" precedence)
    classified: dict[str, str] = {i_id: "unsold" for i_id in unsold_ids_list}
    for i_id in sold_ids_list:
        classified[i_id] = "sold"

    all_ids = set(sold_ids_list) | set(unsold_ids_list)
    listings: list[NormalizedListingClassification] = []
    for i_id in sorted(all_ids):
        outcome = (
            SaleClassification.SOLD
            if classified.get(i_id) == "sold"
            else SaleClassification.UNSOLD
        )
        listings.append(
            NormalizedListingClassification(
                item_id=i_id,
                lifecycle_state=ListingLifecycleState.ENDED,
                sale_classification=outcome,
                source=ClassificationSource.TRADING_XML,
                evidence_level=ClassificationEvidence.HIGH,
            )
        )

    has_errors = len(errors) > 0
    has_expected_containers = (
        (sold_container is not None and unsold_container is not None)
        if req_target == "both"
        else (
            sold_container is not None
            if req_target == "SoldList"
            else unsold_container is not None
        )
    )
    is_complete = not has_errors and not is_truncated and has_expected_containers

    return SoldUnsoldCollectionResult(
        listings=tuple(listings),
        sold_item_ids=frozenset(sold_ids_list),
        unsold_item_ids=frozenset(unsold_ids_list),
        selling_classification=classified,
        sold_items_count=len(set(sold_ids_list)),
        unsold_items_count=len(set(unsold_ids_list)),
        is_complete=is_complete,
        is_truncated=is_truncated,
        total_count=len(all_ids),
        window_days=window_days,
        errors=tuple(errors),
        diagnostics=diag_info,
    )


def merge_trading_xml_sold_unsold_pages(
    pages: Sequence[str | bytes | ET.Element],
    *,
    list_name: str = "SoldList",
    max_pages: int = MAX_CLASSIFICATION_PAGES,
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> SoldUnsoldCollectionResult:
    """Merge multi-page Trading XML SoldList or UnsoldList responses fail-closed."""
    validate_max_pages(max_pages)
    target_list = (
        "SoldList"
        if str(list_name).lower().strip() in ("sold", "soldlist")
        else "UnsoldList"
    )
    if not pages:
        return SoldUnsoldCollectionResult(
            listings=(),
            sold_item_ids=frozenset(),
            unsold_item_ids=frozenset(),
            selling_classification={},
            sold_items_count=0,
            unsold_items_count=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "NO_PAGES_PROVIDED",
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["NO_PAGES_PROVIDED"],
                },
            ),
        )

    all_ids: list[str] = []
    seen_ids: set[str] = set()
    errors: list[dict[str, Any]] = []
    declared_total_pages: int | None = None
    declared_total_entries: int | None = None
    seen_page_hashes: set[str] = set()
    is_truncated = False
    processed_count = 0

    for page_idx, page in enumerate(pages):
        # Enforce bounds while processing
        if page_idx >= max_pages:
            is_truncated = True
            errors.append(
                {
                    "code": "MAX_PAGES_EXCEEDED",
                    "diagnostic": f"Total XML pages reached limit {max_pages} while processing page {page_idx + 1}",
                }
            )
            break

        processed_count += 1

        # Loop / cycle detection
        if isinstance(page, (str, bytes)):
            page_bytes = (
                page
                if isinstance(page, bytes)
                else page.encode("utf-8", errors="replace")
            )
            p_hash = hashlib.sha256(page_bytes).hexdigest()
            if p_hash in seen_page_hashes:
                errors.append(
                    {
                        "code": "PAGINATION_CYCLE_DETECTED",
                        "diagnostic": f"Duplicate page payload detected at page {page_idx + 1}",
                    }
                )
            seen_page_hashes.add(p_hash)

        page_res = normalize_trading_xml_sold_unsold(
            page, requested_list=target_list, window_days=window_days
        )
        # Suppress single-page PENDING_CONTINUATION errors during multi-page traversal
        for err in page_res.errors:
            if err.get("code") == "PENDING_CONTINUATION":
                continue
            errors.append(err)

        diag = page_res.diagnostics or {}
        p_total_pages = diag.get(f"{target_list}_total_pages")
        p_total_entries = diag.get(f"{target_list}_total_entries")

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

        # Reconcile pages after declared end
        if declared_total_pages is not None and (page_idx + 1) > declared_total_pages:
            errors.append(
                {
                    "code": "PAGES_AFTER_END_DECLARED",
                    "diagnostic": f"Page {page_idx + 1} received after final declared page {declared_total_pages}",
                }
            )

        page_ids = (
            page_res.sold_item_ids
            if target_list == "SoldList"
            else page_res.unsold_item_ids
        )
        for i_id in page_ids:
            if i_id in seen_ids:
                errors.append(
                    {
                        "code": "DUPLICATE_ITEM_ID_ACROSS_PAGES",
                        "diagnostic": f"Item ID {i_id} repeated across {target_list} pages",
                    }
                )
            seen_ids.add(i_id)
            all_ids.append(i_id)

    if declared_total_pages is not None and processed_count < declared_total_pages:
        is_truncated = True
        errors.append(
            {
                "code": "PENDING_CONTINUATION",
                "diagnostic": f"Retrieved {processed_count} of {declared_total_pages} declared XML pages",
            }
        )
    elif declared_total_pages is None:
        errors.append(
            {
                "code": "UNVERIFIED_COLLECTION_CENSUS",
                "diagnostic": "No declared total pages available to verify census completeness",
            }
        )

    if declared_total_entries is not None and len(all_ids) != declared_total_entries:
        errors.append(
            {
                "code": "TOTAL_RECORDS_MISMATCH",
                "diagnostic": f"Collected {len(all_ids)} entries but declared total is {declared_total_entries}",
            }
        )

    outcome = (
        SaleClassification.SOLD
        if target_list == "SoldList"
        else SaleClassification.UNSOLD
    )
    listings = tuple(
        NormalizedListingClassification(
            item_id=i_id,
            lifecycle_state=ListingLifecycleState.ENDED,
            sale_classification=outcome,
            source=ClassificationSource.TRADING_XML,
            evidence_level=ClassificationEvidence.HIGH,
        )
        for i_id in sorted(seen_ids)
    )

    sold_set = frozenset(seen_ids) if target_list == "SoldList" else frozenset()
    unsold_set = frozenset(seen_ids) if target_list == "UnsoldList" else frozenset()
    classified = {
        i_id: "sold" if target_list == "SoldList" else "unsold" for i_id in seen_ids
    }

    is_complete = (
        len(errors) == 0
        and not is_truncated
        and declared_total_pages is not None
        and declared_total_entries is not None
    )

    return SoldUnsoldCollectionResult(
        listings=listings,
        sold_item_ids=sold_set,
        unsold_item_ids=unsold_set,
        selling_classification=classified,
        sold_items_count=len(sold_set),
        unsold_items_count=len(unsold_set),
        is_complete=is_complete,
        is_truncated=is_truncated,
        total_count=declared_total_entries
        if declared_total_entries is not None
        else len(seen_ids),
        window_days=window_days,
        errors=tuple(errors),
    )


def merge_trading_xml_combined_responses(
    pages: Sequence[str | bytes | ET.Element],
    *,
    max_pages: int = MAX_CLASSIFICATION_PAGES,
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> SoldUnsoldCollectionResult:
    """Merge multi-page Trading XML responses containing both SoldList and UnsoldList."""
    validate_max_pages(max_pages)
    if not pages:
        return SoldUnsoldCollectionResult(
            listings=(),
            sold_item_ids=frozenset(),
            unsold_item_ids=frozenset(),
            selling_classification={},
            sold_items_count=0,
            unsold_items_count=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "NO_PAGES_PROVIDED",
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["NO_PAGES_PROVIDED"],
                },
            ),
        )

    sold_res = merge_trading_xml_sold_unsold_pages(
        pages, list_name="SoldList", max_pages=max_pages, window_days=window_days
    )
    unsold_res = merge_trading_xml_sold_unsold_pages(
        pages, list_name="UnsoldList", max_pages=max_pages, window_days=window_days
    )

    all_errors = [*sold_res.errors, *unsold_res.errors]
    is_complete = sold_res.is_complete and unsold_res.is_complete
    is_truncated = sold_res.is_truncated or unsold_res.is_truncated

    # Apply sold precedence:
    classified: dict[str, str] = {i_id: "unsold" for i_id in unsold_res.unsold_item_ids}
    for i_id in sold_res.sold_item_ids:
        classified[i_id] = "sold"

    all_ids = set(sold_res.sold_item_ids) | set(unsold_res.unsold_item_ids)
    listings = tuple(
        NormalizedListingClassification(
            item_id=i_id,
            lifecycle_state=ListingLifecycleState.ENDED,
            sale_classification=SaleClassification.SOLD
            if classified.get(i_id) == "sold"
            else SaleClassification.UNSOLD,
            source=ClassificationSource.TRADING_XML,
            evidence_level=ClassificationEvidence.HIGH,
        )
        for i_id in sorted(all_ids)
    )

    return SoldUnsoldCollectionResult(
        listings=listings,
        sold_item_ids=sold_res.sold_item_ids,
        unsold_item_ids=unsold_res.unsold_item_ids,
        selling_classification=classified,
        sold_items_count=len(sold_res.sold_item_ids),
        unsold_items_count=len(unsold_res.unsold_item_ids),
        is_complete=is_complete,
        is_truncated=is_truncated,
        total_count=len(all_ids),
        window_days=window_days,
        errors=tuple(all_errors),
    )


# ---------------------------------------------------------------------------
# Modern GraphQL Listing API Normalization
# ---------------------------------------------------------------------------
def normalize_graphql_seller_listing_search(
    payload: Mapping[str, Any] | None,
    *,
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> SoldUnsoldCollectionResult:
    """Normalize GraphQL sellerListingSearch ended listings payload fail-closed.

    Critical rules:
    - Ended listings are NOT automatically classified as unsold.
    - If quantitySold > 0, they are classified as SOLD.
    - If quantitySold == 0 and lifecycle is ENDED, they are classified as UNSOLD.
    - Active, scheduled, or unknown listings with quantitySold == 0 are NOT UNSOLD.
    - Missing pagination metadata, contradictory totals, or unverified census fail-closed.
    """
    if not isinstance(payload, Mapping):
        return SoldUnsoldCollectionResult(
            listings=(),
            sold_item_ids=frozenset(),
            unsold_item_ids=frozenset(),
            selling_classification={},
            sold_items_count=0,
            unsold_items_count=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "MALFORMED_PAGE",
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["MALFORMED_PAGE"],
                },
            ),
        )

    errors: list[dict[str, Any]] = []

    # Check top-level GraphQL errors
    raw_errors = payload.get("errors")
    if isinstance(raw_errors, Sequence) and not isinstance(raw_errors, (str, bytes)):
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
                        "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES.get(
                            sanitize_diagnostic_token(code, kind="error_code"),
                            "GraphQL service returned an error condition",
                        ),
                    }
                )

    data = payload.get("data")
    if not isinstance(data, Mapping):
        if not errors:
            errors.append(
                {
                    "code": "MISSING_DATA",
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["MISSING_DATA"],
                }
            )
        return SoldUnsoldCollectionResult(
            listings=(),
            sold_item_ids=frozenset(),
            unsold_item_ids=frozenset(),
            selling_classification={},
            sold_items_count=0,
            unsold_items_count=0,
            is_complete=False,
            is_truncated=False,
            errors=tuple(errors),
        )

    # Handshake-only check
    if set(data.keys()) == {"__typename"}:
        errors.append(
            {
                "code": "HANDSHAKE_ONLY",
                "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["HANDSHAKE_ONLY"],
            }
        )
        return SoldUnsoldCollectionResult(
            listings=(),
            sold_item_ids=frozenset(),
            unsold_item_ids=frozenset(),
            selling_classification={},
            sold_items_count=0,
            unsold_items_count=0,
            is_complete=False,
            is_truncated=False,
            errors=tuple(errors),
            diagnostics={"handshake_typename": data.get("__typename")},
        )

    # Locate container
    container: Mapping[str, Any] | None = None
    for k in ("sellerListingSearch", "sellerListings", "endedListings", "items"):
        if k in data and isinstance(data[k], Mapping):
            container = data[k]
            break

    if container is None:
        errors.append(
            {
                "code": "NO_ITEMS_CONTAINER",
                "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["NO_ITEMS_CONTAINER"],
            }
        )
        return SoldUnsoldCollectionResult(
            listings=(),
            sold_item_ids=frozenset(),
            unsold_item_ids=frozenset(),
            selling_classification={},
            sold_items_count=0,
            unsold_items_count=0,
            is_complete=False,
            is_truncated=False,
            errors=tuple(errors),
        )

    # Check for officially documented grouped result buckets
    buckets_to_process: list[tuple[str, Mapping[str, Any], ListingLifecycleState]] = []
    for b_key, b_lifecycle in (
        ("endedListings", ListingLifecycleState.ENDED),
        ("activeListings", ListingLifecycleState.ACTIVE),
        ("scheduledListings", ListingLifecycleState.SCHEDULED),
    ):
        if b_key in container and isinstance(container[b_key], Mapping):
            buckets_to_process.append((b_key, container[b_key], b_lifecycle))

    is_grouped = len(buckets_to_process) > 0

    declared_total = _to_int(
        container.get("total")
        if "total" in container
        else container.get("totalCount")
        if "totalCount" in container
        else container.get("totalEntries")
    )

    page_info = (
        container.get("pageInfo")
        if isinstance(container.get("pageInfo"), Mapping)
        else None
    )
    is_truncated = False
    if page_info is not None:
        has_next = page_info.get("hasNextPage")
        if has_next is True:
            is_truncated = True
            errors.append(
                {
                    "code": "PENDING_CONTINUATION",
                    "diagnostic": "GraphQL collection has pending continuation (hasNextPage=True)",
                }
            )
        elif has_next is not None and not isinstance(has_next, bool):
            errors.append(
                {
                    "code": "MALFORMED_PAGE_INFO",
                    "diagnostic": "pageInfo.hasNextPage must be boolean",
                }
            )

    listings: list[NormalizedListingClassification] = []
    sold_ids: set[str] = set()
    unsold_ids: set[str] = set()
    classified: dict[str, str] = {}
    seen_ids: set[str] = set()

    if is_grouped:
        # Process each official grouped bucket independently
        bucket_totals = 0
        for b_name, b_data, default_lifecycle in buckets_to_process:
            b_total = _to_int(
                b_data.get("total")
                if "total" in b_data
                else b_data.get("totalCount")
                if "totalCount" in b_data
                else b_data.get("totalEntries")
            )
            if b_total is not None:
                bucket_totals += b_total

            # Validate each grouped bucket's own pagination metadata independently
            b_page_info = (
                b_data.get("pageInfo")
                if isinstance(b_data.get("pageInfo"), Mapping)
                else None
            )
            if b_page_info is not None:
                b_has_next = b_page_info.get("hasNextPage")
                if b_has_next is True:
                    is_truncated = True
                    errors.append(
                        {
                            "code": "PENDING_CONTINUATION",
                            "diagnostic": f"Bucket {b_name} has pending continuation (hasNextPage=True)",
                        }
                    )
                elif b_has_next is not None and not isinstance(b_has_next, bool):
                    errors.append(
                        {
                            "code": "MALFORMED_PAGE_INFO",
                            "diagnostic": f"Bucket {b_name} pageInfo.hasNextPage must be boolean",
                        }
                    )

            b_items = (
                b_data.get("items")
                if "items" in b_data
                else b_data.get("listings")
                if "listings" in b_data
                else b_data.get("edges")
            ) or []
            bucket_item_count = 0
            if isinstance(b_items, Sequence) and not isinstance(b_items, (str, bytes)):
                for raw in b_items:
                    node = (
                        raw.get("node")
                        if isinstance(raw, Mapping) and "node" in raw
                        else raw
                    )
                    if not isinstance(node, Mapping):
                        continue
                    bucket_item_count += 1
                    i_id = str(
                        node.get("itemId") or node.get("listingId") or ""
                    ).strip()
                    if not i_id:
                        errors.append(
                            {
                                "code": "MISSING_ITEM_ID",
                                "diagnostic": f"Listing in bucket {b_name} missing required identifier",
                            }
                        )
                        continue
                    if i_id in seen_ids:
                        errors.append(
                            {
                                "code": "DUPLICATE_ITEM_ID",
                                "diagnostic": f"Duplicate item ID in bucket {b_name}: {i_id}",
                            }
                        )
                    seen_ids.add(i_id)

                    raw_q_sold = (
                        node.get("quantitySold")
                        if "quantitySold" in node
                        and node.get("quantitySold") is not None
                        else node.get("soldQuantity")
                    )
                    q_sold = _to_int(raw_q_sold)
                    raw_q_total = (
                        node.get("quantity")
                        if "quantity" in node and node.get("quantity") is not None
                        else node.get("totalQuantity")
                    )
                    q_total = _to_int(raw_q_total)
                    end_time_raw = node.get("itemEndDate") or node.get("endTime")
                    ended_at = _parse_iso_datetime(end_time_raw)

                    # Classification logic for grouped buckets:
                    if default_lifecycle == ListingLifecycleState.ENDED:
                        now_utc = datetime.now(timezone.utc)
                        cutoff_utc = now_utc - timedelta(days=window_days)
                        is_undated = ended_at is None
                        is_off_window = False
                        if ended_at is not None:
                            if ended_at < (
                                cutoff_utc - timedelta(days=1)
                            ) or ended_at > (now_utc + timedelta(days=1)):
                                is_off_window = True

                        if q_sold is not None and q_sold > 0:
                            outcome = SaleClassification.SOLD
                            evidence = ClassificationEvidence.HIGH
                            sold_ids.add(i_id)
                            classified[i_id] = "sold"
                        elif q_sold is not None and q_sold == 0:
                            if is_off_window:
                                outcome = SaleClassification.UNKNOWN
                                evidence = ClassificationEvidence.LOW
                                classified[i_id] = "unknown"
                            elif is_undated:
                                outcome = SaleClassification.UNSOLD
                                evidence = ClassificationEvidence.MEDIUM
                                unsold_ids.add(i_id)
                                classified[i_id] = "unsold"
                            else:
                                outcome = SaleClassification.UNSOLD
                                evidence = ClassificationEvidence.HIGH
                                unsold_ids.add(i_id)
                                classified[i_id] = "unsold"
                        else:
                            outcome = SaleClassification.UNKNOWN
                            evidence = ClassificationEvidence.UNKNOWN
                            classified[i_id] = "unknown"
                    else:
                        # Active or scheduled listings with 0 sold are NOT unsold
                        outcome = SaleClassification.UNKNOWN
                        evidence = ClassificationEvidence.LOW
                        classified[i_id] = "unknown"

                    listings.append(
                        NormalizedListingClassification(
                            item_id=i_id,
                            lifecycle_state=default_lifecycle,
                            sale_classification=outcome,
                            source=ClassificationSource.GRAPHQL,
                            quantity_sold=q_sold,
                            quantity_total=q_total,
                            ended_at=ended_at,
                            evidence_level=evidence,
                        )
                    )

            if b_total is not None and bucket_item_count != b_total:
                errors.append(
                    {
                        "code": "TOTAL_RECORDS_MISMATCH",
                        "diagnostic": f"Bucket {b_name} collected {bucket_item_count} items but declared total is {b_total}",
                    }
                )

        if declared_total is None and bucket_totals > 0:
            declared_total = bucket_totals
    else:
        # Flat container processing
        if page_info is None and declared_total is None:
            errors.append(
                {
                    "code": "UNVERIFIED_COLLECTION_CENSUS",
                    "diagnostic": "Flat GraphQL response lacks both total and pageInfo pagination metadata",
                }
            )

        raw_items = (
            container.get("items")
            if "items" in container
            else container.get("listings")
            if "listings" in container
            else container.get("edges")
        )

        if raw_items is None:
            # Check if count-only query
            if declared_total is not None and not any(
                k in container for k in ("items", "listings", "edges")
            ):
                errors.append(
                    {
                        "code": "COUNT_ONLY_QUERY",
                        "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["COUNT_ONLY_QUERY"],
                    }
                )
                return SoldUnsoldCollectionResult(
                    listings=(),
                    sold_item_ids=frozenset(),
                    unsold_item_ids=frozenset(),
                    selling_classification={},
                    sold_items_count=0,
                    unsold_items_count=0,
                    is_complete=False,
                    is_truncated=False,
                    total_count=declared_total,
                    errors=tuple(errors),
                )
            raw_items = []

        for raw in raw_items:
            node = (
                raw.get("node") if isinstance(raw, Mapping) and "node" in raw else raw
            )
            if not isinstance(node, Mapping):
                continue
            i_id = str(node.get("itemId") or node.get("listingId") or "").strip()
            if not i_id:
                errors.append(
                    {
                        "code": "MISSING_ITEM_ID",
                        "diagnostic": "Listing record missing required itemId or listingId",
                    }
                )
                continue
            if i_id in seen_ids:
                errors.append(
                    {
                        "code": "DUPLICATE_ITEM_ID",
                        "diagnostic": f"Duplicate item ID encountered in page: {i_id}",
                    }
                )
            seen_ids.add(i_id)

            # Strict lifecycle state mapping: NEVER default unknown status to ENDED!
            raw_status = str(node.get("status") or "").upper().strip()
            if raw_status in ("ENDED", "COMPLETED", "CLOSED"):
                lifecycle = ListingLifecycleState.ENDED
            elif raw_status in ("ACTIVE", "LIVE"):
                lifecycle = ListingLifecycleState.ACTIVE
            elif raw_status in ("SCHEDULED",):
                lifecycle = ListingLifecycleState.SCHEDULED
            else:
                lifecycle = ListingLifecycleState.UNKNOWN

            raw_q_sold = (
                node.get("quantitySold")
                if "quantitySold" in node and node.get("quantitySold") is not None
                else node.get("soldQuantity")
            )
            q_sold = _to_int(raw_q_sold)
            raw_q_total = (
                node.get("quantity")
                if "quantity" in node and node.get("quantity") is not None
                else node.get("totalQuantity")
            )
            q_total = _to_int(raw_q_total)
            end_time_raw = node.get("itemEndDate") or node.get("endTime")
            ended_at = _parse_iso_datetime(end_time_raw)

            # Strict classification outcome
            if lifecycle == ListingLifecycleState.ENDED:
                now_utc = datetime.now(timezone.utc)
                cutoff_utc = now_utc - timedelta(days=window_days)
                is_undated = ended_at is None
                is_off_window = False
                if ended_at is not None:
                    if ended_at < (cutoff_utc - timedelta(days=1)) or ended_at > (
                        now_utc + timedelta(days=1)
                    ):
                        is_off_window = True

                if q_sold is not None and q_sold > 0:
                    outcome = SaleClassification.SOLD
                    evidence = ClassificationEvidence.HIGH
                    sold_ids.add(i_id)
                    classified[i_id] = "sold"
                elif q_sold is not None and q_sold == 0:
                    if is_off_window:
                        outcome = SaleClassification.UNKNOWN
                        evidence = ClassificationEvidence.LOW
                        classified[i_id] = "unknown"
                    elif is_undated:
                        outcome = SaleClassification.UNSOLD
                        evidence = ClassificationEvidence.MEDIUM
                        unsold_ids.add(i_id)
                        classified[i_id] = "unsold"
                    else:
                        outcome = SaleClassification.UNSOLD
                        evidence = ClassificationEvidence.HIGH
                        unsold_ids.add(i_id)
                        classified[i_id] = "unsold"
                else:
                    outcome = SaleClassification.UNKNOWN
                    evidence = ClassificationEvidence.UNKNOWN
                    classified[i_id] = "unknown"
            else:
                # Active, scheduled, or unknown listings with 0 sold are NOT unsold
                outcome = SaleClassification.UNKNOWN
                evidence = ClassificationEvidence.LOW
                classified[i_id] = "unknown"

            listings.append(
                NormalizedListingClassification(
                    item_id=i_id,
                    lifecycle_state=lifecycle,
                    sale_classification=outcome,
                    source=ClassificationSource.GRAPHQL,
                    quantity_sold=q_sold,
                    quantity_total=q_total,
                    ended_at=ended_at,
                    evidence_level=evidence,
                )
            )

    # Fail-closed census checks
    if declared_total is None and (
        page_info is None or page_info.get("hasNextPage") is None
    ):
        errors.append(
            {
                "code": "UNVERIFIED_COLLECTION_CENSUS",
                "diagnostic": "No total count or pageInfo provided in GraphQL response to verify completeness",
            }
        )

    if declared_total is not None and len(listings) != declared_total:
        errors.append(
            {
                "code": "TOTAL_RECORDS_MISMATCH",
                "diagnostic": f"Parsed {len(listings)} items but declared total is {declared_total}",
            }
        )

    is_complete = (
        len(errors) == 0
        and not is_truncated
        and (
            declared_total is not None
            or (page_info is not None and page_info.get("hasNextPage") is False)
        )
    )

    account_id = (
        container.get("accountId") or container.get("sellerId") or data.get("accountId")
    )
    account_verified = bool(account_id)
    has_ended = any(
        item.lifecycle_state == ListingLifecycleState.ENDED for item in listings
    )
    window_verified = (
        not is_truncated
        and not any(
            err.get("code") in ("WINDOW_MISMATCH", "TOTAL_RECORDS_MISMATCH")
            for err in errors
        )
        and (
            not has_ended
            or all(
                item.ended_at is not None
                for item in listings
                if item.lifecycle_state == ListingLifecycleState.ENDED
            )
        )
    )
    reconciliation_ready = (
        is_complete and account_verified and window_verified and len(errors) == 0
    )

    return SoldUnsoldCollectionResult(
        listings=tuple(listings),
        sold_item_ids=frozenset(sold_ids),
        unsold_item_ids=frozenset(unsold_ids),
        selling_classification=classified,
        sold_items_count=len(sold_ids),
        unsold_items_count=len(unsold_ids),
        is_complete=is_complete,
        is_truncated=is_truncated,
        total_count=declared_total if declared_total is not None else len(listings),
        account_id=str(account_id) if account_id else None,
        account_verified=account_verified,
        window_days=window_days,
        window_verified=window_verified,
        reconciliation_ready=reconciliation_ready,
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# REST Sell Fulfillment API getOrders Normalization
# ---------------------------------------------------------------------------
def normalize_rest_fulfillment_orders(
    payload: Mapping[str, Any] | None,
    *,
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> SoldUnsoldCollectionResult:
    """Normalize REST Sell Fulfillment getOrders response.

    Fulfillment orders confirm sales (SOLD). An order contains line items,
    which identify the sold legacyItemId / listingId.
    Orders DO NOT identify unsold items (unsold items generate 0 orders).
    Order count != sold items count (multi-item orders, multi-quantity).
    """
    if not isinstance(payload, Mapping):
        return SoldUnsoldCollectionResult(
            listings=(),
            sold_item_ids=frozenset(),
            unsold_item_ids=frozenset(),
            selling_classification={},
            sold_items_count=0,
            unsold_items_count=0,
            is_complete=False,
            is_truncated=False,
            errors=(
                {
                    "code": "MALFORMED_PAGE",
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["MALFORMED_PAGE"],
                },
            ),
        )

    errors: list[dict[str, Any]] = []

    # Check for REST error arrays
    if "errors" in payload and isinstance(payload["errors"], Sequence):
        for err in payload["errors"]:
            if isinstance(err, Mapping):
                code = err.get("errorId") or err.get("errorCode") or "API_ERROR"
                errors.append(
                    {
                        "code": sanitize_diagnostic_token(code, kind="error_code"),
                        "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES.get(
                            sanitize_diagnostic_token(code, kind="error_code"),
                            "REST Fulfillment API returned an error condition",
                        ),
                    }
                )

    raw_orders = payload.get("orders")
    declared_total = _to_int(payload.get("total") if "total" in payload else None)
    offset = _to_int(payload.get("offset") if "offset" in payload else None)
    limit = _to_int(payload.get("limit") if "limit" in payload else None)
    has_next_link = bool(payload.get("next"))
    is_truncated = False

    if raw_orders is None:
        if declared_total == 0:
            raw_orders = []
        else:
            errors.append(
                {
                    "code": "MISSING_REQUIRED_CONTAINER",
                    "diagnostic": "Missing orders array in Fulfillment API payload",
                }
            )
            raw_orders = []
    elif not isinstance(raw_orders, Sequence) or isinstance(raw_orders, (str, bytes)):
        errors.append(
            {
                "code": "MISSING_REQUIRED_CONTAINER",
                "diagnostic": "Orders container is not a sequence",
            }
        )
        raw_orders = []

    # Check required pagination metadata
    if declared_total is None or offset is None or limit is None:
        errors.append(
            {
                "code": "MISSING_PAGINATION_METADATA",
                "diagnostic": "Fulfillment response missing required pagination metadata (total, offset, or limit)",
            }
        )

    # Offset greater than 0 means terminal or partial page, not a complete census
    if offset is not None and offset > 0:
        is_truncated = True
        errors.append(
            {
                "code": "PENDING_CONTINUATION",
                "diagnostic": f"Fulfillment response starts at offset {offset}; incomplete census without preceding pages",
            }
        )

    if has_next_link or (
        offset is not None
        and limit is not None
        and declared_total is not None
        and (offset + len(raw_orders) < declared_total)
    ):
        is_truncated = True
        errors.append(
            {
                "code": "PENDING_CONTINUATION",
                "diagnostic": "Fulfillment response is partial page with pending continuation",
            }
        )

    if (
        declared_total is not None
        and len(raw_orders) != declared_total
        and not is_truncated
        and offset == 0
    ):
        errors.append(
            {
                "code": "TOTAL_RECORDS_MISMATCH",
                "diagnostic": f"Collected {len(raw_orders)} orders but declared total is {declared_total}",
            }
        )

    sold_by_item: dict[str, int] = {}
    orders_by_item: dict[str, list[str]] = {}
    seen_order_ids: set[str] = set()

    if isinstance(raw_orders, Sequence) and not isinstance(raw_orders, (str, bytes)):
        for order in raw_orders:
            if not isinstance(order, Mapping):
                errors.append(
                    {
                        "code": "MALFORMED_ORDER",
                        "diagnostic": "Order record is not a structured mapping",
                    }
                )
                continue
            order_id = str(order.get("orderId") or "").strip()
            if not order_id:
                errors.append(
                    {
                        "code": "MALFORMED_ORDER",
                        "diagnostic": "Order record missing orderId",
                    }
                )
                continue
            if order_id in seen_order_ids:
                errors.append(
                    {
                        "code": "DUPLICATE_ORDER_ID",
                        "diagnostic": f"Duplicate order ID in response: {order_id}",
                    }
                )
            seen_order_ids.add(order_id)

            # Check cancel and payment status
            cancel_status = (
                order.get("cancelStatus")
                if isinstance(order.get("cancelStatus"), Mapping)
                else {}
            )
            cancel_state = str(cancel_status.get("cancelState") or "").upper()
            if cancel_state == "CANCELED":
                # Canceled orders are not evidence of completed sales
                continue

            line_items = order.get("lineItems")
            if not isinstance(line_items, Sequence) or not line_items:
                errors.append(
                    {
                        "code": "MALFORMED_ORDER",
                        "diagnostic": f"Order {order_id} missing line items",
                    }
                )
                continue

            for line in line_items:
                if not isinstance(line, Mapping):
                    errors.append(
                        {
                            "code": "MALFORMED_ORDER",
                            "diagnostic": f"Line item in order {order_id} is not a structured mapping",
                        }
                    )
                    continue
                item_id = str(
                    line.get("legacyItemId")
                    or line.get("listingId")
                    or line.get("itemId")
                    or ""
                ).strip()
                if not item_id:
                    errors.append(
                        {
                            "code": "MISSING_ITEM_ID",
                            "diagnostic": f"Line item in order {order_id} missing listing identifier",
                        }
                    )
                    continue

                raw_qty = line.get("quantity")
                parsed_qty = _to_int(raw_qty)
                if parsed_qty is None or parsed_qty <= 0:
                    errors.append(
                        {
                            "code": "MALFORMED_ORDER",
                            "diagnostic": f"Line item in order {order_id} has invalid quantity {raw_qty}",
                        }
                    )
                    qty = 0
                else:
                    qty = parsed_qty

                sold_by_item[item_id] = sold_by_item.get(item_id, 0) + qty
                orders_by_item.setdefault(item_id, []).append(order_id)

    listings: list[NormalizedListingClassification] = []
    sold_ids: set[str] = set(sold_by_item.keys())
    classified: dict[str, str] = {}

    for i_id in sorted(sold_ids):
        classified[i_id] = "sold"
        listings.append(
            NormalizedListingClassification(
                item_id=i_id,
                lifecycle_state=ListingLifecycleState.UNKNOWN,  # Order confirms sale, not that listing has ended!
                sale_classification=SaleClassification.SOLD,
                source=ClassificationSource.REST_FULFILLMENT,
                quantity_sold=sold_by_item[i_id],
                order_ids=tuple(orders_by_item.get(i_id, ())),
                evidence_level=ClassificationEvidence.HIGH
                if not errors and not is_truncated
                else ClassificationEvidence.MEDIUM,
            )
        )

    is_complete = (
        len(errors) == 0
        and not is_truncated
        and declared_total is not None
        and offset == 0
        and limit is not None
        and len(raw_orders) == declared_total
    )
    account_id = payload.get("accountId") or payload.get("sellerId")
    account_verified = bool(account_id)
    window_verified = not is_truncated and not any(
        err.get("code") in ("WINDOW_MISMATCH", "TOTAL_RECORDS_MISMATCH")
        for err in errors
    )
    reconciliation_ready = (
        is_complete and account_verified and window_verified and len(errors) == 0
    )

    return SoldUnsoldCollectionResult(
        listings=tuple(listings),
        sold_item_ids=frozenset(sold_ids),
        unsold_item_ids=frozenset(),  # Orders API can NEVER confirm unsold items
        selling_classification=classified,
        sold_items_count=len(sold_ids),
        unsold_items_count=0,
        is_complete=is_complete,
        is_truncated=is_truncated,
        total_count=declared_total if declared_total is not None else len(raw_orders),
        account_id=str(account_id) if account_id else None,
        account_verified=account_verified,
        window_days=window_days,
        window_verified=window_verified,
        reconciliation_ready=reconciliation_ready,
        errors=tuple(errors),
        diagnostics={
            "orders_count": len(raw_orders),
            "unique_sold_items_count": len(sold_ids),
        },
    )


def merge_rest_fulfillment_orders_pages(
    pages: Sequence[Mapping[str, Any]],
    *,
    max_pages: int = MAX_CLASSIFICATION_PAGES,
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> SoldUnsoldCollectionResult:
    """Merge multi-page REST Sell Fulfillment getOrders responses fail-closed."""
    validate_max_pages(max_pages)
    if not pages:
        return SoldUnsoldCollectionResult(
            listings=(),
            sold_item_ids=frozenset(),
            unsold_item_ids=frozenset(),
            selling_classification={},
            sold_items_count=0,
            unsold_items_count=0,
            is_complete=False,
            is_truncated=False,
            account_verified=False,
            window_verified=False,
            reconciliation_ready=False,
            errors=(
                {
                    "code": "NO_PAGES_PROVIDED",
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["NO_PAGES_PROVIDED"],
                },
            ),
        )

    all_orders: list[Any] = []
    errors: list[dict[str, Any]] = []
    declared_total: int | None = None
    expected_offset = 0
    is_truncated = False
    account_id: str | None = None
    last_limit: int | None = None

    for page_idx, page in enumerate(pages):
        # Enforce bounds while processing
        if page_idx >= max_pages:
            is_truncated = True
            errors.append(
                {
                    "code": "MAX_PAGES_EXCEEDED",
                    "diagnostic": f"Total REST pages reached limit {max_pages} while processing page {page_idx + 1}",
                }
            )
            break

        if not isinstance(page, Mapping):
            errors.append(
                {
                    "code": "MALFORMED_PAGE",
                    "diagnostic": f"REST page {page_idx + 1} is not a valid mapping",
                }
            )
            continue

        # Check for errors in page
        if "errors" in page and isinstance(page["errors"], Sequence):
            for err in page["errors"]:
                if isinstance(err, Mapping):
                    code = err.get("errorId") or err.get("errorCode") or "API_ERROR"
                    errors.append(
                        {
                            "code": sanitize_diagnostic_token(code, kind="error_code"),
                            "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES.get(
                                sanitize_diagnostic_token(code, kind="error_code"),
                                "REST Fulfillment API returned an error condition",
                            ),
                        }
                    )

        p_total = _to_int(page.get("total") if "total" in page else None)
        p_offset = _to_int(page.get("offset") if "offset" in page else None)
        p_limit = _to_int(page.get("limit") if "limit" in page else None)

        if p_limit is not None:
            last_limit = p_limit

        if p_total is None or p_offset is None or p_limit is None:
            errors.append(
                {
                    "code": "MISSING_PAGINATION_METADATA",
                    "diagnostic": f"Page {page_idx + 1} missing required pagination metadata (total, offset, limit)",
                }
            )

        if declared_total is None and p_total is not None:
            declared_total = p_total
        elif (
            declared_total is not None
            and p_total is not None
            and declared_total != p_total
        ):
            errors.append(
                {
                    "code": "CONTRADICTORY_PAGINATION_TOTALS",
                    "diagnostic": f"Declared total changed from {declared_total} to {p_total}",
                }
            )

        # Account consistency check across pages
        p_acc = page.get("accountId") or page.get("sellerId")
        if p_acc:
            if account_id is None:
                account_id = str(p_acc)
            elif account_id != str(p_acc):
                errors.append(
                    {
                        "code": "ACCOUNT_MISMATCH",
                        "diagnostic": f"Account mismatch across pages: {account_id} vs {p_acc}",
                    }
                )

        # Offset sequence validation
        if page_idx == 0:
            if p_offset is not None and p_offset != 0:
                errors.append(
                    {
                        "code": "PAGINATION_SEQUENCE_ERROR",
                        "diagnostic": f"First page offset must be 0, got {p_offset}",
                    }
                )
        else:
            if p_offset is not None and p_offset != expected_offset:
                errors.append(
                    {
                        "code": "PAGINATION_SEQUENCE_ERROR",
                        "diagnostic": f"Page {page_idx + 1} offset {p_offset} did not match expected {expected_offset}",
                    }
                )

        orders_list = page.get("orders")
        if (
            orders_list is None
            or not isinstance(orders_list, Sequence)
            or isinstance(orders_list, (str, bytes))
        ):
            errors.append(
                {
                    "code": "MISSING_REQUIRED_CONTAINER",
                    "diagnostic": f"Page {page_idx + 1} missing orders container",
                }
            )
            orders_list = []
        else:
            if (
                declared_total is not None
                and expected_offset >= declared_total
                and page_idx > 0
            ):
                errors.append(
                    {
                        "code": "PAGES_AFTER_END_DECLARED",
                        "diagnostic": f"Received page {page_idx + 1} after reaching declared total {declared_total}",
                    }
                )
            all_orders.extend(orders_list)
            if p_offset is not None:
                expected_offset = p_offset + len(orders_list)
            else:
                expected_offset += len(orders_list)

        # Check pending continuation on last provided page
        if page_idx == len(pages) - 1:
            has_next = bool(page.get("next"))
            if has_next or (
                declared_total is not None and expected_offset < declared_total
            ):
                is_truncated = True
                errors.append(
                    {
                        "code": "PENDING_CONTINUATION",
                        "diagnostic": "Fulfillment pages sequence has pending continuation or uncollected items",
                    }
                )

    combined_payload = {
        "orders": all_orders,
        "total": declared_total if declared_total is not None else len(all_orders),
        "limit": len(all_orders) if all_orders else (last_limit or 50),
        "offset": 0,
        "accountId": account_id,
    }
    result = normalize_rest_fulfillment_orders(
        combined_payload, window_days=window_days
    )
    merged_errors = [*errors, *result.errors]

    account_verified = bool(account_id)
    window_verified = (
        not is_truncated
        and not result.is_truncated
        and not any(
            err.get("code") in ("WINDOW_MISMATCH", "TOTAL_RECORDS_MISMATCH")
            for err in merged_errors
        )
    )
    is_complete = (
        len(merged_errors) == 0
        and not is_truncated
        and not result.is_truncated
        and declared_total is not None
        and len(all_orders) == declared_total
    )
    reconciliation_ready = (
        is_complete and account_verified and window_verified and len(merged_errors) == 0
    )

    return SoldUnsoldCollectionResult(
        listings=result.listings,
        sold_item_ids=result.sold_item_ids,
        unsold_item_ids=result.unsold_item_ids,
        selling_classification=result.selling_classification,
        sold_items_count=result.sold_items_count,
        unsold_items_count=result.unsold_items_count,
        is_complete=is_complete,
        is_truncated=is_truncated or result.is_truncated,
        total_count=declared_total if declared_total is not None else len(all_orders),
        account_id=str(account_id) if account_id else None,
        account_verified=account_verified,
        window_days=window_days,
        window_verified=window_verified,
        reconciliation_ready=reconciliation_ready,
        errors=tuple(merged_errors),
        diagnostics={
            "orders_count": len(all_orders),
            "unique_sold_items_count": len(result.sold_item_ids),
        },
    )


# ---------------------------------------------------------------------------
# Composite Modern Normalization (GraphQL Ended + REST Orders)
# ---------------------------------------------------------------------------
def merge_composite_modern_sold_unsold(
    graphql_result: SoldUnsoldCollectionResult,
    orders_result: SoldUnsoldCollectionResult,
    *,
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> SoldUnsoldCollectionResult:
    """Merge GraphQL ended listings with REST Fulfillment orders to produce composite classification.

    - Conflict-aware: If GraphQL says UNSOLD and REST says SOLD -> AMBIGUOUS (fail-closed)
    - Order-only listings retain ListingLifecycleState.UNKNOWN (an order does not prove listing ended)
    - Confirms account identity and time-window compatibility
    - Propagates incomplete collection states
    """
    errors = [*graphql_result.errors, *orders_result.errors]

    # Verify account identity matching
    account_verified = False
    if graphql_result.account_id and orders_result.account_id:
        if graphql_result.account_id != orders_result.account_id:
            errors.append(
                {
                    "code": "ACCOUNT_MISMATCH",
                    "diagnostic": f"GraphQL account {graphql_result.account_id} does not match orders account {orders_result.account_id}",
                }
            )
        else:
            account_verified = True
    else:
        account_verified = False

    # Verify time-window matching
    window_verified = False
    if graphql_result.window_days != orders_result.window_days:
        errors.append(
            {
                "code": "WINDOW_MISMATCH",
                "diagnostic": f"GraphQL window {graphql_result.window_days}d does not match orders window {orders_result.window_days}d",
            }
        )
    else:
        window_verified = (
            graphql_result.window_verified and orders_result.window_verified
        )

    is_complete = (
        graphql_result.is_complete and orders_result.is_complete and len(errors) == 0
    )
    is_truncated = graphql_result.is_truncated or orders_result.is_truncated
    reconciliation_ready = (
        is_complete and account_verified and window_verified and len(errors) == 0
    )

    order_sold_ids = set(orders_result.sold_item_ids)
    all_item_ids = {item.item_id for item in graphql_result.listings} | order_sold_ids

    listings: list[NormalizedListingClassification] = []
    sold_ids: set[str] = set()
    unsold_ids: set[str] = set()
    classified: dict[str, str] = {}

    gql_map = {item.item_id: item for item in graphql_result.listings}

    for i_id in sorted(all_item_ids):
        gql_item = gql_map.get(i_id)
        has_order = i_id in order_sold_ids

        # CONFLICT DETECTION: GraphQL says UNSOLD but REST order says SOLD!
        if (
            gql_item is not None
            and gql_item.sale_classification == SaleClassification.UNSOLD
            and has_order
        ):
            outcome = SaleClassification.AMBIGUOUS
            classified[i_id] = "ambiguous"
            listings.append(
                NormalizedListingClassification(
                    item_id=i_id,
                    lifecycle_state=gql_item.lifecycle_state,
                    sale_classification=outcome,
                    source=ClassificationSource.COMPOSITE,
                    quantity_sold=gql_item.quantity_sold,
                    ended_at=gql_item.ended_at,
                    evidence_level=ClassificationEvidence.LOW,
                    notes="Contradictory evidence: GraphQL reports UNSOLD but REST reports order",
                )
            )
            errors.append(
                {
                    "code": "MALFORMED_ORDER",
                    "diagnostic": f"Contradictory classification for item {i_id}: GraphQL reports UNSOLD but REST reports order",
                }
            )
        elif has_order:
            outcome = SaleClassification.SOLD
            sold_ids.add(i_id)
            classified[i_id] = "sold"
            # If not in GraphQL ended listings, lifecycle is UNKNOWN (not ended!)
            lifecycle = (
                gql_item.lifecycle_state
                if gql_item is not None
                else ListingLifecycleState.UNKNOWN
            )
            qty = gql_item.quantity_sold if gql_item and gql_item.quantity_sold else 1
            ended_at = gql_item.ended_at if gql_item else None
            listings.append(
                NormalizedListingClassification(
                    item_id=i_id,
                    lifecycle_state=lifecycle,
                    sale_classification=outcome,
                    source=ClassificationSource.COMPOSITE,
                    quantity_sold=qty,
                    ended_at=ended_at,
                    evidence_level=ClassificationEvidence.HIGH
                    if orders_result.is_complete
                    else ClassificationEvidence.MEDIUM,
                )
            )
        elif (
            gql_item is not None
            and gql_item.sale_classification == SaleClassification.SOLD
        ):
            outcome = SaleClassification.SOLD
            sold_ids.add(i_id)
            classified[i_id] = "sold"
            listings.append(
                NormalizedListingClassification(
                    item_id=i_id,
                    lifecycle_state=gql_item.lifecycle_state,
                    sale_classification=outcome,
                    source=ClassificationSource.COMPOSITE,
                    quantity_sold=gql_item.quantity_sold or 1,
                    ended_at=gql_item.ended_at,
                    evidence_level=ClassificationEvidence.HIGH,
                )
            )
        elif (
            gql_item is not None
            and gql_item.sale_classification == SaleClassification.UNSOLD
            and not has_order
        ):
            # Only confirm UNSOLD if both collections are verified complete
            if graphql_result.is_complete and orders_result.is_complete:
                outcome = SaleClassification.UNSOLD
                unsold_ids.add(i_id)
                classified[i_id] = "unsold"
                ev = ClassificationEvidence.HIGH
            else:
                # Incomplete collections: absence of order cannot prove unsold!
                outcome = SaleClassification.UNKNOWN
                classified[i_id] = "unknown"
                ev = ClassificationEvidence.LOW

            listings.append(
                NormalizedListingClassification(
                    item_id=i_id,
                    lifecycle_state=gql_item.lifecycle_state,
                    sale_classification=outcome,
                    source=ClassificationSource.COMPOSITE,
                    quantity_sold=0,
                    ended_at=gql_item.ended_at,
                    evidence_level=ev,
                )
            )
        else:
            outcome = SaleClassification.UNKNOWN
            classified[i_id] = "unknown"
            listings.append(
                NormalizedListingClassification(
                    item_id=i_id,
                    lifecycle_state=gql_item.lifecycle_state
                    if gql_item
                    else ListingLifecycleState.UNKNOWN,
                    sale_classification=outcome,
                    source=ClassificationSource.COMPOSITE,
                    evidence_level=ClassificationEvidence.UNKNOWN,
                )
            )

    return SoldUnsoldCollectionResult(
        listings=tuple(listings),
        sold_item_ids=frozenset(sold_ids),
        unsold_item_ids=frozenset(unsold_ids),
        selling_classification=classified,
        sold_items_count=len(sold_ids),
        unsold_items_count=len(unsold_ids),
        is_complete=is_complete and len(errors) == 0,
        is_truncated=is_truncated,
        total_count=len(all_item_ids),
        account_id=graphql_result.account_id or orders_result.account_id,
        account_verified=account_verified,
        window_days=window_days,
        window_verified=window_verified,
        reconciliation_ready=reconciliation_ready,
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# Parity Comparison Engine
# ---------------------------------------------------------------------------
def _compare_single_field(
    field_name: str,
    xml_val: Any,
    cand_val: Any,
) -> tuple[str, str | None]:
    """Compare a single field between XML baseline and candidate snapshot."""
    if xml_val is None and cand_val is None:
        if field_name in ("quantity_sold", "ended_at"):
            return PARITY_UNSUPPORTED, "Field not provided in baseline Trading XML"
        return PARITY_UNVERIFIED, "Field absent in both baseline and candidate"
    if xml_val is not None and cand_val is None:
        return (
            PARITY_MISSING_FIELD,
            "Field present in baseline but missing in candidate",
        )
    if xml_val is None and cand_val is not None:
        return PARITY_UNSUPPORTED, "Field present in candidate but missing in baseline"

    if field_name in ("sold_items_count", "unsold_items_count", "quantity_sold"):
        i_xml = _to_int(xml_val)
        i_cand = _to_int(cand_val)
        if i_xml is not None and i_cand is not None:
            if i_xml == i_cand:
                return PARITY_EXACT_MATCH, None
            return (
                PARITY_DIFFERENT_SEMANTICS,
                f"Count mismatch: baseline={i_xml}, candidate={i_cand}",
            )
        return PARITY_DIFFERENT_SEMANTICS, "Numeric parse failure"

    if field_name in ("sold_item_ids", "unsold_item_ids"):
        set_xml = (
            set(xml_val)
            if isinstance(xml_val, (set, frozenset, list, tuple))
            else set()
        )
        set_cand = (
            set(cand_val)
            if isinstance(cand_val, (set, frozenset, list, tuple))
            else set()
        )
        if set_xml == set_cand:
            return PARITY_EXACT_MATCH, None
        return (
            PARITY_DIFFERENT_SEMANTICS,
            f"Set mismatch: xml={len(set_xml)}, candidate={len(set_cand)}",
        )

    if field_name == "selling_classification":
        if isinstance(xml_val, Mapping) and isinstance(cand_val, Mapping):
            diffs = 0
            for k, v in xml_val.items():
                if cand_val.get(k) != v:
                    diffs += 1
            if diffs == 0 and len(xml_val) == len(cand_val):
                return PARITY_EXACT_MATCH, None
            return (
                PARITY_DIFFERENT_SEMANTICS,
                f"Classification mismatch across {diffs} items",
            )
        return PARITY_DIFFERENT_SEMANTICS, "Type mismatch on selling_classification"

    if field_name in ("lifecycle_state", "sale_classification"):
        s_xml = str(xml_val).lower().strip()
        s_cand = str(cand_val).lower().strip()
        if s_xml == s_cand:
            return PARITY_EXACT_MATCH, None
        return (
            PARITY_DIFFERENT_SEMANTICS,
            f"State mismatch: baseline={s_xml}, candidate={s_cand}",
        )

    if field_name == "ended_at":
        dt_xml = _parse_iso_datetime(xml_val)
        dt_cand = _parse_iso_datetime(cand_val)
        if dt_xml is not None and dt_cand is not None:
            if abs((dt_xml - dt_cand).total_seconds()) < 2.0:
                return PARITY_EXACT_MATCH, None
            return PARITY_DIFFERENT_SEMANTICS, "Timestamp mismatch"
        return PARITY_DIFFERENT_SEMANTICS, "Date parse failure"

    if str(xml_val).strip() == str(cand_val).strip():
        return PARITY_EXACT_MATCH, None

    return PARITY_DIFFERENT_SEMANTICS, "Value mismatch"


def compare_sold_unsold_snapshots(
    xml_snapshot: SoldUnsoldCollectionResult,
    candidate_snapshot: SoldUnsoldCollectionResult,
) -> dict[str, Any]:
    """Compare XML baseline vs Candidate classification snapshot fail-closed."""
    field_comparisons: dict[str, dict[str, Any]] = {}
    counts = {
        PARITY_EXACT_MATCH: 0,
        PARITY_EQUIVALENT_NORMALIZED: 0,
        PARITY_DIFFERENT_SEMANTICS: 0,
        PARITY_MISSING_FIELD: 0,
        PARITY_UNVERIFIED: 0,
        PARITY_UNSUPPORTED: 0,
    }

    # Fail-closed check: if either is incomplete or truncated or has errors or window mismatch
    window_mismatch = xml_snapshot.window_days != candidate_snapshot.window_days
    all_errors = [*xml_snapshot.errors, *candidate_snapshot.errors]
    if window_mismatch:
        all_errors.append(
            {
                "code": "WINDOW_MISMATCH",
                "diagnostic": f"Window days mismatch: baseline={xml_snapshot.window_days}d, candidate={candidate_snapshot.window_days}d",
            }
        )

    is_inconclusive = (
        not xml_snapshot.is_complete
        or not candidate_snapshot.is_complete
        or xml_snapshot.is_truncated
        or candidate_snapshot.is_truncated
        or len(all_errors) > 0
        or window_mismatch
    )

    if is_inconclusive:
        for fld in MONITORED_CLASSIFICATION_FIELDS:
            field_comparisons[fld] = {
                "status": PARITY_UNVERIFIED,
                "label": PARITY_LABELS[PARITY_UNVERIFIED],
                "note": "Snapshot is incomplete, truncated, or contains errors; fail-closed",
            }
            counts[PARITY_UNVERIFIED] += 1

        return {
            "overall_status": "inconclusive",
            "is_inconclusive": True,
            "supported_fields_parity": "inconclusive",
            "comparable_supported_fields_count": 0,
            "unsupported_fields_count": 0,
            "unverified_fields_count": len(MONITORED_CLASSIFICATION_FIELDS),
            "migration_readiness": "blocked",
            "migration_readiness_reason": "COLLECTION_NOT_RECONCILIATION_READY",
            "field_counts": counts,
            "field_comparisons": field_comparisons,
            "baseline_summary": {
                "sold_items_count": xml_snapshot.sold_items_count,
                "unsold_items_count": xml_snapshot.unsold_items_count,
                "total_listings": len(xml_snapshot.listings),
            },
            "candidate_summary": {
                "sold_items_count": candidate_snapshot.sold_items_count,
                "unsold_items_count": candidate_snapshot.unsold_items_count,
                "total_listings": len(candidate_snapshot.listings),
            },
            "errors": all_errors,
        }

    # 1. Compare aggregate count and set fields
    for fld, x_v, c_v in (
        (
            "sold_items_count",
            xml_snapshot.sold_items_count,
            candidate_snapshot.sold_items_count,
        ),
        (
            "unsold_items_count",
            xml_snapshot.unsold_items_count,
            candidate_snapshot.unsold_items_count,
        ),
        ("sold_item_ids", xml_snapshot.sold_item_ids, candidate_snapshot.sold_item_ids),
        (
            "unsold_item_ids",
            xml_snapshot.unsold_item_ids,
            candidate_snapshot.unsold_item_ids,
        ),
        (
            "selling_classification",
            xml_snapshot.selling_classification,
            candidate_snapshot.selling_classification,
        ),
    ):
        st, note = _compare_single_field(fld, x_v, c_v)
        field_comparisons[fld] = {
            "status": st,
            "label": PARITY_LABELS[st],
            "note": note,
        }
        counts[st] += 1

    # 2. Compare record-level fields
    xml_by_id = {item.item_id: item for item in xml_snapshot.listings if item.item_id}
    cand_by_id = {
        item.item_id: item for item in candidate_snapshot.listings if item.item_id
    }
    xml_ids = set(xml_by_id.keys())
    cand_ids = set(cand_by_id.keys())
    matched_ids = xml_ids & cand_ids
    missing_in_candidate = xml_ids - cand_ids
    extra_in_candidate = cand_ids - xml_ids

    record_fields = (
        "item_id",
        "lifecycle_state",
        "sale_classification",
        "quantity_sold",
        "ended_at",
    )

    # Anti-false-positive rule: verified empty collection
    if not xml_snapshot.listings and not candidate_snapshot.listings:
        for fld in record_fields:
            field_comparisons[fld] = {
                "status": PARITY_UNVERIFIED,
                "label": PARITY_LABELS[PARITY_UNVERIFIED],
                "note": "Verified empty collection; no records available to evaluate record-level field",
            }
            counts[PARITY_UNVERIFIED] += 1
        overall_parity = "inconclusive"
        supported_fields_parity = "inconclusive"
        comparable_supported_count = 0
        unsupported_count = counts[PARITY_UNSUPPORTED]
        unverified_count = counts[PARITY_UNVERIFIED]
        migration_readiness = "blocked"
        migration_readiness_reason = "EMPTY_COLLECTION"
    else:
        # Check identity set match
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
            field_comparisons["item_id"] = {
                "status": st_id,
                "label": PARITY_LABELS[st_id],
                "note": note_str,
            }
            counts[st_id] += 1
        else:
            field_comparisons["item_id"] = {
                "status": PARITY_EXACT_MATCH,
                "label": PARITY_LABELS[PARITY_EXACT_MATCH],
                "note": f"All {len(matched_ids)} item IDs matched exactly",
            }
            counts[PARITY_EXACT_MATCH] += 1

        other_fields = tuple(f for f in record_fields if f != "item_id")
        if not matched_ids:
            for fld in other_fields:
                field_comparisons[fld] = {
                    "status": PARITY_MISSING_FIELD,
                    "label": PARITY_LABELS[PARITY_MISSING_FIELD],
                    "note": f"No matching item IDs between XML ({len(xml_by_id)}) and candidate ({len(cand_by_id)})",
                }
                counts[PARITY_MISSING_FIELD] += 1
        else:
            for fld in other_fields:
                field_states: list[str] = []
                for mid in matched_ids:
                    x_item = xml_by_id[mid]
                    c_item = cand_by_id[mid]
                    x_v = getattr(x_item, fld, None)
                    c_v = getattr(c_item, fld, None)
                    st_f, _ = _compare_single_field(fld, x_v, c_v)
                    field_states.append(st_f)

                if any(s == PARITY_DIFFERENT_SEMANTICS for s in field_states):
                    overall_f = PARITY_DIFFERENT_SEMANTICS
                elif any(s == PARITY_MISSING_FIELD for s in field_states):
                    overall_f = PARITY_MISSING_FIELD
                elif any(s == PARITY_UNSUPPORTED for s in field_states):
                    overall_f = PARITY_UNSUPPORTED
                elif any(s == PARITY_UNVERIFIED for s in field_states):
                    overall_f = PARITY_UNVERIFIED
                elif all(s == PARITY_EXACT_MATCH for s in field_states):
                    overall_f = PARITY_EXACT_MATCH
                elif all(
                    s in (PARITY_EXACT_MATCH, PARITY_EQUIVALENT_NORMALIZED)
                    for s in field_states
                ):
                    overall_f = PARITY_EQUIVALENT_NORMALIZED
                else:
                    overall_f = PARITY_UNVERIFIED

                field_comparisons[fld] = {
                    "status": overall_f,
                    "label": PARITY_LABELS[overall_f],
                    "note": f"Evaluated across {len(matched_ids)} matched listings",
                }
                counts[overall_f] += 1

        supported_fields = [
            fld
            for fld, comp in field_comparisons.items()
            if comp["status"] != PARITY_UNSUPPORTED
        ]
        unsupported_count = counts[PARITY_UNSUPPORTED]
        unverified_count = counts[PARITY_UNVERIFIED]
        comparable_supported_count = len(supported_fields)

        if counts[PARITY_DIFFERENT_SEMANTICS] > 0 or counts[PARITY_MISSING_FIELD] > 0:
            supported_fields_parity = "fail"
        elif (
            xml_ids == cand_ids
            and len(matched_ids) > 0
            and comparable_supported_count > 0
            and all(
                field_comparisons[f]["status"]
                in (PARITY_EXACT_MATCH, PARITY_EQUIVALENT_NORMALIZED)
                for f in supported_fields
            )
        ):
            supported_fields_parity = "pass"
        else:
            supported_fields_parity = "inconclusive"

        # Overall parity requires ALL 10 monitored fields to be evaluated without unsupported/unverified
        if counts[PARITY_DIFFERENT_SEMANTICS] > 0 or counts[PARITY_MISSING_FIELD] > 0:
            overall_parity = "fail"
        elif unsupported_count > 0 or unverified_count > 0:
            overall_parity = "inconclusive"
        elif supported_fields_parity == "pass":
            overall_parity = "pass"
        else:
            overall_parity = "fail"

        # Migration readiness requires verified full parity AND reconciliation readiness on both sides
        if (
            overall_parity == "pass"
            and xml_snapshot.reconciliation_ready
            and candidate_snapshot.reconciliation_ready
        ):
            migration_readiness = "ready"
            migration_readiness_reason = "VERIFIED_FULL_PARITY"
        else:
            migration_readiness = "blocked"
            if unsupported_count > 0:
                migration_readiness_reason = "UNSUPPORTED_FIELDS_IN_BASELINE"
            elif not (
                xml_snapshot.reconciliation_ready
                and candidate_snapshot.reconciliation_ready
            ):
                migration_readiness_reason = "COLLECTION_NOT_RECONCILIATION_READY"
            elif overall_parity == "fail" or supported_fields_parity == "fail":
                migration_readiness_reason = "PARITY_FAILED"
            else:
                migration_readiness_reason = "BLOCKED"

    return {
        "overall_status": overall_parity,
        "is_inconclusive": overall_parity == "inconclusive",
        "supported_fields_parity": supported_fields_parity,
        "comparable_supported_fields_count": comparable_supported_count,
        "unsupported_fields_count": unsupported_count,
        "unverified_fields_count": unverified_count,
        "migration_readiness": migration_readiness,
        "migration_readiness_reason": migration_readiness_reason,
        "field_counts": counts,
        "field_comparisons": field_comparisons,
        "baseline_summary": {
            "sold_items_count": xml_snapshot.sold_items_count,
            "unsold_items_count": xml_snapshot.unsold_items_count,
            "total_listings": len(xml_snapshot.listings),
        },
        "candidate_summary": {
            "sold_items_count": candidate_snapshot.sold_items_count,
            "unsold_items_count": candidate_snapshot.unsold_items_count,
            "total_listings": len(candidate_snapshot.listings),
        },
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Privacy Protection & Report Sanitization (Default-Deny)
# ---------------------------------------------------------------------------
SAFE_MIGRATION_READINESS_REASONS: dict[str, str] = {
    "UNSUPPORTED_FIELDS_IN_BASELINE": "Monitored fields (quantity_sold, ended_at) are unsupported in Trading XML; full parity blocked",
    "COLLECTION_NOT_RECONCILIATION_READY": "Candidate collection is not verified complete, account-matched, and window-bounded",
    "EMPTY_COLLECTION": "Empty collection; record-level fields unverified",
    "PARITY_FAILED": "Field comparison found semantic differences or missing fields",
    "VERIFIED_FULL_PARITY": "Full 10-field parity verified and reconciliation ready",
    "BLOCKED": "Migration readiness blocked",
}

ALLOWED_REPORT_TOP_KEYS: frozenset[str] = frozenset(
    {
        "overall_status",
        "is_inconclusive",
        "supported_fields_parity",
        "comparable_supported_fields_count",
        "unsupported_fields_count",
        "unverified_fields_count",
        "migration_readiness",
        "migration_readiness_reason",
        "field_counts",
        "field_comparisons",
        "baseline_summary",
        "candidate_summary",
        "errors",
        "selling_classification",
        "sold_item_ids",
        "unsold_item_ids",
    }
)


def sanitize_sold_unsold_parity_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitize sold/unsold parity report applying strict default-deny schema, pseudonymization, and generic diagnostics."""
    if not isinstance(report, Mapping):
        return {
            "overall_status": "inconclusive",
            "is_inconclusive": True,
            "supported_fields_parity": "inconclusive",
            "comparable_supported_fields_count": 0,
            "unsupported_fields_count": 0,
            "unverified_fields_count": len(MONITORED_CLASSIFICATION_FIELDS),
            "migration_readiness": "blocked",
            "migration_readiness_reason": SAFE_MIGRATION_READINESS_REASONS[
                "COLLECTION_NOT_RECONCILIATION_READY"
            ],
            "field_counts": {},
            "field_comparisons": {},
            "errors": [
                {
                    "code": "MALFORMED_PAGE",
                    "diagnostic": GENERIC_DIAGNOSTIC_MESSAGES["MALFORMED_PAGE"],
                }
            ],
        }

    sanitized: dict[str, Any] = {}

    # 1. Overall status
    raw_status = str(report.get("overall_status", "inconclusive")).lower()
    sanitized["overall_status"] = (
        raw_status if raw_status in ("pass", "fail", "inconclusive") else "inconclusive"
    )

    # 2. is_inconclusive
    sanitized["is_inconclusive"] = bool(
        report.get("is_inconclusive", sanitized["overall_status"] == "inconclusive")
    )

    # 3. Supported fields parity
    raw_sfp = str(report.get("supported_fields_parity", "inconclusive")).lower()
    sanitized["supported_fields_parity"] = (
        raw_sfp if raw_sfp in ("pass", "fail", "inconclusive") else "inconclusive"
    )

    # 4. Field breakdown counts
    for cnt_k in (
        "comparable_supported_fields_count",
        "unsupported_fields_count",
        "unverified_fields_count",
    ):
        v = report.get(cnt_k)
        sanitized[cnt_k] = (
            max(0, int(v))
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            else 0
        )

    # 5. Migration readiness
    raw_mr = str(report.get("migration_readiness", "blocked")).lower()
    sanitized["migration_readiness"] = (
        raw_mr if raw_mr in ("ready", "blocked") else "blocked"
    )

    # 6. Migration readiness reason (idempotent lookup)
    raw_mrr = str(report.get("migration_readiness_reason") or "BLOCKED")
    if raw_mrr in SAFE_MIGRATION_READINESS_REASONS.values():
        sanitized["migration_readiness_reason"] = raw_mrr
    elif raw_mrr in SAFE_MIGRATION_READINESS_REASONS:
        sanitized["migration_readiness_reason"] = SAFE_MIGRATION_READINESS_REASONS[
            raw_mrr
        ]
    else:
        sanitized["migration_readiness_reason"] = SAFE_MIGRATION_READINESS_REASONS[
            "BLOCKED"
        ]

    # 3. Field counts
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

    # 4. Field comparisons
    raw_comps = report.get("field_comparisons")
    sanitized_comps: dict[str, dict[str, str]] = {}
    if isinstance(raw_comps, Mapping):
        for fld, fld_data in raw_comps.items():
            if fld in MONITORED_CLASSIFICATION_FIELDS and isinstance(fld_data, Mapping):
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

    # 5. Summaries
    for sum_key in ("baseline_summary", "candidate_summary"):
        raw_sum = report.get(sum_key)
        if isinstance(raw_sum, Mapping):
            sanitized[sum_key] = {
                "sold_items_count": max(0, int(raw_sum.get("sold_items_count") or 0)),
                "unsold_items_count": max(
                    0, int(raw_sum.get("unsold_items_count") or 0)
                ),
                "total_listings": max(0, int(raw_sum.get("total_listings") or 0)),
            }

    # 6. Pseudonymized ID lists if provided
    for id_list_key in ("sold_item_ids", "unsold_item_ids"):
        raw_list = report.get(id_list_key)
        if isinstance(raw_list, (Sequence, set, frozenset)) and not isinstance(
            raw_list, (str, bytes)
        ):
            sanitized_ids: list[str] = []
            for item_id in raw_list:
                key_str = str(item_id).strip()
                if not key_str:
                    continue
                clean_id = (
                    key_str
                    if _is_safe_item_pseudonym(key_str)
                    else pseudonymize_identifier(key_str, prefix="item")
                )
                sanitized_ids.append(clean_id)
            sanitized[id_list_key] = sorted(set(sanitized_ids))

    # 7. Pseudonymized classification mapping if provided
    raw_class = report.get("selling_classification")
    if isinstance(raw_class, Mapping):
        sanitized_class: dict[str, str] = {}
        for item_id, outcome in raw_class.items():
            key_str = str(item_id).strip()
            if not key_str:
                continue
            clean_id = (
                key_str
                if _is_safe_item_pseudonym(key_str)
                else pseudonymize_identifier(key_str, prefix="item")
            )
            outcome_str = str(outcome).lower().strip()
            safe_outcome = (
                outcome_str
                if outcome_str in ("sold", "unsold", "ambiguous", "unknown")
                else "unknown"
            )
            sanitized_class[clean_id] = safe_outcome
        sanitized["selling_classification"] = sanitized_class

    # 8. Errors: symbolic codes only, safe generic diagnostics
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


def render_sold_unsold_parity_markdown(report: Mapping[str, Any]) -> str:
    """Render a sanitized markdown summary of the sold/unsold classification comparison."""
    sanitized = sanitize_sold_unsold_parity_report(report)
    status = sanitized.get("overall_status", "unknown").upper()
    sfp = str(sanitized.get("supported_fields_parity", "inconclusive")).upper()
    mr = str(sanitized.get("migration_readiness", "blocked")).upper()
    mrr = sanitized.get("migration_readiness_reason", "")
    comp_supp = sanitized.get("comparable_supported_fields_count", 0)
    unsupp = sanitized.get("unsupported_fields_count", 0)
    counts = sanitized.get("field_counts", {})
    comparisons = sanitized.get("field_comparisons", {})

    lines: list[str] = [
        "# Sold & Unsold Listing Classification Parity Report",
        "",
        f"**Overall Status:** `{status}`",
        f"**Supported Fields Parity:** `{sfp}`",
        f"**Migration Readiness:** `{mr}` ({mrr})",
        f"**Comparable Supported Fields:** {comp_supp} / {len(MONITORED_CLASSIFICATION_FIELDS)}",
        f"**Unsupported Monitored Fields:** {unsupp} / {len(MONITORED_CLASSIFICATION_FIELDS)}",
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


def render_sold_unsold_parity_json(report: Mapping[str, Any]) -> str:
    """Render a sanitized, deterministic JSON report."""
    sanitized = sanitize_sold_unsold_parity_report(report)
    return json.dumps(sanitized, indent=2, sort_keys=True)


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
    except (ValueError, TypeError):
        return None
