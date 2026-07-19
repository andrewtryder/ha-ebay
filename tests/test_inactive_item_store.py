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


def test_parse_inactive_since_variants() -> None:
    from custom_components.ebay.inactive_item_store import _parse_inactive_since

    naive = datetime(2026, 7, 10, 12, 0)
    assert _parse_inactive_since(naive) == naive.replace(tzinfo=timezone.utc)

    aware = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    assert _parse_inactive_since(aware) == aware

    assert _parse_inactive_since("") is None
    assert _parse_inactive_since(123) is None
    assert _parse_inactive_since("not-a-date") is None

    from_iso_naive = _parse_inactive_since("2026-07-10T12:00:00")
    assert from_iso_naive == datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

    from_iso_aware = _parse_inactive_since("2026-07-10T12:00:00+00:00")
    assert from_iso_aware == datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def test_load_inactive_item_sensors_skips_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = Mock()
    stores: dict[str, dict[str, Any]] = {
        "ebay.entry-1.inactive_item_sensors": {
            "inactive_since": {
                "": "2026-07-10T12:00:00+00:00",
                12: "2026-07-10T12:00:00+00:00",
                "bad": "not-a-date",
                "good": "2026-07-10T12:00:00+00:00",
            }
        }
    }

    class _MemStore:
        def __init__(
            self, _hass: Any, _version: int, key: str, *args: Any, **kwargs: Any
        ) -> None:
            self.key = key

        async def async_load(self) -> dict[str, Any] | None:
            return stores.get(self.key)

    monkeypatch.setattr(
        "custom_components.ebay.inactive_item_store.Store",
        _MemStore,
    )

    loaded = asyncio.run(async_load_inactive_item_sensors(hass, "entry-1"))
    assert list(loaded) == ["good"]


def test_load_inactive_item_sensors_defensive_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = Mock()
    payloads: list[Any] = [None, {"inactive_since": "nope"}, "x"]
    index = {"i": 0}

    class _MemStore:
        def __init__(
            self, _hass: Any, _version: int, key: str, *args: Any, **kwargs: Any
        ) -> None:
            self.key = key

        async def async_load(self) -> Any:
            value = payloads[index["i"]]
            index["i"] += 1
            return value

    monkeypatch.setattr(
        "custom_components.ebay.inactive_item_store.Store",
        _MemStore,
    )

    assert asyncio.run(async_load_inactive_item_sensors(hass, "entry-1")) == {}
    assert asyncio.run(async_load_inactive_item_sensors(hass, "entry-1")) == {}
    assert asyncio.run(async_load_inactive_item_sensors(hass, "entry-1")) == {}
