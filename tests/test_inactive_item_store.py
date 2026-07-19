"""Tests for persisted inactive item sensor timestamps."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from custom_components.ebay.inactive_item_store import (
    async_load_inactive_item_sensors,
    async_remove_inactive_item_sensors,
    async_save_inactive_item_sensors,
    inactive_item_store,
)


def test_inactive_item_store_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
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
        "custom_components.ebay.inactive_item_store.Store",
        _MemStore,
    )

    entry_id = "entry-1"
    when = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    inactive = {"entry-1_selling_1_bids": when}
    asyncio.run(async_save_inactive_item_sensors(hass, entry_id, inactive))
    loaded = asyncio.run(async_load_inactive_item_sensors(hass, entry_id))
    assert loaded == inactive
    assert inactive_item_store(hass, entry_id).key.endswith("inactive_item_sensors")

    asyncio.run(async_remove_inactive_item_sensors(hass, entry_id))
    assert asyncio.run(async_load_inactive_item_sensors(hass, entry_id)) == {}
