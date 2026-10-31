"""Unit tests for Milestone 8: Remaining Legacy Trading XML Operations & Identity Parity."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.validate_legacy_fields_parity as cli_harness
from custom_components.ebay.const import EBAY_XML_NS
from custom_components.ebay.parsers.identity_parity import (
    COMMERCE_IDENTITY_READONLY_SCOPE,
    EBAY_SITE_TO_MARKETPLACE,
    MAX_IDENTITY_PAGES,
    MONITORED_IDENTITY_FIELDS,
    PARITY_DIFFERENT_SEMANTICS,
    PARITY_EQUIVALENT_NORMALIZED,
    PARITY_EXACT_MATCH,
    PARITY_MISSING_FIELD,
    PARITY_UNVERIFIED,
    AccountType,
    IdentityCollectionResult,
    NormalizedAccountIdentity,
    ProviderVerificationEvidence,
    _is_safe_user_pseudonym,
    compare_identity_snapshots,
    normalize_graphql_user,
    normalize_rest_identity_get_user,
    normalize_trading_xml_get_user,
    pseudonymize_identifier,
    render_identity_parity_json,
    render_identity_parity_markdown,
    sanitize_diagnostic_string,
    sanitize_identity_parity_report,
    validate_all_identity_contract_probes,
    validate_max_pages,
)


# ---------------------------------------------------------------------------
# Test Probes & Helpers
# ---------------------------------------------------------------------------
def test_validate_all_identity_contract_probes() -> None:
    """Identity contract probes must all be valid read-only GraphQL."""
    validate_all_identity_contract_probes()


def test_validate_max_pages_bounds() -> None:
    """Validate max pages boundaries."""
    assert validate_max_pages(1) == 1
    assert validate_max_pages(MAX_IDENTITY_PAGES) == MAX_IDENTITY_PAGES
    assert validate_max_pages(10) == 10

    with pytest.raises(ValueError, match="must be an integer"):
        validate_max_pages("10")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must be an integer"):
        validate_max_pages(True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="must be between 1 and 25"):
        validate_max_pages(0)

    with pytest.raises(ValueError, match="must be between 1 and 25"):
        validate_max_pages(26)


def test_pseudonymize_identifier_and_safety() -> None:
    """Pseudonymization must be deterministic, non-empty, and format-verified."""
    assert pseudonymize_identifier("") == ""
    assert pseudonymize_identifier(None) == ""

    p1 = pseudonymize_identifier("test_seller_1", prefix="user")
    p2 = pseudonymize_identifier("test_seller_1", prefix="user")
    assert p1 == p2
    assert p1.startswith("user_")
    assert _is_safe_user_pseudonym(p1) is True

    # Raw string or numeric shouldn't pass
    assert _is_safe_user_pseudonym("test_seller_1") is False
    assert _is_safe_user_pseudonym("user_123456789012") is False


def test_sanitize_diagnostic_string() -> None:
    """Verify redacting credentials, tokens, amounts, word amounts, and emails."""
    text = (
        "Error with client_secret=shhh_secret_key! "
        "Bearer v^1.1#token123 and user email test@example.com for $149.99 "
        "or eighty dollars or fifty thousand euros"
    )
    sanitized = sanitize_diagnostic_string(text)
    assert "shhh_secret_key" not in sanitized
    assert "token123" not in sanitized
    assert "test@example.com" not in sanitized
    assert "149.99" not in sanitized
    assert "eighty dollars" not in sanitized
    assert "fifty thousand euros" not in sanitized
    assert "[REDACTED]" in sanitized
    assert "[REDACTED_TOKEN]" in sanitized
    assert "[REDACTED_EMAIL]" in sanitized
    assert "[REDACTED_AMOUNT]" in sanitized

    # Non-string input
    assert sanitize_diagnostic_string(123) == "123"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test Fail-Closed Legacy Trading XML Normalization
# ---------------------------------------------------------------------------
def test_normalize_trading_xml_get_user_success() -> None:
    """Normalize a valid Trading XML GetUser response."""
    xml_str = f"""<?xml version="1.0" encoding="utf-8"?>
<GetUserResponse xmlns="{EBAY_XML_NS}">
  <Ack>Success</Ack>
  <User>
    <UserID>alpha_seller</UserID>
    <FeedbackScore>1200</FeedbackScore>
    <PositiveFeedbackPercent>99.8</PositiveFeedbackPercent>
    <RegistrationDate>2018-06-15T08:30:00.000Z</RegistrationDate>
    <Site>US</Site>
    <Status>Confirmed</Status>
    <SellerInfo>
      <SellerBusinessType>Commercial</SellerBusinessType>
    </SellerInfo>
  </User>
