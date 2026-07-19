"""Tests for eBay sensor lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from unittest.mock import AsyncMock, Mock

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.ebay import sensor as sensor_module
from custom_components.ebay.const import (
    DEFAULT_OPTIONS,
    ENTITY_MODE_BALANCED,
    ENTITY_MODE_DETAILED,
)
from custom_components.ebay.coordinator import _select_item_entities, _pinned_ids
from custom_components.ebay.sensor import (
    DIAGNOSTIC_SENSORS,
    ITEM_SENSOR_FIELDS,
    SUMMARY_SENSORS,
    EbayItemSensor,
    async_setup_entry,
    summary_sensors_for_options,
)

_FIXED_SENSORS = len(summary_sensors_for_options(DEFAULT_OPTIONS)) + len(
    DIAGNOSTIC_SENSORS
)


class _SelectionCoordinator:
    """Minimal coordinator stub with cached selection."""

    def __init__(
        self, *, options: dict[str, Any], data: dict[str, Any] | None = None
    ) -> None:
        self.options = options
        self.data = data
        self.last_update_success = True
        self.ending_soon_threshold_seconds = 3600
        self.selected_item_keys: set[tuple[str, str]] = set()
        self.inactive_item_sensors: dict[str, datetime] = {}
        self._item_entity_cleanup: Callable[..., Any] | None = None
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

    def register_item_entity_cleanup(self, cleanup: Callable[..., Any] | None) -> None:
        self._item_entity_cleanup = cleanup

    async def async_cleanup_inactive_item_entities(self, *, force: bool = True) -> int:
        if self._item_entity_cleanup is None:
            return 0
        return await self._item_entity_cleanup(force=force)

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

    assert [len(batch) for batch in added] == [_FIXED_SENSORS]
    assert listener is not None
    assert len(unloads) == 2

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
        _FIXED_SENSORS,
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


def test_item_sensor_uses_title_name_monetary_and_end_time() -> None:
    """Per-item sensors expose title names, monetary units, and end_time timestamps."""
    end_time = datetime.now(timezone.utc) + timedelta(hours=1)
    coordinator = _SelectionCoordinator(
        options={
            **DEFAULT_OPTIONS,
            "entity_mode": ENTITY_MODE_DETAILED,
            "per_item_cap": 5,
        },
        data={
            "summary": {},
            "selling": {},
            "watched": {
                "001": {
                    "item_id": "001",
                    "title": "Vintage Camera",
                    "current_price": 42.5,
                    "currency": "USD",
                    "end_time": end_time,
                    "seconds_left": 3600,
                },
            },
            "bidding": {},
            "last_successful_update": datetime.now(timezone.utc),
        },
    )
    entry = Mock(entry_id="entry-1")
    price = EbayItemSensor(
        coordinator, entry, "watched", "001", "price", "current_price", None
    )
    ending = EbayItemSensor(
        coordinator, entry, "watched", "001", "end_time", "end_time", None
    )
    seconds = EbayItemSensor(
        coordinator,
        entry,
        "watched",
        "001",
        "seconds_left",
        "seconds_left",
        "s",
    )

    assert price.name == "Vintage Camera Price"
    assert price.has_entity_name is True
    assert price.device_class == SensorDeviceClass.MONETARY
    assert price.native_unit_of_measurement == "USD"
    assert price.extra_state_attributes["item_id"] == "001"

    assert ending.name == "Vintage Camera End Time"
    assert ending.device_class == SensorDeviceClass.TIMESTAMP
    assert ending.native_value == end_time
    assert ending.native_unit_of_measurement is None

    assert seconds.device_class is None
    assert seconds.native_unit_of_measurement == "s"
    assert ("end_time", "end_time", None) in ITEM_SENSOR_FIELDS["watched"]
    assert ("end_time", "end_time", None) in ITEM_SENSOR_FIELDS["selling"]
    assert ("end_time", "end_time", None) in ITEM_SENSOR_FIELDS["bidding"]


def test_setup_retains_stale_registered_item_sensors_during_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-item registry entries outside selection stay during the grace period."""

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
    monkeypatch.setattr(
        sensor_module,
        "async_save_inactive_item_sensors",
        AsyncMock(),
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

    asyncio.run(
        async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities)))
    )

    assert removed == []
    item_ids = {
        sensor.item_id for sensor in added[0] if isinstance(sensor, EbayItemSensor)
    }
    assert item_ids == {"001", "999"}
    assert "entry-1_watched_999_price" in entry.runtime_data.inactive_item_sensors


