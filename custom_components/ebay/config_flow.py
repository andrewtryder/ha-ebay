"""Config flow for the eBay integration."""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
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
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SELLING_ENABLED,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    DEFAULT_SITE_ID,
    DOMAIN,
    ENTITY_MODES,
    ENVIRONMENTS,
)


class EbayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an eBay config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._state = secrets.token_urlsafe(24)
        self._consent_url: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Collect eBay app settings."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = dict(user_input)
            await self.async_set_unique_id(
                f"{user_input[CONF_ENVIRONMENT]}:{user_input[CONF_CLIENT_ID]}"
            )
            self._abort_if_unique_id_configured()
            self._consent_url = build_consent_url(
                user_input[CONF_ENVIRONMENT],
                user_input[CONF_CLIENT_ID],
                user_input[CONF_RUNAME],
                self._state,
            )
            return await self.async_step_oauth()

        schema = vol.Schema(
            {
                vol.Required(CONF_ENVIRONMENT, default="production"): vol.In(ENVIRONMENTS),
                vol.Required(CONF_CLIENT_ID): str,
                vol.Required(CONF_CLIENT_SECRET): str,
                vol.Required(CONF_RUNAME): str,
                vol.Required(CONF_SITE_ID, default=DEFAULT_SITE_ID): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def async_step_oauth(
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
                return self.async_show_form(
                    step_id="oauth",
                    description_placeholders={
                        "consent_url": self._consent_url or "",
                    },
                    data_schema=vol.Schema({vol.Required("authorization_code"): str}),
                    errors=errors,
                )
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
                data = {
                    **self._data,
                    CONF_REFRESH_TOKEN: payload[CONF_REFRESH_TOKEN],
                }
                return self.async_create_entry(
                    title="eBay",
                    data=data,
                    options=DEFAULT_OPTIONS,
                )

        return self.async_show_form(
            step_id="oauth",
            description_placeholders={
                "consent_url": self._consent_url or "",
            },
            data_schema=vol.Schema({vol.Required("authorization_code"): str}),
            errors=errors,
        )

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