</GetUserResponse>"""
    ev = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="alpha_seller",
        api_entitlement_verified=True,
        token_binding_verified=True,
        provenance_source="live_oauth_token",
    )
    result = normalize_trading_xml_get_user(xml_str, evidence=ev)

    assert result.is_complete is True
    assert result.account_verified is True
    assert result.entitlement_verified is True
    assert result.username == "alpha_seller"
    assert result.identity is not None
    assert result.identity.username == "alpha_seller"
    assert result.identity.account_type == AccountType.BUSINESS
    assert result.identity.registration_marketplace_id == "EBAY_US"
    assert result.identity.feedback_score == 1200
    assert result.identity.positive_feedback_percent == 99.8
    assert result.identity.registration_date == datetime(
        2018, 6, 15, 8, 30, tzinfo=timezone.utc
    )
    assert result.identity.evidence_level == "verified"


def test_normalize_trading_xml_site_mapping() -> None:
    """Verify documented site-to-marketplace conversions."""
    for site, expected_mkt in EBAY_SITE_TO_MARKETPLACE.items():
        xml_str = f"""<GetUserResponse xmlns="{EBAY_XML_NS}">
          <Ack>Success</Ack>
          <User><UserID>user1</UserID><Site>{site}</Site></User>
        </GetUserResponse>"""
        res = normalize_trading_xml_get_user(xml_str)
        assert res.is_complete is True
        assert res.identity is not None
        assert res.identity.registration_marketplace_id == expected_mkt

    # Undocumented site name should not blindly prefix
    undoc_xml = f"""<GetUserResponse xmlns="{EBAY_XML_NS}">
      <Ack>Success</Ack>
      <User><UserID>user1</UserID><Site>UnknownSite123</Site></User>
    </GetUserResponse>"""
    res_undoc = normalize_trading_xml_get_user(undoc_xml)
    assert res_undoc.identity is not None
    assert res_undoc.identity.registration_marketplace_id is None


def test_normalize_trading_xml_root_and_namespace_enforcement() -> None:
    """Fail closed when XML root or namespace is unexpected."""
    # Arbitrary root containing a User tag
    arbitrary_root = """<ArbitraryResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <User><UserID>attacker</UserID></User>
    </ArbitraryResponse>"""
    res1 = normalize_trading_xml_get_user(arbitrary_root)
    assert res1.is_complete is False
    assert "INVALID_XML_ROOT" in res1.errors

    # Foreign namespace
    evil_ns = """<GetUserResponse xmlns="http://evil.example.com/api">
      <Ack>Success</Ack>
      <User><UserID>attacker</UserID></User>
    </GetUserResponse>"""
    res2 = normalize_trading_xml_get_user(evil_ns)
    assert res2.is_complete is False
    assert "XML_NAMESPACE_MISMATCH" in res2.errors


def test_normalize_trading_xml_input_type_and_empty_failures() -> None:
    """Trading XML normalizer fails closed on empty, malformed, or invalid input types."""
    assert "EMPTY_XML_PAYLOAD" in normalize_trading_xml_get_user("").errors
    assert "EMPTY_XML_PAYLOAD" in normalize_trading_xml_get_user(b"   ").errors
    assert "MALFORMED_XML" in normalize_trading_xml_get_user("<unclosed").errors
    assert (
        "INVALID_XML_INPUT_TYPE" in normalize_trading_xml_get_user(12345).errors  # type: ignore[arg-type]
    )


def test_normalize_graphql_user_branches() -> None:
    """Test GraphQL viewer vs user object and missing data container branches."""
    # User object instead of viewer
    user_payload = {
        "data": {
            "user": {
                "id": "urn:ebay:user:user_branch",
                "username": "user_branch",
                "accountType": "INDIVIDUAL",
            }
        }
    }
    res_user = normalize_graphql_user(user_payload)
    assert res_user.is_complete is True
    assert res_user.identity is not None
    assert res_user.identity.username == "user_branch"
    assert res_user.identity.account_type == AccountType.INDIVIDUAL

    # Missing data object
    res_no_data = normalize_graphql_user({})
    assert "MISSING_DATA_OBJECT" in res_no_data.errors

    # Missing viewer or user object
    res_no_viewer = normalize_graphql_user({"data": {"other": {}}})
    assert "MISSING_VIEWER_OR_USER_OBJECT" in res_no_viewer.errors


def test_compare_identity_snapshots_missing_username_branches() -> None:
    """Test branches where XML or modern identity lacks username."""
    xml_no_user = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username=None),
        is_complete=False,
    )
    mod_with_user = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username="mod_user"),
        is_complete=True,
    )
    rep1 = compare_identity_snapshots(xml_no_user, mod_with_user)
    assert rep1["fields"]["username"]["status"] == PARITY_MISSING_FIELD

    xml_with_user = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username="xml_user"),
        is_complete=True,
    )
    mod_no_user = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username=None),
        is_complete=False,
    )
    rep2 = compare_identity_snapshots(xml_with_user, mod_no_user)
    assert rep2["fields"]["username"]["status"] == PARITY_MISSING_FIELD
    assert "MISSING_OR_INVALID_USERNAME" in rep2["blocking_reasons"]


def test_sanitize_identity_parity_report_warning_and_unknown_blocking() -> None:
    """Test sanitizing warnings and unknown blocking reason codes."""
    raw = {
        "xml_source": "trading_xml_get_user",
        "modern_source": "rest_identity_get_user",
        "blocking_reasons": ["UNKNOWN_CUSTOM_ERROR", "TOKEN_EXPIRED"],
        "warnings": ["RATE_LIMITED", "UNKNOWN_WARN_CODE"],
    }
    sanitized = sanitize_identity_parity_report(raw)
    assert "BLOCKED_UNKNOWN" in sanitized["blocking_reasons"]
    assert "TOKEN_EXPIRED" in sanitized["blocking_reasons"]
    assert "RATE_LIMITED" in sanitized["warnings"]
    assert "UNKNOWN_WARN_CODE" not in sanitized["warnings"]


def test_normalize_trading_xml_ack_enforcement() -> None:
    """Reject Failure, PartialFailure, missing, or unknown Ack."""
    # Ack=Failure
    fail_xml = """<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Failure</Ack>
      <User><UserID>user1</UserID></User>
    </GetUserResponse>"""
    assert "XML_ACK_FAILURE" in normalize_trading_xml_get_user(fail_xml).errors

    # Ack=PartialFailure
    partial_xml = """<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>PartialFailure</Ack>
      <User><UserID>user1</UserID></User>
    </GetUserResponse>"""
    assert (
        "XML_ACK_PARTIAL_FAILURE" in normalize_trading_xml_get_user(partial_xml).errors
    )

    # Missing Ack
    no_ack_xml = """<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <User><UserID>user1</UserID></User>
    </GetUserResponse>"""
    assert (
        "XML_ACK_MISSING_OR_UNKNOWN"
        in normalize_trading_xml_get_user(no_ack_xml).errors
    )

    # Unknown Ack
    unknown_ack = """<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>CustomStatus</Ack>
      <User><UserID>user1</UserID></User>
    </GetUserResponse>"""
    assert (
        "XML_ACK_MISSING_OR_UNKNOWN"
        in normalize_trading_xml_get_user(unknown_ack).errors
    )


def test_normalize_trading_xml_fatal_errors_container() -> None:
    """Fatal Errors container alongside Ack=Success must fail closed without raw error leaks."""
    fatal_xml = """<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <Errors>
        <SeverityCode>Error</SeverityCode>
        <ShortMessage>Database connection to postgres://internal:secret@host failed</ShortMessage>
      </Errors>
      <User><UserID>user1</UserID></User>
    </GetUserResponse>"""
    res = normalize_trading_xml_get_user(fatal_xml)
    assert res.is_complete is False
    assert "XML_FATAL_ERROR_CONTAINER" in res.errors
    # Upstream exception or short message must NOT be copied into errors
    for err in res.errors:
        assert "postgres" not in err
        assert "secret" not in err


def test_normalize_trading_xml_warning_error_container_accepted() -> None:
    """Warning-level errors container alongside Ack=Success or Warning is accepted."""
    warn_xml = """<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Warning</Ack>
      <Errors>
        <SeverityCode>Warning</SeverityCode>
        <ShortMessage>Minor deprecation warning</ShortMessage>
      </Errors>
      <User><UserID>user1</UserID></User>
    </GetUserResponse>"""
    res = normalize_trading_xml_get_user(warn_xml)
    assert res.is_complete is True
    assert res.username == "user1"


def test_normalize_trading_xml_malformed_optional_fields() -> None:
    """Malformed numbers and dates must be handled safely without crashing."""
    malformed_xml = """<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <User>
        <UserID>user1</UserID>
        <FeedbackScore>not_a_number</FeedbackScore>
        <PositiveFeedbackPercent>invalid_float</PositiveFeedbackPercent>
        <RegistrationDate>invalid-date-format</RegistrationDate>
      </User>
    </GetUserResponse>"""
    res = normalize_trading_xml_get_user(malformed_xml)
    assert res.is_complete is True
    assert res.identity is not None
    assert res.identity.feedback_score is None
    assert res.identity.positive_feedback_percent is None
    assert res.identity.registration_date is None


def test_normalize_trading_xml_get_user_element_input() -> None:
    """Accept an ET.Element directly."""
    elem = ET.fromstring(
        f'<GetUserResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack><User><UserID>elem_user</UserID></User></GetUserResponse>'
    )
    result = normalize_trading_xml_get_user(elem)
    assert result.is_complete is True
    assert result.username == "elem_user"


def test_normalize_trading_xml_missing_or_whitespace_userid() -> None:
    """Reject missing or whitespace-only UserID."""
    no_userid = f'<GetUserResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack><User></User></GetUserResponse>'
    assert "MISSING_USER_ID" in normalize_trading_xml_get_user(no_userid).errors

    ws_userid = f'<GetUserResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack><User><UserID>   </UserID></User></GetUserResponse>'
    assert "MISSING_USER_ID" in normalize_trading_xml_get_user(ws_userid).errors


# ---------------------------------------------------------------------------
# Test Modern REST Commerce Identity Normalization
# ---------------------------------------------------------------------------
def test_normalize_rest_identity_get_user_success() -> None:
    """Normalize a valid REST Commerce Identity getUser response."""
    data = {
        "userId": "urn:ebay:user:12345678",
        "username": "alpha_seller",
        "accountType": "BUSINESS",
        "registrationMarketplaceId": "EBAY_US",
    }
    ev = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="urn:ebay:user:12345678",
        api_entitlement_verified=True,
        token_binding_verified=True,
        provenance_source="live_oauth_token",
    )
    result = normalize_rest_identity_get_user(data, evidence=ev)

    assert result.is_complete is True
    assert result.account_verified is True
    assert result.entitlement_verified is True
    assert result.account_id == "urn:ebay:user:12345678"
    assert result.username == "alpha_seller"
    assert result.identity is not None
    assert result.identity.user_id == "urn:ebay:user:12345678"
    assert result.identity.username == "alpha_seller"
    assert result.identity.account_type == AccountType.BUSINESS
    assert result.identity.registration_marketplace_id == "EBAY_US"
    assert result.identity.feedback_score is None
    assert result.identity.evidence_level == "verified"


def test_normalize_rest_identity_conflicting_account_id() -> None:
    """Conflicting external verified account ID must fail closed."""
    data = {
        "userId": "urn:ebay:user:OBSERVED_ID",
        "username": "alpha_seller",
    }
    ev = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="urn:ebay:user:DIFFERENT_TRUSTED_ID",
        api_entitlement_verified=True,
        token_binding_verified=True,
        provenance_source="live_oauth_token",
    )
    result = normalize_rest_identity_get_user(data, evidence=ev)
    assert result.is_complete is False
    assert result.account_verified is False
    assert "ACCOUNT_ID_CONFLICT" in result.errors


def test_normalize_rest_identity_offline_provenance_never_live_verified() -> None:
    """Offline fixtures never confer live account verification."""
    data = {
        "userId": "urn:ebay:user:12345",
        "username": "alpha_seller",
    }
    ev = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="urn:ebay:user:12345",
        api_entitlement_verified=True,
        token_binding_verified=True,
        provenance_source="unverified_offline",
    )
    result = normalize_rest_identity_get_user(data, evidence=ev)
    assert result.account_verified is False
    assert result.identity is not None
    assert result.identity.evidence_level == "unverified_offline"


def test_normalize_rest_identity_get_user_failures() -> None:
    """Fail closed on empty, malformed, error payload, or missing fields."""
    assert "EMPTY_JSON_PAYLOAD" in normalize_rest_identity_get_user("").errors
    assert "MALFORMED_JSON" in normalize_rest_identity_get_user("{bad json").errors
    assert (
        "INVALID_JSON_INPUT_TYPE" in normalize_rest_identity_get_user(12345).errors  # type: ignore[arg-type]
    )

    # API error response
    err_json = {"errors": [{"message": "Access Denied"}]}
    assert "REST_API_ERROR" in normalize_rest_identity_get_user(err_json).errors

    # Missing username or userId
    assert (
        "MISSING_OR_INVALID_USERNAME"
        in normalize_rest_identity_get_user({"userId": "123"}).errors
    )
    assert (
        "MISSING_OR_INVALID_USER_ID"
        in normalize_rest_identity_get_user({"username": "user1"}).errors
    )


# ---------------------------------------------------------------------------
# Test Candidate GraphQL User Normalization
# ---------------------------------------------------------------------------
def test_normalize_graphql_user_success() -> None:
    """Normalize a candidate GraphQL viewer query response."""
    data = {
        "data": {
            "viewer": {
                "id": "viewer_123",
                "username": "alpha_seller",
                "accountType": "INDIVIDUAL",
            }
        }
    }
    result = normalize_graphql_user(data)
    assert result.is_complete is True
    assert result.username == "alpha_seller"
    assert result.identity is not None
    assert result.identity.account_type == AccountType.INDIVIDUAL
    assert result.identity.source == "graphql_user"


def test_normalize_graphql_user_failures() -> None:
    """Fail closed on missing data, errors, or malformed payloads."""
    err_data = {"errors": [{"message": "Field 'viewer' not found"}]}
    assert "GRAPHQL_API_ERROR" in normalize_graphql_user(err_data).errors
    assert "MISSING_DATA_OBJECT" in normalize_graphql_user({"data": None}).errors
    assert (
        "MISSING_VIEWER_OR_USER_OBJECT"
        in normalize_graphql_user({"data": {"other": {}}}).errors
    )
    assert (
        "MISSING_OR_INVALID_USERNAME"
        in normalize_graphql_user({"data": {"viewer": {"id": "123"}}}).errors
    )


# ---------------------------------------------------------------------------
# Test Field Parity Semantics & Account Continuity
# ---------------------------------------------------------------------------
def test_compare_identity_snapshots_exact_match_offline() -> None:
    """Compare matching XML and REST identity snapshots under offline evidence."""
    xml_ident = NormalizedAccountIdentity(
        username="alpha_seller",
        account_type=AccountType.BUSINESS,
        registration_marketplace_id="EBAY_US",
        feedback_score=500,
        registration_date=datetime(2020, 1, 1, tzinfo=timezone.utc),
        source="trading_xml_get_user",
    )
    xml_snap = IdentityCollectionResult(
        identity=xml_ident,
        is_complete=True,
        account_verified=False,  # Offline default
        entitlement_verified=False,
        username="alpha_seller",
    )

    rest_ident = NormalizedAccountIdentity(
        user_id="urn:ebay:user:12345",
        username="alpha_seller",
        account_type=AccountType.BUSINESS,
        registration_marketplace_id="EBAY_US",
        source="rest_identity_get_user",
    )
    rest_snap = IdentityCollectionResult(
        identity=rest_ident,
        is_complete=True,
        account_verified=False,
        entitlement_verified=False,
        username="alpha_seller",
    )

    report = compare_identity_snapshots(xml_snap, rest_snap)

    assert report["username_matches"] is True
    # Offline defaults NEVER confer live account continuity
    assert report["account_continuity_verified"] is False
    assert report["migration_readiness"] == "blocked"
    assert "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" in report["blocking_reasons"]
    assert "ACCOUNT_IDENTITY_UNVERIFIED" in report["blocking_reasons"]
    assert "MODERN_IDENTITY_ENTITLEMENT_UNVERIFIED" in report["blocking_reasons"]

    fields = report["fields"]
    assert fields["username"]["status"] == PARITY_EXACT_MATCH
    assert fields["user_id"]["status"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["account_type"]["status"] == PARITY_EXACT_MATCH
    assert fields["registration_marketplace_id"]["status"] == PARITY_EXACT_MATCH
    assert fields["feedback_score"]["status"] == PARITY_MISSING_FIELD
    assert fields["registration_date"]["status"] == PARITY_MISSING_FIELD


def test_compare_identity_snapshots_case_insensitive_username() -> None:
    """Case differences normalize cleanly with PARITY_EQUIVALENT_NORMALIZED."""
    xml_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username="AlphaSeller"),
        is_complete=True,
    )
    rest_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username="alphaseller"),
        is_complete=True,
    )
    report = compare_identity_snapshots(xml_snap, rest_snap)
    assert report["username_matches"] is True
    assert report["fields"]["username"]["status"] == PARITY_EQUIVALENT_NORMALIZED


def test_compare_identity_snapshots_account_type_conflict() -> None:
    """Verified BUSINESS vs INDIVIDUAL is DIFFERENT_SEMANTICS and blocks continuity."""
    xml_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            username="seller", account_type=AccountType.BUSINESS
        ),
        is_complete=True,
        account_verified=True,
    )
    rest_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            username="seller", account_type=AccountType.INDIVIDUAL
        ),
        is_complete=True,
        account_verified=True,
    )
    report = compare_identity_snapshots(xml_snap, rest_snap)
    assert report["account_continuity_verified"] is False
    assert "ACCOUNT_TYPE_MISMATCH" in report["blocking_reasons"]
    assert report["fields"]["account_type"]["status"] == PARITY_DIFFERENT_SEMANTICS


def test_compare_identity_snapshots_account_type_unknown_is_unverified() -> None:
    """Unknown account type is PARITY_UNVERIFIED, not equivalent."""
    xml_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            username="seller", account_type=AccountType.UNKNOWN
        ),
        is_complete=True,
    )
    rest_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            username="seller", account_type=AccountType.BUSINESS
        ),
        is_complete=True,
    )
    report = compare_identity_snapshots(xml_snap, rest_snap)
    assert report["fields"]["account_type"]["status"] == PARITY_UNVERIFIED


def test_compare_identity_snapshots_marketplace_conflict() -> None:
    """EBAY_US vs EBAY_DE is DIFFERENT_SEMANTICS and blocks continuity."""
    xml_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            username="seller", registration_marketplace_id="EBAY_US"
        ),
        is_complete=True,
        account_verified=True,
    )
    rest_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            username="seller", registration_marketplace_id="EBAY_DE"
        ),
        is_complete=True,
        account_verified=True,
    )
    report = compare_identity_snapshots(xml_snap, rest_snap)
    assert report["account_continuity_verified"] is False
    assert "MARKETPLACE_MISMATCH" in report["blocking_reasons"]
    assert (
        report["fields"]["registration_marketplace_id"]["status"]
        == PARITY_DIFFERENT_SEMANTICS
    )


def test_compare_identity_snapshots_account_id_conflict_blocks_continuity() -> None:
    """Account ID conflict reported in modern snapshot blocks continuity."""
    xml_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username="seller"),
        is_complete=True,
        account_verified=True,
    )
    rest_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username="seller"),
        is_complete=False,
        account_verified=False,
        errors=("ACCOUNT_ID_CONFLICT",),
    )
    report = compare_identity_snapshots(xml_snap, rest_snap)
    assert report["account_continuity_verified"] is False
    assert "ACCOUNT_ID_CONFLICT" in report["blocking_reasons"]


# ---------------------------------------------------------------------------
# Test Default-Deny Report Sanitization & Privacy Export
# ---------------------------------------------------------------------------
def test_sanitize_identity_parity_report_strict_default_deny() -> None:
    """Default-deny schema must strip unknown keys, dynamic notes, word amounts, and injections."""
    dirty_report = {
        "xml_source": "trading_xml_get_user",
        "modern_source": "rest_identity_get_user",
        "username_matches": True,
        "unexpected_top_level_key": "injected_secret",
        "nested_bad_obj": {"secret_token": "bearer 1234"},
        "blocking_reasons": [
            "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER",
            "Custom dynamic error: user user_123 failed authentication",
            "<script>alert('xss')</script>",
        ],
        "fields": {
            "username": {
                "status": PARITY_EXACT_MATCH,
                "label": "Exact match",
                "notes": "Custom text: buyer promised eighty dollars at test@example.com",
            },
            "malicious_unmonitored_field": {
                "status": "exact_match",
                "notes": "evil",
            },
        },
        "diagnostics": {
            "xml_complete": True,
            "arbitrary_diagnostic_leak": "user_handle_alpha",
            "xml_errors": ["INVALID_XML_ROOT", "raw_exception_leak"],
        },
    }

    clean = sanitize_identity_parity_report(dirty_report)

    # 1. Top-level allowlist
    assert "unexpected_top_level_key" not in clean
    assert "nested_bad_obj" not in clean

    # 2. Blocking reasons allowed only from symbolic error codes
    assert "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" in clean["blocking_reasons"]
    assert (
        "Custom dynamic error: user user_123 failed authentication"
        not in clean["blocking_reasons"]
    )
    assert "<script>alert('xss')</script>" not in clean["blocking_reasons"]
    # Unrecognized codes become BLOCKED_UNKNOWN
    assert clean["blocking_reasons"].count("BLOCKED_UNKNOWN") == 2

    # 3. Monitored fields only with fixed templates
    assert "malicious_unmonitored_field" not in clean["fields"]
    assert "username" in clean["fields"]
    # Custom note with word amount and email was replaced with fixed fallback
    assert "eighty dollars" not in clean["fields"]["username"]["notes"]
    assert "test@example.com" not in clean["fields"]["username"]["notes"]
    assert clean["fields"]["username"]["notes"] == "Status verified."

    # 4. Diagnostics allowlist
    assert "arbitrary_diagnostic_leak" not in clean["diagnostics"]
    assert clean["diagnostics"]["xml_complete"] is True
    assert "INVALID_XML_ROOT" in clean["diagnostics"]["xml_errors"]
    assert "raw_exception_leak" not in clean["diagnostics"]["xml_errors"]


def test_render_identity_parity_markdown_and_json() -> None:
    """Render markdown and JSON exports deterministically without secret leaks."""
    report = {
        "xml_source": "trading_xml_get_user",
        "modern_source": "rest_identity_get_user",
        "username_matches": True,
        "account_continuity_verified": False,
        "migration_readiness": "blocked",
        "blocking_reasons": ["M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER"],
        "warnings": [],
        "fields": {
            f: {
                "status": PARITY_EXACT_MATCH,
                "label": "Exact match",
                "notes": "Status verified.",
            }
            for f in MONITORED_IDENTITY_FIELDS
        },
        "diagnostics": {
            "xml_complete": True,
            "modern_complete": True,
            "required_scope": COMMERCE_IDENTITY_READONLY_SCOPE,
        },
    }

    md = render_identity_parity_markdown(report)
    assert "# eBay Account Identity Parity Audit (Milestone 8)" in md
    assert "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" in md
    for f in MONITORED_IDENTITY_FIELDS:
        assert f"`{f}`" in md

    json_str = render_identity_parity_json(report)
    parsed = json.loads(json_str)
    assert parsed["username_matches"] is True
    assert parsed["migration_readiness"] == "blocked"


# ---------------------------------------------------------------------------
# Test CLI Harness scripts/validate_legacy_fields_parity.py
# ---------------------------------------------------------------------------
def test_cli_harness_synthetic_demo_and_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI default runs synthetic demo and exits 0."""
    code = cli_harness.main([])
    assert code == 0
    captured = capsys.readouterr()
    assert "# eBay Account Identity Parity Audit (Milestone 8)" in captured.out