def test_setup_removes_stale_registered_item_sensors_past_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-item registry entries inactive longer than grace are removed on setup."""

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
    monkeypatch.setattr(
        sensor_module,
        "async_save_inactive_item_sensors",
        AsyncMock(),
    )
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
    coordinator.inactive_item_sensors["entry-1_watched_999_price"] = datetime.now(
        timezone.utc
    ) - timedelta(days=8)
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list[list[Any]] = []

    asyncio.run(
        async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities)))
    )

    assert removed == ["sensor.ebay_watched_999_price"]
    assert {
        sensor.item_id for sensor in added[0] if isinstance(sensor, EbayItemSensor)
    } == {"001"}
    assert "entry-1_watched_999_price" not in coordinator.inactive_item_sensors


def test_selection_priorities_and_modes() -> None:
    """Pinned and actionable items outrank others; modes filter candidates."""
    data = {
        "selling": {
            "s_action": {"item_id": "s_action", "bid_count": 1, "seconds_left": 99999},
            "s_plain": {
                "item_id": "s_plain",
                "bid_count": 0,
                "offers": 0,
                "questions": 0,
                "seconds_left": 99999,
            },
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
    balanced = _select_item_entities(data, ENTITY_MODE_BALANCED, 10, {"pinned"}, 60)
    assert ("watched", "pinned") in balanced
    assert ("selling", "s_action") in balanced
    assert ("watched", "w_soon") in balanced
    assert ("selling", "s_plain") not in balanced
    assert ("watched", "w_plain") not in balanced

    detailed = _select_item_entities(data, ENTITY_MODE_DETAILED, 2, {"pinned"}, 60)
    assert detailed[0] == ("watched", "pinned")
    assert len(detailed) == 2

    minimal = _select_item_entities(data, "minimal", 10, {"pinned"}, 60)
    # minimal still returns candidates from the helper; sensors skip via mode gate
    assert ("watched", "pinned") in minimal


def test_is_item_sensor_unique_id_excludes_summary_sensors() -> None:
    from custom_components.ebay.sensor import _is_item_sensor_unique_id

    entry_id = "entry-1"
    assert _is_item_sensor_unique_id(entry_id, f"{entry_id}_selling_123_bids") is True
    assert (
        _is_item_sensor_unique_id(entry_id, f"{entry_id}_selling_total_bids") is False
    )
    assert (
        _is_item_sensor_unique_id(entry_id, f"{entry_id}_selling_total_views") is False
    )
    assert (
        _is_item_sensor_unique_id(entry_id, f"{entry_id}_active_selling_items") is False
    )


def test_summary_sensor_attributes_include_truncation_and_warnings() -> None:
    from custom_components.ebay.sensor import EbaySummarySensor

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
    assert attrs == {"last_update": None}
    assert "partial_failures" not in attrs

    context_desc = next(
        item for item in SUMMARY_SENSORS if item.key == "refresh_context"
    )
    context = EbaySummarySensor(coordinator, Mock(entry_id="entry-1"), context_desc)
    context_attrs = context.extra_state_attributes
    assert context_attrs["partial_failures"] == ["selling_truncated"]
    assert context_attrs["truncated_collections"] == {"selling": True}
    assert context_attrs["api_warnings"] == [{"code": "1"}]


def test_summary_sensors_are_gated_by_feature_options() -> None:
    from custom_components.ebay.const import (
        CONF_FEEDBACK_ENABLED,
        CONF_FULFILLMENT_ENABLED,
        CONF_MESSAGES_ENABLED,
        CONF_SELLER_STANDARDS_ENABLED,
    )
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.const import PERCENTAGE

    default_keys = {item.key for item in summary_sensors_for_options(DEFAULT_OPTIONS)}
    assert "orders_overdue" not in default_keys
    assert "unread_conversations" not in default_keys
    assert "seller_level" not in default_keys
    assert "watched_items" in default_keys
    assert "active_selling_items" in default_keys
    assert "sold_items_count" in default_keys
    assert "unsold_items_count" in default_keys

    enabled = summary_sensors_for_options(
        {
            **DEFAULT_OPTIONS,
            CONF_FULFILLMENT_ENABLED: True,
            CONF_SELLER_STANDARDS_ENABLED: True,
            CONF_FEEDBACK_ENABLED: True,
            CONF_MESSAGES_ENABLED: True,
        }
    )
    by_key = {item.key: item for item in enabled}
    assert by_key["orders_overdue"].entity_registry_enabled_default is not False
    assert by_key["open_payment_disputes"].entity_registry_enabled_default is not False
    assert by_key["unread_conversations"].entity_registry_enabled_default is not False
    assert by_key["orders_awaiting_shipment"].entity_registry_enabled_default is False
    assert by_key["transaction_defect_rate"].entity_registry_enabled_default is False
    assert by_key["transaction_defect_rate"].native_unit_of_measurement == PERCENTAGE
    assert by_key["late_shipment_rate"].native_unit_of_measurement == PERCENTAGE
    assert by_key["positive_feedback_percent"].native_unit_of_measurement == PERCENTAGE
    assert by_key["seller_level"].device_class == SensorDeviceClass.ENUM
    assert by_key["item_not_received_metric"].device_class == SensorDeviceClass.ENUM
    assert (
        by_key["seller_next_evaluation_date"].device_class
        == SensorDeviceClass.TIMESTAMP
    )


def test_summary_timestamp_and_enum_native_values() -> None:
    from custom_components.ebay.sensor import EbaySummarySensor

    coordinator = _SelectionCoordinator(
        options=dict(DEFAULT_OPTIONS),
        data={
            "summary": {
                "seller_next_evaluation_date": "2026-08-01T00:00:00+00:00",
                "seller_level": "TOP_RATED",
                "positive_feedback_percent": 99.5,
            },
            "selling": {},
            "watched": {},
            "bidding": {},
        },
    )
    next_eval = next(
        item for item in SUMMARY_SENSORS if item.key == "seller_next_evaluation_date"
    )
    level = next(item for item in SUMMARY_SENSORS if item.key == "seller_level")
    percent = next(
        item for item in SUMMARY_SENSORS if item.key == "positive_feedback_percent"
    )
    assert isinstance(
        EbaySummarySensor(coordinator, Mock(entry_id="e"), next_eval).native_value,
        datetime,
    )
    assert (
        EbaySummarySensor(coordinator, Mock(entry_id="e"), level).native_value
        == "TOP_RATED"
    )
    assert (
        EbaySummarySensor(coordinator, Mock(entry_id="e"), percent).native_value == 99.5
    )


def test_unknown_enum_maps_to_unknown_and_preserves_raw() -> None:
    """Unrecognized eBay enums must stay within declared options."""
    from custom_components.ebay.sensor import (
        CUSTOMER_SERVICE_RATING_OPTIONS,
        ENUM_UNKNOWN,
        EbaySummarySensor,
        SELLER_LEVEL_OPTIONS,
    )

    coordinator = _SelectionCoordinator(
        options=dict(DEFAULT_OPTIONS),
        data={
            "summary": {
                "seller_level": "PLATINUM_PLUS",
                "item_not_received_metric": "0.42",
            },
            "selling": {},
            "watched": {},
            "bidding": {},
        },
    )
    level_desc = next(item for item in SUMMARY_SENSORS if item.key == "seller_level")
    inr_desc = next(
        item for item in SUMMARY_SENSORS if item.key == "item_not_received_metric"
    )
    level = EbaySummarySensor(coordinator, Mock(entry_id="e"), level_desc)
    inr = EbaySummarySensor(coordinator, Mock(entry_id="e"), inr_desc)

    assert level.native_value == ENUM_UNKNOWN
    assert level.native_value in (level_desc.options or SELLER_LEVEL_OPTIONS)
    assert level.extra_state_attributes["raw_value"] == "PLATINUM_PLUS"

    assert inr.native_value == ENUM_UNKNOWN
    assert inr.native_value in (inr_desc.options or CUSTOMER_SERVICE_RATING_OPTIONS)
    assert inr.extra_state_attributes["raw_value"] == "0.42"
    assert ENUM_UNKNOWN in SELLER_LEVEL_OPTIONS
    assert ENUM_UNKNOWN in CUSTOMER_SERVICE_RATING_OPTIONS


def test_parse_customer_service_prefers_rating_enum() -> None:
    from custom_components.ebay.seller_ops import parse_customer_service_metric

    parsed = parse_customer_service_metric(
        {
            "dimensionMetrics": [
                {
                    "metrics": [
                        {
                            "avgRatingOrScore": 0.12,
                            "benchmark": {"rating": "AVERAGE"},
                        }
                    ]
                }
            ]
        }
    )
    assert parsed["rating"] == "AVERAGE"


def test_unfetched_seller_ops_summary_uses_none_not_zero() -> None:
    from custom_components.ebay.api import _seller_ops_summary_keys
    from custom_components.ebay.seller_ops import empty_seller_ops

    summary = _seller_ops_summary_keys(empty_seller_ops())
    assert summary["orders_overdue"] is None
    assert summary["open_payment_disputes"] is None
    assert summary["unread_conversations"] is None
    assert summary["seller_standard_at_risk"] is None
    assert summary["recent_positive_feedback"] is None


def test_sold_unsold_window_days_attribute() -> None:
    from custom_components.ebay.api import SOLD_UNSOLD_DURATION_DAYS
    from custom_components.ebay.sensor import EbaySummarySensor

    coordinator = _SelectionCoordinator(
        options=dict(DEFAULT_OPTIONS),
        data={
            "summary": {
                "sold_items_count": 3,
                "sold_unsold_window_days": SOLD_UNSOLD_DURATION_DAYS,
                "last_update": "2026-07-13T00:00:00+00:00",
            },
            "selling": {},
            "watched": {},
            "bidding": {},
        },
    )
    description = next(
        item for item in SUMMARY_SENSORS if item.key == "sold_items_count"
    )
    attrs = EbaySummarySensor(
        coordinator, Mock(entry_id="e"), description
    ).extra_state_attributes
    assert attrs["window_days"] == SOLD_UNSOLD_DURATION_DAYS


def test_should_fetch_sold_unsold_on_disappearance_or_stale() -> None:
    from datetime import datetime, timedelta, timezone

    from custom_components.ebay.api import _should_fetch_sold_unsold

    now = datetime.now(timezone.utc)
    assert _should_fetch_sold_unsold({"a": {}}, {}, None, now=now) is True
    assert (
        _should_fetch_sold_unsold(
            {"a": {}}, {"a": {}}, now - timedelta(hours=1), now=now
        )
        is False
    )
    assert (
        _should_fetch_sold_unsold(
            {"a": {}}, {"a": {}}, now - timedelta(hours=7), now=now
        )
        is True
    )
    assert (
        _should_fetch_sold_unsold({"a": {}}, {}, now - timedelta(hours=1), now=now)
        is True
    )
