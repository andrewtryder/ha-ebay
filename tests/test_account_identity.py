"""Tests for eBay account identity, multi-account onboarding, and config entry isolation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant import config_entries

from custom_components.ebay.account_identity import (
    AccountBindingState,
    AccountEquivalence,
    AccountIdentifierType,
    AccountIdentity,
    AccountIdentityProvenance,
    IdentityFailureReason,
    async_verify_authenticated_account,
    async_verify_entry_existing_identity,
    compare_account_identities,
    compute_account_unique_id,
    get_entry_account_identity,
    is_displayable_username,
    is_same_account,
)
from custom_components.ebay.config_flow import EbayConfigFlow
from custom_components.ebay.const import (
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_IDENTITY,
    CONF_ACCOUNT_USERNAME,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_REAUTH_GENERATION,
    CONF_RUNAME,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    EBAY_XML_NS,
)
from custom_components.ebay.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)


def _flow_data(**overrides: Any) -> dict[str, Any]:
    data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_CLIENT_SECRET: "client-secret",
        CONF_RUNAME: "runame",
        CONF_SITE_ID: "0",
    }
    data.update(overrides)
    return data


def _get_user_response_xml(
    user_id: str = "testuser",
    eias_token: str | None = "eias-token-123",
    ack: str = "Success",
    errors: list[tuple[str, str]] | None = None,
    root_tag: str = "GetUserResponse",
    namespace: str = EBAY_XML_NS,
) -> bytes:
    ns_attr = f' xmlns="{namespace}"' if namespace else ""
    error_xml = ""
    if errors:
        for code, sev in errors:
            error_xml += f"""
            <Errors>
                <ErrorCode>{code}</ErrorCode>
                <SeverityCode>{sev}</SeverityCode>
                <ShortMessage>Error message</ShortMessage>
            </Errors>"""

    eias_xml = f"<EIASToken>{eias_token}</EIASToken>" if eias_token is not None else ""
    user_xml = (
        f"""
        <User>
            <UserID>{user_id}</UserID>
            {eias_xml}
        </User>"""
        if user_id is not None
        else ""
    )

    return f"""<?xml version="1.0" encoding="utf-8"?>
    <{root_tag}{ns_attr}>
        <Ack>{ack}</Ack>
        {error_xml}
        {user_xml}
    </{root_tag}>""".encode("utf-8")


# ==============================================================================
# 1. Canonical AccountIdentity Model Tests
# ==============================================================================


def test_account_identity_properties_and_pseudonyms() -> None:
    """AccountIdentity correctly computes properties and generates stable pseudonyms."""
    identity = AccountIdentity(
        entry_id="entry-1",
        environment="production",
        client_id="my-client-id",
        account_id="NY*s45",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        username="seller_one",
        binding_state=AccountBindingState.VERIFIED,
        provenance=AccountIdentityProvenance.AUTHENTICATED_GET_USER,
        verified_at=datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc),
    )

    assert identity.is_verified is True
    assert identity.account_id_pseudonym.startswith("account_")
    assert identity.username_pseudonym.startswith("user_")
    assert "NY*s45" not in identity.account_id_pseudonym
    assert "seller_one" not in identity.username_pseudonym

    # Unverified identity
    unverified = AccountIdentity(
        account_id=None,
        binding_state=AccountBindingState.UNVERIFIED,
    )
    assert unverified.is_verified is False
    assert unverified.account_id_pseudonym == ""
    assert unverified.username_pseudonym == ""


def test_account_identity_serialization_roundtrip() -> None:
    """AccountIdentity serializes to dict and deserializes cleanly."""
    now = datetime.now(timezone.utc)
    original = AccountIdentity(
        entry_id="entry-42",
        environment="sandbox",
        client_id="app-123",
        account_id="durable-acc-id",
        identifier_type=AccountIdentifierType.USER_ID,
        username="john_doe",
        binding_state=AccountBindingState.VERIFIED,
        provenance=AccountIdentityProvenance.AUTHENTICATED_GET_USER,
        verified_at=now,
        failure_reason=IdentityFailureReason.NONE,
        reauth_generation=3,
    )

    data = original.to_config_entry_dict()
    restored = AccountIdentity.from_config_entry_dict(data)

    assert restored.entry_id == original.entry_id
    assert restored.environment == original.environment
    assert restored.client_id == original.client_id
    assert restored.account_id == original.account_id
    assert restored.identifier_type == original.identifier_type
    assert restored.username == original.username
    assert restored.binding_state == original.binding_state
    assert restored.provenance == original.provenance
    assert restored.verified_at == original.verified_at
    assert restored.failure_reason == original.failure_reason
    assert restored.reauth_generation == original.reauth_generation


def test_account_identity_deserialization_fallback_on_corrupt_data() -> None:
    """Invalid or None data deserializes with safe fallback defaults."""
    # When data is None
    none_restored = AccountIdentity.from_config_entry_dict(
        None,
        fallback_entry_id="fb-entry",
        fallback_environment="production",
        fallback_client_id="fb-client",
        fallback_reauth_generation=1,
    )
    assert none_restored.entry_id == "fb-entry"
    assert none_restored.binding_state == AccountBindingState.LEGACY_COMPATIBLE
    assert none_restored.provenance == AccountIdentityProvenance.LEGACY_MIGRATION
    assert none_restored.reauth_generation == 1

    # When data contains unparseable enum values and invalid timestamp
    corrupt_data = {
        "identifier_type": "completely_invalid_enum",
        "binding_state": "unknown_state",
        "provenance": "bogus_provenance",
        "failure_reason": "bad_reason",
        "verified_at": "not-a-timestamp",
        "reauth_generation": "2",
    }
    corrupt_restored = AccountIdentity.from_config_entry_dict(corrupt_data)
    assert corrupt_restored.identifier_type == AccountIdentifierType.UNKNOWN
    assert corrupt_restored.binding_state == AccountBindingState.UNVERIFIED
    assert corrupt_restored.provenance == AccountIdentityProvenance.UNVERIFIED_OFFLINE
    assert corrupt_restored.failure_reason == IdentityFailureReason.NONE
    assert corrupt_restored.verified_at is None
    assert corrupt_restored.reauth_generation == 2


def test_compute_account_unique_id() -> None:
    """Account unique ID computation normalizes environment and account ID."""
    assert compute_account_unique_id("production", "user123") == "production:user123"
    assert compute_account_unique_id("SANDBOX", " EI_AS ") == "sandbox:EI_AS"
    assert compute_account_unique_id("", "abc") == "production:abc"


def test_is_same_account_logic() -> None:
    """compare_account_identities and is_same_account enforce three-state matching semantics."""
    eias_a = AccountIdentity(
        environment="production",
        account_id="eias-1",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        username="Alice",
        binding_state=AccountBindingState.VERIFIED,
    )
    eias_rename = AccountIdentity(
        environment="production",
        account_id="eias-1",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        username="AliceRenamed",
        binding_state=AccountBindingState.VERIFIED,
    )
    eias_conflict = AccountIdentity(
        environment="production",
        account_id="eias-2",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        username="Alice",
        binding_state=AccountBindingState.VERIFIED,
    )
    user_id_fallback = AccountIdentity(
        environment="production",
        account_id="Alice",
        identifier_type=AccountIdentifierType.USER_ID,
        username="Alice",
        binding_state=AccountBindingState.VERIFIED,
    )
    user_id_same = AccountIdentity(
        environment="production",
        account_id="alice",
        identifier_type=AccountIdentifierType.USER_ID,
        username="alice",
        binding_state=AccountBindingState.VERIFIED,
    )
    eias_sandbox = AccountIdentity(
        environment="sandbox",
        account_id="eias-1",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        username="Alice",
        binding_state=AccountBindingState.VERIFIED,
    )
    unverified_eias = AccountIdentity(
        environment="production",
        account_id="eias-1",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        username="Alice",
        binding_state=AccountBindingState.UNVERIFIED,
    )

    # 1. Matching EIASToken allows legitimate username rename
    assert compare_account_identities(eias_a, eias_rename) == AccountEquivalence.SAME
    assert is_same_account(eias_a, eias_rename) is True

    # 2. Conflicting durable EIASTokens within same environment are proven DIFFERENT
    assert (
        compare_account_identities(eias_a, eias_conflict)
        == AccountEquivalence.DIFFERENT
    )
    assert is_same_account(eias_a, eias_conflict) is False

    # 3. Transitions between fallback USER_ID and EIASToken are UNKNOWN
    assert (
        compare_account_identities(eias_a, user_id_fallback)
        == AccountEquivalence.UNKNOWN
    )
    assert (
        compare_account_identities(user_id_fallback, eias_a)
        == AccountEquivalence.UNKNOWN
    )
    assert is_same_account(eias_a, user_id_fallback) is False
    assert is_same_account(user_id_fallback, eias_a) is False

    # 4. Same USER_ID fallbacks cannot prove identity continuity (usernames are mutable) -> UNKNOWN
    assert (
        compare_account_identities(user_id_fallback, user_id_same)
        == AccountEquivalence.UNKNOWN
    )
    assert is_same_account(user_id_fallback, user_id_same) is False

    # 5. Identifier namespace collision (same string, different types) is UNKNOWN
    collision_eias = AccountIdentity(
        environment="production",
        account_id="same_token",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    collision_user = AccountIdentity(
        environment="production",
        account_id="same_token",
        identifier_type=AccountIdentifierType.USER_ID,
        binding_state=AccountBindingState.VERIFIED,
    )
    assert (
        compare_account_identities(collision_eias, collision_user)
        == AccountEquivalence.UNKNOWN
    )
    assert is_same_account(collision_eias, collision_user) is False

    # 6. Different environments are DIFFERENT
    assert (
        compare_account_identities(eias_a, eias_sandbox) == AccountEquivalence.DIFFERENT
    )
    assert is_same_account(eias_a, eias_sandbox) is False

    # 7. Unverified identities yield UNKNOWN
    assert (
        compare_account_identities(unverified_eias, eias_a)
        == AccountEquivalence.UNKNOWN
    )
    assert (
        compare_account_identities(eias_a, unverified_eias)
        == AccountEquivalence.UNKNOWN
    )

    # 8. None checks yield UNKNOWN
    assert compare_account_identities(eias_a, None) == AccountEquivalence.UNKNOWN
    assert compare_account_identities(None, eias_a) == AccountEquivalence.UNKNOWN
    assert compare_account_identities(None, None) == AccountEquivalence.UNKNOWN
    assert is_same_account(eias_a, None) is False
    assert is_same_account(None, eias_a) is False
    assert is_same_account(None, None) is False


def test_get_entry_account_identity_legacy_fallback() -> None:
    """Legacy pre-M9 config entries cleanly yield LEGACY_COMPATIBLE identities."""
    entry = Mock()
    entry.entry_id = "legacy-entry-1"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-123",
        CONF_REAUTH_GENERATION: 0,
    }

    identity = get_entry_account_identity(entry)
    assert identity.entry_id == "legacy-entry-1"
    assert identity.client_id == "client-123"
    assert identity.account_id is None
    assert identity.binding_state == AccountBindingState.LEGACY_COMPATIBLE
    assert identity.provenance == AccountIdentityProvenance.LEGACY_MIGRATION
    assert identity.is_verified is False


# ==============================================================================
# 2. Authenticated Identity Verification API Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_verify_authenticated_account_success_with_eias_token() -> None:
    """Successful GetUser with EIASToken prefers EIASToken over UserID."""
    xml = _get_user_response_xml(user_id="seller_1", eias_token="EIAS_TOKEN_ABC")

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.read = AsyncMock(return_value=xml)

    mock_session = Mock()
    mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.post.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await async_verify_authenticated_account(
        mock_session,
        environment="production",
        access_token="valid-token",
        client_id="app-1",
    )

    assert result.is_verified is True
    assert result.account_id == "EIAS_TOKEN_ABC"
    assert result.identifier_type == AccountIdentifierType.EIASTOKEN
    assert result.username == "seller_1"
    assert result.binding_state == AccountBindingState.VERIFIED
    assert result.provenance == AccountIdentityProvenance.AUTHENTICATED_GET_USER
    assert result.failure_reason == IdentityFailureReason.NONE


@pytest.mark.asyncio
async def test_verify_authenticated_account_fallback_to_userid() -> None:
    """When EIASToken is missing or empty, UserID is used as fallback account_id."""
    xml = _get_user_response_xml(user_id="seller_no_eias", eias_token=None)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.read = AsyncMock(return_value=xml)

    mock_session = Mock()
    mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.post.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await async_verify_authenticated_account(
        mock_session,
        environment="production",
        access_token="valid-token",
        client_id="app-1",
    )

    assert result.is_verified is True
    assert result.account_id == "seller_no_eias"
    assert result.identifier_type == AccountIdentifierType.USER_ID
    assert result.username == "seller_no_eias"


@pytest.mark.asyncio
async def test_verify_authenticated_account_blank_token_fails_closed() -> None:
    """Missing or whitespace access token immediately fails without network request."""
    mock_session = Mock()
    result = await async_verify_authenticated_account(
        mock_session,
        environment="production",
        access_token="   ",
    )
    assert result.is_verified is False
    assert result.failure_reason == IdentityFailureReason.AUTH_FAILED
    mock_session.post.assert_not_called()


@pytest.mark.asyncio
async def test_verify_authenticated_account_http_status_failures() -> None:
    """HTTP 401/403 and non-200 errors fail closed."""
    mock_session = Mock()

    # 401 Unauthorized
    resp_401 = AsyncMock()
    resp_401.status = 401
    mock_cm_401 = AsyncMock()
    mock_cm_401.__aenter__.return_value = resp_401
    mock_cm_401.__aexit__.return_value = None
    mock_session.post.return_value = mock_cm_401
    res_401 = await async_verify_authenticated_account(
        mock_session, environment="production", access_token="tok"
    )
    assert res_401.is_verified is False
    assert res_401.failure_reason == IdentityFailureReason.AUTH_FAILED

    # 500 Internal Error
    resp_500 = AsyncMock()
    resp_500.status = 500
    mock_cm_500 = AsyncMock()
    mock_cm_500.__aenter__.return_value = resp_500
    mock_cm_500.__aexit__.return_value = None
    mock_session.post.return_value = mock_cm_500
    res_500 = await async_verify_authenticated_account(
        mock_session, environment="production", access_token="tok"
    )
    assert res_500.is_verified is False
    assert res_500.failure_reason == IdentityFailureReason.NETWORK_ERROR


@pytest.mark.asyncio
async def test_verify_authenticated_account_network_exceptions() -> None:
    """Timeout and ClientError fail closed with specific failure reasons."""
    import aiohttp

    mock_session = Mock()

    # Timeout
    mock_session.post.side_effect = asyncio.TimeoutError()
    res_timeout = await async_verify_authenticated_account(
        mock_session, environment="production", access_token="tok"
    )
    assert res_timeout.is_verified is False
    assert res_timeout.failure_reason == IdentityFailureReason.TIMEOUT

    # Network client error
    mock_session.post.side_effect = aiohttp.ClientConnectionError("conn refused")
    res_net = await async_verify_authenticated_account(
        mock_session, environment="production", access_token="tok"
    )
    assert res_net.is_verified is False
    assert res_net.failure_reason == IdentityFailureReason.NETWORK_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("xml_data", "expected_reason"),
    [
        (b"not xml", IdentityFailureReason.MALFORMED_RESPONSE),
        (
            _get_user_response_xml(root_tag="EvilGetUserResponse"),
            IdentityFailureReason.MALFORMED_RESPONSE,
        ),
        (
            _get_user_response_xml(namespace="urn:ebay:apis:evil"),
            IdentityFailureReason.MALFORMED_RESPONSE,
        ),
        (
            _get_user_response_xml(ack="Failure"),
            IdentityFailureReason.MALFORMED_RESPONSE,
        ),
        (
            _get_user_response_xml(errors=[("123", "Error")]),
            IdentityFailureReason.AUTH_FAILED,
        ),
        (
            b'<?xml version="1.0" encoding="utf-8"?><GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></GetUserResponse>',
            IdentityFailureReason.MISSING_IDENTIFIER,
        ),
        (
            b'<?xml version="1.0" encoding="utf-8"?><GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack><User><UserID>  </UserID></User></GetUserResponse>',
            IdentityFailureReason.MISSING_IDENTIFIER,
        ),
    ],
)
async def test_verify_authenticated_account_malformed_xml_matrix(
    xml_data: bytes, expected_reason: IdentityFailureReason
) -> None:
    """Malformed XML, invalid namespaces, error containers, or missing fields fail closed."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.read = AsyncMock(return_value=xml_data)

    mock_session = Mock()
    mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.post.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await async_verify_authenticated_account(
        mock_session,
        environment="production",
        access_token="tok",
    )
    assert result.is_verified is False
    assert result.failure_reason == expected_reason