def test_cli_harness_audit_matrix(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --audit-matrix outputs markdown table and exits 0."""
    code = cli_harness.main(["--audit-matrix"])
    assert code == 0
    captured = capsys.readouterr()
    assert "# Master eBay Legacy Operations & Dependencies Audit Matrix" in captured.out
    assert "GetUser" in captured.out
    assert "GetMyeBayBuying" in captured.out
    assert "GetMyeBaySelling" in captured.out
    assert "GetSellerList" in captured.out
    assert "GetBestOffers" in captured.out
    assert "GetFeedback" in captured.out
    assert "GetMemberMessages" in captured.out


def test_cli_harness_audit_matrix_json(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --audit-matrix --format json outputs JSON array."""
    code = cli_harness.main(["--audit-matrix", "--format", "json"])
    assert code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)
    ops = [item["operation"] for item in data]
    assert "GetUser" in ops
    assert "GetFeedback" in ops


def test_cli_harness_output_to_file(tmp_path: Path) -> None:
    """CLI --output writes report to file."""
    out_file = tmp_path / "report.md"
    code = cli_harness.main(["--output", str(out_file)])
    assert code == 0
    assert out_file.is_file()
    assert "# eBay Account Identity Parity Audit" in out_file.read_text(
        encoding="utf-8"
    )


