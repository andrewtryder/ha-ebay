"""The eBay integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EbayApiClient
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    PLATFORMS,
)
from .coordinator import EbayDataUpdateCoordinator

EbayConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: EbayConfigEntry) -> bool:
    """Set up eBay from a config entry."""
    options = {**DEFAULT_OPTIONS, **entry.options}
    client = EbayApiClient(
        async_get_clientsession(hass),
        environment=entry.data[CONF_ENVIRONMENT],
        client_id=entry.data[CONF_CLIENT_ID],
        client_secret=entry.data[CONF_CLIENT_SECRET],
        runame=entry.data[CONF_RUNAME],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        site_id=entry.data[CONF_SITE_ID],
    )
    coordinator = EbayDataUpdateCoordinator(hass, entry, client, options)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EbayConfigEntry) -> bool:
    """Unload an eBay config entry."""
    coordinator = entry.runtime_data
    coordinator.cancel_scheduled_events()
    return await hass.config_entries.async_unload_platforms(
        entry, [Platform(platform) for platform in PLATFORMS]
    )


async def _async_update_listener(hass: HomeAssistant, entry: EbayConfigEntry) -> None:
    """Reload after options change."""
    await hass.config_entries.async_reload(entry.entry_id)
