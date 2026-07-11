"""Tests for eBay sensor lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import Mock

import pytest

from custom_components.ebay import sensor as sensor_module
from custom_components.ebay.const import (
    DEFAULT_OPTIONS,
    ENTITY_MODE_BALANCED,
    ENTITY_MODE_DETAILED,
)
from custom_components.ebay.sensor import (
    ITEM_SENSOR_FIELDS,
    SUMMARY_SENSORS,
    EbayItemSensor,
    async_setup_entry,
)


def test_item_sensors_are_added_after_late_coordinator_data() -> None:
    """Per-item sensors are created when selected items appear after setup."""
    listener: Callable[[], None] | None = None
    unloads: list[Callable[[], None]] = []

    class Coordinator:
        data: dict[str, Any] | None = None
        options = {**DEFAULT_OPTIONS, "entity_mode": ENTITY_MODE_BALANCED}
        last_update_success = True
        ending_soon_threshold_seconds = 3600

        def async_add_listener(
            self, update_callback: Callable[[], None], context: Any | None = None
        ) -> Callable[[], None]:
            nonlocal listener
            listener = update_callback
            return lambda: None

    coordinator = Coordinator()
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    entry.async_on_unload.side_effect = unloads.append
    tasks: list[Any] = []
    hass = Mock(data={})
    hass.async_create_task.side_effect = tasks.append
    added: list[list[Any]] = []

    def async_add_entities(entities: Any) -> None:
        added.append(list(entities))

    asyncio.run(async_setup_entry(hass, entry, async_add_entities))

    assert [len(batch) for batch in added] == [len(SUMMARY_SENSORS)]
    assert listener is not None
    assert len(unloads) == 1

    coordinator.data = {
        "summary": {},
        "selling": {
            "123": {
                "item_id": "123",
                "bid_count": 1,
                "end_time": datetime.now(timezone.utc) + timedelta(minutes=30),
                "seconds_left": 1800,
            }
        },
        "watched": {},
        "bidding": {},
        "last_successful_update": datetime.now(timezone.utc),
    }

    listener()
    asyncio.run(tasks.pop(0))
    listener()
    asyncio.run(tasks.pop(0))

    assert [len(batch) for batch in added] == [
        len(SUMMARY_SENSORS),
        len(ITEM_SENSOR_FIELDS["selling"]),
    ]
    assert {sensor.item_id for sensor in added[1]} == {"123"}


def test_item_sensor_availability_follows_current_cap_selection() -> None:
    """Displaced per-item sensors should become unavailable while still present."""

    class Coordinator:
        options = {
            **DEFAULT_OPTIONS,
            "entity_mode": ENTITY_MODE_DETAILED,
            "per_item_cap": 1,
        }
        last_update_success = True
        ending_soon_threshold_seconds = 3600
        data = {
            "summary": {},
            "selling": {},
            "watched": {
                "001": {"item_id": "001", "current_price": 10},
            },
            "bidding": {},
            "last_successful_update": datetime.now(timezone.utc),
        }

        def async_add_listener(
            self, update_callback: Callable[[], None], context: Any | None = None
        ) -> Callable[[], None]:
            return lambda: None

    coordinator = Coordinator()
    entry = Mock(entry_id="entry-1")
    sensor = EbayItemSensor(
        coordinator,
        entry,
        "watched",
        "001",
        "price",
        "current_price",
        None,
    )

    assert sensor.available is True
    assert sensor.native_value == 10

    coordinator.data["watched"]["000"] = {"item_id": "000", "current_price": 20}

    assert sensor.available is False
    assert sensor.native_value is None


def test_setup_removes_stale_registered_item_sensors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-item registry entries outside the current selection should be removed."""

    class Coordinator:
        options = {
            **DEFAULT_OPTIONS,
            "entity_mode": ENTITY_MODE_DETAILED,
            "per_item_cap": 1,
        }
        last_update_success = True
        ending_soon_threshold_seconds = 3600
        data = {
            "summary": {},
            "selling": {},
            "watched": {
                "001": {"item_id": "001", "current_price": 10},
            },
            "bidding": {},
            "last_successful_update": datetime.now(timezone.utc),
        }

        def async_add_listener(
            self, update_callback: Callable[[], None], context: Any | None = None
        ) -> Callable[[], None]:
            return lambda: None

    class RegistryEntries:
        def get_entries_for_config_entry_id(self, config_entry_id: str) -> list[Any]:
            return [
                SimpleNamespace(
                    domain="sensor",
                    platform="ebay",
                    unique_id=f"{config_entry_id}_watched_999_price",
                )
            ]

    removed: list[str] = []
    registry = Mock(entities=RegistryEntries())
    registry.async_get_entity_id.return_value = "sensor.ebay_watched_999_price"
    registry.async_remove.side_effect = removed.append
    monkeypatch.setattr(sensor_module, "_entity_registry", lambda hass: registry)
    monkeypatch.setattr(
        sensor_module, "_current_entity_platform", lambda: Mock(entities={})
    )
    entry = Mock(entry_id="entry-1", runtime_data=Coordinator())
    added: list[list[Any]] = []

    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities))))

    assert removed == ["sensor.ebay_watched_999_price"]
    assert {sensor.item_id for sensor in added[0] if isinstance(sensor, EbayItemSensor)} == {
        "001"
    }