def test_cli_harness_live_not_implemented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI --live exits 2 with error message."""
    code = cli_harness.main(["--live"])
    assert code == 2
    captured = capsys.readouterr()
    assert "not implemented" in captured.err


def test_cli_harness_custom_fixtures_success(tmp_path: Path) -> None:
    """CLI with custom valid fixtures matching username exits 0."""
    xml_path = tmp_path / "user.xml"
    rest_path = tmp_path / "user.json"

    xml_path.write_text(
        f'<GetUserResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack><User><UserID>alpha</UserID></User></GetUserResponse>',
        encoding="utf-8",
    )
    rest_path.write_text(
        json.dumps({"userId": "123", "username": "alpha"}),
        encoding="utf-8",
    )

    code = cli_harness.main(
        ["--xml-fixture", str(xml_path), "--rest-fixture", str(rest_path)]
    )
    assert code == 0


def test_cli_harness_invalid_ack_fails_even_with_matching_username(
    tmp_path: Path,
) -> None:
    """CLI must fail (exit 1) if XML Ack is Failure even if username matches."""
    xml_path = tmp_path / "user.xml"
    rest_path = tmp_path / "user.json"

    xml_path.write_text(
        f'<GetUserResponse xmlns="{EBAY_XML_NS}"><Ack>Failure</Ack><User><UserID>alpha</UserID></User></GetUserResponse>',
        encoding="utf-8",
    )
    rest_path.write_text(
        json.dumps({"userId": "123", "username": "alpha"}),
        encoding="utf-8",
    )

    code = cli_harness.main(
        ["--xml-fixture", str(xml_path), "--rest-fixture", str(rest_path)]
    )
    assert code == 1


def test_cli_harness_marketplace_mismatch_fails(tmp_path: Path) -> None:
    """CLI must fail (exit 1) if marketplace conflicts even if username matches."""
    xml_path = tmp_path / "user.xml"
    rest_path = tmp_path / "user.json"

    xml_path.write_text(
        f'<GetUserResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack><User><UserID>alpha</UserID><Site>US</Site></User></GetUserResponse>',
        encoding="utf-8",
    )
    rest_path.write_text(
        json.dumps(
            {
                "userId": "123",
                "username": "alpha",
                "registrationMarketplaceId": "EBAY_DE",
            }
        ),
        encoding="utf-8",
    )

    code = cli_harness.main(
        ["--xml-fixture", str(xml_path), "--rest-fixture", str(rest_path)]
    )
    assert code == 1


def test_cli_harness_account_type_mismatch_fails(tmp_path: Path) -> None:
    """CLI must fail (exit 1) if account type conflicts."""
    xml_path = tmp_path / "user.xml"
    rest_path = tmp_path / "user.json"

    xml_path.write_text(
        f'<GetUserResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack><User><UserID>alpha</UserID><SellerInfo><SellerBusinessType>Commercial</SellerBusinessType></SellerInfo></User></GetUserResponse>',
        encoding="utf-8",
    )
    rest_path.write_text(
        json.dumps(
            {
                "userId": "123",
                "username": "alpha",
                "accountType": "INDIVIDUAL",
            }
        ),
        encoding="utf-8",
    )

    code = cli_harness.main(
        ["--xml-fixture", str(xml_path), "--rest-fixture", str(rest_path)]
    )
    assert code == 1


def test_cli_harness_failed_rest_response_fails(tmp_path: Path) -> None:
    """CLI must fail (exit 1) if REST response contains errors."""
    xml_path = tmp_path / "user.xml"
    rest_path = tmp_path / "user.json"

    xml_path.write_text(
        f'<GetUserResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack><User><UserID>alpha</UserID></User></GetUserResponse>',
        encoding="utf-8",
    )
    rest_path.write_text(
        json.dumps({"errors": [{"message": "Forbidden"}]}),
        encoding="utf-8",
    )

    code = cli_harness.main(
        ["--xml-fixture", str(xml_path), "--rest-fixture", str(rest_path)]
    )
    assert code == 1


def test_cli_harness_custom_fixtures_partial_rejected(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """CLI rejects partial fixtures."""
    xml_path = tmp_path / "user.xml"
    xml_path.write_text("<xml/>", encoding="utf-8")

    code = cli_harness.main(["--xml-fixture", str(xml_path)])
    assert code == 1
    captured = capsys.readouterr()
    assert "at least one modern fixture" in captured.err


def test_cli_harness_missing_fixture_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """CLI reports missing fixture file."""
    non_existent = tmp_path / "non_existent.xml"
    rest_path = tmp_path / "user.json"
    rest_path.write_text("{}", encoding="utf-8")

    code = cli_harness.main(
        ["--xml-fixture", str(non_existent), "--rest-fixture", str(rest_path)]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "XML fixture file not found" in captured.err


def test_parse_dotenv_file(tmp_path: Path) -> None:
    """Test dotenv parsing utility."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Comment\nexport KEY1=value1\nKEY2='value2'\nKEY3=\"value3\"\nINVALID\n",
        encoding="utf-8",
    )
    parsed = cli_harness._parse_dotenv_file(env_file)
    assert parsed["KEY1"] == "value1"
    assert parsed["KEY2"] == "value2"
    assert parsed["KEY3"] == "value3"
    assert "INVALID" not in parsed


