"""Unit tests for Milestone 8: Remaining Legacy Trading XML Operations & Identity Parity."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.validate_legacy_fields_parity as cli_harness
from custom_components.ebay.parsers.identity_parity import (
    COMMERCE_IDENTITY_READONLY_SCOPE,
    FORBIDDEN_REPORT_KEYS,
    MAX_IDENTITY_PAGES,
    MONITORED_IDENTITY_FIELDS,
    PARITY_DIFFERENT_SEMANTICS,
    PARITY_EQUIVALENT_NORMALIZED,
    PARITY_EXACT_MATCH,
    PARITY_MISSING_FIELD,
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
    """Verify redacting credentials, tokens, amounts, and emails."""
    text = (
        "Error with client_secret=shhh_secret_key! "
        "Bearer v^1.1#token123 and user email test@example.com for $149.99"
    )
    sanitized = sanitize_diagnostic_string(text)
    assert "shhh_secret_key" not in sanitized
    assert "token123" not in sanitized
    assert "test@example.com" not in sanitized
    assert "149.99" not in sanitized
    assert "[REDACTED]" in sanitized
    assert "[REDACTED_TOKEN]" in sanitized
    assert "[REDACTED_EMAIL]" in sanitized
    assert "[REDACTED_AMOUNT]" in sanitized

    # Non-string input
    assert sanitize_diagnostic_string(123) == "123"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test Legacy Trading XML Normalization
# ---------------------------------------------------------------------------
def test_normalize_trading_xml_get_user_success() -> None:
    """Normalize a valid Trading XML GetUser response."""
    xml_str = """<?xml version="1.0" encoding="utf-8"?>
<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
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


def test_normalize_trading_xml_get_user_private_individual() -> None:
    """Normalize a private seller with Individual account type."""
    xml_str = """<?xml version="1.0" encoding="utf-8"?>
<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <User>
    <UserID>beta_buyer</UserID>
    <SellerInfo>
      <SellerBusinessType>Private</SellerBusinessType>
    </SellerInfo>
  </User>
</GetUserResponse>"""
    result = normalize_trading_xml_get_user(xml_str)
    assert result.is_complete is True
    assert result.identity is not None
    assert result.identity.account_type == AccountType.INDIVIDUAL
    assert result.identity.evidence_level == "unverified_offline"


def test_normalize_trading_xml_get_user_element_input() -> None:
    """Accept an ET.Element directly."""
    elem = ET.fromstring(
        '<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents"><User><UserID>elem_user</UserID></User></GetUserResponse>'
    )
    result = normalize_trading_xml_get_user(elem)
    assert result.is_complete is True
    assert result.username == "elem_user"


def test_normalize_trading_xml_get_user_failures() -> None:
    """Fail closed on empty, malformed, ack failure, or missing user."""
    # Empty
    res_empty = normalize_trading_xml_get_user("")
    assert res_empty.is_complete is False
    assert "EMPTY_XML_PAYLOAD" in res_empty.errors

    # Malformed
    res_malformed = normalize_trading_xml_get_user("<User>incomplete")
    assert res_malformed.is_complete is False
    assert any("MALFORMED_XML" in e for e in res_malformed.errors)

    # Invalid input type
    res_type = normalize_trading_xml_get_user(12345)  # type: ignore[arg-type]
    assert res_type.is_complete is False
    assert "INVALID_XML_INPUT_TYPE" in res_type.errors

    # Ack Failure
    ack_fail = """<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Failure</Ack>
      <Errors><ShortMessage>User not found</ShortMessage></Errors>
    </GetUserResponse>"""
    res_ack = normalize_trading_xml_get_user(ack_fail)
    assert res_ack.is_complete is False
    assert any("TRADING_API_ERROR" in e for e in res_ack.errors)

    # Missing User element
    no_user = '<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></GetUserResponse>'
    res_no_user = normalize_trading_xml_get_user(no_user)
    assert res_no_user.is_complete is False
    assert "MISSING_USER_ELEMENT" in res_no_user.errors

    # Missing UserID inside User
    no_userid = '<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents"><User><Site>US</Site></User></GetUserResponse>'
    res_no_id = normalize_trading_xml_get_user(no_userid)
    assert res_no_id.is_complete is False
    assert "MISSING_USER_ID" in res_no_id.errors


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
    assert result.identity.feedback_score is None  # Omitted in REST Identity
    assert result.identity.evidence_level == "verified"


