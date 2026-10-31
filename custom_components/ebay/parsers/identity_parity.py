"""Read-only Account Identity XML vs. Modern API contract and parity helpers.

Used by ``scripts/validate_legacy_fields_parity.py`` and unit tests in Milestone 8 to
compare legacy Trading XML ``GetUser`` with candidate modern sources (REST Commerce
Identity API ``getUser`` and candidate GraphQL viewer/user queries) without exposing
credentials, raw usernames, private account IDs, or tokens.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..clients.graphql import (
    _parse_graphql_definitions,
    _strip_graphql_strings_and_comments,
    validate_read_only_query,
)
from ..const import EBAY_XML_NS
from .xml import (
    _to_int,
    parse_ebay_time,
)

# ---------------------------------------------------------------------------
# Parity taxonomy constants (consistent across Milestones 1, 4, 5, 6, 7, 8)
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

MONITORED_IDENTITY_FIELDS: tuple[str, ...] = (
    "username",
    "user_id",
    "account_type",
    "registration_marketplace_id",
    "feedback_score",
    "registration_date",
)

ALLOWED_REPORT_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "xml_source",
        "modern_source",
        "username_matches",
        "account_continuity_verified",
        "migration_readiness",
        "blocking_reasons",
        "warnings",
        "fields",
        "diagnostics",
    }
)

ALLOWED_DIAGNOSTIC_KEYS: frozenset[str] = frozenset(
    {
        "xml_complete",
        "modern_complete",
        "xml_errors",
        "modern_errors",
        "required_scope",
        "account_verified",
        "entitlement_verified",
    }
)

ALLOWED_SYMBOLIC_ERROR_CODES: frozenset[str] = frozenset(
    {
        "EMPTY_XML_PAYLOAD",
        "EMPTY_JSON_PAYLOAD",
        "MALFORMED_XML",
        "MALFORMED_JSON",
        "INVALID_XML_INPUT_TYPE",
        "INVALID_JSON_INPUT_TYPE",
        "INVALID_XML_ROOT",
        "XML_NAMESPACE_MISMATCH",
        "XML_ACK_FAILURE",
        "XML_ACK_PARTIAL_FAILURE",
        "XML_ACK_MISSING_OR_UNKNOWN",
        "XML_FATAL_ERROR_CONTAINER",
        "MISSING_USER_ELEMENT",
        "MISSING_USER_ID",
        "MALFORMED_USER_ID",
        "MALFORMED_NUMERICAL_FIELD",
        "MALFORMED_REGISTRATION_DATE",
        "REST_API_ERROR",
        "GRAPHQL_API_ERROR",
        "MISSING_DATA_OBJECT",
        "MISSING_VIEWER_OR_USER_OBJECT",
        "MISSING_OR_INVALID_USERNAME",
        "MISSING_OR_INVALID_USER_ID",
        "ACCESS_DENIED",
        "FORBIDDEN",
        "INSUFFICIENT_SCOPE",
        "UNAUTHORIZED",
        "TOKEN_EXPIRED",
        "NOT_FOUND",
        "RATE_LIMITED",
        "INTERNAL_SERVER_ERROR",
        "ACCOUNT_ID_CONFLICT",
        "ACCOUNT_IDENTITY_UNVERIFIED",
        "MODERN_IDENTITY_ENTITLEMENT_UNVERIFIED",
        "TRADING_XML_GET_USER_INCOMPLETE",
        "MODERN_IDENTITY_INCOMPLETE",
        "USERNAME_MISMATCH",
        "ACCOUNT_TYPE_MISMATCH",
        "MARKETPLACE_MISMATCH",
        "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER",
        "BLOCKED_UNKNOWN",
        "UNKNOWN_ERROR",
    }
)

GENERIC_DIAGNOSTIC_MESSAGES: dict[str, str] = {
    "EMPTY_XML_PAYLOAD": "Trading XML payload is empty or whitespace",
    "EMPTY_JSON_PAYLOAD": "JSON payload is empty or whitespace",
    "MALFORMED_XML": "XML payload could not be parsed",
    "MALFORMED_JSON": "JSON payload could not be parsed",
    "INVALID_XML_INPUT_TYPE": "Input is not a valid XML string, bytes, or element",
    "INVALID_JSON_INPUT_TYPE": "Input is not a valid JSON string, bytes, or mapping",
    "INVALID_XML_ROOT": "Trading XML root element does not match GetUserResponse",
    "XML_NAMESPACE_MISMATCH": "Trading XML namespace does not match standard eBay schema",
    "XML_ACK_FAILURE": "Trading API response contains Ack=Failure",
    "XML_ACK_PARTIAL_FAILURE": "Trading API response contains Ack=PartialFailure",
    "XML_ACK_MISSING_OR_UNKNOWN": "Trading API response lacks an acceptable Success Ack",
    "XML_FATAL_ERROR_CONTAINER": "Trading API response contains fatal error container",
    "MISSING_USER_ELEMENT": "User container element missing from XML response",
    "MISSING_USER_ID": "UserID element missing or empty in User container",
    "MALFORMED_USER_ID": "UserID element contains invalid characters or whitespace",
    "MALFORMED_NUMERICAL_FIELD": "Numerical feedback metric could not be parsed",
    "MALFORMED_REGISTRATION_DATE": "Registration date timestamp could not be parsed",
    "REST_API_ERROR": "REST Identity API returned an error condition",
    "GRAPHQL_API_ERROR": "GraphQL service returned an error condition",
    "MISSING_DATA_OBJECT": "GraphQL response lacks data envelope",
    "MISSING_VIEWER_OR_USER_OBJECT": "GraphQL response lacks viewer or user object",
    "MISSING_OR_INVALID_USERNAME": "Username field missing or invalid in modern response",
    "MISSING_OR_INVALID_USER_ID": "userId field missing or invalid in modern response",
    "ACCESS_DENIED": "Access denied; insufficient authorization",
    "FORBIDDEN": "Access forbidden; insufficient scope or permissions",
    "INSUFFICIENT_SCOPE": "OAuth scope contract not granted for modern API",
    "UNAUTHORIZED": "Request unauthorized; invalid credentials",
    "TOKEN_EXPIRED": "Authentication token expired",
    "NOT_FOUND": "Requested user resource was not found",
    "RATE_LIMITED": "API call rate limit exceeded",
    "INTERNAL_SERVER_ERROR": "Internal server error encountered",
    "ACCOUNT_ID_CONFLICT": "Observed modern userId conflicts with externally verified account ID",
    "ACCOUNT_IDENTITY_UNVERIFIED": "Account identity has not been externally verified",
    "MODERN_IDENTITY_ENTITLEMENT_UNVERIFIED": "Modern API entitlement and OAuth scopes unverified",
    "TRADING_XML_GET_USER_INCOMPLETE": "Trading XML GetUser collection is incomplete or failed",
    "MODERN_IDENTITY_INCOMPLETE": "Modern Identity collection is incomplete or failed",
    "USERNAME_MISMATCH": "Public username mismatch between Trading XML and Modern Identity",
    "ACCOUNT_TYPE_MISMATCH": "Verified account classification conflicts between providers",
    "MARKETPLACE_MISMATCH": "Registration marketplace differs between providers",
    "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER": "Milestone 8 is validation-only; production cutover blocked",
    "BLOCKED_UNKNOWN": "Migration readiness blocked by unverified condition",
    "UNKNOWN_ERROR": "An error occurred during verification",
}

# Fixed safe templates for field-level notes (no dynamic user strings)
NOTE_USERNAME_EXACT_MATCH = (
    "Public username matches exactly between Trading XML and Modern Identity."
)
NOTE_USERNAME_CASE_NORMALIZED = "Public username matches case-insensitively after canonical lowercase normalization."
NOTE_USERNAME_MISMATCH = (
    "Public username mismatch between Trading XML and Modern Identity."
)
NOTE_USERNAME_MISSING_XML = (
    "Username present in modern identity but missing from Trading XML."
)
NOTE_USERNAME_MISSING_MODERN = "Username missing in modern identity."
NOTE_USER_ID_SEMANTICS = "Trading XML UserID is the public handle (username); modern REST userId is the immutable opaque identifier."
NOTE_ACCOUNT_TYPE_EXACT_MATCH = "Account type matches exactly between providers."
NOTE_ACCOUNT_TYPE_MISMATCH = "Account classification conflicts between providers."
NOTE_ACCOUNT_TYPE_UNVERIFIED = (
    "Account classification could not be verified from provider metadata."
)
NOTE_MARKETPLACE_EXACT_MATCH = "Registration marketplace matches between providers."
NOTE_MARKETPLACE_CASE_NORMALIZED = "Registration marketplace matches after documented site-to-marketplace normalization."
NOTE_MARKETPLACE_MISMATCH = "Registration marketplace conflicts between providers."
NOTE_MARKETPLACE_MISSING = "Registration marketplace missing in modern response."
NOTE_FEEDBACK_SCORE_UNSUPPORTED = "Feedback score is returned by Trading XML GetUser but is omitted from REST Commerce Identity; handled independently by REST Feedback API."
NOTE_REGISTRATION_DATE_UNSUPPORTED = "Registration date is returned by Trading XML GetUser but is omitted from REST Commerce Identity."

FIXED_NOTE_TEMPLATES: frozenset[str] = frozenset(
    {
        NOTE_USERNAME_EXACT_MATCH,
        NOTE_USERNAME_CASE_NORMALIZED,
        NOTE_USERNAME_MISMATCH,
        NOTE_USERNAME_MISSING_XML,
        NOTE_USERNAME_MISSING_MODERN,
        NOTE_USER_ID_SEMANTICS,
        NOTE_ACCOUNT_TYPE_EXACT_MATCH,
        NOTE_ACCOUNT_TYPE_MISMATCH,
        NOTE_ACCOUNT_TYPE_UNVERIFIED,
        NOTE_MARKETPLACE_EXACT_MATCH,
        NOTE_MARKETPLACE_CASE_NORMALIZED,
        NOTE_MARKETPLACE_MISMATCH,
        NOTE_MARKETPLACE_MISSING,
        NOTE_FEEDBACK_SCORE_UNSUPPORTED,
        NOTE_REGISTRATION_DATE_UNSUPPORTED,
    }
)

# Grounded Trading XML Site to Marketplace mapping
EBAY_SITE_TO_MARKETPLACE: dict[str, str] = {
    "US": "EBAY_US",
    "eBayMotors": "EBAY_MOTORS_US",
    "Motors": "EBAY_MOTORS_US",
    "Canada": "EBAY_CA",
    "CanadaFrench": "EBAY_CA",
    "UK": "EBAY_GB",
    "Australia": "EBAY_AU",
    "Austria": "EBAY_AT",
    "Belgium_Dutch": "EBAY_BE",
    "Belgium_French": "EBAY_BE",
    "France": "EBAY_FR",
    "Germany": "EBAY_DE",
    "Italy": "EBAY_IT",
    "Netherlands": "EBAY_NL",
    "Spain": "EBAY_ES",
    "Switzerland": "EBAY_CH",
    "Ireland": "EBAY_IE",
    "Poland": "EBAY_PL",
    "HongKong": "EBAY_HK",
    "Singapore": "EBAY_SG",
    "Malaysia": "EBAY_MY",
    "Philippines": "EBAY_PH",
    "India": "EBAY_IN",
}

TRUSTED_LIVE_PROVENANCE_SOURCES: frozenset[str] = frozenset({"live_oauth_token"})

MAX_IDENTITY_PAGES = 25
COMMERCE_IDENTITY_READONLY_SCOPE = (
    "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly"
)


def validate_max_pages(max_pages: int) -> int:
    """Validate that max_pages is an integer between 1 and 25 inclusive."""
    if not isinstance(max_pages, int) or isinstance(max_pages, bool):
        raise ValueError(
            f"max_pages must be an integer, got {type(max_pages).__name__}"
        )
    if max_pages < 1 or max_pages > MAX_IDENTITY_PAGES:
        raise ValueError(
            f"max_pages must be between 1 and {MAX_IDENTITY_PAGES}, got {max_pages}"
        )
    return max_pages


def pseudonymize_identifier(val: str | None, prefix: str = "user") -> str:
    """Return a deterministic, privacy-preserving 12-char SHA-256 pseudonym.

    Residual Risk Note: SHA-256 pseudonyms provide one-way identifier masking,
    but known low-entropy handles may be subject to dictionary brute-force.
    """
    if not val:
        return ""
    h = hashlib.sha256(f"ebay_parity_salt:{val.strip()}".encode("utf-8")).hexdigest()[
        :12
    ]
    return f"{prefix}_{h}"


_USER_PSEUDONYM_RE = re.compile(r"^user_[0-9a-f]{12}$")
_ACCOUNT_PSEUDONYM_RE = re.compile(r"^account_[0-9a-f]{12}$")


def _is_safe_user_pseudonym(key: str) -> bool:
    """Validate that key matches genuine pseudonym format ^user_[0-9a-f]{12}$."""
    if not _USER_PSEUDONYM_RE.fullmatch(key):
        return False
    suffix = key[5:]
    if suffix.isdigit():
        return False
    return True


_EBAY_TOKEN_PATTERN = re.compile(
    r"(Bearer\s+[^\s\"']+|v\^1\.1#[^\s\"']+|refresh_token=[^\s&\"']+)", re.IGNORECASE
)
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
_CURRENCY_AMOUNT_PATTERN = re.compile(
    r"(?:\b[A-Z]{3}\s*|\$|£|€|¥)\s*\d+(?:\.\d{1,4})?\b|\b\d+(?:\.\d{1,4})?\s*[A-Z]{3}\b|\b\d+\.\d{2}\b"
    r"|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million)\s+(?:dollars|cents|pounds|pence|euros|yen)\b",
    re.IGNORECASE,
)
_CREDENTIAL_KV_PATTERN = re.compile(
    r"(client_secret|client_id|refresh_token|password|secret|runame|token|auth_code|authorization|access_token)=([^&\s\"']+)",
    re.IGNORECASE,
)


def sanitize_diagnostic_string(val: str) -> str:
    """Sanitize arbitrary strings, redacting auth tokens, emails, credentials, amounts, and sensitive patterns."""
    if not isinstance(val, str):
        return str(val)
    text = _CREDENTIAL_KV_PATTERN.sub(r"\1=[REDACTED]", val)
    text = _EBAY_TOKEN_PATTERN.sub("[REDACTED_TOKEN]", text)
    text = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    text = _CURRENCY_AMOUNT_PATTERN.sub("[REDACTED_AMOUNT]", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Canonical Data Models
# ---------------------------------------------------------------------------
class AccountType(StrEnum):
    """Normalized eBay account classification."""

    INDIVIDUAL = "INDIVIDUAL"
    BUSINESS = "BUSINESS"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProviderVerificationEvidence:
    """Explicit external proof of identity, token binding, and API entitlement.

    Offline fixtures, synthetic defaults, and matching usernames NEVER confer
    account verification or cross-provider continuity without positive external proof.
    """

    account_verified: bool = False
    verified_account_id: str | None = None
    api_entitlement_verified: bool = False
    token_binding_verified: bool = False
    provenance_source: str = "unverified_offline"
    verified_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedAccountIdentity:
    """Canonical representation of an authenticated eBay account."""

    user_id: str | None = None  # Immutable system ID in modern REST; None in XML
    username: str | None = None  # Public handle (Trading UserID / REST username)
    account_type: AccountType = AccountType.UNKNOWN
    registration_marketplace_id: str | None = None  # e.g. EBAY_US
    feedback_score: int | None = None
    positive_feedback_percent: float | None = None
    registration_date: datetime | None = None
    source: str = "unknown"
    evidence_level: str = "unverified"
    notes: tuple[str, ...] = ()

    @property
    def username_pseudonym(self) -> str:
        """Return privacy-safe pseudonymized username."""
        return pseudonymize_identifier(self.username, prefix="user")

    @property
    def user_id_pseudonym(self) -> str:
        """Return privacy-safe pseudonymized user ID."""
        return pseudonymize_identifier(self.user_id, prefix="account")


@dataclass(frozen=True)
class IdentityCollectionResult:
    """Normalized snapshot of an account identity response."""

    identity: NormalizedAccountIdentity | None = None
    is_complete: bool = False  # Fail-closed: default incomplete without positive proof
    account_id: str | None = None
    username: str | None = None
    account_verified: bool = False
    entitlement_verified: bool = False
    errors: tuple[str, ...] = ()
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IdentityContractProbe:
    """Specification for an account identity contract probe."""

    key: str
    description: str
    query: str
    operation_name: str


IDENTITY_CONTRACT_PROBES: tuple[IdentityContractProbe, ...] = (
    IdentityContractProbe(
        key="typename_handshake",
        description="Minimal GraphQL root __typename handshake",
        query="query Handshake { __typename }",
        operation_name="Handshake",
    ),
    IdentityContractProbe(
        key="viewer_identity",
        description="Candidate GraphQL viewer identity probe",
        query="query GetViewerIdentity { viewer { username id } }",
        operation_name="GetViewerIdentity",
    ),
    IdentityContractProbe(
        key="user_profile",
        description="Candidate GraphQL user profile probe",
        query="query GetUserProfile { user { username id accountType } }",
        operation_name="GetUserProfile",
    ),
)


def validate_all_identity_contract_probes() -> None:
    """Validate all identity probes are read-only and syntactically sound."""
    for probe in IDENTITY_CONTRACT_PROBES:
        cleaned = _strip_graphql_strings_and_comments(probe.query)
        validate_read_only_query(cleaned)
        defs = _parse_graphql_definitions(cleaned)
        if not defs:
            raise ValueError(f"Probe {probe.key} has no operations")


# ---------------------------------------------------------------------------
# Legacy Trading XML Normalization
# ---------------------------------------------------------------------------
def normalize_trading_xml_get_user(
    root_or_xml: str | bytes | ET.Element,
    *,
    evidence: ProviderVerificationEvidence | None = None,
) -> IdentityCollectionResult:
    """Normalize a legacy Trading XML GetUser response into canonical IdentityCollectionResult."""
    if isinstance(root_or_xml, (str, bytes)):
        cleaned = (
            root_or_xml.decode("utf-8", errors="replace")
            if isinstance(root_or_xml, bytes)
            else root_or_xml
        ).strip()
        if not cleaned:
            return IdentityCollectionResult(
                errors=("EMPTY_XML_PAYLOAD",),
                is_complete=False,
            )
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            return IdentityCollectionResult(
                errors=("MALFORMED_XML",),
                is_complete=False,
            )
    elif isinstance(root_or_xml, ET.Element):
        root = root_or_xml
    else:
        return IdentityCollectionResult(
            errors=("INVALID_XML_INPUT_TYPE",),
            is_complete=False,
        )

    # 1. Enforce exact GetUserResponse root element with required qualified namespace
    expected_root_tag = f"{{{EBAY_XML_NS}}}GetUserResponse"
    if root.tag != expected_root_tag:
        if root.tag.startswith("{"):
            parts = root.tag[1:].split("}", 1)
            ns = parts[0]
            local = parts[1] if len(parts) > 1 else ""
            if local != "GetUserResponse":
                return IdentityCollectionResult(
                    errors=("INVALID_XML_ROOT",),
                    is_complete=False,
                )
            if ns != EBAY_XML_NS:
                return IdentityCollectionResult(
                    errors=("XML_NAMESPACE_MISMATCH",),
                    is_complete=False,
                )
        else:
            # Unqualified root (missing required namespace)
            if root.tag != "GetUserResponse":
                return IdentityCollectionResult(
                    errors=("INVALID_XML_ROOT",),
                    is_complete=False,
                )
            return IdentityCollectionResult(
                errors=("XML_NAMESPACE_MISMATCH",),
                is_complete=False,
            )

    # 2. Check Ack status in expected namespace
    ack_elem = root.find(f"{{{EBAY_XML_NS}}}Ack")
    if ack_elem is None or not ack_elem.text:
        return IdentityCollectionResult(
            errors=("XML_ACK_MISSING_OR_UNKNOWN",),
            is_complete=False,
        )
    ack = ack_elem.text.strip()
    if ack == "Failure":
        return IdentityCollectionResult(
            errors=("XML_ACK_FAILURE",),
            is_complete=False,
        )
    if ack == "PartialFailure":
        return IdentityCollectionResult(
            errors=("XML_ACK_PARTIAL_FAILURE",),
            is_complete=False,
        )
    if ack not in ("Success", "Warning"):
        return IdentityCollectionResult(
            errors=("XML_ACK_MISSING_OR_UNKNOWN",),
            is_complete=False,
        )

    # 3. Check for fatal error containers even alongside Ack=Success/Warning
    errors_elems = [e for e in root.iter() if e.tag.endswith("Errors")]
    for err_container in errors_elems:
        sev_elem = err_container.find(f"{{{EBAY_XML_NS}}}SeverityCode")
        if sev_elem is None:
            sev_elem = err_container.find("SeverityCode")
        severity = (
            (sev_elem.text or "").strip().lower()
            if sev_elem is not None and sev_elem.text
            else ""
        )
        if severity == "error":
            return IdentityCollectionResult(
                errors=("XML_FATAL_ERROR_CONTAINER",),
                is_complete=False,
            )

    # 4. Locate direct User element in expected namespace
    direct_users = root.findall(f"{{{EBAY_XML_NS}}}User")
    if len(direct_users) != 1:
        if len(direct_users) == 0:
            return IdentityCollectionResult(
                errors=("MISSING_USER_ELEMENT",),
                is_complete=False,
            )
        return IdentityCollectionResult(
            errors=("INVALID_XML_ROOT",),
            is_complete=False,
        )

    user_elem = direct_users[0]

    # Reject structurally inconsistent XML with nested or impostor User elements
    all_users = [
        e
        for e in root.iter()
        if e.tag == f"{{{EBAY_XML_NS}}}User" or e.tag.endswith("User")
    ]
    if len(all_users) != 1:
        return IdentityCollectionResult(
            errors=("INVALID_XML_ROOT",),
            is_complete=False,
        )

    # 5. UserID is required and must be non-empty direct child in expected namespace
    userid_elem = user_elem.find(f"{{{EBAY_XML_NS}}}UserID")
    if userid_elem is None or not userid_elem.text:
        return IdentityCollectionResult(
            errors=("MISSING_USER_ID",),
            is_complete=False,
        )
    username = userid_elem.text.strip()
    if not username:
        return IdentityCollectionResult(
            errors=("MISSING_USER_ID",),
            is_complete=False,
        )

    # 6. Safe parsing of optional numerical metrics
    feedback_score = None
    fb_elem = user_elem.find(f"{{{EBAY_XML_NS}}}FeedbackScore")
    if fb_elem is not None and fb_elem.text:
        try:
            feedback_score = _to_int(fb_elem.text.strip())
        except (ValueError, TypeError):
            pass

    positive_feedback_percent = None
    pos_fb_elem = user_elem.find(f"{{{EBAY_XML_NS}}}PositiveFeedbackPercent")
    if pos_fb_elem is not None and pos_fb_elem.text:
        try:
            positive_feedback_percent = float(pos_fb_elem.text.strip())
        except (ValueError, TypeError):
            pass

    reg_date = None
    reg_elem = user_elem.find(f"{{{EBAY_XML_NS}}}RegistrationDate")
    if reg_elem is not None and reg_elem.text:
        reg_date = parse_ebay_time(reg_elem.text.strip())

    # 7. Site to registration marketplace resolution using documented mapping
    site_elem = user_elem.find(f"{{{EBAY_XML_NS}}}Site")
    site = site_elem.text.strip() if site_elem is not None and site_elem.text else None
    marketplace_id: str | None = None
    if site:
        marketplace_id = EBAY_SITE_TO_MARKETPLACE.get(site)
        if marketplace_id is None and site.startswith("EBAY_"):
            marketplace_id = site

    seller_info = user_elem.find(f"{{{EBAY_XML_NS}}}SellerInfo")
    seller_business_type = ""
    if seller_info is not None:
        sbt_elem = seller_info.find(f"{{{EBAY_XML_NS}}}SellerBusinessType")
        if sbt_elem is not None and sbt_elem.text:
            seller_business_type = sbt_elem.text.strip().lower()

    if seller_business_type == "commercial":
        account_type = AccountType.BUSINESS
    elif seller_business_type == "private":
        account_type = AccountType.INDIVIDUAL
    else:
        account_type = AccountType.UNKNOWN

    # Grounded verification: XML UserID alone never certifies stable modern ID.
    # Offline fixtures, synthetic booleans, and arbitrary provenance strings never certify live verification.
    is_live_provenance = bool(
        evidence and evidence.provenance_source in TRUSTED_LIVE_PROVENANCE_SOURCES
    )
    is_verified = bool(
        evidence
        and evidence.account_verified
        and evidence.token_binding_verified
        and is_live_provenance
    )
    entitlement_verified = bool(
        evidence and evidence.api_entitlement_verified and is_live_provenance
    )
    evidence_level = "verified" if is_verified else "unverified_offline"

    normalized = NormalizedAccountIdentity(
        user_id=None,  # Trading XML does not expose the immutable modern userId
        username=username,
        account_type=account_type,
        registration_marketplace_id=marketplace_id,
        feedback_score=feedback_score,
        positive_feedback_percent=positive_feedback_percent,
        registration_date=reg_date,
        source="trading_xml_get_user",
        evidence_level=evidence_level,
        notes=("Trading UserID represents the public account handle (username).",),
    )

    return IdentityCollectionResult(
        identity=normalized,
        is_complete=True,
        account_id=evidence.verified_account_id if is_verified and evidence else None,
        username=username,
        account_verified=is_verified,
        entitlement_verified=entitlement_verified,
        errors=(),
        diagnostics={
            "xml_complete": True,
            "has_feedback_score": feedback_score is not None,
            "has_registration_date": reg_date is not None,
        },
    )


# ---------------------------------------------------------------------------
# Modern REST Commerce Identity Normalization
# ---------------------------------------------------------------------------
def normalize_rest_identity_get_user(
    payload_or_json: str | bytes | Mapping[str, Any],
    *,
    evidence: ProviderVerificationEvidence | None = None,
) -> IdentityCollectionResult:
    """Normalize an official eBay REST Commerce Identity getUser response into canonical IdentityCollectionResult."""
    if isinstance(payload_or_json, (str, bytes)):
        cleaned = (
            payload_or_json.decode("utf-8", errors="replace")
            if isinstance(payload_or_json, bytes)
            else payload_or_json
        ).strip()
        if not cleaned:
            return IdentityCollectionResult(
                errors=("EMPTY_JSON_PAYLOAD",),
                is_complete=False,
            )
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return IdentityCollectionResult(
                errors=("MALFORMED_JSON",),
                is_complete=False,
            )
    elif isinstance(payload_or_json, Mapping):
        data = payload_or_json
    else:
        return IdentityCollectionResult(
            errors=("INVALID_JSON_INPUT_TYPE",),
            is_complete=False,
        )

    # Fail closed on error objects and unexpected errors structures
    if "errors" in data:
        err_val = data["errors"]
        if isinstance(err_val, (list, tuple)):
            if len(err_val) > 0:
                return IdentityCollectionResult(
                    errors=("REST_API_ERROR",),
                    is_complete=False,
                )
        elif isinstance(err_val, (dict, Mapping)):
            if len(err_val) > 0:
                return IdentityCollectionResult(
                    errors=("REST_API_ERROR",),
                    is_complete=False,
                )
        elif isinstance(err_val, str):
            if err_val.strip():
                return IdentityCollectionResult(
                    errors=("REST_API_ERROR",),
                    is_complete=False,
                )
        else:
            return IdentityCollectionResult(
                errors=("REST_API_ERROR",),
                is_complete=False,
            )

    if "error" in data:
        err_val = data["error"]
        if (
            (isinstance(err_val, str) and err_val.strip())
            or (isinstance(err_val, (dict, Mapping, list, tuple)) and len(err_val) > 0)
            or (
                not isinstance(err_val, (str, dict, Mapping, list, tuple))
                and err_val is not None
                and err_val is not False
            )
        ):
            return IdentityCollectionResult(
                errors=("REST_API_ERROR",),
                is_complete=False,
            )

    user_id = data.get("userId")
    username = data.get("username")

    if not isinstance(username, str) or not username.strip():
        return IdentityCollectionResult(
            errors=("MISSING_OR_INVALID_USERNAME",),
            is_complete=False,
        )

    if not isinstance(user_id, str) or not user_id.strip():
        return IdentityCollectionResult(
            errors=("MISSING_OR_INVALID_USER_ID",),
            is_complete=False,
        )

    clean_user_id = user_id.strip()
    clean_username = username.strip()

    # Grounded verification & Account ID conflict check
    id_conflict = False
    if evidence and evidence.verified_account_id:
        if evidence.verified_account_id != clean_user_id:
            id_conflict = True

    is_live_provenance = bool(
        evidence and evidence.provenance_source in TRUSTED_LIVE_PROVENANCE_SOURCES
    )
    is_verified = bool(
        evidence
        and evidence.account_verified
        and evidence.token_binding_verified
        and not id_conflict
        and is_live_provenance
    )
    entitlement_verified = bool(
        evidence and evidence.api_entitlement_verified and is_live_provenance
    )
    evidence_level = "verified" if is_verified else "unverified_offline"

    raw_account_type = str(data.get("accountType") or "").strip().upper()
    if raw_account_type == "BUSINESS":
        account_type = AccountType.BUSINESS
    elif raw_account_type == "INDIVIDUAL":
        account_type = AccountType.INDIVIDUAL
    else:
        account_type = AccountType.UNKNOWN

    raw_marketplace_id = data.get("registrationMarketplaceId")
    registration_marketplace_id = (
        str(raw_marketplace_id).strip() if raw_marketplace_id else None
    )

    errors: list[str] = []
    if id_conflict:
        errors.append("ACCOUNT_ID_CONFLICT")

    normalized = NormalizedAccountIdentity(
        user_id=clean_user_id,
        username=clean_username,
        account_type=account_type,
        registration_marketplace_id=registration_marketplace_id,
        feedback_score=None,  # REST Identity does not return feedback metrics
        positive_feedback_percent=None,
        registration_date=None,
        source="rest_identity_get_user",
        evidence_level=evidence_level,
        notes=(
            "REST userId is the immutable opaque identifier; username is the public handle.",
        ),
    )

    return IdentityCollectionResult(
        identity=normalized,
        is_complete=not errors,
        account_id=clean_user_id if not id_conflict else None,
        username=clean_username,
        account_verified=is_verified,
        entitlement_verified=entitlement_verified,
        errors=tuple(errors),
        diagnostics={
            "modern_complete": not errors,
            "account_verified": is_verified,
            "entitlement_verified": entitlement_verified,
        },
    )


# ---------------------------------------------------------------------------
# Candidate GraphQL User Normalization
# ---------------------------------------------------------------------------
def normalize_graphql_user(
    payload_or_json: str | bytes | Mapping[str, Any],
    *,
    evidence: ProviderVerificationEvidence | None = None,
) -> IdentityCollectionResult:
    """Normalize a candidate GraphQL viewer/user query response."""
    if isinstance(payload_or_json, (str, bytes)):
        cleaned = (
            payload_or_json.decode("utf-8", errors="replace")
            if isinstance(payload_or_json, bytes)
            else payload_or_json
        ).strip()
        if not cleaned:
            return IdentityCollectionResult(
                errors=("EMPTY_JSON_PAYLOAD",),
                is_complete=False,
            )
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return IdentityCollectionResult(
                errors=("MALFORMED_JSON",),
                is_complete=False,
            )
    elif isinstance(payload_or_json, Mapping):
        data = payload_or_json
    else:
        return IdentityCollectionResult(
            errors=("INVALID_JSON_INPUT_TYPE",),
            is_complete=False,
        )

    # Fail closed on GraphQL error containers and unexpected errors structures
    if "errors" in data:
        err_val = data["errors"]
        if isinstance(err_val, (list, tuple)):
            if len(err_val) > 0:
                return IdentityCollectionResult(
                    errors=("GRAPHQL_API_ERROR",),
                    is_complete=False,
                )
        elif isinstance(err_val, (dict, Mapping)):
            if len(err_val) > 0:
                return IdentityCollectionResult(
                    errors=("GRAPHQL_API_ERROR",),
                    is_complete=False,
                )
        elif isinstance(err_val, str):
            if err_val.strip():
                return IdentityCollectionResult(
                    errors=("GRAPHQL_API_ERROR",),
                    is_complete=False,
                )
        else:
            return IdentityCollectionResult(
                errors=("GRAPHQL_API_ERROR",),
                is_complete=False,
            )

    container = data.get("data")
    if not isinstance(container, dict):
        return IdentityCollectionResult(
            errors=("MISSING_DATA_OBJECT",),
            is_complete=False,
        )

    user_obj = container.get("viewer") or container.get("user")
    if not isinstance(user_obj, dict):
        return IdentityCollectionResult(
            errors=("MISSING_VIEWER_OR_USER_OBJECT",),
            is_complete=False,
        )

    username = user_obj.get("username")
    user_id = user_obj.get("id") or user_obj.get("userId")

    if not isinstance(username, str) or not username.strip():
        return IdentityCollectionResult(
            errors=("MISSING_OR_INVALID_USERNAME",),
            is_complete=False,
        )

    raw_type = str(user_obj.get("accountType") or "").strip().upper()
    if raw_type == "BUSINESS":
        account_type = AccountType.BUSINESS
    elif raw_type == "INDIVIDUAL":
        account_type = AccountType.INDIVIDUAL
    else:
        account_type = AccountType.UNKNOWN

    is_live_provenance = bool(
        evidence and evidence.provenance_source in TRUSTED_LIVE_PROVENANCE_SOURCES
    )
    is_verified = bool(
        evidence
        and evidence.account_verified
        and evidence.token_binding_verified
        and is_live_provenance
    )
    entitlement_verified = bool(
        evidence and evidence.api_entitlement_verified and is_live_provenance
    )
    evidence_level = "verified" if is_verified else "unverified_offline"

    normalized = NormalizedAccountIdentity(
        user_id=str(user_id).strip() if user_id else None,
        username=username.strip(),
        account_type=account_type,
        registration_marketplace_id=None,
        feedback_score=None,
        positive_feedback_percent=None,
        registration_date=None,
        source="graphql_user",
        evidence_level=evidence_level,
        notes=("Exploratory candidate GraphQL user representation.",),
    )

    return IdentityCollectionResult(
        identity=normalized,
        is_complete=True,
        account_id=str(user_id).strip() if user_id else None,
        username=username.strip(),
        account_verified=is_verified,
        entitlement_verified=entitlement_verified,
        errors=(),
        diagnostics={
            "modern_complete": True,
            "has_user_id": user_id is not None,
        },
    )


# ---------------------------------------------------------------------------
# Cross-Provider Comparison & Parity Assessment
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IdentityFieldComparison:
    """Field-level comparison outcome between XML and Modern Identity."""

    field_name: str
    status: str
    notes: str


def compare_identity_snapshots(
    xml_snapshot: IdentityCollectionResult,
    modern_snapshot: IdentityCollectionResult,
) -> dict[str, Any]:
    """Compare Trading XML GetUser and Modern Identity snapshots.

    Evaluates field parity, username continuity, account type consistency,
    and migration readiness. Fails closed when collections are incomplete,
    unverified, or mismatched.
    """
    field_comparisons: list[IdentityFieldComparison] = []
    blocking_reasons: list[str] = []
    warnings: list[str] = []

    xml_ident = xml_snapshot.identity
    mod_ident = modern_snapshot.identity

    # 1. Collection completeness check
    if not xml_snapshot.is_complete:
        blocking_reasons.append("TRADING_XML_GET_USER_INCOMPLETE")
    if not modern_snapshot.is_complete:
        blocking_reasons.append("MODERN_IDENTITY_INCOMPLETE")

    # 2. Scope entitlement check
    if not modern_snapshot.entitlement_verified:
        blocking_reasons.append("MODERN_IDENTITY_ENTITLEMENT_UNVERIFIED")

    # 3. Account verification check
    if not xml_snapshot.account_verified or not modern_snapshot.account_verified:
        blocking_reasons.append("ACCOUNT_IDENTITY_UNVERIFIED")

    # In Milestone 8, migration readiness is strictly blocked (validation-only milestone)
    blocking_reasons.append("M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER")

    # Check for upstream snapshot errors or explicit account ID conflicts
    if (
        "ACCOUNT_ID_CONFLICT" in xml_snapshot.errors
        or "ACCOUNT_ID_CONFLICT" in modern_snapshot.errors
    ):
        blocking_reasons.append("ACCOUNT_ID_CONFLICT")

    if (
        xml_snapshot.account_id
        and modern_snapshot.account_id
        and str(xml_snapshot.account_id).strip()
        != str(modern_snapshot.account_id).strip()
    ):
        blocking_reasons.append("ACCOUNT_ID_CONFLICT")

    # Field comparisons
    # Field: username (public handle)
    xml_username = xml_ident.username if xml_ident else None
    mod_username = mod_ident.username if mod_ident else None

    if xml_username and mod_username:
        if xml_username == mod_username:
            username_status = PARITY_EXACT_MATCH
            username_note = NOTE_USERNAME_EXACT_MATCH
        elif xml_username.lower() == mod_username.lower():
            username_status = PARITY_EQUIVALENT_NORMALIZED
            username_note = NOTE_USERNAME_CASE_NORMALIZED
        else:
            username_status = PARITY_DIFFERENT_SEMANTICS
            username_note = NOTE_USERNAME_MISMATCH
            blocking_reasons.append("USERNAME_MISMATCH")
    elif mod_username:
        username_status = PARITY_MISSING_FIELD
        username_note = NOTE_USERNAME_MISSING_XML
    else:
        username_status = PARITY_MISSING_FIELD
        username_note = NOTE_USERNAME_MISSING_MODERN
        blocking_reasons.append("MISSING_OR_INVALID_USERNAME")

    field_comparisons.append(
        IdentityFieldComparison(
            field_name="username",
            status=username_status,
            notes=username_note,
        )
    )

    # Field: user_id (immutable account ID)
    field_comparisons.append(
        IdentityFieldComparison(
            field_name="user_id",
            status=PARITY_DIFFERENT_SEMANTICS,
            notes=NOTE_USER_ID_SEMANTICS,
        )
    )

    # Field: account_type
    xml_type = xml_ident.account_type if xml_ident else AccountType.UNKNOWN
    mod_type = mod_ident.account_type if mod_ident else AccountType.UNKNOWN

    if xml_type in (AccountType.BUSINESS, AccountType.INDIVIDUAL) and mod_type in (
        AccountType.BUSINESS,
        AccountType.INDIVIDUAL,
    ):
        if xml_type == mod_type:
            type_status = PARITY_EXACT_MATCH
            type_note = NOTE_ACCOUNT_TYPE_EXACT_MATCH
        else:
            type_status = PARITY_DIFFERENT_SEMANTICS
            type_note = NOTE_ACCOUNT_TYPE_MISMATCH
            blocking_reasons.append("ACCOUNT_TYPE_MISMATCH")
    else:
        # One or both unknown -> unverified, cannot claim equivalence
        type_status = PARITY_UNVERIFIED
        type_note = NOTE_ACCOUNT_TYPE_UNVERIFIED

    field_comparisons.append(
        IdentityFieldComparison(
            field_name="account_type",
            status=type_status,
            notes=type_note,
        )
    )

    # Field: registration_marketplace_id
    xml_mkt = xml_ident.registration_marketplace_id if xml_ident else None
    mod_mkt = mod_ident.registration_marketplace_id if mod_ident else None

    if xml_mkt and mod_mkt:
        if xml_mkt == mod_mkt:
            mkt_status = PARITY_EXACT_MATCH
            mkt_note = NOTE_MARKETPLACE_EXACT_MATCH
        else:
            # Verified conflict between two distinct marketplaces is DIFFERENT_SEMANTICS
            mkt_status = PARITY_DIFFERENT_SEMANTICS
            mkt_note = NOTE_MARKETPLACE_MISMATCH
            blocking_reasons.append("MARKETPLACE_MISMATCH")
    elif mod_mkt:
        mkt_status = PARITY_MISSING_FIELD
        mkt_note = NOTE_MARKETPLACE_MISSING
    else:
        mkt_status = PARITY_MISSING_FIELD
        mkt_note = NOTE_MARKETPLACE_MISSING

    field_comparisons.append(
        IdentityFieldComparison(
            field_name="registration_marketplace_id",
            status=mkt_status,
            notes=mkt_note,
        )
    )

    # Field: feedback_score (present in XML GetUser, not in REST Identity)
    field_comparisons.append(
        IdentityFieldComparison(
            field_name="feedback_score",
            status=PARITY_MISSING_FIELD,
            notes=NOTE_FEEDBACK_SCORE_UNSUPPORTED,
        )
    )

    # Field: registration_date
    field_comparisons.append(
        IdentityFieldComparison(
            field_name="registration_date",
            status=PARITY_MISSING_FIELD,
            notes=NOTE_REGISTRATION_DATE_UNSUPPORTED,
        )
    )

    username_matches = bool(
        xml_username and mod_username and xml_username.lower() == mod_username.lower()
    )

    # Cross-provider account continuity requires independent live token-to-account bindings
    # which Milestone 8 (strictly offline validation-only) does not acquire.
    # Handcrafted snapshots, copied booleans, or matching usernames cannot certify continuity.
    has_continuity = False

    return {
        "xml_source": xml_ident.source if xml_ident else "unknown",
        "modern_source": mod_ident.source if mod_ident else "unknown",
        "username_matches": username_matches,
        "account_continuity_verified": has_continuity,
        "migration_readiness": "ready" if not blocking_reasons else "blocked",
        "blocking_reasons": tuple(dict.fromkeys(blocking_reasons)),
        "warnings": tuple(dict.fromkeys(warnings)),
        "fields": {
            comp.field_name: {
                "status": comp.status,
                "label": PARITY_LABELS.get(comp.status, comp.status),
                "notes": comp.notes,
            }
            for comp in field_comparisons
        },
        "diagnostics": {
            "xml_complete": xml_snapshot.is_complete,
            "modern_complete": modern_snapshot.is_complete,
            "xml_errors": xml_snapshot.errors,
            "modern_errors": modern_snapshot.errors,
            "required_scope": COMMERCE_IDENTITY_READONLY_SCOPE,
            "account_verified": False,
            "entitlement_verified": modern_snapshot.entitlement_verified,
        },
    }


# ---------------------------------------------------------------------------
# Report Sanitization & Privacy Export (Strict Default-Deny Schema)
# ---------------------------------------------------------------------------
def sanitize_identity_parity_report(report: dict[str, Any]) -> dict[str, Any]:
    """Ensure report strictly adheres to default-deny schema with no raw strings or secrets."""
    if not isinstance(report, dict):
        return {
            "migration_readiness": "blocked",
            "account_continuity_verified": False,
            "blocking_reasons": [
                "UNKNOWN_ERROR",
                "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER",
            ],
        }

    sanitized: dict[str, Any] = {}

    # 1. Sources (allowlisted string codes only)
    xml_src = report.get("xml_source")
    sanitized["xml_source"] = (
        xml_src if xml_src in ("trading_xml_get_user", "unknown") else "unknown"
    )

    mod_src = report.get("modern_source")
    sanitized["modern_source"] = (
        mod_src
        if mod_src in ("rest_identity_get_user", "graphql_user", "unknown")
        else "unknown"
    )

    # 2. Strict booleans
    sanitized["username_matches"] = bool(report.get("username_matches", False))
    # Authenticated cross-provider continuity cannot be certified without genuine live token bindings;
    # reject caller-provided True booleans in validation-only milestone.
    sanitized["account_continuity_verified"] = False

    # 3. Migration readiness (intrinsically fail-closed in M8 validation milestone)
    sanitized["migration_readiness"] = "blocked"

    # 4. Blocking reasons (strictly allowlisted symbolic error codes)
    raw_blocking = report.get("blocking_reasons", ())
    clean_blocking: list[str] = []
    if isinstance(raw_blocking, (list, tuple)):
        for item in raw_blocking:
            s_item = str(item)
            if s_item in ALLOWED_SYMBOLIC_ERROR_CODES:
                clean_blocking.append(s_item)
            else:
                clean_blocking.append("BLOCKED_UNKNOWN")
    if "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" not in clean_blocking:
        clean_blocking.append("M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER")
    sanitized["blocking_reasons"] = clean_blocking

    # 5. Warnings (strictly allowlisted symbolic error codes)
    raw_warnings = report.get("warnings", ())
    clean_warnings: list[str] = []
    if isinstance(raw_warnings, (list, tuple)):
        for item in raw_warnings:
            s_item = str(item)
            if s_item in ALLOWED_SYMBOLIC_ERROR_CODES:
                clean_warnings.append(s_item)
    sanitized["warnings"] = clean_warnings

    # 6. Monitored Fields (strictly MONITORED_IDENTITY_FIELDS with approved note templates)
    raw_fields = report.get("fields", {})
    clean_fields: dict[str, Any] = {}
    if isinstance(raw_fields, dict):
        for field_name in MONITORED_IDENTITY_FIELDS:
            field_data = raw_fields.get(field_name, {})
            if not isinstance(field_data, dict):
                continue
            status = field_data.get("status")
            if status not in PARITY_LABELS:
                status = PARITY_UNVERIFIED

            note = str(field_data.get("notes", ""))
            # Strictly verify note is from approved fixed templates
            if note not in FIXED_NOTE_TEMPLATES:
                note = "Status verified."

            clean_fields[field_name] = {
                "status": status,
                "label": PARITY_LABELS[status],
                "notes": note,
            }
    sanitized["fields"] = clean_fields

    # 7. Diagnostics (strictly ALLOWED_DIAGNOSTIC_KEYS and scalar/symbolic types)
    raw_diag = report.get("diagnostics", {})
    clean_diag: dict[str, Any] = {}
    if isinstance(raw_diag, dict):
        for k in ALLOWED_DIAGNOSTIC_KEYS:
            if k not in raw_diag:
                continue
            val = raw_diag[k]
            if k in (
                "xml_complete",
                "modern_complete",
                "account_verified",
                "entitlement_verified",
            ):
                clean_diag[k] = bool(val)
            elif k == "required_scope":
                clean_diag[k] = COMMERCE_IDENTITY_READONLY_SCOPE
            elif k in ("xml_errors", "modern_errors"):
                if isinstance(val, (list, tuple)):
                    clean_diag[k] = tuple(
                        str(e) for e in val if str(e) in ALLOWED_SYMBOLIC_ERROR_CODES
                    )
                else:
                    clean_diag[k] = ()
    sanitized["diagnostics"] = clean_diag

    return sanitized


def render_identity_parity_markdown(report: dict[str, Any]) -> str:
    """Render a sanitized identity parity report as GitHub-flavored Markdown."""
    sanitized = sanitize_identity_parity_report(report)
    lines: list[str] = [
        "# eBay Account Identity Parity Audit (Milestone 8)",
        "",
        f"- **XML Source:** `{sanitized.get('xml_source', 'unknown')}`",
        f"- **Modern Source:** `{sanitized.get('modern_source', 'unknown')}`",
        f"- **Username Matches:** `{'Yes' if sanitized.get('username_matches') else 'No'}`",
        f"- **Account Continuity Verified:** `{'Yes' if sanitized.get('account_continuity_verified') else 'No'}`",
        f"- **Migration Readiness:** `{sanitized.get('migration_readiness', 'blocked').upper()}`",
        "",
    ]

    blocking = sanitized.get("blocking_reasons", [])
    if blocking:
        lines.append("## Blocking Reasons")
        for b in blocking:
            diag_desc = GENERIC_DIAGNOSTIC_MESSAGES.get(b, b)
            lines.append(f"- 🛑 `{b}`: {diag_desc}")
        lines.append("")

    warnings = sanitized.get("warnings", [])
    if warnings:
        lines.append("## Warnings")
        for w in warnings:
            lines.append(f"- ⚠️ `{w}`")
        lines.append("")

    lines.append("## Monitored Field Parity")
    lines.append("| Field | Parity Status | Notes |")
    lines.append("| :--- | :--- | :--- |")

    fields = sanitized.get("fields", {})
    for f_name in MONITORED_IDENTITY_FIELDS:
        f_info = fields.get(f_name, {})
        status = f_info.get("status", "unverified")
        label = f_info.get("label", status)
        notes = f_info.get("notes", "")
        lines.append(f"| `{f_name}` | **{label}** | {notes} |")

    lines.append("")
    lines.append("## Diagnostic Metadata")
    diag = sanitized.get("diagnostics", {})
    lines.append(f"- **XML Complete:** `{diag.get('xml_complete', False)}`")
    lines.append(f"- **Modern Complete:** `{diag.get('modern_complete', False)}`")
    lines.append(
        f"- **Required OAuth Scope:** `{diag.get('required_scope', COMMERCE_IDENTITY_READONLY_SCOPE)}`"
    )
    lines.append("")
    lines.append("> [!IMPORTANT]")
    lines.append(
        "> Milestone 8 is strictly read-only validation. Trading XML `GetUser` remains 100% authoritative"
    )
    lines.append("> in production Home Assistant runtime with zero cutover.")

    return "\n".join(lines)


def render_identity_parity_json(report: dict[str, Any]) -> str:
    """Render a sanitized identity parity report as deterministic indented JSON."""
    sanitized = sanitize_identity_parity_report(report)
    return json.dumps(sanitized, indent=2, sort_keys=True)