# ---------------------------------------------------------------------------
# Adversarial Security & Trust-Boundary Tests (Review 5722226679)
# ---------------------------------------------------------------------------
def test_normalize_trading_xml_suffix_smuggling_and_unqualified() -> None:
    """Trading XML root must reject suffix-smuggling, unqualified roots, and foreign namespaces."""
    # 1. Suffix smuggling with eBay namespace
    smuggle_qualified = f"""<EvilGetUserResponse xmlns="{EBAY_XML_NS}">
      <Ack>Success</Ack>
      <User><UserID>impostor</UserID></User>
    </EvilGetUserResponse>"""
    res1 = normalize_trading_xml_get_user(smuggle_qualified)
    assert res1.is_complete is False
    assert "INVALID_XML_ROOT" in res1.errors

    # 2. Suffix smuggling without namespace
    smuggle_unqualified = """<EvilGetUserResponse>
      <Ack>Success</Ack>
      <User><UserID>impostor</UserID></User>
    </EvilGetUserResponse>"""
    res2 = normalize_trading_xml_get_user(smuggle_unqualified)
    assert res2.is_complete is False
    assert "INVALID_XML_ROOT" in res2.errors

    # 3. Bare unqualified GetUserResponse
    bare_unqualified = """<GetUserResponse>
      <Ack>Success</Ack>
      <User><UserID>impostor</UserID></User>
    </GetUserResponse>"""
    res3 = normalize_trading_xml_get_user(bare_unqualified)
    assert res3.is_complete is False
    assert "XML_NAMESPACE_MISMATCH" in res3.errors

    # 4. Foreign namespace with GetUserResponse
    foreign_ns = """<foreign:GetUserResponse xmlns:foreign="http://attacker.example.com/ns">
      <foreign:Ack>Success</foreign:Ack>
      <foreign:User><foreign:UserID>impostor</foreign:UserID></foreign:User>
    </foreign:GetUserResponse>"""
    res4 = normalize_trading_xml_get_user(foreign_ns)
    assert res4.is_complete is False
    assert "XML_NAMESPACE_MISMATCH" in res4.errors