def test_normalize_rest_identity_get_user_failures() -> None:
    """Fail closed on empty, malformed, error payload, or missing fields."""
    # Empty string
    res_empty = normalize_rest_identity_get_user("")
    assert res_empty.is_complete is False
    assert "EMPTY_JSON_PAYLOAD" in res_empty.errors

    # Malformed JSON
    res_malformed = normalize_rest_identity_get_user("{not json")
    assert res_malformed.is_complete is False
    assert any("MALFORMED_JSON" in e for e in res_malformed.errors)

    # Invalid input type
    res_type = normalize_rest_identity_get_user(12345)  # type: ignore[arg-type]
    assert res_type.is_complete is False
    assert "INVALID_JSON_INPUT_TYPE" in res_type.errors

    # API error response
    err_json = {"errors": [{"message": "Access Denied"}]}
    res_err = normalize_rest_identity_get_user(err_json)
    assert res_err.is_complete is False
    assert any("REST_API_ERROR" in e for e in res_err.errors)

    # Missing username
    no_username = {"userId": "12345"}
    res_no_uname = normalize_rest_identity_get_user(no_username)
    assert res_no_uname.is_complete is False
    assert "MISSING_OR_INVALID_USERNAME" in res_no_uname.errors

    # Missing userId
    no_userid = {"username": "foo"}
    res_no_uid = normalize_rest_identity_get_user(no_userid)
    assert res_no_uid.is_complete is False
    assert "MISSING_OR_INVALID_USER_ID" in res_no_uid.errors


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
    # API error
    err_data = {"errors": [{"message": "Field 'viewer' not found"}]}
    res_err = normalize_graphql_user(err_data)
    assert res_err.is_complete is False
    assert any("GRAPHQL_API_ERROR" in e for e in res_err.errors)

    # Missing data
    res_no_data = normalize_graphql_user({"data": None})
    assert res_no_data.is_complete is False
    assert "MISSING_DATA_OBJECT" in res_no_data.errors

    # Missing viewer or user
    res_no_viewer = normalize_graphql_user({"data": {"other": {}}})
    assert res_no_viewer.is_complete is False
    assert "MISSING_VIEWER_OR_USER_OBJECT" in res_no_viewer.errors

    # Missing username in viewer
    res_no_uname = normalize_graphql_user({"data": {"viewer": {"id": "123"}}})
    assert res_no_uname.is_complete is False
    assert "MISSING_OR_INVALID_USERNAME" in res_no_uname.errors


# ---------------------------------------------------------------------------
# Test Snapshot Comparison & Field Parity
# ---------------------------------------------------------------------------
def test_compare_identity_snapshots_exact_match() -> None:
    """Compare matching XML and REST identity snapshots."""
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
        account_verified=True,
        entitlement_verified=True,
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
        account_verified=True,
        entitlement_verified=True,
        username="alpha_seller",
    )

    report = compare_identity_snapshots(xml_snap, rest_snap)

    assert report["username_matches"] is True
    assert report["account_continuity_verified"] is True
    # In M8, migration_readiness is strictly blocked (validation-only)
    assert report["migration_readiness"] == "blocked"
    assert "M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER" in report["blocking_reasons"]

    fields = report["fields"]
    assert fields["username"]["status"] == PARITY_EXACT_MATCH
    assert fields["user_id"]["status"] == PARITY_DIFFERENT_SEMANTICS
    assert fields["account_type"]["status"] == PARITY_EXACT_MATCH
    assert fields["registration_marketplace_id"]["status"] == PARITY_EXACT_MATCH
    assert fields["feedback_score"]["status"] == PARITY_MISSING_FIELD
    assert fields["registration_date"]["status"] == PARITY_MISSING_FIELD


