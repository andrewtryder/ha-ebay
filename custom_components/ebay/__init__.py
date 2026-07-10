"""The eBay integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    PLATFORMS,
)
from .oauth2 import (
    EbayOAuth2Implementation,
    auth_implementation_domain,
    legacy_token_from_refresh_token,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up eBay from a config entry."""
    from homeassistant.const import Platform
    from homeassistant.helpers import config_entry_oauth2_flow
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .api import EbayApiClient
    from .coordinator import EbayDataUpdateCoordinator

    options = {**DEFAULT_OPTIONS, **entry.options}
    oauth_session = None
    if "token" in entry.data:
        implementation = EbayOAuth2Implementation(hass, entry.data)
        config_entry_oauth2_flow.async_register_implementation(
            hass, DOMAIN, implementation
        )
        oauth_session = config_entry_oauth2_flow.OAuth2Session(
            hass, entry, implementation
        )
    client = EbayApiClient(
        async_get_clientsession(hass),
        environment=entry.data[CONF_ENVIRONMENT],
        client_id=entry.data[CONF_CLIENT_ID],
        client_secret=entry.data[CONF_CLIENT_SECRET],
        runame=entry.data[CONF_RUNAME],
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        site_id=entry.data[CONF_SITE_ID],
        oauth_session=oauth_session,
    )
    coordinator = EbayDataUpdateCoordinator(hass, entry, client, options)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an eBay config entry."""
    from homeassistant.const import Platform

    coordinator = entry.runtime_data
    coordinator.cancel_scheduled_events()
    return await hass.config_entries.async_unload_platforms(
        entry, [Platform(platform) for platform in PLATFORMS]
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy refresh-token entries to HA OAuth token storage."""
    if entry.version > 2:
        return False

    if entry.version == 1 and "token" not in entry.data:
        refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
        if refresh_token:
            data = dict(entry.data)
            data["auth_implementation"] = auth_implementation_domain(
                data[CONF_ENVIRONMENT], data[CONF_CLIENT_ID]
            )
            data["token"] = legacy_token_from_refresh_token(refresh_token)
            hass.config_entries.async_update_entry(entry, data=data, version=2)
            return True

    if entry.version < 2:
        hass.config_entries.async_update_entry(entry, version=2)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after options change."""
    await hass.config_entries.async_reload(entry.entry_id)
