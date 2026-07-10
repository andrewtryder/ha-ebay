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
    EbayApiClient,
    EbayAuthError,
    build_consent_url,
    extract_authorization_code,
    extract_oauth_callback_params,
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
    CONF_OAUTH_MODE,
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    CONF_POLL_INTERVAL,
    CONF_RUNAME,
    CONF_SELLING_ENABLED,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    DEFAULT_SITE_ID,
    DOMAIN,
    ENTITY_MODES,
    ENVIRONMENTS,
    OAUTH_MODE_CALLBACK,
    OAUTH_MODE_MANUAL,
    OAUTH_MODES,
)
from .oauth2 import (
    DEVELOPER_CONSOLE_URL,
    EbayOAuth2Implementation,
    async_get_ebay_callback_url,
    normalize_new_token,
)

_LOGGER = logging.getLogger(__name__)


class EbayConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle an eBay config flow."""

    DOMAIN = DOMAIN
    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow."""
        super().__init__()
        self._data: dict[str, Any] = {}
        self._state = secrets.token_urlsafe(24)
        self._consent_url: str | None = None

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect eBay app settings and choose OAuth mode."""
        if user_input is not None:
            self._data = dict(user_input)
            oauth_mode = self._data.pop(CONF_OAUTH_MODE, OAUTH_MODE_CALLBACK)
            await self.async_set_unique_id(
                f"{self._data[CONF_ENVIRONMENT]}:{self._data[CONF_CLIENT_ID]}"
            )
            if self.source != config_entries.SOURCE_REAUTH:
                self._abort_if_unique_id_configured()

            self.flow_impl = EbayOAuth2Implementation(self.hass, self._data)
            if oauth_mode == OAUTH_MODE_MANUAL:
                self._consent_url = build_consent_url(
                    self._data[CONF_ENVIRONMENT],
                    self._data[CONF_CLIENT_ID],
                    self._data[CONF_RUNAME],
                    self._state,
                )
                return await self.async_step_manual()
            return await self.async_step_auth()

        defaults = self._reauth_defaults()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_OAUTH_MODE, default=OAUTH_MODE_CALLBACK
                    ): vol.In(OAUTH_MODES),
                    vol.Required(
                        CONF_ENVIRONMENT,
                        default=defaults.get(CONF_ENVIRONMENT, "production"),
                    ): vol.In(ENVIRONMENTS),
                    vol.Required(
                        CONF_CLIENT_ID, default=defaults.get(CONF_CLIENT_ID, "")
                    ): str,
                    vol.Required(
                        CONF_CLIENT_SECRET,
                        default=defaults.get(CONF_CLIENT_SECRET, ""),
                    ): str,
                    vol.Required(
                        CONF_RUNAME, default=defaults.get(CONF_RUNAME, "")
                    ): str,
                    vol.Required(
                        CONF_SITE_ID,
                        default=defaults.get(CONF_SITE_ID, DEFAULT_SITE_ID),
                    ): str,
                }
            ),
            description_placeholders={
                "callback_url": async_get_ebay_callback_url(self.hass),
                "developer_console_url": DEVELOPER_CONSOLE_URL,
            },
        )

    async def async_oauth_create_entry(
        self, data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Create or update an eBay entry after native OAuth completes."""
        entry_data = {
            **self._data,
            "auth_implementation": data["auth_implementation"],
            "token": normalize_new_token(data["token"]),
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
        return await self.async_step_user()

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Accept an OAuth authorization code or callback URL."""
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
                entry_data = {
                    **self._data,
                    "auth_implementation": self.flow_impl.domain,
                    "token": normalize_new_token(payload),
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

    def _reauth_defaults(self) -> dict[str, Any]:
        """Return defaults from the existing entry during reauth."""
        if self.source != config_entries.SOURCE_REAUTH:
            return {}
        return dict(self._get_reauth_entry().data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return options flow."""
        return EbayOptionsFlow(config_entry)


class EbayOptionsFlow(config_entries.OptionsFlow):
    """Handle eBay options."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = {**DEFAULT_OPTIONS, **self._config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL, default=options[CONF_POLL_INTERVAL]
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=1440)),
                vol.Required(
                    CONF_ENDING_SOON_THRESHOLD,
                    default=options[CONF_ENDING_SOON_THRESHOLD],
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                vol.Required(
                    CONF_ENTITY_MODE, default=options[CONF_ENTITY_MODE]
                ): vol.In(ENTITY_MODES),
                vol.Required(
                    CONF_PER_ITEM_CAP, default=options[CONF_PER_ITEM_CAP]
                ): vol.In(ALLOWED_PER_ITEM_CAPS),
                vol.Optional(
                    CONF_PINNED_ITEM_IDS, default=options[CONF_PINNED_ITEM_IDS]
                ): str,
                vol.Required(
                    CONF_ANALYTICS_ENABLED, default=options[CONF_ANALYTICS_ENABLED]
                ): bool,
                vol.Required(
                    CONF_BUYING_ENABLED, default=options[CONF_BUYING_ENABLED]
                ): bool,
                vol.Required(
                    CONF_SELLING_ENABLED, default=options[CONF_SELLING_ENABLED]
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
