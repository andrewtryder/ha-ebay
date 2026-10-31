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
from datetime import datetime, timezone
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
        "BAD_GATEWAY",
        "BAD_USER_INPUT",
        "CONTRADICTORY_PAGINATION_TOTALS",
        "COUNT_ONLY_OPERATION",
        "COUNT_ONLY_QUERY",
        "DOWNSTREAM_SERVICE_ERROR",
        "DUPLICATE_ITEM_ID",
        "DUPLICATE_ITEM_ID_ACROSS_PAGES",
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
        "XML_ACK_FAILURE",
        "XML_ACK_MISSING",
        "XML_ACK_PARTIAL_FAILURE",
        "XML_ACK_UNKNOWN",
        "XML_PARSE_ERROR",
    }
)

GENERIC_DIAGNOSTIC_MESSAGES: dict[str, str] = {
    "ACCESS_DENIED": "Access denied; authorization check failed",
    "BAD_GATEWAY": "Bad gateway encountered communicating with remote service",
    "BAD_USER_INPUT": "Invalid argument or input parameter provided",
    "CONTRADICTORY_PAGINATION_TOTALS": "Declared pagination totals changed across pages",
    "COUNT_ONLY_OPERATION": "Count-only operation does not retrieve classification records",
    "COUNT_ONLY_QUERY": "Count-only operation does not retrieve classification records",
    "DOWNSTREAM_SERVICE_ERROR": "Downstream service error encountered",
    "DUPLICATE_ITEM_ID": "Duplicate item identifier encountered within page",
    "DUPLICATE_ITEM_ID_ACROSS_PAGES": "Duplicate item identifier encountered across pages",
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
    is_complete: bool = True
    is_truncated: bool = False
    total_count: int | None = None
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS
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

    sold_container = root.find("e:SoldList", namespaces=NS)
    unsold_container = root.find("e:UnsoldList", namespaces=NS)

    if sold_container is None and unsold_container is None:
        errors.append(
            {
                "code": "MISSING_REQUIRED_CONTAINER",
                "diagnostic": "Missing SoldList and UnsoldList containers in Trading XML response",
            }
        )

    sold_ids_list: list[str] = []
    unsold_ids_list: list[str] = []
    seen_sold: set[str] = set()
    seen_unsold: set[str] = set()

    # Diagnostics metadata for pagination reconciliation
    diag_info: dict[str, Any] = {}

    for container, target_list, seen_set, c_name in (
        (sold_container, sold_ids_list, seen_sold, "SoldList"),
        (unsold_container, unsold_ids_list, seen_unsold, "UnsoldList"),
    ):
        if container is not None:
            pag = container.find("e:PaginationResult", namespaces=NS)
            if pag is not None:
                total_p = _to_int(_text(pag, "e:TotalNumberOfPages"))
                total_e = _to_int(_text(pag, "e:TotalNumberOfEntries"))
                diag_info[f"{c_name}_total_pages"] = total_p
                diag_info[f"{c_name}_total_entries"] = total_e
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
    return SoldUnsoldCollectionResult(
        listings=tuple(listings),
        sold_item_ids=frozenset(sold_ids_list),
        unsold_item_ids=frozenset(unsold_ids_list),
        selling_classification=classified,
        sold_items_count=len(set(sold_ids_list)),
        unsold_items_count=len(set(unsold_ids_list)),
        is_complete=not has_errors,
        is_truncated=False,
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

    for page_idx, page in enumerate(pages):
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

        page_res = normalize_trading_xml_sold_unsold(page, window_days=window_days)
        errors.extend(page_res.errors)

        diag = page_res.diagnostics or {}
        p_total_pages = diag.get(f"{list_name}_total_pages")
        p_total_entries = diag.get(f"{list_name}_total_entries")

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
            if list_name == "SoldList"
            else page_res.unsold_item_ids
        )
        for i_id in page_ids:
            if i_id in seen_ids:
                errors.append(
                    {
                        "code": "DUPLICATE_ITEM_ID_ACROSS_PAGES",
                        "diagnostic": f"Item ID {i_id} repeated across {list_name} pages",
                    }
                )
            seen_ids.add(i_id)
            all_ids.append(i_id)

    is_truncated = False
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
        if list_name == "SoldList"
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

    sold_set = frozenset(seen_ids) if list_name == "SoldList" else frozenset()
    unsold_set = frozenset(seen_ids) if list_name == "UnsoldList" else frozenset()
    classified = {
        i_id: "sold" if list_name == "SoldList" else "unsold" for i_id in seen_ids
    }

    is_complete = len(errors) == 0 and not is_truncated
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


# ---------------------------------------------------------------------------
# Modern GraphQL Listing API Normalization
# ---------------------------------------------------------------------------
def normalize_graphql_seller_listing_search(
    payload: Mapping[str, Any] | None,
    *,
    window_days: int = SOLD_UNSOLD_DEFAULT_WINDOW_DAYS,
) -> SoldUnsoldCollectionResult:
    """Normalize GraphQL sellerListingSearch ended listings payload fail-closed.

    Critical rule: Ended listings are NOT automatically classified as unsold.
    If quantitySold > 0, they are classified as SOLD.
    If quantitySold == 0, they are classified as UNSOLD.
    If quantitySold is None or unverified, they are classified as UNKNOWN.
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

    declared_total = _to_int(
        container.get("total")
        if "total" in container
        else container.get("totalCount")
        if "totalCount" in container
        else container.get("totalEntries")
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

    listings: list[NormalizedListingClassification] = []
    sold_ids: set[str] = set()
    unsold_ids: set[str] = set()
    classified: dict[str, str] = {}
    seen_ids: set[str] = set()

    for raw in raw_items:
        node = raw.get("node") if isinstance(raw, Mapping) and "node" in raw else raw
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

        # Lifecycle state
        raw_status = str(node.get("status") or "").upper()
        lifecycle = (
            ListingLifecycleState.ENDED
            if raw_status in ("ENDED", "COMPLETED", "CLOSED")
            else ListingLifecycleState.ACTIVE
            if raw_status in ("ACTIVE", "LIVE")
            else ListingLifecycleState.SCHEDULED
            if raw_status in ("SCHEDULED",)
            else ListingLifecycleState.ENDED  # Default to ended if evaluating endedListings search
        )

        q_sold = _to_int(
            node.get("quantitySold")
            if "quantitySold" in node
            else node.get("soldQuantity")
        )
        q_total = _to_int(
            node.get("quantity") if "quantity" in node else node.get("totalQuantity")
        )
        end_time_raw = node.get("itemEndDate") or node.get("endTime")
        ended_at = _parse_iso_datetime(end_time_raw)

        # Classification outcome
        if q_sold is not None and q_sold > 0:
            outcome = SaleClassification.SOLD
            evidence = ClassificationEvidence.HIGH
            sold_ids.add(i_id)
            classified[i_id] = "sold"
        elif q_sold is not None and q_sold == 0:
            outcome = SaleClassification.UNSOLD
            evidence = ClassificationEvidence.HIGH
            unsold_ids.add(i_id)
            classified[i_id] = "unsold"
        else:
            outcome = SaleClassification.UNKNOWN
            evidence = ClassificationEvidence.UNKNOWN
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

    # Check census count
    if declared_total is not None and len(listings) != declared_total:
        errors.append(
            {
                "code": "TOTAL_RECORDS_MISMATCH",
                "diagnostic": f"Parsed {len(listings)} items but declared total is {declared_total}",
            }
        )

    is_complete = len(errors) == 0
    return SoldUnsoldCollectionResult(
        listings=tuple(listings),
        sold_item_ids=frozenset(sold_ids),
        unsold_item_ids=frozenset(unsold_ids),
        selling_classification=classified,
        sold_items_count=len(sold_ids),
        unsold_items_count=len(unsold_ids),
        is_complete=is_complete,
        is_truncated=False,
        total_count=declared_total if declared_total is not None else len(listings),
        window_days=window_days,
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
    if raw_orders is None and not errors:
        raw_orders = []

    declared_total = _to_int(payload.get("total"))
    sold_by_item: dict[str, int] = {}
    orders_by_item: dict[str, list[str]] = {}

    if isinstance(raw_orders, Sequence) and not isinstance(raw_orders, (str, bytes)):
        for order in raw_orders:
            if not isinstance(order, Mapping):
                continue
            order_id = str(order.get("orderId") or "").strip()
            line_items = order.get("lineItems") or []
            if isinstance(line_items, Sequence):
                for line in line_items:
                    if not isinstance(line, Mapping):
                        continue
                    item_id = str(
                        line.get("legacyItemId")
                        or line.get("listingId")
                        or line.get("itemId")
                        or ""
                    ).strip()
                    if not item_id:
                        continue
                    qty = max(1, _to_int(line.get("quantity")) or 1)
                    sold_by_item[item_id] = sold_by_item.get(item_id, 0) + qty
                    if order_id:
                        orders_by_item.setdefault(item_id, []).append(order_id)

    listings: list[NormalizedListingClassification] = []
    sold_ids: set[str] = set(sold_by_item.keys())
    classified: dict[str, str] = {}

    for i_id in sorted(sold_ids):
        classified[i_id] = "sold"
        listings.append(
            NormalizedListingClassification(
                item_id=i_id,
                lifecycle_state=ListingLifecycleState.ENDED,
                sale_classification=SaleClassification.SOLD,
                source=ClassificationSource.REST_FULFILLMENT,
                quantity_sold=sold_by_item[i_id],
                order_ids=tuple(orders_by_item.get(i_id, ())),
                evidence_level=ClassificationEvidence.HIGH,
            )
        )

    is_complete = len(errors) == 0
    return SoldUnsoldCollectionResult(
        listings=tuple(listings),
        sold_item_ids=frozenset(sold_ids),
        unsold_item_ids=frozenset(),  # Orders API can NEVER confirm unsold items
        selling_classification=classified,
        sold_items_count=len(sold_ids),
        unsold_items_count=0,
        is_complete=is_complete,
        is_truncated=False,
        total_count=declared_total
        if declared_total is not None
        else len(raw_orders if raw_orders else []),
        window_days=window_days,
        errors=tuple(errors),
        diagnostics={
            "orders_count": len(raw_orders) if raw_orders else 0,
            "unique_sold_items_count": len(sold_ids),
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

    - Ended listings with matching orders or quantitySold > 0 -> SOLD
    - Ended listings with 0 orders and quantitySold == 0 in complete collections -> UNSOLD
    - Incomplete collections or conflicting signals -> UNKNOWN or AMBIGUOUS (fail-closed)
    """
    is_complete = graphql_result.is_complete and orders_result.is_complete
    is_truncated = graphql_result.is_truncated or orders_result.is_truncated
    errors = [*graphql_result.errors, *orders_result.errors]

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

        if has_order or (
            gql_item is not None
            and gql_item.sale_classification == SaleClassification.SOLD
        ):
            outcome = SaleClassification.SOLD
            sold_ids.add(i_id)
            classified[i_id] = "sold"
            qty = (
                (gql_item.quantity_sold or 0)
                if gql_item and gql_item.quantity_sold
                else 1
            )
            ended_at = gql_item.ended_at if gql_item else None
            listings.append(
                NormalizedListingClassification(
                    item_id=i_id,
                    lifecycle_state=ListingLifecycleState.ENDED,
                    sale_classification=outcome,
                    source=ClassificationSource.COMPOSITE,
                    quantity_sold=qty,
                    ended_at=ended_at,
                    evidence_level=ClassificationEvidence.HIGH,
                )
            )
        elif (
            gql_item is not None
            and gql_item.sale_classification == SaleClassification.UNSOLD
            and not has_order
        ):
            outcome = SaleClassification.UNSOLD
            unsold_ids.add(i_id)
            classified[i_id] = "unsold"
            listings.append(
                NormalizedListingClassification(
                    item_id=i_id,
                    lifecycle_state=ListingLifecycleState.ENDED,
                    sale_classification=outcome,
                    source=ClassificationSource.COMPOSITE,
                    quantity_sold=0,
                    ended_at=gql_item.ended_at,
                    evidence_level=ClassificationEvidence.HIGH,
                )
            )
        else:
            outcome = SaleClassification.UNKNOWN
            classified[i_id] = "unknown"
            listings.append(
                NormalizedListingClassification(
                    item_id=i_id,
                    lifecycle_state=ListingLifecycleState.ENDED,
                    sale_classification=outcome,
                    source=ClassificationSource.COMPOSITE,
                    evidence_level=ClassificationEvidence.LOW,
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
        window_days=window_days,
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
        return PARITY_EXACT_MATCH, None
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

    # Fail-closed check: if either is incomplete or truncated or has errors
    is_inconclusive = (
        not xml_snapshot.is_complete
        or not candidate_snapshot.is_complete
        or xml_snapshot.is_truncated
        or candidate_snapshot.is_truncated
        or len(xml_snapshot.errors) > 0
        or len(candidate_snapshot.errors) > 0
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
            "errors": [
                *xml_snapshot.errors,
                *candidate_snapshot.errors,
            ],
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
        "overall_status": overall_parity,
        "is_inconclusive": overall_parity == "inconclusive",
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
ALLOWED_REPORT_TOP_KEYS: frozenset[str] = frozenset(
    {
        "overall_status",
        "is_inconclusive",
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
    counts = sanitized.get("field_counts", {})
    comparisons = sanitized.get("field_comparisons", {})

    lines: list[str] = [
        "# Sold & Unsold Listing Classification Parity Report",
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