def test_normalize_trading_xml_nested_and_multiple_impostor_users() -> None:
    """Trading XML must reject multiple or nested User elements."""
    # Multiple direct User elements
    multi_users = f"""<GetUserResponse xmlns="{EBAY_XML_NS}">
      <Ack>Success</Ack>
      <User><UserID>user1</UserID></User>
      <User><UserID>user2</UserID></User>
    </GetUserResponse>"""
    res1 = normalize_trading_xml_get_user(multi_users)
    assert res1.is_complete is False
    assert "INVALID_XML_ROOT" in res1.errors

    # Nested impostor User element inside container
    nested_user = f"""<GetUserResponse xmlns="{EBAY_XML_NS}">
      <Ack>Success</Ack>
      <User><UserID>real_user</UserID></User>
      <AttackerContainer>
        <User><UserID>impostor_user</UserID></User>
      </AttackerContainer>
    </GetUserResponse>"""
    res2 = normalize_trading_xml_get_user(nested_user)
    assert res2.is_complete is False
    assert "INVALID_XML_ROOT" in res2.errors

    # Missing direct User element with only nested impostor
    only_nested = f"""<GetUserResponse xmlns="{EBAY_XML_NS}">
      <Ack>Success</Ack>
      <AttackerContainer>
        <User><UserID>impostor_user</UserID></User>
      </AttackerContainer>
    </GetUserResponse>"""
    res3 = normalize_trading_xml_get_user(only_nested)
    assert res3.is_complete is False
    assert "MISSING_USER_ELEMENT" in res3.errors


