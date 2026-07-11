"""Tests for eBay sensor lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from unittest.mock import Mock

from custom_components.ebay import sensor as sensor_module
from custom_components.ebay.const import (
    DEFAULT_OPTIONS,
    ENTITY_MODE_BALANCED,
    ENTITY_MODE_DETAILED,
)
from custom_components.ebay.coordinator import _select_item_entities, _pinned_ids
from custom_components.ebay.sensor import (
    ITEM_SENSOR_FIELDS,
    SUMMARY_SENSORS,
    EbayItemSensor,
    async_setup_entry,
)


class _SelectionCoordinator:
    """Minimal coordinator stub with cached selection."""

    def __init__(self, *, options: dict[str, Any], data: dict[str, Any] | None = None) -> None:
        self.options = options
        self.data = data
        self.last_update_success = True
        self.ending_soon_threshold_seconds = 3600
        self.selected_item_keys: set[tuple[str, str]] = set()
        self.refresh_selected_item_keys()

    def refresh_selected_item_keys(
        self, data: dict[str, Any] | None = None
    ) -> set[tuple[str, str]]:
        payload = data if data is not None else self.data
        mode = self.options["entity_mode"]
        if mode == "minimal" or not payload:
            self.selected_item_keys = set()
            return self.selected_item_keys
        selected = _select_item_entities(
            payload,
            mode,
            int(self.options["per_item_cap"]),
            _pinned_ids(self.options.get("pinned_item_ids", "")),
            self.ending_soon_threshold_seconds,
        )
        self.selected_item_keys = set(selected)
        return self.selected_item_keys

    def async_add_listener(
        self, update_callback: Callable[[], None], context: Any | None = None
    ) -> Callable[[], None]:
        return lambda: None


def test_item_sensors_are_added_after_late_coordinator_data() -> None:
    """Per-item sensors are created when selected items appear after setup."""
    listener: Callable[[], None] | None = None
    unloads: list[Callable[[], None]] = []

    class Coordinator(_SelectionCoordinator):
        def async_add_listener(
            self, update_callback: Callable[[], None], context: Any | None = None
        ) -> Callable[[], None]:
            nonlocal listener
            listener = update_callback
            return lambda: None

    coordinator = Coordinator(
        options={**DEFAULT_OPTIONS, "entity_mode": ENTITY_MODE_BALANCED},
        data=None,
    )
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
    coordinator = _SelectionCoordinator(
        options={
            **DEFAULT_OPTIONS,
            "entity_mode": ENTITY_MODE_DETAILED,
            "per_item_cap": 1,
        },
        data={
            "summary": {},
            "selling": {},
            "watched": {
                "001": {"item_id": "001", "current_price": 10},
            },
            "bidding": {},
            "last_successful_update": datetime.now(timezone.utc),
        },
    )
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
    coordinator.refresh_selected_item_keys()

    assert sensor.available is False
    assert sensor.native_value is None


def test_setup_removes_stale_registered_item_sensors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-item registry entries outside the current selection should be removed."""

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
    entry = Mock(
        entry_id="entry-1",
        runtime_data=_SelectionCoordinator(
            options={
                **DEFAULT_OPTIONS,
                "entity_mode": ENTITY_MODE_DETAILED,
                "per_item_cap": 1,
            },
            data={
                "summary": {},
                "selling": {},
                "watched": {
                    "001": {"item_id": "001", "current_price": 10},
                },
                "bidding": {},
                "last_successful_update": datetime.now(timezone.utc),
            },
        ),
    )
    added: list[list[Any]] = []

    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities))))

    assert removed == ["sensor.ebay_watched_999_price"]
    assert {sensor.item_id for sensor in added[0] if isinstance(sensor, EbayItemSensor)} == {
        "001"
    }


def test_selection_priorities_and_modes() -> None:
    """Pinned and actionable items outrank others; modes filter candidates."""
    data = {
        "selling": {
            "s_action": {"item_id": "s_action", "bid_count": 1, "seconds_left": 99999},
            "s_plain": {"item_id": "s_plain", "bid_count": 0, "offers": 0, "questions": 0, "seconds_left": 99999},
        },
        "watched": {
            "w_soon": {"item_id": "w_soon", "seconds_left": 30},
            "w_plain": {"item_id": "w_plain", "seconds_left": 99999},
            "pinned": {"item_id": "pinned", "seconds_left": 99999},
        },
        "bidding": {
            "b1": {"item_id": "b1", "seconds_left": 99999},
        },
    }
    balanced = _select_item_entities(
        data, ENTITY_MODE_BALANCED, 10, {"pinned"}, 60
    )
    assert ("watched", "pinned") in balanced
    assert ("selling", "s_action") in balanced
    assert ("watched", "w_soon") in balanced
    assert ("selling", "s_plain") not in balanced
    assert ("watched", "w_plain") not in balanced

    detailed = _select_item_entities(
        data, ENTITY_MODE_DETAILED, 2, {"pinned"}, 60
    )
    assert detailed[0] == ("watched", "pinned")
    assert len(detailed) == 2

    minimal = _select_item_entities(
        data, "minimal", 10, {"pinned"}, 60
    )
    # minimal still returns candidates from the helper; sensors skip via mode gate
    assert ("watched", "pinned") in minimal


def test_summary_sensor_attributes_include_truncation_and_warnings() -> None:
    from custom_components.ebay.sensor import EbaySummarySensor, SUMMARY_SENSORS

    coordinator = _SelectionCoordinator(
        options={**DEFAULT_OPTIONS, "entity_mode": ENTITY_MODE_BALANCED},
        data={
            "summary": {
                "active_selling_items": 1,
                "partial_failures": ["selling_truncated"],
                "truncated_collections": {"selling": True},
                "api_warnings": [{"code": "1"}],
                "items_ending_soon": [],
                "items_with_offers": [],
                "items_with_bids": [],
                "items_with_questions": [],
            },
            "selling": {},
            "watched": {},
            "bidding": {},
        },
    )
    description = next(
        item for item in SUMMARY_SENSORS if item.key == "active_selling_items"
    )
    sensor = EbaySummarySensor(coordinator, Mock(entry_id="entry-1"), description)
    attrs = sensor.extra_state_attributes
    assert attrs["partial_failures"] == ["selling_truncated"]
    assert attrs["truncated_collections"] == {"selling": True}
    assert attrs["api_warnings"] == [{"code": "1"}]
