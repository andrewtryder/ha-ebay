"""Config flow for the eBay integration."""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CORE_SCOPE,
    EbayApiClient,
    EbayAuthError,
    build_consent_url,
    extract_authorization_code,
    extract_oauth_callback_params,
    missing_scopes_for_options,
    resolve_granted_scopes,
    scopes_for_options,
)
from .const import (
    ALLOWED_PER_ITEM_CAPS,
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENDING_SOON_THRESHOLD,
    CONF_ENTITY_MODE,
    CONF_ENVIRONMENT,
    CONF_FEEDBACK_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_OAUTH_SCOPES,
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    CONF_PINNED_ITEM_PRICE_TARGETS,
    CONF_POLL_INTERVAL,
    CONF_RUNAME,
    CONF_SELLING_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_SITE_ID,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
    DEFAULT_OPTIONS,
    DEFAULT_SITE_ID,
    DOMAIN,
    ENTITY_MODES,
    ENVIRONMENTS,
    OAUTH_MODE_CALLBACK,
    OAUTH_MODE_MANUAL,
)
from .options_parse import validate_price_options
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
    VERSION = 4

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
        return self.async_show_menu(
            step_id="user",
            menu_options=["automatic", "manual"],
        )

    async def async_step_authorization_method(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose automatic callback or manual authorization."""
        return await self.async_step_user(user_input)

    async def async_step_automatic(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Use Home Assistant's native OAuth callback."""
        self._oauth_mode = OAUTH_MODE_CALLBACK
        return await self.async_step_prepare_application()

    async def async_step_prepare_application(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show eBay developer-console setup instructions before credentials."""
        if user_input is not None:
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="prepare_application",
            data_schema=vol.Schema({}),
            description_placeholders=self._setup_description_placeholders(),
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect eBay app settings."""
        if user_input is not None:
            self._data, errors = _normalize_credentials(user_input)
            if errors:
                return self.async_show_form(
                    step_id="credentials",
                    data_schema=self._credentials_schema(self._data),
                    errors=errors,
                )
            await self.async_set_unique_id(
                f"{self._data[CONF_ENVIRONMENT]}:{self._data[CONF_CLIENT_ID]}"
            )
            if self.source != config_entries.SOURCE_REAUTH:
                self._abort_if_unique_id_configured()

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
        )

    async def async_oauth_create_entry(
        self, data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Create or update an eBay entry after native OAuth completes."""
        token = normalize_new_token(data["token"])
        entry_data = {
            **self._data,
            "auth_implementation": data["auth_implementation"],
            "token": token,
            CONF_OAUTH_SCOPES: resolve_granted_scopes(self._requested_scopes, token),
        }
        if self.source == config_entries.SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=entry_data
            )
        return self.async_create_entry(
            title="eBay",
            data=entry_data,
            options=DEFAULT_OPTIONS,
        )

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
                token = normalize_new_token(payload)
                entry_data = {
                    **self._data,
                    "auth_implementation": self.flow_impl.domain,
                    "token": token,
                    CONF_OAUTH_SCOPES: resolve_granted_scopes(
                        self._requested_scopes, token
                    ),
                }
                if self.source == config_entries.SOURCE_REAUTH:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data=entry_data
                    )
                return self.async_create_entry(
                    title="eBay",
                    data=entry_data,
                    options=DEFAULT_OPTIONS,
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
                vol.Required(CONF_CLIENT_SECRET, default=""): str,
                vol.Required(
                    CONF_RUNAME,
                    default=defaults.get(CONF_RUNAME, ""),
                ): str,
                vol.Required(
                    CONF_SITE_ID,
                    default=defaults.get(CONF_SITE_ID, DEFAULT_SITE_ID),
                ): str,
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
        """Return defaults from the existing entry during reauth."""
        if self.source != config_entries.SOURCE_REAUTH:
            return {}
        return dict(self._get_reauth_entry().data)

    def _scopes_for_authorization(self) -> str:
        """Return scopes for the current authorize/reauth attempt."""
        if self.source != config_entries.SOURCE_REAUTH:
            return CORE_SCOPE
        entry = self._get_reauth_entry()
        options = {**DEFAULT_OPTIONS, **dict(getattr(entry, "options", {}) or {})}
        return scopes_for_options(options)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return options flow."""
        return EbayOptionsFlow(config_entry)


OPTIONS_MENU = [
    "general",
    "buying_alerts",
    "selling",
    "seller_operations",
    "entities",
    "diagnostics",
    "save",
]

_MISSING_SCOPE_NOTE = "Reauthorization will be required when you save."


class EbayOptionsFlow(config_entries.OptionsFlow):
    """Handle eBay options across menu sections."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._draft: dict[str, Any] | None = None

    def _ensure_draft(self) -> dict[str, Any]:
        """Return the in-memory options draft, seeding it on first use."""
        if self._draft is None:
            self._draft = {**DEFAULT_OPTIONS, **self._config_entry.options}
        return self._draft

    def _missing_scope_note(self, *feature_keys: str) -> str:
        """Return a reauth hint when draft-enabled features lack granted scopes."""
        draft = self._ensure_draft()
        granted = self._config_entry.data.get(CONF_OAUTH_SCOPES)
        missing = missing_scopes_for_options(granted, draft)
        if any(draft.get(key) and key in missing for key in feature_keys):
            return _MISSING_SCOPE_NOTE
        return ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the options section menu."""
        self._ensure_draft()
        return self.async_show_menu(step_id="init", menu_options=OPTIONS_MENU)

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit general polling options."""
        draft = self._ensure_draft()
        if user_input is not None:
            draft.update(user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id="general",
            data_schema=_schema_general(draft),
        )

    async def async_step_buying_alerts(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit buying and price-alert options."""
        draft = self._ensure_draft()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {**draft, **user_input}
            errors = validate_price_options(candidate)
            if not errors:
                draft.update(user_input)
                return await self.async_step_init()
            draft_for_form = candidate
        else:
            draft_for_form = draft
        return self.async_show_form(
            step_id="buying_alerts",
            data_schema=_schema_buying_alerts(draft_for_form),
            errors=errors,
        )

    async def async_step_selling(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit selling and analytics options."""
        draft = self._ensure_draft()
        if user_input is not None:
            draft.update(user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id="selling",
            data_schema=_schema_selling(draft),
            description_placeholders={
                "missing_scope_note": self._missing_scope_note(CONF_ANALYTICS_ENABLED),
            },
        )

    async def async_step_seller_operations(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit seller-operations module toggles."""
        draft = self._ensure_draft()
        if user_input is not None:
            draft.update(user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id="seller_operations",
            data_schema=_schema_seller_operations(draft),
            description_placeholders={
                "missing_scope_note": self._missing_scope_note(
                    CONF_FULFILLMENT_ENABLED,
                    CONF_SELLER_STANDARDS_ENABLED,
                    CONF_FEEDBACK_ENABLED,
                    CONF_MESSAGES_ENABLED,
                ),
            },
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit per-item entity selection options."""
        draft = self._ensure_draft()
        if user_input is not None:
            draft.update(user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id="entities",
            data_schema=_schema_entities(draft),
        )

    async def async_step_diagnostics(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show diagnostics guidance (no editable options)."""
        self._ensure_draft()
        if user_input is not None:
            return await self.async_step_init()
        return self.async_show_form(
            step_id="diagnostics",
            data_schema=vol.Schema({}),
        )

    async def async_step_save(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate the draft and persist options."""
        draft = self._ensure_draft()
        errors = validate_price_options(draft)
        if errors:
            return self.async_show_form(
                step_id="save",
                data_schema=vol.Schema({}),
                errors=errors,
                description_placeholders={
                    "missing_scope_note": self._missing_scope_note(
                        CONF_ANALYTICS_ENABLED,
                        CONF_FULFILLMENT_ENABLED,
                        CONF_SELLER_STANDARDS_ENABLED,
                        CONF_FEEDBACK_ENABLED,
                        CONF_MESSAGES_ENABLED,
                    ),
                },
            )

        result = self.async_create_entry(title="", data=dict(draft))
        granted = self._config_entry.data.get(CONF_OAUTH_SCOPES)
        if missing_scopes_for_options(granted, draft):
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={
                        "source": config_entries.SOURCE_REAUTH,
                        "entry_id": self._config_entry.entry_id,
                    },
                    data=self._config_entry.data,
                )
            )
        return result


def _schema_general(options: dict[str, Any]) -> vol.Schema:
    """Build the General options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_POLL_INTERVAL, default=options[CONF_POLL_INTERVAL]
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=1440)),
        }
    )


def _schema_buying_alerts(options: dict[str, Any]) -> vol.Schema:
    """Build the Buying and alerts options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_BUYING_ENABLED, default=options[CONF_BUYING_ENABLED]
            ): bool,
            vol.Required(
                CONF_ENDING_SOON_THRESHOLD,
                default=options[CONF_ENDING_SOON_THRESHOLD],
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
            vol.Optional(
                CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
                default=options.get(CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT, 0),
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                CONF_WATCHED_PRICE_DROP_THRESHOLD,
                default=options.get(CONF_WATCHED_PRICE_DROP_THRESHOLD, ""),
            ): str,
            vol.Optional(
                CONF_WATCHED_PRICE_DROP_CURRENCY,
                default=options.get(CONF_WATCHED_PRICE_DROP_CURRENCY, ""),
            ): str,
            vol.Optional(
                CONF_PINNED_ITEM_PRICE_TARGETS,
                default=options.get(CONF_PINNED_ITEM_PRICE_TARGETS, ""),
            ): str,
        }
    )


def _schema_selling(options: dict[str, Any]) -> vol.Schema:
    """Build the Selling options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SELLING_ENABLED, default=options[CONF_SELLING_ENABLED]
            ): bool,
            vol.Required(
                CONF_ANALYTICS_ENABLED, default=options[CONF_ANALYTICS_ENABLED]
            ): bool,
        }
    )