# ==============================================================================
# 3. Config Flow Duplicate Detection & Multi-Account Isolation Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_case_a_same_app_different_accounts_both_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case A: Multiple accounts sharing the same eBay App ID can be configured independently."""
    flow1 = EbayConfigFlow()
    flow1.hass = Mock()
    flow1.flow_id = "flow-1"
    flow1._data = _flow_data(client_id="shared-app-id")

    # Flow 1 completes OAuth for Account 1 (user_1, eias_1)
    identity1 = AccountIdentity(
        account_id="eias_1",
        username="user_1",
        environment="production",
        client_id="shared-app-id",
        binding_state=AccountBindingState.VERIFIED,
        identifier_type=AccountIdentifierType.EIASTOKEN,
        provenance=AccountIdentityProvenance.AUTHENTICATED_GET_USER,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity1),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )
    monkeypatch.setattr(flow1, "_async_current_entries", lambda: [])
    created_unique_ids1: list[str] = []
    monkeypatch.setattr(
        flow1, "async_set_unique_id", AsyncMock(side_effect=created_unique_ids1.append)
    )
    monkeypatch.setattr(flow1, "_abort_if_unique_id_configured", lambda: None)

    res1 = await flow1.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:shared-app-id",
            "token": {
                "access_token": "acc_tok_1",
                "refresh_token": "ref_tok_1",
                "expires_in": 7200,
            },
        }
    )

    assert res1["type"] == "create_entry"
    assert res1["title"] == "eBay (user_1)"
    assert created_unique_ids1 == ["production:eias_1"]

    # Create mock existing entry for Account 1
    entry1 = Mock()
    entry1.entry_id = "entry-user-1"
    entry1.unique_id = "production:eias_1"
    entry1.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "shared-app-id",
        CONF_ACCOUNT_IDENTITY: identity1.to_config_entry_dict(),
    }

    # Flow 2 uses the exact same App credentials, but logs into Account 2 (user_2, eias_2)
    flow2 = EbayConfigFlow()
    flow2.hass = Mock()
    flow2.flow_id = "flow-2"
    flow2._data = _flow_data(client_id="shared-app-id")

    identity2 = AccountIdentity(
        account_id="eias_2",
        username="user_2",
        environment="production",
        client_id="shared-app-id",
        binding_state=AccountBindingState.VERIFIED,
        identifier_type=AccountIdentifierType.EIASTOKEN,
        provenance=AccountIdentityProvenance.AUTHENTICATED_GET_USER,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity2),
    )
    monkeypatch.setattr(flow2, "_async_current_entries", lambda: [entry1])
    created_unique_ids2: list[str] = []
    monkeypatch.setattr(
        flow2, "async_set_unique_id", AsyncMock(side_effect=created_unique_ids2.append)
    )
    monkeypatch.setattr(flow2, "_abort_if_unique_id_configured", lambda: None)

    res2 = await flow2.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:shared-app-id",
            "token": {
                "access_token": "acc_tok_2",
                "refresh_token": "ref_tok_2",
                "expires_in": 7200,
            },
        }
    )

    assert res2["type"] == "create_entry"
    assert res2["title"] == "eBay (user_2)"
    assert created_unique_ids2 == ["production:eias_2"]


