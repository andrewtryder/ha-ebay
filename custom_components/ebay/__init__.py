"""The eBay integration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed

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
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    ENV_PRODUCTION,
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


def async_get_rate_limit_tracker(
    hass: HomeAssistant,
    environment: str,
    client_id: str,
    entry_id: str | None = None,
) -> Any:
    """Return the shared RateLimitTracker for the developer keyset in this HA instance."""
    from .clients.resilience import RateLimitTracker

    if not hasattr(hass, "data") or not isinstance(getattr(hass, "data", None), dict):
        return RateLimitTracker()

    domain_data = hass.data.setdefault(DOMAIN, {})
    if not isinstance(domain_data, dict):
        return RateLimitTracker()
    trackers = domain_data.setdefault("rate_limit_trackers", {})
    if not isinstance(trackers, dict):
        trackers = {}
        domain_data["rate_limit_trackers"] = trackers

    safe_hash = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:12]
    key = f"{environment}:{safe_hash}"
    if key not in trackers:
        trackers[key] = {
            "tracker": RateLimitTracker(),
            "entries": set(),
            "ref_count": 0,
        }
    if entry_id:
        trackers[key]["entries"].add(entry_id)
        trackers[key]["ref_count"] = len(trackers[key]["entries"])
    else:
        trackers[key]["ref_count"] += 1
    return trackers[key]["tracker"]


def async_release_rate_limit_tracker(
    hass: HomeAssistant,
    environment: str,
    client_id: str,
    entry_id: str | None = None,
) -> None:
    """Release a reference to the keyset tracker; clean up when ref_count reaches 0."""
    if not hasattr(hass, "data") or not isinstance(getattr(hass, "data", None), dict):
        return
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return
    trackers = domain_data.get("rate_limit_trackers")
    if not isinstance(trackers, dict):
        return
    safe_hash = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:12]
    key = f"{environment}:{safe_hash}"
    if key in trackers:
        info = trackers[key]
        if entry_id:
            info["entries"].discard(entry_id)
            info["ref_count"] = len(info["entries"])
            if not info["entries"]:
                trackers.pop(key, None)
        else:
            info["ref_count"] -= 1
            if info["ref_count"] <= 0:
                trackers.pop(key, None)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up eBay from a config entry."""
    from homeassistant.const import Platform
    from homeassistant.helpers import config_entry_oauth2_flow
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .api import EbayApiClient
    from .clients.scopes import has_core_scope
    from .coordinator import EbayDataUpdateCoordinator
    from .storage import (
        async_load_ending_soon_fired,
        async_load_inactive_item_sensors,
        async_load_repair_streaks,
    )

    if not has_core_scope(entry.data.get(CONF_OAUTH_SCOPES)):
        raise ConfigEntryAuthFailed(
            "eBay OAuth is missing the core API scope; reauthorize to continue"
        )

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
    tracker = async_get_rate_limit_tracker(
        hass,
        entry.data[CONF_ENVIRONMENT],
        entry.data[CONF_CLIENT_ID],
        entry_id=entry.entry_id,
    )
    setup_ok = False
    try:
        client = EbayApiClient(
            async_get_clientsession(hass),
            environment=entry.data[CONF_ENVIRONMENT],
            client_id=entry.data[CONF_CLIENT_ID],
            client_secret=entry.data[CONF_CLIENT_SECRET],
            runame=entry.data[CONF_RUNAME],
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
            site_id=entry.data[CONF_SITE_ID],
            oauth_session=oauth_session,
            rate_limit_tracker=tracker,
        )
        coordinator = EbayDataUpdateCoordinator(hass, entry, client, options)
        coordinator._loaded_config = _loaded_config_snapshot(entry, options)
        coordinator._repair_streaks = await async_load_repair_streaks(
            hass, entry.entry_id
        )
        coordinator._fired_ending_soon = await async_load_ending_soon_fired(
            hass, entry.entry_id
        )
        coordinator.inactive_item_sensors = await async_load_inactive_item_sensors(
            hass, entry.entry_id
        )
        await coordinator.async_config_entry_first_refresh()
        coordinator._loaded_config = _loaded_config_snapshot(entry, options)
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(
            entry, [Platform(platform) for platform in PLATFORMS]
        )
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        setup_ok = True
        return True
    finally:
        if not setup_ok:
            async_release_rate_limit_tracker(
                hass,
                entry.data[CONF_ENVIRONMENT],
                entry.data[CONF_CLIENT_ID],
                entry_id=entry.entry_id,
            )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an eBay config entry.

    Repair issues stay in the registry across reload. Streaks and fired
    ending-soon keys are flushed so the next setup restores them immediately.
    """
    from homeassistant.const import Platform

    from .storage import (
        async_save_ending_soon_fired,
        async_save_inactive_item_sensors,
        async_save_repair_streaks,
    )

    coordinator = entry.runtime_data
    if hasattr(coordinator, "async_shutdown"):
        shutdown_res = coordinator.async_shutdown()
        if asyncio.iscoroutine(shutdown_res) or hasattr(shutdown_res, "__await__"):
            await shutdown_res
        elif hasattr(coordinator, "cancel_scheduled_events"):
            coordinator.cancel_scheduled_events()
    elif hasattr(coordinator, "cancel_scheduled_events"):
        coordinator.cancel_scheduled_events()
    await async_save_repair_streaks(hass, entry.entry_id, coordinator._repair_streaks)
    await async_save_ending_soon_fired(
        hass, entry.entry_id, coordinator._fired_ending_soon
    )
    await async_save_inactive_item_sensors(
        hass, entry.entry_id, coordinator.inactive_item_sensors
    )
    coordinator._fired_ending_soon_dirty = False
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    if unload_ok:
        async_release_rate_limit_tracker(
            hass,
            entry.data[CONF_ENVIRONMENT],
            entry.data[CONF_CLIENT_ID],
            entry_id=entry.entry_id,
        )
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up repairs and persisted state when a config entry is removed."""
    from .repairs import async_delete_entry_issues
    from .storage import (
        async_remove_ending_soon_fired,
        async_remove_inactive_item_sensors,
        async_remove_repair_streaks,
    )

    async_delete_entry_issues(hass, entry)
    await async_remove_repair_streaks(hass, entry.entry_id)
    await async_remove_ending_soon_fired(hass, entry.entry_id)
    await async_remove_inactive_item_sensors(hass, entry.entry_id)
    async_release_rate_limit_tracker(
        hass,
        entry.data.get(CONF_ENVIRONMENT, ENV_PRODUCTION),
        entry.data.get(CONF_CLIENT_ID, ""),
        entry_id=entry.entry_id,
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries to the current schema and entity unique IDs."""
    if entry.version > 5:
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

    if entry.version < 4:
        # Drop stale scope marker so setup requires reauth for expanded scopes.
        data = dict(entry.data)
        data.pop(CONF_OAUTH_SCOPES, None)
        hass.config_entries.async_update_entry(entry, data=data, version=4)
        _LOGGER.debug(
            "Migrated eBay config entry for expanded OAuth scopes entry_id=%s to_version=4",
            entry.entry_id,
        )

    if entry.version < 5:
        # Backward-compatible account identity association without rewriting unique IDs.
        data = dict(entry.data)
        if CONF_ACCOUNT_IDENTITY not in data:
            data[CONF_ACCOUNT_IDENTITY] = {
                "entry_id": entry.entry_id,
                "environment": data.get(CONF_ENVIRONMENT, "production"),
                "client_id": data.get(CONF_CLIENT_ID, ""),
                "account_id": data.get(CONF_ACCOUNT_ID),
                "identifier_type": "legacy_app_environment",
                "username": data.get(CONF_ACCOUNT_USERNAME),
                "binding_state": "legacy_compatible",
                "provenance": "legacy_migration",
                "verified_at": None,
                "failure_reason": "none",
                "reauth_generation": data.get(CONF_REAUTH_GENERATION, 0),
            }
        hass.config_entries.async_update_entry(entry, data=data, version=5)
        _LOGGER.debug(
            "Migrated eBay config entry for account identity isolation entry_id=%s to_version=5",
            entry.entry_id,
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


def _loaded_config_snapshot(
    entry: ConfigEntry, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return a comparable snapshot of reload-relevant entry data and options.

    The entire OAuth ``token`` dictionary (including ``access_token``,
    ``refresh_token``, ``expires_at``, and ``expires_in``) and in-flight
    ``pending_options`` drafts are excluded so routine token refreshes—even
    when eBay rotates the refresh token—and cancelled reauth flows do not
    trigger integration reloads. Explicit user-completed reauthorizations are
    tracked via the nonsecret ``reauth_generation`` counter recorded by
    ``EbayConfigFlow._finish_reauth``.
    """
    raw_data = dict(getattr(entry, "data", None) or {})
    raw_data.pop(CONF_PENDING_OPTIONS, None)
    raw_data.pop("manual_authorization", None)
    raw_data.pop("token", None)
    raw_generation = raw_data.pop(CONF_REAUTH_GENERATION, 0)
    reauth_generation = (
        raw_generation
        if isinstance(raw_generation, int) and not isinstance(raw_generation, bool)
        else 0
    )
    resolved_options = (
        dict(options)
        if options is not None
        else {**DEFAULT_OPTIONS, **dict(getattr(entry, "options", None) or {})}
    )
    return {
        "data": raw_data,
        "reauth_generation": reauth_generation,
        "options": resolved_options,
    }


def _config_snapshot_changed(loaded: dict[str, Any], current: dict[str, Any]) -> bool:
    """Return whether a config entry update changed reload-relevant state."""
    if loaded.get("options") != current.get("options"):
        return True
    if loaded.get("data") != current.get("data"):
        return True
    return loaded.get("reauth_generation", 0) != current.get("reauth_generation", 0)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after configuration, credential, or options change."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None:
        current_snapshot = _loaded_config_snapshot(entry)
        loaded_snapshot = getattr(coordinator, "_loaded_config", None)
        if (
            not isinstance(loaded_snapshot, dict)
            and hasattr(coordinator, "options")
            and not getattr(entry, "data", None)
        ):
            loaded_snapshot = _loaded_config_snapshot(
                entry, options=dict(coordinator.options)
            )
        if isinstance(loaded_snapshot, dict) and not _config_snapshot_changed(
            loaded_snapshot, current_snapshot
        ):
            _LOGGER.debug(
                "Skipping reload for entry %s: configuration and options have not changed",
                entry.entry_id,
            )
            return
        coordinator._loaded_config = current_snapshot
    await hass.config_entries.async_reload(entry.entry_id)
