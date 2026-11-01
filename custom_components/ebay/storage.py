"""Unified Home Assistant Store persistence for config-entry state across restarts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .parsers.xml import parse_ebay_time

STORAGE_VERSION = 1
ENDING_SOON_FIRED_STORAGE_VERSION = STORAGE_VERSION
INACTIVE_ITEM_STORAGE_VERSION = STORAGE_VERSION
REPAIR_STREAKS_STORAGE_VERSION = STORAGE_VERSION

ENDING_SOON_FIRED_RETENTION = timedelta(hours=1)

EndingSoonFiredKey = tuple[str, str, str, int, str]


class EndingSoonFiredKeyDict(TypedDict):
    """Serialized ending-soon fired key."""

    entry_id: str
    event_kind: str
    item_id: str
    threshold: int
    normalized_end_time: str


class EndingSoonFiredStoreData(TypedDict):
    """Persisted fired ending-soon keys for a config entry."""

    fired: list[EndingSoonFiredKeyDict]


class InactiveItemStoreData(TypedDict):
    """Persisted inactive-since timestamps keyed by sensor unique_id."""

    inactive_since: dict[str, str]


class RepairStreaksStoreData(TypedDict):
    """Persisted repair streak counters for a config entry."""

    streaks: dict[str, int]


def _entry_store(hass: HomeAssistant, entry_id: str, suffix: str) -> Store[Any]:
    """Return a versioned Store scoped to a config entry."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.{suffix}")


# ------------------------------------------------------------------
# Ending-Soon Fired Keys Store
# ------------------------------------------------------------------


def ending_soon_fired_store(
    hass: HomeAssistant, entry_id: str
) -> Store[EndingSoonFiredStoreData]:
    """Return the storage helper for an entry's fired ending-soon keys."""
    return _entry_store(hass, entry_id, "ending_soon_fired")


def serialize_ending_soon_fired_key(key: EndingSoonFiredKey) -> EndingSoonFiredKeyDict:
    """Serialize a fired ending-soon key for storage."""
    entry_id, event_kind, item_id, threshold, normalized_end_time = key
    return {
        "entry_id": entry_id,
        "event_kind": event_kind,
        "item_id": item_id,
        "threshold": threshold,
        "normalized_end_time": normalized_end_time,
    }


def deserialize_ending_soon_fired_key(
    raw: Any,
) -> EndingSoonFiredKey | None:
    """Parse one stored fired key, or return None when invalid."""
    if isinstance(raw, str):
        parts = raw.split("|", 4)
        if len(parts) != 5:
            return None
        entry_id, event_kind, item_id, threshold_raw, normalized_end_time = parts
        try:
            threshold = int(threshold_raw)
        except ValueError:
            return None
        if not entry_id or not event_kind or not item_id or not normalized_end_time:
            return None
        return (entry_id, event_kind, item_id, threshold, normalized_end_time)

    if not isinstance(raw, dict):
        return None
    try:
        entry_id = str(raw["entry_id"])
        event_kind = str(raw["event_kind"])
        item_id = str(raw["item_id"])
        threshold = int(raw["threshold"])
        normalized_end_time = str(raw["normalized_end_time"])
    except (KeyError, TypeError, ValueError):
        return None
    if not entry_id or not event_kind or not item_id or not normalized_end_time:
        return None
    return (entry_id, event_kind, item_id, threshold, normalized_end_time)


def prune_ending_soon_fired_keys(
    keys: set[EndingSoonFiredKey],
    now: datetime | None = None,
    *,
    retention: timedelta = ENDING_SOON_FIRED_RETENTION,
) -> set[EndingSoonFiredKey]:
    """Drop fired keys whose listing end_time plus retention has elapsed."""
    now_utc = parse_ebay_time(now) if now is not None else datetime.now(timezone.utc)
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    kept: set[EndingSoonFiredKey] = set()
    for key in keys:
        end_time = parse_ebay_time(key[4])
        if end_time is None:
            continue
        if now_utc < end_time + retention:
            kept.add(key)
    return kept


async def async_load_ending_soon_fired(
    hass: HomeAssistant, entry_id: str
) -> set[EndingSoonFiredKey]:
    """Load persisted fired ending-soon keys for a config entry."""
    data = await ending_soon_fired_store(hass, entry_id).async_load()
    if not isinstance(data, dict):
        return set()
    raw = data.get("fired")
    if not isinstance(raw, list):
        return set()
    keys: set[EndingSoonFiredKey] = set()
    for item in raw:
        parsed = deserialize_ending_soon_fired_key(item)
        if parsed is not None:
            keys.add(parsed)
    pruned = prune_ending_soon_fired_keys(keys)
    if pruned != keys:
        await async_save_ending_soon_fired(hass, entry_id, pruned)
    return pruned


async def async_save_ending_soon_fired(
    hass: HomeAssistant, entry_id: str, keys: set[EndingSoonFiredKey]
) -> None:
    """Persist fired ending-soon keys for a config entry."""
    payload: EndingSoonFiredStoreData = {
        "fired": [serialize_ending_soon_fired_key(key) for key in sorted(keys)]
    }
    await ending_soon_fired_store(hass, entry_id).async_save(payload)


async def async_remove_ending_soon_fired(hass: HomeAssistant, entry_id: str) -> None:
    """Remove persisted fired ending-soon keys for a config entry."""
    await ending_soon_fired_store(hass, entry_id).async_remove()


# ------------------------------------------------------------------
# Inactive Item Sensors Store
# ------------------------------------------------------------------


def inactive_item_store(
    hass: HomeAssistant, entry_id: str
) -> Store[InactiveItemStoreData]:
    """Return the storage helper for an entry's inactive item sensors."""
    return _entry_store(hass, entry_id, "inactive_item_sensors")


def _parse_inactive_since(raw: Any) -> datetime | None:
    """Parse an inactive-since timestamp, or return None when invalid."""
    return parse_ebay_time(raw)


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


# ------------------------------------------------------------------
# Repair Streaks Store
# ------------------------------------------------------------------


def repair_streaks_store(
    hass: HomeAssistant, entry_id: str
) -> Store[RepairStreaksStoreData]:
    """Return the storage helper for an entry's repair streaks."""
    return _entry_store(hass, entry_id, "repair_streaks")


async def async_load_repair_streaks(
    hass: HomeAssistant, entry_id: str
) -> dict[str, int]:
    """Load persisted repair streak counters for a config entry."""
    data = await repair_streaks_store(hass, entry_id).async_load()
    if not isinstance(data, dict):
        return {}
    raw = data.get("streaks")
    if not isinstance(raw, dict):
        return {}
    streaks: dict[str, int] = {}
    for key, value in raw.items():
        try:
            streaks[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return streaks


async def async_save_repair_streaks(
    hass: HomeAssistant, entry_id: str, streaks: dict[str, int]
) -> None:
    """Persist repair streak counters for a config entry."""
    payload: RepairStreaksStoreData = {"streaks": dict(streaks)}
    await repair_streaks_store(hass, entry_id).async_save(payload)


async def async_remove_repair_streaks(hass: HomeAssistant, entry_id: str) -> None:
    """Remove persisted repair streak counters for a config entry."""
    await repair_streaks_store(hass, entry_id).async_remove()
