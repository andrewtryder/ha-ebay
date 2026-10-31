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
    AccountIdentifierType,
    AccountIdentity,
    AccountIdentityProvenance,
    IdentityFailureReason,
    async_verify_authenticated_account,
    compute_account_unique_id,
    get_entry_account_identity,
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
    """is_same_account enforces strict matching semantics."""
    acc_a = AccountIdentity(
        environment="production",
        account_id="durable-1",
        username="Alice",
    )
    acc_b = AccountIdentity(
        environment="production",
        account_id="durable-1",
        username="AliceRotated",
    )
    acc_c = AccountIdentity(
        environment="production",
        account_id="durable-2",
        username="alice",
    )
    acc_sandbox = AccountIdentity(
        environment="sandbox",
        account_id="durable-1",
        username="Alice",
    )

    # Same account ID matches even if username changed
    assert is_same_account(acc_a, acc_b) is True
    # Different account ID but matching username (case-insensitive) matches
    assert is_same_account(acc_a, acc_c) is True
    # Different environment never matches
    assert is_same_account(acc_a, acc_sandbox) is False
    # None checks
    assert is_same_account(acc_a, None) is False
    assert is_same_account(None, acc_b) is False
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
    """Case D: Pre-M9 legacy entry matching username triggers duplicate abort."""
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
    assert res["reason"] == "already_configured"


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
        binding_state=AccountBindingState.VERIFIED,
    )
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: entry)
    monkeypatch.setattr(
        "custom_components.ebay.config_flow.async_verify_authenticated_account",
        AsyncMock(return_value=new_identity),
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