def test_compare_identity_snapshots_case_insensitive_username() -> None:
    """Case differences normalize cleanly."""
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


def test_compare_identity_snapshots_username_mismatch() -> None:
    """Mismatched username blocks migration and marks status different semantics."""
    xml_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username="seller_one"),
        is_complete=True,
    )
    rest_snap = IdentityCollectionResult(
        identity=NormalizedAccountIdentity(username="seller_two"),
        is_complete=True,
    )
    report = compare_identity_snapshots(xml_snap, rest_snap)
    assert report["username_matches"] is False
    assert "USERNAME_MISMATCH" in report["blocking_reasons"]
    assert report["fields"]["username"]["status"] == PARITY_DIFFERENT_SEMANTICS


def test_compare_identity_snapshots_incomplete_and_unverified() -> None:
    """Incomplete collections and unverified accounts fail closed."""
    xml_snap = IdentityCollectionResult(is_complete=False)
    rest_snap = IdentityCollectionResult(is_complete=False)

    report = compare_identity_snapshots(xml_snap, rest_snap)
    assert "TRADING_XML_GET_USER_INCOMPLETE" in report["blocking_reasons"]
    assert "MODERN_IDENTITY_INCOMPLETE" in report["blocking_reasons"]
    assert "ACCOUNT_IDENTITY_UNVERIFIED" in report["blocking_reasons"]
    assert "MODERN_IDENTITY_ENTITLEMENT_UNVERIFIED" in report["blocking_reasons"]


# ---------------------------------------------------------------------------
# Test Sanitization & Rendering
# ---------------------------------------------------------------------------
def test_sanitize_identity_parity_report_removes_forbidden_keys() -> None:
    """Forbidden sensitive keys must never leak in the sanitized report."""
    dirty_report = {
        "xml_source": "trading_xml_get_user",
        "modern_source": "rest_identity_get_user",
        "access_token": "secret_token_123",
        "client_secret": "my_secret",
        "email": "user@example.com",
        "username_matches": True,
        "fields": {
            "username": {
                "status": PARITY_EXACT_MATCH,
                "label": "Exact match",
                "notes": "Matches perfectly for user@example.com",
            },
            "unmonitored_evil_field": {
                "status": "bad",
            },
        },
        "blocking_reasons": ["Error token=secret_456"],
        "diagnostics": {
            "xml_complete": True,
            "access_token": "leak",
        },
    }

    clean = sanitize_identity_parity_report(dirty_report)
    for forbidden in FORBIDDEN_REPORT_KEYS:
        assert forbidden not in clean
    assert "access_token" not in clean["diagnostics"]
    assert "unmonitored_evil_field" not in clean["fields"]
    assert "[REDACTED_EMAIL]" in clean["fields"]["username"]["notes"]
    assert "[REDACTED]" in clean["blocking_reasons"][0]


def test_render_identity_parity_markdown_and_json() -> None:
    """Render markdown and JSON exports deterministically."""
    report = {
        "xml_source": "trading_xml_get_user",
        "modern_source": "rest_identity_get_user",
        "username_matches": True,
        "account_continuity_verified": True,
        "migration_readiness": "blocked",
        "blocking_reasons": ["M8_VALIDATION_ONLY_NO_PRODUCTION_CUTOVER"],
        "warnings": ["ACCOUNT_TYPE_DIFFERENCE"],
        "fields": {
            f: {"status": PARITY_EXACT_MATCH, "label": "Exact match", "notes": ""}
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
    assert "ACCOUNT_TYPE_DIFFERENCE" in md
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


def test_cli_harness_live_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI --live exits 2 with error message."""
    code = cli_harness.main(["--live"])
    assert code == 2
    captured = capsys.readouterr()
    assert "not implemented" in captured.err


def test_cli_harness_custom_fixtures(tmp_path: Path) -> None:
    """CLI with custom valid fixtures."""
    xml_path = tmp_path / "user.xml"
    rest_path = tmp_path / "user.json"

    xml_path.write_text(
        '<GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents"><User><UserID>alpha</UserID></User></GetUserResponse>',
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