def test_normalize_rest_identity_malformed_errors_containers() -> None:
    """REST Identity normalizer must fail closed on dict, string, and unexpected errors values."""
    # errors as a mapping
    res_dict = normalize_rest_identity_get_user(
        {
            "userId": "123",
            "username": "alpha",
            "errors": {"code": 500, "message": "Backend failure"},
        }
    )
    assert res_dict.is_complete is False
    assert "REST_API_ERROR" in res_dict.errors

    # errors as a string
    res_str = normalize_rest_identity_get_user(
        {"userId": "123", "username": "alpha", "errors": "An unexpected error occurred"}
    )
    assert res_str.is_complete is False
    assert "REST_API_ERROR" in res_str.errors

    # errors as an int/unexpected type
    res_int = normalize_rest_identity_get_user(
        {"userId": "123", "username": "alpha", "errors": 403}
    )
    assert res_int.is_complete is False
    assert "REST_API_ERROR" in res_int.errors

    # error (singular) as a string (OAuth format)
    res_oauth_str = normalize_rest_identity_get_user(
        {"userId": "123", "username": "alpha", "error": "invalid_grant"}
    )
    assert res_oauth_str.is_complete is False
    assert "REST_API_ERROR" in res_oauth_str.errors

    # error (singular) as a mapping
    res_oauth_dict = normalize_rest_identity_get_user(
        {
            "userId": "123",
            "username": "alpha",
            "error": {"error_description": "Token expired"},
        }
    )
    assert res_oauth_dict.is_complete is False
    assert "REST_API_ERROR" in res_oauth_dict.errors


