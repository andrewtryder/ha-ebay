"""Config flow for the eBay integration."""

from __future__ import annotations

import logging
import secrets
import unicodedata
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account_identity import (
    AccountEquivalence,
    AccountIdentifierType,
    async_verify_authenticated_account,
    async_verify_entry_existing_identity,
    compare_account_identities,
    compute_account_unique_id,
    get_entry_account_identity,
    is_displayable_username,
)
from .options_flow import EbayOptionsFlow
from .api import EbayApiClient, EbayAuthError
from .clients.endpoints import (
    build_consent_url,
    extract_authorization_code,
    extract_oauth_callback_params,
)
from .clients.scopes import (
    CORE_SCOPE,
    resolve_granted_scopes,
    scopes_for_options,
)
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_IDENTITY,
    CONF_ACCOUNT_USERNAME,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_OAUTH_SCOPES,
    CONF_PENDING_OPTIONS,
    CONF_REAUTH_GENERATION,
    CONF_RUNAME,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    DEFAULT_SITE_ID,
    DOMAIN,
    ENVIRONMENTS,
    OAUTH_MODE_CALLBACK,
    OAUTH_MODE_MANUAL,
)
from .marketplace import MARKETPLACE_NAMES, is_known_site_id
from .oauth2 import (
    DEVELOPER_CONSOLE_URL,
    OAUTH_SETUP_GUIDE_URL,
    PRIVACY_POLICY_URL,
    EbayOAuth2Implementation,
    async_get_ebay_callback_url,
    normalize_new_token,
)

_LOGGER = logging.getLogger(__name__)


class EbayConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle an eBay config flow."""

    DOMAIN = DOMAIN
    VERSION = 5

    def __init__(self) -> None:
        """Initialize the flow."""
        super().__init__()
        self._data: dict[str, Any] = {}
        self._state = secrets.token_urlsafe(24)
        self._consent_url: str | None = None
        self._oauth_mode = OAUTH_MODE_CALLBACK
        self._requested_scopes = CORE_SCOPE

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Start the guided setup flow."""
        self._oauth_mode = OAUTH_MODE_CALLBACK
        if user_input is not None:
            if CONF_CLIENT_ID in user_input or CONF_ENVIRONMENT in user_input:
                return await self.async_step_credentials(user_input)
        return self.async_show_menu(
            step_id="user",
            menu_options=["redirect_guidance", "credentials"],
            description_placeholders=self._setup_description_placeholders(),
        )

    async def async_step_redirect_guidance(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Display exact callback URLs and RuName setup instructions."""
        if user_input is not None:
            return await self.async_step_credentials()
        return self.async_show_form(
            step_id="redirect_guidance",
            data_schema=vol.Schema({}),
            description_placeholders=self._setup_description_placeholders(),
        )

    async def async_step_authorization_method(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Backward-compatible alias for the initial credentials step."""
        return await self.async_step_user(user_input)

    async def async_step_automatic(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Use Home Assistant's native OAuth callback directly."""
        self._oauth_mode = OAUTH_MODE_CALLBACK
        return await self.async_step_credentials(user_input)

    async def async_step_prepare_application(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Backward-compatible alias for the redirect configuration step."""
        return await self.async_step_redirect_guidance(user_input)

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect eBay app settings."""
        if user_input is not None:
            self._data, errors = _normalize_credentials(user_input)
            if user_input.get("manual_authorization") is not None:
                self._oauth_mode = (
                    OAUTH_MODE_MANUAL
                    if user_input["manual_authorization"]
                    else OAUTH_MODE_CALLBACK
                )
            if errors:
                clean_defaults = {
                    k: v for k, v in self._data.items() if k != CONF_CLIENT_SECRET
                }
                return self.async_show_form(
                    step_id="credentials",
                    data_schema=self._credentials_schema(clean_defaults),
                    errors=errors,
                    description_placeholders=self._setup_description_placeholders(),
                )
            if self.source == config_entries.SOURCE_REAUTH:
                entry = self._get_reauth_entry()
                entry_unique_id = getattr(entry, "unique_id", None)
                if entry_unique_id:
                    await self.async_set_unique_id(entry_unique_id)
                    self._abort_if_unique_id_mismatch(reason="wrong_account")

            self._requested_scopes = self._scopes_for_authorization()
            self.flow_impl = EbayOAuth2Implementation(
                self.hass, self._data, scope=self._requested_scopes
            )
            if self._oauth_mode == OAUTH_MODE_MANUAL:
                self._consent_url = build_consent_url(
                    self._data[CONF_ENVIRONMENT],
                    self._data[CONF_CLIENT_ID],
                    self._data[CONF_RUNAME],
                    self._state,
                    scope=self._requested_scopes,
                )
                return await self.async_step_manual()
            return await self.async_step_auth()

        defaults = self._reauth_defaults()
        return self.async_show_form(
            step_id="credentials",
            data_schema=self._credentials_schema(defaults),
            description_placeholders=self._setup_description_placeholders(),
        )

    async def async_oauth_create_entry(
        self, data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Create or update an eBay entry after OAuth completes (native or manual)."""
        token = normalize_new_token(data["token"])
        entry_data = {
            **self._data,
            "auth_implementation": data["auth_implementation"],
            "token": token,
            CONF_OAUTH_SCOPES: resolve_granted_scopes(self._requested_scopes, token),
        }

        # Authenticate and verify account identity with the freshly obtained access token
        access_token = token.get("access_token", "")
        session = async_get_clientsession(self.hass)
        account_identity = await async_verify_authenticated_account(
            session,
            environment=self._data[CONF_ENVIRONMENT],
            access_token=access_token,
            client_id=self._data.get(CONF_CLIENT_ID, ""),
            site_id=self._data.get(CONF_SITE_ID, "0"),
        )

        if self.source == config_entries.SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            entry_id = getattr(entry, "entry_id", None)
            # If identity verification fails during reauth, fail closed
            if not account_identity.is_verified or not account_identity.account_id:
                _LOGGER.warning(
                    "Reauthorization aborted: newly authenticated account verification failed (reason=%s) for entry_id=%s",
                    account_identity.failure_reason,
                    entry_id,
                )
                return self.async_abort(reason="cannot_connect")

            if account_identity.identifier_type != AccountIdentifierType.EIASTOKEN:
                _LOGGER.warning(
                    "Reauthorization aborted: newly authenticated account lacks durable EIASToken identifier for entry_id=%s",
                    entry_id,
                )
                return self.async_abort(reason="account_unverifiable")

            existing_identity = get_entry_account_identity(entry)
            if not existing_identity.is_verified or not existing_identity.account_id:
                existing_identity = await async_verify_entry_existing_identity(
                    session, entry
                )

            equiv = compare_account_identities(existing_identity, account_identity)
            if equiv == AccountEquivalence.DIFFERENT:
                _LOGGER.warning(
                    "Reauthorization aborted: account mismatch detected for entry_id=%s",
                    entry_id,
                )
                return self.async_abort(reason="account_mismatch")

            if equiv != AccountEquivalence.SAME:
                _LOGGER.warning(
                    "Reauthorization aborted: cannot verify identity continuity for entry_id=%s (equivalence=%s)",
                    entry_id,
                    equiv,
                )
                return self.async_abort(reason="account_unverifiable")

            # Update entry data with verified identity
            entry_data[CONF_ACCOUNT_IDENTITY] = account_identity.to_config_entry_dict()
            if account_identity.username:
                entry_data[CONF_ACCOUNT_USERNAME] = account_identity.username
            if account_identity.account_id:
                entry_data[CONF_ACCOUNT_ID] = account_identity.account_id

            # Legitimate rename: stage title update if username is a valid display name
            new_title = (
                f"eBay ({account_identity.username})"
                if is_displayable_username(account_identity.username)
                else None
            )

            return self._finish_reauth(entry_data, title=new_title)

        # New configuration flow:
        if not account_identity.is_verified or not account_identity.account_id:
            _LOGGER.warning(
                "Setup aborted: account identity verification failed (reason=%s)",
                account_identity.failure_reason,
            )
            return self.async_abort(reason="account_unverifiable")

        # Require durable EIASToken for multi-account onboarding
        if account_identity.identifier_type != AccountIdentifierType.EIASTOKEN:
            _LOGGER.warning(
                "Setup aborted: authenticated account lacks durable EIASToken identifier"
            )
            return self.async_abort(reason="account_unverifiable")

        account_unique_id = compute_account_unique_id(
            self._data[CONF_ENVIRONMENT], account_identity.account_id
        )

        # Duplicate detection across all existing entries
        for existing_entry in self._async_current_entries():
            if getattr(existing_entry, "unique_id", None) == account_unique_id:
                return self.async_abort(reason="already_configured")

            existing_id = get_entry_account_identity(existing_entry)
            if not existing_id.is_verified or not existing_id.account_id:
                existing_id = await async_verify_entry_existing_identity(
                    session, existing_entry
                )

            equiv = compare_account_identities(existing_id, account_identity)
            if equiv == AccountEquivalence.SAME:
                _LOGGER.warning(
                    "Setup aborted: matching durable account identity already configured in entry_id=%s",
                    getattr(existing_entry, "entry_id", None),
                )
                return self.async_abort(reason="already_configured")

            if equiv == AccountEquivalence.UNKNOWN:
                _LOGGER.warning(
                    "Setup aborted: cannot verify whether candidate account is distinct from existing entry_id=%s (equivalence=UNKNOWN)",
                    getattr(existing_entry, "entry_id", None),
                )
                return self.async_abort(reason="account_ambiguous")

        await self.async_set_unique_id(account_unique_id)
        self._abort_if_unique_id_configured()

        entry_data[CONF_ACCOUNT_IDENTITY] = account_identity.to_config_entry_dict()
        entry_data[CONF_ACCOUNT_USERNAME] = account_identity.username
        entry_data[CONF_ACCOUNT_ID] = account_identity.account_id

        title = (
            f"eBay ({account_identity.username})"
            if is_displayable_username(account_identity.username)
            else "eBay"
        )
        return self.async_create_entry(
            title=title,
            data=entry_data,
            options=DEFAULT_OPTIONS,
        )

    def async_remove(self) -> None:
        """Discard pending options when reauth is cancelled or fails."""
        if self.source != config_entries.SOURCE_REAUTH:
            return
        try:
            entry = self._get_reauth_entry()
        except config_entries.UnknownEntry:
            return
        if CONF_PENDING_OPTIONS not in entry.data:
            return
        data = {
            key: value
            for key, value in entry.data.items()
            if key != CONF_PENDING_OPTIONS
        }
        self.hass.config_entries.async_update_entry(entry, data=data)

    def _finish_reauth(
        self, entry_data: dict[str, Any], title: str | None = None
    ) -> config_entries.ConfigFlowResult:
        """Update the reauth entry, committing pending options and staged title when present."""
        entry = self._get_reauth_entry()
        pending = entry.data.get(CONF_PENDING_OPTIONS)
        cleaned = {
            key: value
            for key, value in entry_data.items()
            if key != CONF_PENDING_OPTIONS and key != "manual_authorization"
        }
        raw_generation = entry.data.get(CONF_REAUTH_GENERATION)
        previous_generation = (
            raw_generation
            if isinstance(raw_generation, int) and not isinstance(raw_generation, bool)
            else 0
        )
        cleaned[CONF_REAUTH_GENERATION] = previous_generation + 1
        kwargs: dict[str, Any] = {"data": cleaned}
        if title is not None and title != getattr(entry, "title", None):
            kwargs["title"] = title
        if isinstance(pending, dict):
            kwargs["options"] = dict(pending)
        return self.async_update_and_abort(entry, **kwargs)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start reauthorization after token refresh fails."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm before collecting credentials for reauth."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        self._oauth_mode = OAUTH_MODE_CALLBACK
        return await self.async_step_credentials()

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose or complete manual OAuth authorization."""
        if user_input is None and not self._data:
            self._oauth_mode = OAUTH_MODE_MANUAL
            return await self.async_step_credentials()

        errors: dict[str, str] = {}
        if user_input is not None:
            authorization_value = user_input["authorization_code"]
            callback_params = extract_oauth_callback_params(authorization_value)
            state = callback_params.get("state", [None])[0]
            if callback_params and state != self._state:
                errors["base"] = "invalid_state"
                return self._manual_form(errors)
            try:
                code = extract_authorization_code(authorization_value)
                client = EbayApiClient(
                    async_get_clientsession(self.hass),
                    environment=self._data[CONF_ENVIRONMENT],
                    client_id=self._data[CONF_CLIENT_ID],
                    client_secret=self._data[CONF_CLIENT_SECRET],
                    runame=self._data[CONF_RUNAME],
                    refresh_token=None,
                    site_id=self._data[CONF_SITE_ID],
                )
                payload = await client.async_exchange_authorization_code(code)
            except EbayAuthError:
                errors["base"] = "cannot_connect"
            else:
                return await self.async_oauth_create_entry(
                    {
                        "auth_implementation": self.flow_impl.domain,
                        "token": payload,
                    }
                )

        return self._manual_form(errors)

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Backward-compatible alias for in-progress legacy manual flows."""
        return await self.async_step_manual(user_input)

    def _manual_form(self, errors: dict[str, str]) -> config_entries.ConfigFlowResult:
        """Return the manual OAuth fallback form."""
        return self.async_show_form(
            step_id="manual",
            description_placeholders={
                "consent_url": self._consent_url or "",
            },
            data_schema=vol.Schema({vol.Required("authorization_code"): str}),
            errors=errors,
        )

    def _credentials_schema(self, defaults: dict[str, Any]) -> vol.Schema:
        """Return the eBay application credential schema."""
        current_site_id = str(defaults.get(CONF_SITE_ID, DEFAULT_SITE_ID))
        options = [
            selector.SelectOptionDict(value=site_id, label=name)
            for site_id, name in MARKETPLACE_NAMES.items()
        ]
        if current_site_id and current_site_id not in MARKETPLACE_NAMES:
            options.append(
                selector.SelectOptionDict(
                    value=current_site_id, label=f"Site ID {current_site_id}"
                )
            )

        return vol.Schema(
            {
                vol.Required(
                    CONF_ENVIRONMENT,
                    default=defaults.get(CONF_ENVIRONMENT, "production"),
                ): vol.In(ENVIRONMENTS),
                vol.Required(
                    CONF_CLIENT_ID,
                    default=defaults.get(CONF_CLIENT_ID, ""),
                ): str,
                vol.Required(CONF_CLIENT_SECRET): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(
                    CONF_RUNAME,
                    default=defaults.get(CONF_RUNAME, ""),
                ): str,
                vol.Required(
                    CONF_SITE_ID,
                    default=current_site_id,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    "manual_authorization",
                    default=defaults.get(
                        "manual_authorization", self._oauth_mode == OAUTH_MODE_MANUAL
                    ),
                ): bool,
            }
        )

    def _setup_description_placeholders(self) -> dict[str, str]:
        """Return setup instruction URLs for translation placeholders."""
        return {
            "callback_url": async_get_ebay_callback_url(self.hass),
            "developer_console_url": DEVELOPER_CONSOLE_URL,
            "oauth_setup_guide_url": OAUTH_SETUP_GUIDE_URL,
            "privacy_policy_url": PRIVACY_POLICY_URL,
        }

    def _reauth_defaults(self) -> dict[str, Any]:
        """Return safe non-secret defaults from the existing entry during reauth."""
        if self.source != config_entries.SOURCE_REAUTH:
            return {}
        return {
            k: v
            for k, v in self._get_reauth_entry().data.items()
            if k != CONF_CLIENT_SECRET
        }

    def _scopes_for_authorization(self) -> str:
        """Return scopes for the current authorize/reauth attempt."""
        if self.source != config_entries.SOURCE_REAUTH:
            return CORE_SCOPE
        entry = self._get_reauth_entry()
        pending = entry.data.get(CONF_PENDING_OPTIONS)
        if isinstance(pending, dict):
            options = {**DEFAULT_OPTIONS, **pending}
        else:
            options = {**DEFAULT_OPTIONS, **dict(getattr(entry, "options", {}) or {})}
        return scopes_for_options(options)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return options flow."""
        return EbayOptionsFlow(config_entry)


def _has_disallowed_identifier_chars(val: str) -> bool:
    """Check if an identifier contains whitespace, control characters, or non-printable chars."""
    for c in val:
        if c.isspace():
            return True
        code = ord(c)
        if code < 32 or (127 <= code <= 159):
            return True
        cat = unicodedata.category(c)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"):
            return True
    return False


def _normalize_credentials(
    user_input: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalize credential form input, trim whitespace, and validate formats."""
    data = dict(user_input)
    data.pop("manual_authorization", None)
    errors: dict[str, str] = {}

    # Environment validation (fail-closed)
    if CONF_ENVIRONMENT in user_input and user_input[CONF_ENVIRONMENT] is not None:
        raw_env = str(user_input[CONF_ENVIRONMENT]).strip()
        if raw_env in ENVIRONMENTS:
            data[CONF_ENVIRONMENT] = raw_env
        elif raw_env.lower() in ENVIRONMENTS:
            data[CONF_ENVIRONMENT] = raw_env.lower()
        else:
            data[CONF_ENVIRONMENT] = raw_env
            errors[CONF_ENVIRONMENT] = "invalid_environment"
    else:
        data[CONF_ENVIRONMENT] = "production"

    for field in (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_RUNAME,
        CONF_SITE_ID,
    ):
        raw_value = data.get(field)
        value = "" if raw_value is None else str(raw_value).strip()
        data[field] = value
        if not value:
            errors[field] = "required"

    client_id = data.get(CONF_CLIENT_ID, "")
    runame = data.get(CONF_RUNAME, "")

    # Validate RuName format: must not be a URL, Display Title, or contain whitespace/control chars
    if CONF_RUNAME not in errors:
        if (
            "://" in runame
            or runame.startswith("http:")
            or runame.startswith("https:")
            or "/" in runame
        ):
            errors[CONF_RUNAME] = "invalid_runame_url"
        elif " " in runame or runame.lower() in (
            "home assistant",
            "homeassistant",
            "home-assistant",
            "display title",
        ):
            errors[CONF_RUNAME] = "invalid_runame_title"
        elif _has_disallowed_identifier_chars(runame):
            errors[CONF_RUNAME] = "invalid_runame"

    # Validate Client ID format: must not be a URL, an RuName, or contain whitespace/control chars
    if CONF_CLIENT_ID not in errors:
        if (
            "://" in client_id
            or client_id.startswith("http:")
            or client_id.startswith("https:")
            or "/" in client_id
        ):
            errors[CONF_CLIENT_ID] = "invalid_client_id_url"
        elif runame and client_id == runame:
            errors[CONF_CLIENT_ID] = "invalid_client_id_runame"
        elif _has_disallowed_identifier_chars(client_id):
            errors[CONF_CLIENT_ID] = "invalid_client_id"

    if (
        CONF_SITE_ID in data
        and data[CONF_SITE_ID]
        and not is_known_site_id(data[CONF_SITE_ID])
    ):
        errors[CONF_SITE_ID] = "invalid_site_id"
    return data, errors
