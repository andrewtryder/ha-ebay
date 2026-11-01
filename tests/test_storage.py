"""Tests for persisted config-entry storage (ending-soon keys, inactive items, repair streaks)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from custom_components.ebay.storage import (
    _parse_inactive_since,
    async_load_ending_soon_fired,
    async_load_inactive_item_sensors,
    async_remove_ending_soon_fired,
    async_remove_inactive_item_sensors,
    async_save_ending_soon_fired,
    async_save_inactive_item_sensors,
    deserialize_ending_soon_fired_key,
    ending_soon_fired_store,
    inactive_item_store,
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
        "custom_components.ebay.storage.Store",
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
        "custom_components.ebay.storage.Store",
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


def test_deserialize_ending_soon_fired_key_rejects_invalid() -> None:
    assert deserialize_ending_soon_fired_key("a|b|c") is None
    assert deserialize_ending_soon_fired_key("a|b|c|x|end") is None
    assert deserialize_ending_soon_fired_key("||c|1|end") is None
    assert deserialize_ending_soon_fired_key(None) is None
    assert deserialize_ending_soon_fired_key(12) is None
    assert deserialize_ending_soon_fired_key({"entry_id": "e"}) is None
    assert (
        deserialize_ending_soon_fired_key(
            {
                "entry_id": "",
                "event_kind": "watching",
                "item_id": "1",
                "threshold": 60,
                "normalized_end_time": "2026-07-18T12:00:00+00:00",
            }
        )
        is None
    )
    assert (
        deserialize_ending_soon_fired_key(
            {
                "entry_id": "e",
                "event_kind": "watching",
                "item_id": "1",
                "threshold": "nope",
                "normalized_end_time": "2026-07-18T12:00:00+00:00",
            }
        )
        is None
    )


def test_deserialize_ending_soon_fired_key_from_dict() -> None:
    key = deserialize_ending_soon_fired_key(
        {
            "entry_id": "e",
            "event_kind": "watching",
            "item_id": "1",
            "threshold": 3600,
            "normalized_end_time": "2026-07-18T12:00:00+00:00",
        }
    )
    assert key == ("e", "watching", "1", 3600, "2026-07-18T12:00:00+00:00")


def test_prune_ending_soon_fired_keys_handles_naive_and_invalid() -> None:
    naive_now = datetime(2026, 7, 18, 12, 0)  # noqa: DTZ001
    end_time = datetime(2026, 7, 18, 11, 30, tzinfo=timezone.utc)
    key = ("e", "watching", "1", 3600, end_time.isoformat())
    naive_end = ("e", "watching", "2", 3600, "2026-07-18T11:30:00")
    invalid = ("e", "watching", "3", 3600, "not-a-date")
    kept = prune_ending_soon_fired_keys({key, naive_end, invalid}, naive_now)
    assert kept == {key, naive_end}


def test_load_ending_soon_fired_defensive_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass = Mock()
    payloads: list[Any] = [
        None,
        {"fired": "nope"},
        {
            "fired": [
                "bad",
                {
                    "entry_id": "e",
                    "event_kind": "watching",
                    "item_id": "1",
                    "threshold": 3600,
                    "normalized_end_time": (
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    ).isoformat(),
                },
            ]
        },
    ]
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

        async def async_save(self, data: dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "custom_components.ebay.storage.Store",
        _MemStore,
    )

    assert asyncio.run(async_load_ending_soon_fired(hass, "entry-1")) == set()
    assert asyncio.run(async_load_ending_soon_fired(hass, "entry-1")) == set()
    loaded = asyncio.run(async_load_ending_soon_fired(hass, "entry-1"))
    assert len(loaded) == 1


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
        "custom_components.ebay.storage.Store",
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
    naive = datetime(2026, 7, 10, 12, 0)  # noqa: DTZ001
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
        "custom_components.ebay.storage.Store",
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
        "custom_components.ebay.storage.Store",
        _MemStore,
    )

    assert asyncio.run(async_load_inactive_item_sensors(hass, "entry-1")) == {}
    assert asyncio.run(async_load_inactive_item_sensors(hass, "entry-1")) == {}
    assert asyncio.run(async_load_inactive_item_sensors(hass, "entry-1")) == {}