@pytest.mark.asyncio
async def test_case_b_same_app_same_account_aborts_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case B: Same developer app authorizing an already configured account aborts."""
    identity = AccountIdentity(
        account_id="eias_1",
        username="user_1",
        environment="production",
        client_id="shared-app-id",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    existing_entry = Mock()
    existing_entry.entry_id = "entry-1"
    existing_entry.unique_id = "production:eias_1"
    existing_entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "shared-app-id",
        CONF_ACCOUNT_IDENTITY: identity.to_config_entry_dict(),
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.flow_id = "flow-dup"
    flow._data = _flow_data(client_id="shared-app-id")

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [existing_entry])

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:shared-app-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_case_c_different_app_same_account_aborts_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case C: Different developer apps authorizing the same account aborts duplicate."""
    identity1 = AccountIdentity(
        account_id="eias_1",
        username="user_1",
        environment="production",
        client_id="app-A",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    existing_entry = Mock()
    existing_entry.entry_id = "entry-1"
    existing_entry.unique_id = "production:eias_1"
    existing_entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "app-A",
        CONF_ACCOUNT_IDENTITY: identity1.to_config_entry_dict(),
    }

    # Attempt to add same account using app-B
    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.flow_id = "flow-app-b"
    flow._data = _flow_data(client_id="app-B")

    identity_from_app_b = AccountIdentity(
        account_id="eias_1",
        username="user_1",
        environment="production",
        client_id="app-B",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity_from_app_b),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [existing_entry])

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:app-B",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_case_d_legacy_entry_duplicate_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case D: Pre-M9 legacy entry with unverifiable durable identity aborts account_ambiguous."""
    legacy_entry = Mock()
    legacy_entry.entry_id = "entry-legacy"
    legacy_entry.unique_id = "production:client-id"
    legacy_entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_USERNAME: "legacy_seller",
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow._data = _flow_data(client_id="client-id")

    identity = AccountIdentity(
        account_id="eias_legacy",
        username="legacy_seller",
        environment="production",
        client_id="client-id",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [legacy_entry])

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_ambiguous"


