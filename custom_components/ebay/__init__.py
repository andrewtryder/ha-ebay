"""The eBay integration."""

from __future__ import annotations

import logging
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

_LOGGER = logging.getLogger(__name__)


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
    """Migrate config entries to the current schema and entity unique IDs."""
    if entry.version > 3:
        _LOGGER.debug(
            "Rejecting eBay config entry migration entry_id=%s version=%s",
            entry.entry_id,
            entry.version,
        )
        return False

    original_version = entry.version
    _LOGGER.debug(
        "Starting eBay config entry migration entry_id=%s from_version=%s",
        entry.entry_id,
        original_version,
    )

    if entry.version < 2:
        data = dict(entry.data)
        if "token" not in data:
            refresh_token = data.get(CONF_REFRESH_TOKEN)
            if refresh_token:
                data["auth_implementation"] = auth_implementation_domain(
                    data[CONF_ENVIRONMENT], data[CONF_CLIENT_ID]
                )
                data["token"] = legacy_token_from_refresh_token(refresh_token)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.debug(
            "Migrated eBay config entry token schema entry_id=%s to_version=2",
            entry.entry_id,
        )

    renamed_count = 0
    if entry.version < 3:
        renamed_count = await _async_migrate_renamed_sensor_unique_ids(hass, entry)
        hass.config_entries.async_update_entry(entry, version=3)
        _LOGGER.debug(
            "Migrated eBay config entry sensor unique IDs entry_id=%s renamed_count=%s to_version=3",
            entry.entry_id,
            renamed_count,
        )

    _LOGGER.debug(
        "Completed eBay config entry migration entry_id=%s from_version=%s to_version=%s renamed_count=%s",
        entry.entry_id,
        original_version,
        entry.version,
        renamed_count,
    )

    return True


async def _async_migrate_renamed_sensor_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry
) -> int:
    """Rename summary and per-item sensor unique IDs introduced before release."""
    from homeassistant.helpers import entity_registry as er

    renamed_count = 0

    def _update_unique_id(entity_entry: er.RegistryEntry) -> dict[str, str] | None:
        nonlocal renamed_count
        new_unique_id = _renamed_sensor_unique_id(
            entry.entry_id, entity_entry.unique_id
        )
        if new_unique_id is None:
            return None
        renamed_count += 1
        return {"new_unique_id": new_unique_id}

    await er.async_migrate_entries(hass, entry.entry_id, _update_unique_id)
    return renamed_count


def _renamed_sensor_unique_id(entry_id: str, unique_id: str) -> str | None:
    """Return the replacement unique ID for a renamed sensor, if any."""
    if unique_id == f"{entry_id}_selling_total_sold":
        return f"{entry_id}_active_listings_quantity_sold"

    legacy_suffix = "_analytics_views"
    if unique_id.startswith(f"{entry_id}_selling_") and unique_id.endswith(
        legacy_suffix
    ):
        return f"{unique_id[: -len(legacy_suffix)]}_analytics_views_30d"

    return None


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after options change."""
    await hass.config_entries.async_reload(entry.entry_id)