def test_normalize_graphql_malformed_errors_containers() -> None:
    """GraphQL normalizer must fail closed on dict, string, and unexpected errors values."""
    # errors as a mapping
    res_dict = normalize_graphql_user(
        {
            "data": {"viewer": {"id": "1", "username": "u"}},
            "errors": {"message": "Access denied"},
        }
    )
    assert res_dict.is_complete is False
    assert "GRAPHQL_API_ERROR" in res_dict.errors

    # errors as a string
    res_str = normalize_graphql_user(
        {
            "data": {"viewer": {"id": "1", "username": "u"}},
            "errors": "Query execution halted",
        }
    )
    assert res_str.is_complete is False
    assert "GRAPHQL_API_ERROR" in res_str.errors

    # errors as an int
    res_int = normalize_graphql_user(
        {
            "data": {"viewer": {"id": "1", "username": "u"}},
            "errors": 500,
        }
    )
    assert res_int.is_complete is False
    assert "GRAPHQL_API_ERROR" in res_int.errors


def test_compare_identity_snapshots_arbitrary_provenance_and_forged_flags() -> None:
    """Arbitrary provenance strings and forged verification booleans cannot certify continuity."""
    # Provenance source not in TRUSTED_LIVE_PROVENANCE_SOURCES
    ev_pretend = ProviderVerificationEvidence(
        account_verified=True,
        verified_account_id="urn:ebay:user:123",
        token_binding_verified=True,
        provenance_source="pretend_live",
    )
    res_xml = normalize_trading_xml_get_user(
        f'<GetUserResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack><User><UserID>alpha</UserID></User></GetUserResponse>',
        evidence=ev_pretend,
    )
    assert res_xml.account_verified is False
    assert res_xml.identity is not None
    assert res_xml.identity.evidence_level == "unverified_offline"

    # Handcrafted snapshots with account_verified=True
    snap_xml = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            username="same_user",
            account_type=AccountType.BUSINESS,
            source="trading_xml_get_user",
        ),
        is_complete=True,
        username="same_user",
        account_verified=True,
    )
    snap_mod = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            user_id="urn:ebay:user:123",
            username="same_user",
            account_type=AccountType.BUSINESS,
            source="rest_identity_get_user",
        ),
        is_complete=True,
        account_id="urn:ebay:user:123",
        username="same_user",
        account_verified=True,
        entitlement_verified=True,
    )

    report = compare_identity_snapshots(snap_xml, snap_mod)
    # Authenticated continuity must remain False in M8 validation
    assert report["account_continuity_verified"] is False
    assert report["migration_readiness"] == "blocked"
    assert "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" in report["blocking_reasons"]
    # Username parity is verified independently
    assert report["username_matches"] is True
    assert report["fields"]["username"]["status"] == PARITY_EXACT_MATCH


def test_compare_identity_snapshots_conflicting_account_ids() -> None:
    """When both snapshots supply conflicting account IDs, fail closed with ACCOUNT_ID_CONFLICT."""
    snap_xml = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            username="same_user",
            source="trading_xml_get_user",
        ),
        is_complete=True,
        account_id="urn:ebay:user:TRUSTED_ACCOUNT_A",
        username="same_user",
    )
    snap_mod = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(
            user_id="urn:ebay:user:TRUSTED_ACCOUNT_B",
            username="same_user",
            source="rest_identity_get_user",
        ),
        is_complete=True,
        account_id="urn:ebay:user:TRUSTED_ACCOUNT_B",
        username="same_user",
    )

    report = compare_identity_snapshots(snap_xml, snap_mod)
    assert "ACCOUNT_ID_CONFLICT" in report["blocking_reasons"]
    assert report["account_continuity_verified"] is False
    assert report["migration_readiness"] == "blocked"


def test_sanitize_and_render_fabricated_ready_reports() -> None:
    """Report sanitization and renderers must clamp fabricated READY reports to BLOCKED."""
    fabricated = {
        "migration_readiness": "ready",
        "account_continuity_verified": True,
        "username_matches": True,
        "blocking_reasons": [],
        "xml_source": "trading_xml_get_user",
        "modern_source": "rest_identity_get_user",
    }

    sanitized = sanitize_identity_parity_report(fabricated)
    assert sanitized["migration_readiness"] == "blocked"
    assert sanitized["account_continuity_verified"] is False
    assert "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" in sanitized["blocking_reasons"]
    assert sanitized["username_matches"] is True

    # Markdown renderer
    md = render_identity_parity_markdown(fabricated)
    assert "- **Account Continuity Verified:** `No`" in md
    assert "- **Migration Readiness:** `BLOCKED`" in md
    assert "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" in md

    # JSON renderer
    json_out = render_identity_parity_json(fabricated)
    parsed = json.loads(json_out)
    assert parsed["migration_readiness"] == "blocked"
    assert parsed["account_continuity_verified"] is False
    assert "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" in parsed["blocking_reasons"]

    # Non-dict fallback
    non_dict = sanitize_identity_parity_report("not a dict")  # type: ignore[arg-type]
    assert non_dict["migration_readiness"] == "blocked"
    assert non_dict["account_continuity_verified"] is False
    assert "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" in non_dict["blocking_reasons"]