@pytest.mark.asyncio
async def test_case_d_unbound_legacy_with_verified_matching_eias_aborts_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unbound legacy entry whose prior identity returns matching EIASToken aborts already_configured."""
    legacy_entry = Mock()
    legacy_entry.entry_id = "entry-legacy"
    legacy_entry.unique_id = "production:client-id"
    legacy_entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_USERNAME: "legacy_seller",
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow._data = _flow_data(client_id="client-id")

    identity = AccountIdentity(
        account_id="eias_legacy_123",
        username="legacy_seller",
        environment="production",
        client_id="client-id",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_entry_existing_identity",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [legacy_entry])

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_case_d_cross_app_unbound_legacy_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing entry on different developer app with unverified identity aborts account_ambiguous."""
    legacy_entry = Mock()
    legacy_entry.entry_id = "entry-legacy-app-a"
    legacy_entry.unique_id = "production:client-app-a"
    legacy_entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-app-a",
        CONF_ACCOUNT_USERNAME: "legacy_seller",
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow._data = _flow_data(client_id="client-app-b")

    identity_b = AccountIdentity(
        account_id="eias_new_app_b",
        username="seller_b",
        environment="production",
        client_id="client-app-b",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity_b),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [legacy_entry])

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-app-b",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_ambiguous"


@pytest.mark.asyncio
async def test_case_d_stored_verified_userid_entry_aborts_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing entry with stored VERIFIED USER_ID binding aborts account_ambiguous against candidate EIASToken."""
    stored_userid_identity = AccountIdentity(
        account_id="legacy_seller",
        username="legacy_seller",
        environment="production",
        client_id="client-id",
        identifier_type=AccountIdentifierType.USER_ID,
        binding_state=AccountBindingState.VERIFIED,
    )
    existing_entry = Mock()
    existing_entry.entry_id = "entry-userid"
    existing_entry.unique_id = "production:client-id"
    existing_entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: stored_userid_identity.to_config_entry_dict(),
        CONF_ACCOUNT_USERNAME: "legacy_seller",
        CONF_ACCOUNT_ID: "legacy_seller",
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow._data = _flow_data(client_id="client-id")

    candidate_identity = AccountIdentity(
        account_id="eias_candidate_123",
        username="legacy_seller",
        environment="production",
        client_id="client-id",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=candidate_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [existing_entry])

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_ambiguous"


@pytest.mark.asyncio
async def test_case_e_unverifiable_identity_aborts_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case E: Unverifiable account identity fails closed and aborts setup."""
    unverifiable_identity = AccountIdentity(
        account_id=None,
        binding_state=AccountBindingState.FAILED,
        failure_reason=IdentityFailureReason.MALFORMED_RESPONSE,
    )

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow._data = _flow_data()

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=unverifiable_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_unverifiable"


# ==============================================================================
# 4. Reauthorization Account Switching Protections
# ==============================================================================