def _schema_seller_operations(options: dict[str, Any]) -> vol.Schema:
    """Build the Seller operations options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_FULFILLMENT_ENABLED,
                default=options[CONF_FULFILLMENT_ENABLED],
            ): bool,
            vol.Required(
                CONF_SELLER_STANDARDS_ENABLED,
                default=options[CONF_SELLER_STANDARDS_ENABLED],
            ): bool,
            vol.Required(
                CONF_FEEDBACK_ENABLED, default=options[CONF_FEEDBACK_ENABLED]
            ): bool,
            vol.Required(
                CONF_MESSAGES_ENABLED, default=options[CONF_MESSAGES_ENABLED]
            ): bool,
        }
    )


def _schema_entities(options: dict[str, Any]) -> vol.Schema:
    """Build the Entities options schema."""
    return vol.Schema(
        {
            vol.Required(CONF_ENTITY_MODE, default=options[CONF_ENTITY_MODE]): vol.In(
                ENTITY_MODES
            ),
            vol.Required(CONF_PER_ITEM_CAP, default=options[CONF_PER_ITEM_CAP]): vol.In(
                ALLOWED_PER_ITEM_CAPS
            ),
            vol.Optional(
                CONF_PINNED_ITEM_IDS, default=options[CONF_PINNED_ITEM_IDS]
            ): str,
        }
    )


def _normalize_credentials(
    user_input: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalize credential form input and report blank required fields."""
    data = dict(user_input)
    errors: dict[str, str] = {}
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
    return data, errors
