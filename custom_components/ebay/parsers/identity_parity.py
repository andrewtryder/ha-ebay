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
from ..const import NS
from .xml import (
    _text,
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
        "email",
        "password",
        "secret",
        "user_email",
        "phone",
    }
)

_EBAY_TOKEN_PATTERN = re.compile(
    r"(Bearer\s+[^\s\"']+|v\^1\.1#[^\s\"']+|refresh_token=[^\s&\"']+)", re.IGNORECASE
)
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")

_CURRENCY_AMOUNT_PATTERN = re.compile(
    r"(?:\b[A-Z]{3}\s*|\$|£|€|¥)\s*\d+(?:\.\d{1,4})?\b|\b\d+(?:\.\d{1,4})?\s*[A-Z]{3}\b|\b\d+\.\d{2}\b",
    re.IGNORECASE,
)
_CREDENTIAL_KV_PATTERN = re.compile(
    r"(client_secret|client_id|refresh_token|password|secret|runame|token|auth_code|authorization|access_token)=([^&\s\"']+)",
    re.IGNORECASE,
)

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
    """Return a deterministic, privacy-preserving 12-char SHA-256 pseudonym."""
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


def sanitize_diagnostic_string(val: str) -> str:
    """Sanitize arbitrary strings, redacting auth tokens, emails, credentials, and sensitive patterns."""
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
    """Explicit external proof of identity and API entitlement.

    Offline fixtures and self-reported response fields NEVER automatically confer verification.
    """

    account_verified: bool = False
    verified_account_id: str | None = None
    api_entitlement_verified: bool = False
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
        except ET.ParseError as exc:
            return IdentityCollectionResult(
                errors=(f"MALFORMED_XML: {type(exc).__name__}",),
                is_complete=False,
            )
    elif isinstance(root_or_xml, ET.Element):
        root = root_or_xml
    else:
        return IdentityCollectionResult(
            errors=("INVALID_XML_INPUT_TYPE",),
            is_complete=False,
        )

    # Check for root Ack failure
    ack = (_text(root, "Ack") or _text(root, "e:Ack") or "").strip().lower()
    if ack == "failure":
        error_msg = (
            _text(root, "Errors/ShortMessage")
            or _text(root, "e:Errors/e:ShortMessage")
            or "XML_ACK_FAILURE"
        )
        return IdentityCollectionResult(
            errors=(f"TRADING_API_ERROR: {sanitize_diagnostic_string(error_msg)}",),
            is_complete=False,
        )

    # Locate User element
    user_elem = (
        root.find("e:User", NS)
        if root.find("e:User", NS) is not None
        else root.find("User")
    )
    if user_elem is None and (root.tag.endswith("User") or root.tag == "User"):
        user_elem = root

    if user_elem is None:
        return IdentityCollectionResult(
            errors=("MISSING_USER_ELEMENT",),
            is_complete=False,
        )

    # In Trading XML, UserID is the public handle/username
    username = _text(user_elem, "e:UserID") or _text(user_elem, "UserID")
    if not username:
        return IdentityCollectionResult(
            errors=("MISSING_USER_ID",),
            is_complete=False,
        )

    feedback_score = _to_int(
        _text(user_elem, "e:FeedbackScore") or _text(user_elem, "FeedbackScore")
    )
    pos_feedback_raw = _text(user_elem, "e:PositiveFeedbackPercent") or _text(
        user_elem, "PositiveFeedbackPercent"
    )
    positive_feedback_percent = None
    if pos_feedback_raw:
        try:
            positive_feedback_percent = float(pos_feedback_raw)
        except (ValueError, TypeError):
            pass

    reg_date_raw = _text(user_elem, "e:RegistrationDate") or _text(
        user_elem, "RegistrationDate"
    )
    reg_date = parse_ebay_time(reg_date_raw) if reg_date_raw else None

    site = _text(user_elem, "e:Site") or _text(user_elem, "Site")
    marketplace_id = f"EBAY_{site}" if site else None

    seller_business_type = (
        (
            _text(user_elem, "e:SellerInfo/e:SellerBusinessType")
            or _text(user_elem, "SellerInfo/SellerBusinessType")
            or ""
        )
        .strip()
        .lower()
    )

    if seller_business_type == "commercial":
        account_type = AccountType.BUSINESS
    elif seller_business_type == "private":
        account_type = AccountType.INDIVIDUAL
    else:
        account_type = AccountType.UNKNOWN

    is_verified = bool(evidence and evidence.account_verified)
    entitlement_verified = bool(evidence and evidence.api_entitlement_verified)
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
        account_id=evidence.verified_account_id if evidence else None,
        username=username,
        account_verified=is_verified,
        entitlement_verified=entitlement_verified,
        errors=(),
        diagnostics={
            "source": "trading_xml_get_user",
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
        except json.JSONDecodeError as exc:
            return IdentityCollectionResult(
                errors=(f"MALFORMED_JSON: {type(exc).__name__}",),
                is_complete=False,
            )
    elif isinstance(payload_or_json, Mapping):
        data = payload_or_json
    else:
        return IdentityCollectionResult(
            errors=("INVALID_JSON_INPUT_TYPE",),
            is_complete=False,
        )

    # Detect API error response
    if "errors" in data and isinstance(data["errors"], list) and data["errors"]:
        first_err = data["errors"][0]
        err_msg = (
            first_err.get("message") if isinstance(first_err, dict) else str(first_err)
        )
        return IdentityCollectionResult(
            errors=(f"REST_API_ERROR: {sanitize_diagnostic_string(str(err_msg))}",),
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

    is_verified = bool(evidence and evidence.account_verified)
    entitlement_verified = bool(evidence and evidence.api_entitlement_verified)
    evidence_level = "verified" if is_verified else "unverified_offline"

    normalized = NormalizedAccountIdentity(
        user_id=user_id.strip(),
        username=username.strip(),
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
        is_complete=True,
        account_id=user_id.strip(),
        username=username.strip(),
        account_verified=is_verified,
        entitlement_verified=entitlement_verified,
        errors=(),
        diagnostics={
            "source": "rest_identity_get_user",
            "account_type": str(account_type),
            "marketplace_id": registration_marketplace_id or "UNKNOWN",
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
        except json.JSONDecodeError as exc:
            return IdentityCollectionResult(
                errors=(f"MALFORMED_JSON: {type(exc).__name__}",),
                is_complete=False,
            )
    elif isinstance(payload_or_json, Mapping):
        data = payload_or_json
    else:
        return IdentityCollectionResult(
            errors=("INVALID_JSON_INPUT_TYPE",),
            is_complete=False,
        )

    if "errors" in data and isinstance(data["errors"], list) and data["errors"]:
        first_err = data["errors"][0]
        err_msg = (
            first_err.get("message") if isinstance(first_err, dict) else str(first_err)
        )
        return IdentityCollectionResult(
            errors=(f"GRAPHQL_API_ERROR: {sanitize_diagnostic_string(str(err_msg))}",),
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

    is_verified = bool(evidence and evidence.account_verified)
    entitlement_verified = bool(evidence and evidence.api_entitlement_verified)
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
            "source": "graphql_user",
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
    xml_value_pseudonym: str | None
    modern_value_pseudonym: str | None
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

    # Field comparisons
    # Field: username (public handle)
    xml_username = xml_ident.username if xml_ident else None
    mod_username = mod_ident.username if mod_ident else None

    if xml_username and mod_username:
        if xml_username == mod_username:
            username_status = PARITY_EXACT_MATCH
            username_note = "Public username matches exactly between Trading XML and Modern Identity."
        elif xml_username.lower() == mod_username.lower():
            username_status = PARITY_EQUIVALENT_NORMALIZED
            username_note = "Public username matches case-insensitively after canonical lowercase normalization."
        else:
            username_status = PARITY_DIFFERENT_SEMANTICS
            username_note = (
                "Public username mismatch between Trading XML and Modern Identity."
            )
            blocking_reasons.append("USERNAME_MISMATCH")
    elif mod_username:
        username_status = PARITY_MISSING_FIELD
        username_note = (
            "Username present in modern identity but missing from Trading XML."
        )
    else:
        username_status = PARITY_MISSING_FIELD
        username_note = "Username missing in modern identity."
        blocking_reasons.append("MISSING_MODERN_USERNAME")

    field_comparisons.append(
        IdentityFieldComparison(
            field_name="username",
            xml_value_pseudonym=pseudonymize_identifier(xml_username, "user")
            if xml_username
            else None,
            modern_value_pseudonym=pseudonymize_identifier(mod_username, "user")
            if mod_username
            else None,
            status=username_status,
            notes=username_note,
        )
    )

    # Field: user_id (immutable account ID)
    mod_user_id = mod_ident.user_id if mod_ident else None
    field_comparisons.append(
        IdentityFieldComparison(
            field_name="user_id",
            xml_value_pseudonym=None,
            modern_value_pseudonym=pseudonymize_identifier(mod_user_id, "account")
            if mod_user_id
            else None,
            status=PARITY_DIFFERENT_SEMANTICS,
            notes=(
                "Trading XML UserID is the public handle (username); modern REST userId is "
                "the immutable opaque system identifier. They cannot be used interchangeably."
            ),
        )
    )

    # Field: account_type
    xml_type = xml_ident.account_type if xml_ident else AccountType.UNKNOWN
    mod_type = mod_ident.account_type if mod_ident else AccountType.UNKNOWN
    if xml_type != AccountType.UNKNOWN and mod_type != AccountType.UNKNOWN:
        if xml_type == mod_type:
            type_status = PARITY_EXACT_MATCH
            type_note = f"Account type {xml_type} matches exactly."
        else:
            type_status = PARITY_DIFFERENT_SEMANTICS
            type_note = f"Account type differs: XML={xml_type}, Modern={mod_type}."
            warnings.append("ACCOUNT_TYPE_DIFFERENCE")
    elif mod_type != AccountType.UNKNOWN:
        type_status = PARITY_EQUIVALENT_NORMALIZED
        type_note = f"Modern identity provides account type {mod_type}; XML does not report business type."
    else:
        type_status = PARITY_UNVERIFIED
        type_note = "Account type could not be determined."

    field_comparisons.append(
        IdentityFieldComparison(
            field_name="account_type",
            xml_value_pseudonym=str(xml_type),
            modern_value_pseudonym=str(mod_type),
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
            mkt_note = f"Registration marketplace {xml_mkt} matches."
        else:
            mkt_status = PARITY_EQUIVALENT_NORMALIZED
            mkt_note = f"Marketplace normalized: XML={xml_mkt}, Modern={mod_mkt}."
    elif mod_mkt:
        mkt_status = PARITY_EQUIVALENT_NORMALIZED
        mkt_note = f"Modern marketplace {mod_mkt}; XML omitted site."
    else:
        mkt_status = PARITY_MISSING_FIELD
        mkt_note = "Registration marketplace missing in modern response."

    field_comparisons.append(
        IdentityFieldComparison(
            field_name="registration_marketplace_id",
            xml_value_pseudonym=xml_mkt,
            modern_value_pseudonym=mod_mkt,
            status=mkt_status,
            notes=mkt_note,
        )
    )

    # Field: feedback_score (present in XML GetUser, not in REST Identity)
    xml_fb = xml_ident.feedback_score if xml_ident else None
    field_comparisons.append(
        IdentityFieldComparison(
            field_name="feedback_score",
            xml_value_pseudonym="[PRESENT_IN_XML]" if xml_fb is not None else None,
            modern_value_pseudonym="[NOT_IN_IDENTITY_API]",
            status=PARITY_MISSING_FIELD,
            notes=(
                "Feedback score is returned by Trading XML GetUser but is omitted from REST "
                "Commerce Identity. Handled independently by REST Feedback API."
            ),
        )
    )

    # Field: registration_date
    xml_reg = xml_ident.registration_date if xml_ident else None
    field_comparisons.append(
        IdentityFieldComparison(
            field_name="registration_date",
            xml_value_pseudonym="[PRESENT_IN_XML]" if xml_reg is not None else None,
            modern_value_pseudonym="[NOT_IN_IDENTITY_API]",
            status=PARITY_MISSING_FIELD,
            notes=(
                "Registration date is returned by Trading XML GetUser but is omitted from REST "
                "Commerce Identity."
            ),
        )
    )

    username_matches = bool(
        xml_username and mod_username and xml_username.lower() == mod_username.lower()
    )

    return {
        "xml_source": xml_ident.source if xml_ident else "unknown",
        "modern_source": mod_ident.source if mod_ident else "unknown",
        "username_matches": username_matches,
        "account_continuity_verified": bool(
            xml_snapshot.account_verified
            and modern_snapshot.account_verified
            and username_matches
        ),
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
        },
    }


# ---------------------------------------------------------------------------
# Report Sanitization & Privacy Export
# ---------------------------------------------------------------------------
def sanitize_identity_parity_report(report: dict[str, Any]) -> dict[str, Any]:
    """Ensure report contains only allowlisted keys, symbolic codes, and pseudonyms."""
    sanitized: dict[str, Any] = {}

    for k, v in report.items():
        if k in FORBIDDEN_REPORT_KEYS:
            continue
        if k == "fields" and isinstance(v, dict):
            clean_fields: dict[str, Any] = {}
            for field_name, field_data in v.items():
                if field_name not in MONITORED_IDENTITY_FIELDS:
                    continue
                clean_fields[field_name] = {
                    "status": field_data.get("status", "unknown"),
                    "label": field_data.get("label", "Unknown"),
                    "notes": sanitize_diagnostic_string(
                        str(field_data.get("notes", ""))
                    ),
                }
            sanitized["fields"] = clean_fields
        elif k in ("blocking_reasons", "warnings") and isinstance(v, (list, tuple)):
            sanitized[k] = [sanitize_diagnostic_string(str(item)) for item in v]
        elif k == "diagnostics" and isinstance(v, dict):
            sanitized["diagnostics"] = {
                diag_k: (
                    tuple(sanitize_diagnostic_string(str(e)) for e in diag_v)
                    if isinstance(diag_v, (list, tuple))
                    else diag_v
                )
                for diag_k, diag_v in v.items()
                if diag_k not in FORBIDDEN_REPORT_KEYS
            }
        elif isinstance(v, (bool, int, float)) or v is None:
            sanitized[k] = v
        elif isinstance(v, str):
            sanitized[k] = sanitize_diagnostic_string(v)

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
            lines.append(f"- 🛑 `{b}`")
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