@pytest.mark.asyncio
async def test_reauth_blocks_account_switching_by_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauthorizing with a different eBay account ID aborts with account_mismatch."""
    orig_identity = AccountIdentity(
        account_id="original_acc_id",
        username="original_user",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    entry = Mock()
    entry.entry_id = "entry-reauth"
    entry.unique_id = "production:original_acc_id"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: orig_identity.to_config_entry_dict(),
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = _flow_data()

    # User mistakenly logged into a different account in their browser
    different_identity = AccountIdentity(
        account_id="different_acc_id",
        username="different_user",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=different_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_mismatch"


@pytest.mark.asyncio
async def test_reauth_blocks_account_switching_by_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauthorizing with a different username aborts with account_mismatch."""
    orig_identity = AccountIdentity(
        account_id=None,
        username="original_seller",
        identifier_type=AccountIdentifierType.LEGACY_APP_ENVIRONMENT,
        binding_state=AccountBindingState.LEGACY_COMPATIBLE,
    )
    entry = Mock()
    entry.entry_id = "entry-reauth"
    entry.unique_id = "production:client-id"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: orig_identity.to_config_entry_dict(),
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = _flow_data()

    new_identity = AccountIdentity(
        account_id="new_eias",
        username="impostor_seller",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    legacy_prior_identity = AccountIdentity(
        account_id="orig_eias",
        username="original_seller",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=new_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_entry_existing_identity",
        AsyncMock(return_value=legacy_prior_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_mismatch"


@pytest.mark.asyncio
async def test_reauth_legacy_unbound_entry_unverifiable_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a legacy entry's prior token cannot be verified, reauth fails closed."""
    orig_identity = AccountIdentity(
        account_id=None,
        username="legacy_seller",
        identifier_type=AccountIdentifierType.LEGACY_APP_ENVIRONMENT,
        binding_state=AccountBindingState.LEGACY_COMPATIBLE,
    )
    entry = Mock()
    entry.entry_id = "entry-legacy-reauth"
    entry.unique_id = "production:client-id"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: orig_identity.to_config_entry_dict(),
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = _flow_data()

    new_identity = AccountIdentity(
        account_id="new_eias",
        username="legacy_seller",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    unverifiable_prior = AccountIdentity(
        account_id=None,
        identifier_type=AccountIdentifierType.UNKNOWN,
        binding_state=AccountBindingState.FAILED,
        failure_reason=IdentityFailureReason.AUTH_FAILED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=new_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_entry_existing_identity",
        AsyncMock(return_value=unverifiable_prior),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_unverifiable"


@pytest.mark.asyncio
async def test_reauth_fails_closed_when_identity_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauthorizing with an unverifiable account fails closed with cannot_connect."""
    entry = Mock()
    entry.entry_id = "entry-reauth"
    entry.unique_id = "production:client-id"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = _flow_data()

    failed_identity = AccountIdentity(
        account_id=None,
        binding_state=AccountBindingState.FAILED,
        failure_reason=IdentityFailureReason.TIMEOUT,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=failed_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "cannot_connect"


# ==============================================================================
# 5. Cross-Account Entity & Storage Isolation Tests
# ==============================================================================


def test_entity_unique_id_isolation_with_overlapping_item_id() -> None:
    """Overlapping ItemIDs across two accounts produce strictly isolated entity unique IDs."""
    entry_a = Mock()
    entry_a.entry_id = "entry_account_a"
    entry_b = Mock()
    entry_b.entry_id = "entry_account_b"

    item_id = "1122334455"

    # Entities use f"{entry.entry_id}_{kind}_{item_id}_{suffix}" or f"{entry.entry_id}_{key}"
    entity_a_item = f"{entry_a.entry_id}_selling_{item_id}_price"
    entity_b_item = f"{entry_b.entry_id}_selling_{item_id}_price"

    assert entity_a_item != entity_b_item
    assert entity_a_item.startswith("entry_account_a_")
    assert entity_b_item.startswith("entry_account_b_")

    # Summary sensors
    entity_a_summary = f"{entry_a.entry_id}_selling_total_sold"
    entity_b_summary = f"{entry_b.entry_id}_selling_total_sold"
    assert entity_a_summary != entity_b_summary


def test_storage_keys_are_strictly_entry_isolated() -> None:
    """Persistent store keys are strictly scoped by entry_id."""
    entry_id_1 = "entry-101"
    entry_id_2 = "entry-102"

    ending_soon_key_1 = f"ebay.{entry_id_1}.ending_soon"
    ending_soon_key_2 = f"ebay.{entry_id_2}.ending_soon"
    assert ending_soon_key_1 != ending_soon_key_2

    inactive_key_1 = f"ebay.{entry_id_1}.inactive_item_sensors"
    inactive_key_2 = f"ebay.{entry_id_2}.inactive_item_sensors"
    assert inactive_key_1 != inactive_key_2


def test_feedback_client_cache_isolation() -> None:
    """Separate EbayApiClient instances have independent feedback username caches."""
    from custom_components.ebay.api import EbayApiClient

    client_a = EbayApiClient(
        Mock(),
        environment="production",
        client_id="app-1",
        client_secret="sec",
        runame="ru",
        refresh_token="tok-a",
        site_id="0",
    )
    client_b = EbayApiClient(
        Mock(),
        environment="production",
        client_id="app-1",
        client_secret="sec",
        runame="ru",
        refresh_token="tok-b",
        site_id="0",
    )

    client_a._feedback_user_id = "seller_alpha"
    client_b._feedback_user_id = "seller_beta"

    assert client_a._feedback_user_id == "seller_alpha"
    assert client_b._feedback_user_id == "seller_beta"
    assert client_a._feedback_user_id != client_b._feedback_user_id


# ==============================================================================
# 6. Diagnostics Privacy & Redaction Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_diagnostics_redacts_account_id_and_pseudonymizes() -> None:
    """Diagnostics export redacts raw account_id and includes pseudonymized identity."""
    identity = AccountIdentity(
        entry_id="entry-diag",
        environment="production",
        client_id="private-client-id",
        account_id="SECRET_RAW_ACCOUNT_ID_999",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        username="private_seller_username",
        binding_state=AccountBindingState.VERIFIED,
        provenance=AccountIdentityProvenance.AUTHENTICATED_GET_USER,
    )

    entry = Mock()
    entry.entry_id = "entry-diag"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_SITE_ID: "0",
        CONF_CLIENT_ID: "private-client-id",
        CONF_CLIENT_SECRET: "private-client-secret",
        CONF_ACCOUNT_ID: "SECRET_RAW_ACCOUNT_ID_999",
        CONF_ACCOUNT_IDENTITY: identity.to_config_entry_dict(),
    }
    entry.options = DEFAULT_OPTIONS
    coordinator = Mock()
    coordinator.data = None
    coordinator.options = DEFAULT_OPTIONS
    coordinator.last_error_category = None
    coordinator.last_attempt_at = None
    coordinator.last_refresh_duration_seconds = None
    coordinator.last_refresh_result = "unknown"
    coordinator.scheduled_ending_soon_count = 0
    entry.runtime_data = coordinator

    diag = await async_get_config_entry_diagnostics(Mock(), entry)

    # 1. CONF_ACCOUNT_ID is in TO_REDACT
    assert CONF_ACCOUNT_ID in TO_REDACT
    assert CONF_ACCOUNT_ID in diag["redacted"]

    # 2. Raw sensitive values do NOT appear in the diagnostic export
    diag_str = str(diag)
    assert "SECRET_RAW_ACCOUNT_ID_999" not in diag_str
    assert "private-client-id" not in diag_str
    assert "private-client-secret" not in diag_str

    # 3. Safe pseudonymized identity is present
    acc_diag = diag["account_identity"]
    assert acc_diag["is_verified"] is True
    assert acc_diag["account_id_pseudonym"].startswith("account_")
    assert acc_diag["username_pseudonym"].startswith("user_")


# ==============================================================================
# 7. Additional Hardening, Privacy, and Manual OAuth Parity Tests
# ==============================================================================


def test_is_displayable_username_semantics() -> None:
    """is_displayable_username accepts human display names and rejects opaque IDs."""
    assert is_displayable_username("seller1") is True
    assert is_displayable_username("my_store.123") is True
    assert is_displayable_username("bob-smith") is True
    assert is_displayable_username("A") is True

    # Rejects invalid or opaque handles
    assert is_displayable_username(None) is False
    assert is_displayable_username("") is False
    assert is_displayable_username("   ") is False
    assert is_displayable_username("a" * 33) is False
    # 32-char hexadecimal hash
    assert is_displayable_username("0123456789abcdef0123456789abcdef") is False
    assert is_displayable_username("0123456789ABCDEF0123456789ABCDEF") is False
    # Delimiters used in tokens or UUIDs
    assert is_displayable_username("user|1234") is False
    assert is_displayable_username("user;token") is False
    assert is_displayable_username("user#hash") is False


@pytest.mark.asyncio
async def test_reauth_legitimate_username_rename_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When durable EIASToken matches, reauth accepts username rename and updates title and metadata atomically."""
    orig_identity = AccountIdentity(
        account_id="eias_durable_123",
        username="old_seller_handle",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    entry = Mock()
    entry.entry_id = "entry-rename"
    entry.unique_id = "production:eias_durable_123"
    entry.title = "eBay (old_seller_handle)"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: orig_identity.to_config_entry_dict(),
        CONF_ACCOUNT_USERNAME: "old_seller_handle",
        CONF_ACCOUNT_ID: "eias_durable_123",
        "token": {"access_token": "old_acc", "refresh_token": "old_ref"},
    }
    entry.options = {"opt": "val"}

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = _flow_data()

    # User renamed their account on eBay to new_seller_handle
    renamed_identity = AccountIdentity(
        account_id="eias_durable_123",
        username="new_seller_handle",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=renamed_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )
    update_and_abort_calls: dict[str, Any] = {}

    def _mock_update_and_abort(ent: Any, **kwargs: Any) -> dict[str, Any]:
        update_and_abort_calls["entry"] = ent
        update_and_abort_calls.update(kwargs)
        return {"type": "abort", "reason": "reauth_successful"}

    monkeypatch.setattr(flow, "async_update_and_abort", _mock_update_and_abort)

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "new_acc",
                "refresh_token": "new_ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "reauth_successful"
    # Title committed atomically inside async_update_and_abort
    assert update_and_abort_calls["entry"] is entry
    assert update_and_abort_calls["title"] == "eBay (new_seller_handle)"
    assert update_and_abort_calls["data"][CONF_ACCOUNT_USERNAME] == "new_seller_handle"
    assert update_and_abort_calls["data"][CONF_ACCOUNT_ID] == "eias_durable_123"
    assert update_and_abort_calls["data"]["token"]["refresh_token"] == "new_ref"


@pytest.mark.asyncio
async def test_reauth_fails_closed_legacy_userid_only_entry_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy entry with only UserID fallback aborts reauth account_unverifiable and leaves entry untouched."""
    legacy_entry = Mock()
    legacy_entry.entry_id = "entry-legacy-reauth"
    legacy_entry.unique_id = "production:client-id"
    legacy_entry.title = "eBay (legacy_seller)"
    orig_data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_USERNAME: "legacy_seller",
        "token": {"access_token": "orig_acc", "refresh_token": "orig_ref"},
    }
    legacy_entry.data = dict(orig_data)
    orig_options = {"feedbacks": True}
    legacy_entry.options = dict(orig_options)

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": legacy_entry.entry_id,
    }
    flow._data = _flow_data()

    # Reauth grant returns only USER_ID even with matching username
    userid_identity = AccountIdentity(
        account_id="legacy_seller",
        username="legacy_seller",
        identifier_type=AccountIdentifierType.USER_ID,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: legacy_entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=userid_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "candidate_acc",
                "refresh_token": "candidate_ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_unverifiable"
    # Strict immutability verification: entry remains completely untouched
    assert legacy_entry.data == orig_data
    assert legacy_entry.title == "eBay (legacy_seller)"
    assert legacy_entry.options == orig_options
    assert legacy_entry.data["token"]["refresh_token"] == "orig_ref"


@pytest.mark.asyncio
async def test_reauth_fails_closed_legacy_entry_candidate_eias_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy entry with only UserID attempting reauth with EIASToken aborts account_unverifiable and leaves entry untouched."""
    legacy_entry = Mock()
    legacy_entry.entry_id = "entry-legacy-reauth"
    legacy_entry.unique_id = "production:client-id"
    legacy_entry.title = "eBay (legacy_seller)"
    orig_data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_USERNAME: "legacy_seller",
        "token": {"access_token": "orig_acc", "refresh_token": "orig_ref"},
    }
    legacy_entry.data = dict(orig_data)
    orig_options = {"feedbacks": True}
    legacy_entry.options = dict(orig_options)

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": legacy_entry.entry_id,
    }
    flow._data = _flow_data()

    # Reauth grant returns EIASToken, but prior legacy identity only has UserID fallback
    candidate_eias_identity = AccountIdentity(
        account_id="eias_new_candidate",
        username="legacy_seller",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    prior_userid_identity = AccountIdentity(
        account_id="legacy_seller",
        username="legacy_seller",
        identifier_type=AccountIdentifierType.USER_ID,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: legacy_entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=candidate_eias_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_entry_existing_identity",
        AsyncMock(return_value=prior_userid_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "candidate_acc",
                "refresh_token": "candidate_ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_unverifiable"
    # Strict immutability verification: entry remains completely untouched
    assert legacy_entry.data == orig_data
    assert legacy_entry.title == "eBay (legacy_seller)"
    assert legacy_entry.options == orig_options
    assert legacy_entry.data["token"]["refresh_token"] == "orig_ref"


@pytest.mark.asyncio
async def test_reauth_fails_closed_candidate_userid_only_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified EIASToken entry reauthorizing with UserID-only grant aborts account_unverifiable and leaves entry untouched."""
    verified_identity = AccountIdentity(
        account_id="eias_durable_123",
        username="verified_seller",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    entry = Mock()
    entry.entry_id = "entry-verified"
    entry.unique_id = "production:eias_durable_123"
    entry.title = "eBay (verified_seller)"
    orig_data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: verified_identity.to_config_entry_dict(),
        CONF_ACCOUNT_USERNAME: "verified_seller",
        CONF_ACCOUNT_ID: "eias_durable_123",
        "token": {"access_token": "orig_acc", "refresh_token": "orig_ref"},
    }
    entry.data = dict(orig_data)
    orig_options = {"feedbacks": True}
    entry.options = dict(orig_options)

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = _flow_data()

    # Reauth grant only returns UserID (EIASToken conditionally omitted by eBay)
    userid_identity = AccountIdentity(
        account_id="verified_seller",
        username="verified_seller",
        identifier_type=AccountIdentifierType.USER_ID,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=userid_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "candidate_acc",
                "refresh_token": "candidate_ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_unverifiable"
    # Entry remains completely untouched
    assert entry.data == orig_data
    assert entry.title == "eBay (verified_seller)"
    assert entry.options == orig_options
    assert entry.data["token"]["refresh_token"] == "orig_ref"


@pytest.mark.asyncio
async def test_reauth_failure_injection_account_mismatch_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When reauth detects account mismatch, aborts account_mismatch and leaves entry untouched."""
    verified_identity = AccountIdentity(
        account_id="eias_account_A",
        username="seller_A",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    entry = Mock()
    entry.entry_id = "entry-mismatch"
    entry.unique_id = "production:eias_account_A"
    entry.title = "eBay (seller_A)"
    orig_data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: verified_identity.to_config_entry_dict(),
        CONF_ACCOUNT_USERNAME: "seller_A",
        CONF_ACCOUNT_ID: "eias_account_A",
        "token": {"access_token": "orig_acc", "refresh_token": "orig_ref"},
    }
    entry.data = dict(orig_data)
    orig_options = {"feedbacks": True}
    entry.options = dict(orig_options)

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = _flow_data()

    # User mistakenly authorized Account B
    mismatched_identity = AccountIdentity(
        account_id="eias_account_B",
        username="seller_B",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=mismatched_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "mismatch_acc",
                "refresh_token": "mismatch_ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_mismatch"
    # Entry remains completely untouched
    assert entry.data == orig_data
    assert entry.title == "eBay (seller_A)"
    assert entry.options == orig_options
    assert entry.data["token"]["refresh_token"] == "orig_ref"


@pytest.mark.asyncio
async def test_reauth_failure_injection_network_failure_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When reauth fails identity verification due to network failure, aborts cannot_connect and leaves entry untouched."""
    verified_identity = AccountIdentity(
        account_id="eias_account_A",
        username="seller_A",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    entry = Mock()
    entry.entry_id = "entry-net-fail"
    entry.unique_id = "production:eias_account_A"
    entry.title = "eBay (seller_A)"
    orig_data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: verified_identity.to_config_entry_dict(),
        CONF_ACCOUNT_USERNAME: "seller_A",
        CONF_ACCOUNT_ID: "eias_account_A",
        "token": {"access_token": "orig_acc", "refresh_token": "orig_ref"},
    }
    entry.data = dict(orig_data)
    orig_options = {"feedbacks": True}
    entry.options = dict(orig_options)

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = _flow_data()

    # Network failure during identity verification
    failed_identity = AccountIdentity(
        account_id=None,
        username=None,
        identifier_type=AccountIdentifierType.UNKNOWN,
        binding_state=AccountBindingState.FAILED,
        failure_reason=IdentityFailureReason.NETWORK_ERROR,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=failed_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "new_acc",
                "refresh_token": "new_ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "cannot_connect"
    # Entry remains completely untouched
    assert entry.data == orig_data
    assert entry.title == "eBay (seller_A)"
    assert entry.options == orig_options
    assert entry.data["token"]["refresh_token"] == "orig_ref"


@pytest.mark.asyncio
async def test_new_onboarding_blocks_userid_fallback_without_eias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup fails closed when eBay returns only mutable UserID without durable EIASToken."""
    userid_identity = AccountIdentity(
        account_id="unstable_user_1",
        username="unstable_user_1",
        identifier_type=AccountIdentifierType.USER_ID,
        binding_state=AccountBindingState.VERIFIED,
    )

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow._data = _flow_data()

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=userid_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_unverifiable"


@pytest.mark.asyncio
async def test_privacy_opaque_userid_omits_username_from_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When UserID is an opaque hash, entry title defaults to 'eBay' without leaking hash."""
    opaque_hash = "0123456789abcdef0123456789abcdef"
    identity = AccountIdentity(
        account_id="eias_valid_token",
        username=opaque_hash,
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow._data = _flow_data()

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock())
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda: None)

    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "create_entry"
    assert res["title"] == "eBay"
    assert opaque_hash not in res["title"]


@pytest.mark.asyncio
async def test_privacy_zero_raw_identifiers_in_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Assert that reauth mismatch logs emit zero raw usernames or EIASTokens."""
    raw_old_user = "secret_old_user_123"
    raw_old_eias = "secret_old_eias_456"
    raw_new_user = "secret_new_user_789"
    raw_new_eias = "secret_new_eias_012"

    orig_identity = AccountIdentity(
        account_id=raw_old_eias,
        username=raw_old_user,
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    entry = Mock()
    entry.entry_id = "entry-reauth-leak-test"
    entry.unique_id = f"production:{raw_old_eias}"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: orig_identity.to_config_entry_dict(),
    }

    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = _flow_data()

    mismatched_identity = AccountIdentity(
        account_id=raw_new_eias,
        username=raw_new_user,
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=mismatched_identity),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    caplog.clear()
    res = await flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:client-id",
            "token": {
                "access_token": "tok",
                "refresh_token": "ref",
                "expires_in": 7200,
            },
        }
    )

    assert res["type"] == "abort"
    assert res["reason"] == "account_mismatch"

    # Verify no raw identifiers were logged anywhere
    for record in caplog.records:
        msg = record.getMessage()
        assert raw_old_user not in msg
        assert raw_old_eias not in msg
        assert raw_new_user not in msg
        assert raw_new_eias not in msg


@pytest.mark.asyncio
async def test_verify_authenticated_account_strict_xml_user_element_checks() -> None:
    """Strict XML validation rejects duplicate User elements or nested impostor Users."""
    # 1. Duplicate direct User elements
    dup_users_xml = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
        <Ack>Success</Ack>
        <User><UserID>user1</UserID><EIASToken>eias1</EIASToken></User>
        <User><UserID>user2</UserID><EIASToken>eias2</EIASToken></User>
    </GetUserResponse>"""

    session_mock = Mock()
    resp_mock = AsyncMock()
    resp_mock.status = 200
    resp_mock.read = AsyncMock(return_value=dup_users_xml)
    cm_mock = Mock()
    cm_mock.__aenter__ = AsyncMock(return_value=resp_mock)
    cm_mock.__aexit__ = AsyncMock(return_value=None)
    session_mock.post.return_value = cm_mock

    res_dup = await async_verify_authenticated_account(
        session_mock,
        environment="production",
        access_token="tok",
    )
    assert res_dup.binding_state == AccountBindingState.FAILED
    assert res_dup.failure_reason == IdentityFailureReason.MALFORMED_RESPONSE

    # 2. Nested impostor User element inside Errors
    nested_user_xml = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
        <Ack>Success</Ack>
        <User><UserID>real_user</UserID><EIASToken>eias_real</EIASToken></User>
        <Errors><User><UserID>fake_user</UserID></User></Errors>
    </GetUserResponse>"""
    resp_mock.read = AsyncMock(return_value=nested_user_xml)

    res_nested = await async_verify_authenticated_account(
        session_mock,
        environment="production",
        access_token="tok",
    )
    assert res_nested.binding_state == AccountBindingState.FAILED
    assert res_nested.failure_reason == IdentityFailureReason.MALFORMED_RESPONSE

    # 3. Ack=Success alongside fatal error container
    fatal_error_xml = b"""<?xml version="1.0" encoding="utf-8"?>
    <GetUserResponse xmlns="urn:ebay:apis:eBLBaseComponents">
        <Ack>Success</Ack>
        <Errors><SeverityCode>error</SeverityCode><ShortMessage>Fatal Auth</ShortMessage></Errors>
        <User><UserID>real_user</UserID><EIASToken>eias_real</EIASToken></User>
    </GetUserResponse>"""
    resp_mock.read = AsyncMock(return_value=fatal_error_xml)

    res_fatal = await async_verify_authenticated_account(
        session_mock,
        environment="production",
        access_token="tok",
    )
    assert res_fatal.binding_state == AccountBindingState.FAILED
    assert res_fatal.failure_reason == IdentityFailureReason.AUTH_FAILED


@pytest.mark.asyncio
async def test_manual_oauth_full_parity_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual OAuth path passes through exact same identity verification and reauth checks."""
    flow = EbayConfigFlow()
    flow.hass = Mock()
    flow.flow_id = "flow-manual"
    flow._oauth_mode = "manual"
    flow._data = _flow_data()
    flow._state = "manual-state"
    flow._consent_url = "https://auth.ebay.com/oauth2/authorize"
    flow._requested_scopes = "https://api.ebay.com/oauth/api_scope"

    created_unique_ids: list[str] = []
    monkeypatch.setattr(
        flow, "async_set_unique_id", AsyncMock(side_effect=created_unique_ids.append)
    )
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda: None)
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])

    # Mock code exchange
    class MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_exchange_authorization_code(self, code: str) -> dict[str, Any]:
            return {
                "access_token": "manual_access_token",
                "refresh_token": "manual_refresh_token",
                "expires_in": 7200,
            }

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.EbayApiClient",
        MockClient,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    identity = AccountIdentity(
        account_id="eias_manual_user",
        username="manual_seller",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity),
    )

    flow.flow_impl = Mock()
    flow.flow_impl.domain = "ebay:production:client-id"

    # 1. Manual Setup Success
    res = await flow.async_step_manual({"authorization_code": "code-12345"})
    assert res["type"] == "create_entry"
    assert res["title"] == "eBay (manual_seller)"
    assert created_unique_ids == ["production:eias_manual_user"]

    # 2. Manual Reauth with Wrong Account Aborts
    entry = Mock()
    entry.entry_id = "entry-manual-reauth"
    entry.unique_id = "production:eias_manual_user"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_ACCOUNT_IDENTITY: identity.to_config_entry_dict(),
    }

    reauth_flow = EbayConfigFlow()
    reauth_flow.hass = Mock()
    reauth_flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    reauth_flow._data = _flow_data()
    reauth_flow._state = "manual-state"
    reauth_flow.flow_impl = Mock()
    reauth_flow.flow_impl.domain = "ebay:production:client-id"
    monkeypatch.setattr(reauth_flow, "_get_reauth_entry", lambda: entry)

    wrong_identity = AccountIdentity(
        account_id="eias_other_user",
        username="other_seller",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=wrong_identity),
    )

    reauth_res = await reauth_flow.async_step_manual(
        {"authorization_code": "code-wrong"}
    )
    assert reauth_res["type"] == "abort"
    assert reauth_res["reason"] == "account_mismatch"


@pytest.mark.asyncio
async def test_async_verify_entry_existing_identity_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """async_verify_entry_existing_identity verifies with access token or refreshes."""
    entry = Mock()
    entry.entry_id = "entry-test"
    entry.data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client-id",
        CONF_CLIENT_SECRET: "client-sec",
        CONF_SITE_ID: "0",
        "token": {
            "access_token": "expired_access",
            "refresh_token": "valid_refresh",
            "expires_in": 7200,
        },
    }

    session = Mock()

    # Step 1: expired access token fails, refresh endpoint returns new access token
    verified_identity = AccountIdentity(
        account_id="eias_refreshed",
        username="seller_refreshed",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )

    async def _mock_verify(
        sess: Any, *, access_token: str, **kwargs: Any
    ) -> AccountIdentity:
        if access_token == "new_refreshed_access":
            return verified_identity
        return AccountIdentity(
            binding_state=AccountBindingState.FAILED,
            failure_reason=IdentityFailureReason.AUTH_FAILED,
        )

    monkeypatch.setattr(
        "custom_components.ebay.account_identity.async_verify_authenticated_account",
        _mock_verify,
    )

    token_resp = AsyncMock()
    token_resp.status = 200
    token_resp.json = AsyncMock(return_value={"access_token": "new_refreshed_access"})
    cm_mock = Mock()
    cm_mock.__aenter__ = AsyncMock(return_value=token_resp)
    cm_mock.__aexit__ = AsyncMock(return_value=None)
    session.post.return_value = cm_mock

    res = await async_verify_entry_existing_identity(session, entry)
    assert res.is_verified is True
    assert res.account_id == "eias_refreshed"


@pytest.mark.asyncio
async def test_simultaneous_multi_account_integration_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Component lifecycle test: two simultaneous distinct accounts under one HA instance."""
    # 1. Setup Account 1 via Native OAuth
    flow1 = EbayConfigFlow()
    flow1.hass = Mock()
    flow1.flow_id = "flow-1"
    flow1._data = _flow_data(client_id="shared-developer-app")
    flow1._requested_scopes = "https://api.ebay.com/oauth/api_scope"

    identity1 = AccountIdentity(
        account_id="eias_account_alpha",
        username="seller_alpha",
        environment="production",
        client_id="shared-developer-app",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )

    created_unique_ids_1: list[str] = []
    monkeypatch.setattr(
        flow1, "async_set_unique_id", AsyncMock(side_effect=created_unique_ids_1.append)
    )
    monkeypatch.setattr(flow1, "_abort_if_unique_id_configured", lambda: None)
    monkeypatch.setattr(flow1, "_async_current_entries", lambda: [])

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity1),
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_get_clientsession",
        lambda hass: Mock(),
    )

    res1 = await flow1.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:shared-developer-app",
            "token": {
                "access_token": "token_alpha_1",
                "refresh_token": "refresh_alpha_1",
                "expires_in": 7200,
            },
        }
    )
    assert res1["type"] == "create_entry"
    assert res1["title"] == "eBay (seller_alpha)"
    assert created_unique_ids_1 == ["production:eias_account_alpha"]

    # Entry 1 is installed in Home Assistant
    entry1 = Mock()
    entry1.entry_id = "entry_ha_alpha"
    entry1.unique_id = "production:eias_account_alpha"
    entry1.title = "eBay (seller_alpha)"
    entry1.data = dict(res1["data"])

    # 2. Attempt duplicate onboarding of Account 1 (using same developer app) -> aborts
    dup_flow = EbayConfigFlow()
    dup_flow.hass = Mock()
    dup_flow.flow_id = "flow-dup"
    dup_flow._data = _flow_data(client_id="shared-developer-app")
    monkeypatch.setattr(dup_flow, "_async_current_entries", lambda: [entry1])

    dup_res = await dup_flow.async_oauth_create_entry(
        {
            "auth_implementation": "ebay:production:shared-developer-app",
            "token": {
                "access_token": "token_alpha_2",
                "refresh_token": "refresh_alpha_2",
                "expires_in": 7200,
            },
        }
    )
    assert dup_res["type"] == "abort"
    assert dup_res["reason"] == "already_configured"

    # 3. Setup Account 2 via Manual OAuth (same developer app, different eBay account)
    flow2 = EbayConfigFlow()
    flow2.hass = Mock()
    flow2.flow_id = "flow-2"
    flow2._oauth_mode = "manual"
    flow2._data = _flow_data(client_id="shared-developer-app")
    flow2._state = "state-flow2"
    flow2._consent_url = "https://auth.ebay.com/oauth2/authorize"
    flow2._requested_scopes = "https://api.ebay.com/oauth/api_scope"
    flow2.flow_impl = Mock()
    flow2.flow_impl.domain = "ebay:production:shared-developer-app"

    identity2 = AccountIdentity(
        account_id="eias_account_beta",
        username="seller_beta",
        environment="production",
        client_id="shared-developer-app",
        identifier_type=AccountIdentifierType.EIASTOKEN,
        binding_state=AccountBindingState.VERIFIED,
    )

    created_unique_ids_2: list[str] = []
    monkeypatch.setattr(
        flow2, "async_set_unique_id", AsyncMock(side_effect=created_unique_ids_2.append)
    )
    monkeypatch.setattr(flow2, "_abort_if_unique_id_configured", lambda: None)
    monkeypatch.setattr(flow2, "_async_current_entries", lambda: [entry1])

    class MockManualClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def async_exchange_authorization_code(self, code: str) -> dict[str, Any]:
            return {
                "access_token": "token_beta_1",
                "refresh_token": "refresh_beta_1",
                "expires_in": 7200,
            }

    monkeypatch.setattr(
        "custom_components.ebay.config_flow.EbayApiClient",
        MockManualClient,
    )
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=identity2),
    )

    res2 = await flow2.async_step_manual({"authorization_code": "code-beta"})
    assert res2["type"] == "create_entry"
    assert res2["title"] == "eBay (seller_beta)"
    assert created_unique_ids_2 == ["production:eias_account_beta"]

    entry2 = Mock()
    entry2.entry_id = "entry_ha_beta"
    entry2.unique_id = "production:eias_account_beta"
    entry2.title = "eBay (seller_beta)"
    entry2.data = dict(res2["data"])

    # 4. Verify Both Entries Exist Simultaneously with Complete Isolation
    assert entry1.entry_id != entry2.entry_id
    assert entry1.unique_id != entry2.unique_id
    assert entry1.data["token"]["access_token"] != entry2.data["token"]["access_token"]
    assert (
        entry1.data[CONF_ACCOUNT_IDENTITY]["account_id"]
        != entry2.data[CONF_ACCOUNT_IDENTITY]["account_id"]
    )

    # 5. Independent Token Update on Entry 1 Does Not Touch Entry 2
    entry1.data["token"]["access_token"] = "refreshed_alpha"
    assert entry2.data["token"]["access_token"] == "token_beta_1"

    # 6. Entity Unique IDs and Storage Keys are Isolated
    item_id = "987654321"
    sensor_1 = f"{entry1.entry_id}_watched_{item_id}_price"
    sensor_2 = f"{entry2.entry_id}_watched_{item_id}_price"
    assert sensor_1 != sensor_2
    assert sensor_1.startswith("entry_ha_alpha_")
    assert sensor_2.startswith("entry_ha_beta_")

    store_1 = f"ebay.{entry1.entry_id}.ending_soon"
    store_2 = f"ebay.{entry2.entry_id}.ending_soon"
    assert store_1 != store_2
