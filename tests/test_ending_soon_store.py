"""Tests for persisted ending-soon fired keys."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from custom_components.ebay.ending_soon_store import (
    async_load_ending_soon_fired,
    async_remove_ending_soon_fired,
    async_save_ending_soon_fired,
    deserialize_ending_soon_fired_key,
    ending_soon_fired_store,
    prune_ending_soon_fired_keys,
    serialize_ending_soon_fired_key,
)


def test_ending_soon_fired_store_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    hass = Mock()
    stores: dict[str, dict[str, Any]] = {}

    class _MemStore:
        def __init__(
            self, _hass: Any, _version: int, key: str, *args: Any, **kwargs: Any
        ) -> None:
            self.key = key

        async def async_load(self) -> dict[str, Any] | None:
            return stores.get(self.key)

        async def async_save(self, data: dict[str, Any]) -> None:
            stores[self.key] = data

        async def async_remove(self) -> None:
            stores.pop(self.key, None)

    monkeypatch.setattr(
        "custom_components.ebay.ending_soon_store.Store",
        _MemStore,
    )

    entry_id = "entry-1"
    end_time = datetime.now(timezone.utc) + timedelta(hours=2)
    key = (
        entry_id,
        "watching",
        "item-1",
        3600,
        end_time.isoformat(),
    )
    asyncio.run(async_save_ending_soon_fired(hass, entry_id, {key}))
    loaded = asyncio.run(async_load_ending_soon_fired(hass, entry_id))
    assert loaded == {key}
    assert ending_soon_fired_store(hass, entry_id).key.endswith("ending_soon_fired")

    asyncio.run(async_remove_ending_soon_fired(hass, entry_id))
    assert asyncio.run(async_load_ending_soon_fired(hass, entry_id)) == set()


def test_serialize_deserialize_joined_string() -> None:
    key = ("entry-1", "bidding", "99", 1800, "2026-07-18T20:00:00+00:00")
    assert (
        deserialize_ending_soon_fired_key(
            "entry-1|bidding|99|1800|2026-07-18T20:00:00+00:00"
        )
        == key
    )
    assert (
        deserialize_ending_soon_fired_key(serialize_ending_soon_fired_key(key)) == key
    )


def test_prune_ending_soon_fired_keys_keeps_until_retention() -> None:
    end_time = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    key = ("entry-1", "watching", "1", 3600, end_time.isoformat())
    still_kept = prune_ending_soon_fired_keys({key}, end_time + timedelta(minutes=59))
    assert still_kept == {key}
    pruned = prune_ending_soon_fired_keys({key}, end_time + timedelta(hours=1))
    assert pruned == set()


def test_load_prunes_expired_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    hass = Mock()
    stores: dict[str, dict[str, Any]] = {}

    class _MemStore:
        def __init__(
            self, _hass: Any, _version: int, key: str, *args: Any, **kwargs: Any
        ) -> None:
            self.key = key

        async def async_load(self) -> dict[str, Any] | None:
            return stores.get(self.key)

        async def async_save(self, data: dict[str, Any]) -> None:
            stores[self.key] = data

        async def async_remove(self) -> None:
            stores.pop(self.key, None)

    monkeypatch.setattr(
        "custom_components.ebay.ending_soon_store.Store",
        _MemStore,
    )

    entry_id = "entry-1"
    expired_end = datetime.now(timezone.utc) - timedelta(hours=2)
    active_end = datetime.now(timezone.utc) + timedelta(hours=1)
    expired = (
        entry_id,
        "watching",
        "old",
        3600,
        expired_end.isoformat(),
    )
    active = (
        entry_id,
        "watching",
        "new",
        3600,
        active_end.isoformat(),
    )
    asyncio.run(async_save_ending_soon_fired(hass, entry_id, {expired, active}))
    loaded = asyncio.run(async_load_ending_soon_fired(hass, entry_id))
    assert loaded == {active}
