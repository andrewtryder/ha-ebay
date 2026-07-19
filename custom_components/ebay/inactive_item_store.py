"""Persist inactive per-item sensor unique IDs across Home Assistant restarts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

INACTIVE_ITEM_STORAGE_VERSION = 1


class InactiveItemStoreData(TypedDict):
    """Persisted inactive-since timestamps keyed by sensor unique_id."""

    inactive_since: dict[str, str]


def inactive_item_store(
    hass: HomeAssistant, entry_id: str
) -> Store[InactiveItemStoreData]:
    """Return the storage helper for an entry's inactive item sensors."""
    return Store(
        hass,
        INACTIVE_ITEM_STORAGE_VERSION,
        f"{DOMAIN}.{entry_id}.inactive_item_sensors",
    )


def _parse_inactive_since(raw: Any) -> datetime | None:
    """Parse an inactive-since timestamp, or return None when invalid."""
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def async_load_inactive_item_sensors(
    hass: HomeAssistant, entry_id: str
) -> dict[str, datetime]:
    """Load persisted inactive-since timestamps for a config entry."""
    data = await inactive_item_store(hass, entry_id).async_load()
    if not isinstance(data, dict):
        return {}
    raw = data.get("inactive_since")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, datetime] = {}
    for unique_id, value in raw.items():
        if not isinstance(unique_id, str) or not unique_id:
            continue
        parsed = _parse_inactive_since(value)
        if parsed is not None:
            result[unique_id] = parsed
    return result


async def async_save_inactive_item_sensors(
    hass: HomeAssistant, entry_id: str, inactive: dict[str, datetime]
) -> None:
    """Persist inactive-since timestamps for a config entry."""
    payload: InactiveItemStoreData = {
        "inactive_since": {
            unique_id: when.isoformat() for unique_id, when in sorted(inactive.items())
        }
    }
    await inactive_item_store(hass, entry_id).async_save(payload)


async def async_remove_inactive_item_sensors(
    hass: HomeAssistant, entry_id: str
) -> None:
    """Remove persisted inactive item sensor state for a config entry."""
    await inactive_item_store(hass, entry_id).async_remove()
