"""Canonical eBay Account Identity and Multi-Account Isolation Models.

Establishes typed, verified account identity bindings for multiple eBay accounts
under one Home Assistant installation, ensuring complete token, entity, and state
isolation across accounts using the same or different developer applications.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry

from .clients.endpoints import endpoints_for
from .clients.trading import COMPATIBILITY_LEVEL
from .const import (
    CONF_ACCOUNT_IDENTITY,
    CONF_CLIENT_ID,
    CONF_ENVIRONMENT,
    CONF_REAUTH_GENERATION,
    EBAY_XML_NS,
    NS,
)
from .parsers.identity_parity import (
    pseudonymize_identifier,
)
from .parsers.trading_builders import build_get_user_xml
from .parsers.xml import _text

_LOGGER = logging.getLogger(__name__)

# Timeout for user-initiated account identity verification
DEFAULT_IDENTITY_VERIFICATION_TIMEOUT = 15.0


class AccountIdentifierType(StrEnum):
    """Classification of the underlying eBay account identifier."""

    EIASTOKEN = "eias_token"  # Immutable encrypted account token from Trading XML
    USER_ID = "user_id"  # Trading XML UserID / modern opaque user identifier
    LEGACY_APP_ENVIRONMENT = "legacy_app_environment"  # Legacy environment:client_id
    UNKNOWN = "unknown"


class AccountIdentityProvenance(StrEnum):
    """Source of account identity evidence."""

    AUTHENTICATED_GET_USER = "authenticated_get_user"  # Live authenticated XML GetUser
    LEGACY_MIGRATION = "legacy_migration"  # Preserved from pre-M9 ConfigEntry
    UNVERIFIED_OFFLINE = "unverified_offline"  # Offline fixture or synthetic mock


class AccountBindingState(StrEnum):
    """Binding lifecycle state for an account config entry."""

    VERIFIED = "verified"  # Positively bound to an authenticated eBay account
    LEGACY_COMPATIBLE = "legacy_compatible"  # Preserved pre-M9 legacy entry
    UNVERIFIED = "unverified"  # Unverified or pending identification
    CONFLICT = "conflict"  # Conflicting account binding detected (e.g. at reauth)
    FAILED = "failed"  # Verification attempt failed


class IdentityFailureReason(StrEnum):
    """Safe symbolic reason codes for account verification failures."""

    NONE = "none"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    MALFORMED_RESPONSE = "malformed_response"
    MISSING_IDENTIFIER = "missing_identifier"
    ACCOUNT_MISMATCH = "account_mismatch"
    DUPLICATE_ACCOUNT = "duplicate_account"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AccountIdentity:
    """Canonical representation of an authenticated eBay account binding."""

    entry_id: str | None = None
    environment: str = "production"
    client_id: str = ""
    account_id: str | None = (
        None  # Durable identifier: EIASToken if available, else UserID
    )
    identifier_type: AccountIdentifierType = AccountIdentifierType.UNKNOWN
    username: str | None = None  # Observed public display username
    binding_state: AccountBindingState = AccountBindingState.UNVERIFIED
    provenance: AccountIdentityProvenance = AccountIdentityProvenance.UNVERIFIED_OFFLINE
    verified_at: datetime | None = None
    failure_reason: IdentityFailureReason = IdentityFailureReason.NONE
    reauth_generation: int = 0

    @property
    def is_verified(self) -> bool:
        """Return True if account binding is actively verified."""
        return self.binding_state == AccountBindingState.VERIFIED and bool(
            self.account_id
        )

    @property
    def account_id_pseudonym(self) -> str:
        """Return privacy-preserving SHA-256 pseudonym for the stable account ID."""
        return pseudonymize_identifier(self.account_id, prefix="account")

    @property
    def username_pseudonym(self) -> str:
        """Return privacy-preserving SHA-256 pseudonym for display username."""
        return pseudonymize_identifier(self.username, prefix="user")

    def to_config_entry_dict(self) -> dict[str, Any]:
        """Serialize into a JSON-compatible dictionary for ConfigEntry.data storage."""
        return {
            "entry_id": self.entry_id,
            "environment": self.environment,
            "client_id": self.client_id,
            "account_id": self.account_id,
            "identifier_type": str(self.identifier_type),
            "username": self.username,
            "binding_state": str(self.binding_state),
            "provenance": str(self.provenance),
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "failure_reason": str(self.failure_reason),
            "reauth_generation": self.reauth_generation,
        }

    def to_diagnostic_dict(self) -> dict[str, Any]:
        """Return a privacy-safe diagnostic summary with pseudonyms (no raw credentials)."""
        return {
            "environment": self.environment,
            "account_id_pseudonym": self.account_id_pseudonym,
            "username_pseudonym": self.username_pseudonym,
            "identifier_type": str(self.identifier_type),
            "binding_state": str(self.binding_state),
            "provenance": str(self.provenance),
            "is_verified": self.is_verified,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "failure_reason": str(self.failure_reason),
            "reauth_generation": self.reauth_generation,
        }

    @classmethod
    def from_config_entry_dict(
        cls,
        data: dict[str, Any] | None,
        *,
        fallback_entry_id: str | None = None,
        fallback_environment: str = "production",
        fallback_client_id: str = "",
        fallback_reauth_generation: int = 0,
    ) -> AccountIdentity:
        """Deserialize from stored ConfigEntry.data dictionary with safe fallback."""
        if not isinstance(data, dict):
            return cls(
                entry_id=fallback_entry_id,
                environment=fallback_environment,
                client_id=fallback_client_id,
                binding_state=AccountBindingState.LEGACY_COMPATIBLE,
                provenance=AccountIdentityProvenance.LEGACY_MIGRATION,
                reauth_generation=fallback_reauth_generation,
            )

        verified_at: datetime | None = None
        raw_verified_at = data.get("verified_at")
        if isinstance(raw_verified_at, str) and raw_verified_at.strip():
            try:
                verified_at = datetime.fromisoformat(raw_verified_at)
            except ValueError:
                pass

        try:
            id_type = AccountIdentifierType(
                str(data.get("identifier_type", AccountIdentifierType.UNKNOWN))
            )
        except ValueError:
            id_type = AccountIdentifierType.UNKNOWN

        try:
            b_state = AccountBindingState(
                str(data.get("binding_state", AccountBindingState.UNVERIFIED))
            )
        except ValueError:
            b_state = AccountBindingState.UNVERIFIED

        try:
            prov = AccountIdentityProvenance(
                str(
                    data.get("provenance", AccountIdentityProvenance.UNVERIFIED_OFFLINE)
                )
            )
        except ValueError:
            prov = AccountIdentityProvenance.UNVERIFIED_OFFLINE

        try:
            fail_reason = IdentityFailureReason(
                str(data.get("failure_reason", IdentityFailureReason.NONE))
            )
        except ValueError:
            fail_reason = IdentityFailureReason.NONE

        return cls(
            entry_id=data.get("entry_id") or fallback_entry_id,
            environment=data.get("environment") or fallback_environment,
            client_id=data.get("client_id") or fallback_client_id,
            account_id=data.get("account_id"),
            identifier_type=id_type,
            username=data.get("username"),
            binding_state=b_state,
            provenance=prov,
            verified_at=verified_at,
            failure_reason=fail_reason,
            reauth_generation=int(
                data.get("reauth_generation", fallback_reauth_generation)
            ),
        )


def compute_account_unique_id(environment: str, account_id: str) -> str:
    """Return the canonical ConfigEntry unique ID for a verified eBay account.

    Format: <environment>:<account_id>
    """
    clean_env = (environment or "production").strip().lower()
    clean_id = (account_id or "").strip()
    return f"{clean_env}:{clean_id}"


def get_entry_account_identity(entry: ConfigEntry) -> AccountIdentity:
    """Extract or construct the canonical AccountIdentity for a ConfigEntry."""
    entry_data = getattr(entry, "data", {}) or {}
    stored_data = entry_data.get(CONF_ACCOUNT_IDENTITY)
    raw_gen = entry_data.get(CONF_REAUTH_GENERATION, 0)
    reauth_gen = (
        raw_gen if isinstance(raw_gen, int) and not isinstance(raw_gen, bool) else 0
    )

    entry_id = getattr(entry, "entry_id", None)
    if isinstance(stored_data, dict):
        return AccountIdentity.from_config_entry_dict(
            stored_data,
            fallback_entry_id=entry_id,
            fallback_environment=entry_data.get(CONF_ENVIRONMENT, "production"),
            fallback_client_id=entry_data.get(CONF_CLIENT_ID, ""),
            fallback_reauth_generation=reauth_gen,
        )

    # Legacy pre-M9 entry fallback
    return AccountIdentity(
        entry_id=entry_id,
        environment=entry_data.get(CONF_ENVIRONMENT, "production"),
        client_id=entry_data.get(CONF_CLIENT_ID, ""),
        account_id=None,
        identifier_type=AccountIdentifierType.LEGACY_APP_ENVIRONMENT,
        username=None,
        binding_state=AccountBindingState.LEGACY_COMPATIBLE,
        provenance=AccountIdentityProvenance.LEGACY_MIGRATION,
        verified_at=None,
        failure_reason=IdentityFailureReason.NONE,
        reauth_generation=reauth_gen,
    )


def is_same_account(
    identity_a: AccountIdentity | None,
    identity_b: AccountIdentity | None,
) -> bool:
    """Evaluate whether two account identities represent the exact same eBay account.

    Requires positive evidence of matching environment and matching durable account_id,
    or matching public username when account_id is equivalent. Fails closed.
    """
    if identity_a is None or identity_b is None:
        return False

    if identity_a.environment.lower() != identity_b.environment.lower():
        return False

    # 1. Match on durable account_id (e.g. EIASToken or confirmed userId)
    if (
        identity_a.account_id
        and identity_b.account_id
        and identity_a.account_id.strip() == identity_b.account_id.strip()
    ):
        return True

    # 2. Match on verified public username (case-insensitive)
    if (
        identity_a.username
        and identity_b.username
        and identity_a.username.strip().lower() == identity_b.username.strip().lower()
    ):
        return True

    return False


async def async_verify_authenticated_account(
    session: aiohttp.ClientSession,
    *,
    environment: str,
    access_token: str,
    client_id: str = "",
    site_id: str = "0",
    timeout_seconds: float = DEFAULT_IDENTITY_VERIFICATION_TIMEOUT,
) -> AccountIdentity:
    """Verify authenticated eBay account identity via an explicit, read-only GetUser call.

    Occurs exclusively as part of explicit user-initiated onboarding or reauthorization.
    Bound by strict timeout, no raw token logging, and fail-closed error handling.
    """
    if not access_token or not access_token.strip():
        return AccountIdentity(
            environment=environment,
            client_id=client_id,
            binding_state=AccountBindingState.FAILED,
            failure_reason=IdentityFailureReason.AUTH_FAILED,
        )

    endpoints = endpoints_for(environment)
    xml_request = build_get_user_xml()
    headers = {
        "Content-Type": "text/xml;charset=UTF-8",
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
        "X-EBAY-API-CALL-NAME": "GetUser",
        "X-EBAY-API-SITEID": str(site_id),
        "X-EBAY-API-IAF-TOKEN": access_token.strip(),
    }

    try:
        async with session.post(
            endpoints.trading,
            headers=headers,
            data=xml_request.encode("utf-8"),
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as resp:
            if resp.status in (401, 403):
                return AccountIdentity(
                    environment=environment,
                    client_id=client_id,
                    binding_state=AccountBindingState.FAILED,
                    failure_reason=IdentityFailureReason.AUTH_FAILED,
                )
            if resp.status != 200:
                return AccountIdentity(
                    environment=environment,
                    client_id=client_id,
                    binding_state=AccountBindingState.FAILED,
                    failure_reason=IdentityFailureReason.NETWORK_ERROR,
                )
            content = await resp.read()
    except (TimeoutError, asyncio.TimeoutError):
        _LOGGER.warning("Account identity verification timed out")
        return AccountIdentity(
            environment=environment,
            client_id=client_id,
            binding_state=AccountBindingState.FAILED,
            failure_reason=IdentityFailureReason.TIMEOUT,
        )
    except aiohttp.ClientError as err:
        _LOGGER.warning(
            "Account identity verification network error: %s", type(err).__name__
        )
        return AccountIdentity(
            environment=environment,
            client_id=client_id,
            binding_state=AccountBindingState.FAILED,
            failure_reason=IdentityFailureReason.NETWORK_ERROR,
        )

    # Parse and validate response structure fail-closed
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return AccountIdentity(
            environment=environment,
            client_id=client_id,
            binding_state=AccountBindingState.FAILED,
            failure_reason=IdentityFailureReason.MALFORMED_RESPONSE,
        )

    # Qualified root check
    expected_root = f"{{{EBAY_XML_NS}}}GetUserResponse"
    if root.tag != expected_root:
        return AccountIdentity(
            environment=environment,
            client_id=client_id,
            binding_state=AccountBindingState.FAILED,
            failure_reason=IdentityFailureReason.MALFORMED_RESPONSE,
        )

    # Ack status check
    ack = _text(root, "e:Ack")
    if ack not in ("Success", "Warning"):
        return AccountIdentity(
            environment=environment,
            client_id=client_id,
            binding_state=AccountBindingState.FAILED,
            failure_reason=IdentityFailureReason.MALFORMED_RESPONSE,
        )

    # Fatal errors check
    for err_container in root.findall("e:Errors", NS):
        sev = (_text(err_container, "e:SeverityCode") or "").strip().lower()
        if sev == "error":
            return AccountIdentity(
                environment=environment,
                client_id=client_id,
                binding_state=AccountBindingState.FAILED,
                failure_reason=IdentityFailureReason.AUTH_FAILED,
            )

    # Direct User element
    user_elem = root.find(f"{{{EBAY_XML_NS}}}User")
    if user_elem is None:
        return AccountIdentity(
            environment=environment,
            client_id=client_id,
            binding_state=AccountBindingState.FAILED,
            failure_reason=IdentityFailureReason.MISSING_IDENTIFIER,
        )

    raw_user_id = _text(user_elem, "e:UserID")
    username = raw_user_id.strip() if raw_user_id else None
    if not username:
        return AccountIdentity(
            environment=environment,
            client_id=client_id,
            binding_state=AccountBindingState.FAILED,
            failure_reason=IdentityFailureReason.MISSING_IDENTIFIER,
        )

    # Prefer stable EIASToken if present in User element; fall back to UserID
    raw_eias = _text(user_elem, "e:EIASToken")
    if raw_eias and raw_eias.strip():
        account_id = raw_eias.strip()
        id_type = AccountIdentifierType.EIASTOKEN
    else:
        account_id = username
        id_type = AccountIdentifierType.USER_ID

    return AccountIdentity(
        environment=environment,
        client_id=client_id,
        account_id=account_id,
        identifier_type=id_type,
        username=username,
        binding_state=AccountBindingState.VERIFIED,
        provenance=AccountIdentityProvenance.AUTHENTICATED_GET_USER,
        verified_at=datetime.now(timezone.utc),
        failure_reason=IdentityFailureReason.NONE,
    )
