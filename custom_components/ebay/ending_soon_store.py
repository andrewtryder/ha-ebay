"""Persist fired ending-soon event keys across Home Assistant restarts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

ENDING_SOON_FIRED_STORAGE_VERSION = 1
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


def ending_soon_fired_store(
    hass: HomeAssistant, entry_id: str
) -> Store[EndingSoonFiredStoreData]:
    """Return the storage helper for an entry's fired ending-soon keys."""
    return Store(
        hass,
        ENDING_SOON_FIRED_STORAGE_VERSION,
        f"{DOMAIN}.{entry_id}.ending_soon_fired",
    )


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
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    kept: set[EndingSoonFiredKey] = set()
    for key in keys:
        try:
            end_time = datetime.fromisoformat(key[4])
        except ValueError:
            continue
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        else:
            end_time = end_time.astimezone(timezone.utc)
        if now < end_time + retention:
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
