"""Sensor entities for eBay telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ENTITY_MODE,
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    DOMAIN,
    ENTITY_MODE_DETAILED,
    ENTITY_MODE_MINIMAL,
)
from .coordinator import EbayDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class EbaySummarySensorDescription(SensorEntityDescription):
    """Describe a summary sensor."""

    value_key: str


SUMMARY_SENSORS = [
    EbaySummarySensorDescription(
        key="active_selling_items",
        translation_key="active_selling_items",
        value_key="active_selling_items",
    ),
    EbaySummarySensorDescription(
        key="watched_items", translation_key="watched_items", value_key="watched_items"
    ),
    EbaySummarySensorDescription(
        key="bidding_items", translation_key="bidding_items", value_key="bidding_items"
    ),
    EbaySummarySensorDescription(
        key="selling_total_bids",
        translation_key="selling_total_bids",
        value_key="selling_total_bids",
    ),
    EbaySummarySensorDescription(
        key="selling_total_offers",
        translation_key="selling_total_offers",
        value_key="selling_total_offers",
    ),
    EbaySummarySensorDescription(
        key="selling_total_sold",
        translation_key="selling_total_sold",
        value_key="selling_total_sold",
    ),
    EbaySummarySensorDescription(
        key="selling_total_watchers",
        translation_key="selling_total_watchers",
        value_key="selling_total_watchers",
    ),
    EbaySummarySensorDescription(
        key="selling_total_views",
        translation_key="selling_total_views",
        value_key="selling_total_views",
    ),
    EbaySummarySensorDescription(
        key="watched_ending_soon",
        translation_key="watched_ending_soon",
        value_key="watched_ending_soon",
    ),
    EbaySummarySensorDescription(
        key="selling_ending_soon",
        translation_key="selling_ending_soon",
        value_key="selling_ending_soon",
    ),
]

ITEM_SENSOR_FIELDS: dict[str, list[tuple[str, str, str | None]]] = {
    "selling": [
        ("price", "current_price", None),
        ("views", "views", None),
        ("analytics_views", "analytics_views", None),
        ("watchers", "watchers", None),
        ("bids", "bid_count", None),
        ("offers", "offers", None),
        ("questions", "questions", None),
        ("seconds_left", "seconds_left", UnitOfTime.SECONDS),
        ("quantity_available", "quantity_available", None),
        ("quantity_sold", "quantity_sold", None),
    ],
    "watched": [
        ("price", "current_price", None),
        ("bids", "bid_count", None),
        ("seconds_left", "seconds_left", UnitOfTime.SECONDS),
    ],
    "bidding": [
        ("current_price", "current_price", None),
        ("max_bid", "max_bid", None),
        ("seconds_left", "seconds_left", UnitOfTime.SECONDS),
    ],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up eBay sensors."""
    coordinator: EbayDataUpdateCoordinator = entry.runtime_data
    known_item_ids: set[str] = set()

    def _new_item_sensors() -> list["EbayItemSensor"]:
        sensors: list[EbayItemSensor] = []
        for sensor in _item_sensors(coordinator, entry):
            unique_id = sensor.unique_id
            if unique_id is None or unique_id in known_item_ids:
                continue
            known_item_ids.add(unique_id)
            sensors.append(sensor)
        return sensors

    entities: list[SensorEntity] = [
        EbaySummarySensor(coordinator, entry, description)
        for description in SUMMARY_SENSORS
    ]
    entities.extend(_new_item_sensors())
    async_add_entities(entities)

    @callback
    def _handle_coordinator_update() -> None:
        new_sensors = _new_item_sensors()
        if new_sensors:
            async_add_entities(new_sensors)

    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))


def _item_sensors(
    coordinator: EbayDataUpdateCoordinator, entry: ConfigEntry
) -> list["EbayItemSensor"]:
    mode = coordinator.options[CONF_ENTITY_MODE]
    if mode == ENTITY_MODE_MINIMAL or not coordinator.data:
        return []
    selected = _select_item_entities(
        coordinator.data,
        mode,
        int(coordinator.options[CONF_PER_ITEM_CAP]),
        _pinned_ids(coordinator.options.get(CONF_PINNED_ITEM_IDS, "")),
        coordinator.ending_soon_threshold_seconds,
    )
    sensors: list[EbayItemSensor] = []
    for kind, item_id in selected:
        for suffix, field, unit in ITEM_SENSOR_FIELDS[kind]:
            sensors.append(EbayItemSensor(coordinator, entry, kind, item_id, suffix, field, unit))
    return sensors


def _pinned_ids(value: str) -> set[str]:
    return {part.strip() for part in value.replace("\n", ",").split(",") if part.strip()}


def _select_item_entities(
    data: dict[str, Any],
    mode: str,
    cap: int,
    pinned: set[str],
    ending_soon_threshold: int,
) -> list[tuple[str, str]]:
    candidates: list[tuple[int, str, str]] = []
    for kind in ("selling", "watched", "bidding"):
        for item_id, item in data.get(kind, {}).items():
            priority = 100
            if item_id in pinned:
                priority = 0
            elif kind == "selling" and (
                (item.get("bid_count") or 0) > 0
                or (item.get("offers") or 0) > 0
                or (item.get("questions") or 0) > 0
            ):
                priority = 10
            elif (
                item.get("seconds_left") is not None
                and 0 < item["seconds_left"] <= ending_soon_threshold
            ):
                priority = 20
            elif mode != ENTITY_MODE_DETAILED:
                continue
            candidates.append((priority, kind, item_id))
    candidates.sort(key=lambda value: (value[0], value[1], value[2]))
    return [(kind, item_id) for _, kind, item_id in candidates[:cap]]


class EbaySummarySensor(CoordinatorEntity[EbayDataUpdateCoordinator], SensorEntity):
    """Stable eBay summary sensor."""

    entity_description: EbaySummarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EbayDataUpdateCoordinator,
        entry: ConfigEntry,
        description: EbaySummarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
            "manufacturer": "eBay",
        }

    @property
    def native_value(self) -> int | None:
        """Return the summary value."""
        return self.coordinator.data["summary"].get(self.entity_description.value_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return bounded summary context."""
        summary = self.coordinator.data["summary"]
        return {
            "items_ending_soon": summary.get("items_ending_soon"),
            "items_with_offers": summary.get("items_with_offers"),
            "items_with_bids": summary.get("items_with_bids"),
            "items_with_questions": summary.get("items_with_questions"),
            "highest_watcher_count_item": summary.get("highest_watcher_count_item"),
            "highest_view_count_item": summary.get("highest_view_count_item"),
            "last_update": summary.get("last_update"),
            "last_successful_update": summary.get("last_successful_update"),
            "partial_failures": summary.get("partial_failures"),
        }


class EbayItemSensor(CoordinatorEntity[EbayDataUpdateCoordinator], SensorEntity):
    """Optional capped per-item sensor."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: EbayDataUpdateCoordinator,
        entry: ConfigEntry,
        kind: str,
        item_id: str,
        suffix: str,
        field: str,
        unit: str | None,
    ) -> None:
        super().__init__(coordinator)
        self.kind = kind
        self.item_id = item_id
        self.field = field
        self._attr_name = f"eBay {kind} {item_id} {suffix}"
        self._attr_unique_id = f"{entry.entry_id}_{kind}_{item_id}_{suffix}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
            "manufacturer": "eBay",
        }

    @property
    def _item(self) -> dict[str, Any] | None:
        if not self._is_selected:
            return None
        return self.coordinator.data.get(self.kind, {}).get(self.item_id)

    @property
    def _is_selected(self) -> bool:
        selected = _select_item_entities(
            self.coordinator.data,
            self.coordinator.options[CONF_ENTITY_MODE],
            int(self.coordinator.options[CONF_PER_ITEM_CAP]),
            _pinned_ids(self.coordinator.options.get(CONF_PINNED_ITEM_IDS, "")),
            self.coordinator.ending_soon_threshold_seconds,
        )
        return (self.kind, self.item_id) in selected

    @property
    def available(self) -> bool:
        """Return whether item is currently inside the capped selection."""
        return super().available and self._item is not None

    @property
    def native_value(self) -> Any:
        """Return item field value."""
        item = self._item
        return item.get(self.field) if item else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return item context."""
        item = self._item or {}
        end_time = item.get("end_time")
        return {
            "item_id": self.item_id,
            "title": item.get("title"),
            "url": item.get("url"),
            "image": item.get("image"),
            "listing_type": item.get("listing_type"),
            "kind": self.kind,
            "end_time": end_time.isoformat() if hasattr(end_time, "isoformat") else end_time,
            "currency": item.get("currency"),
            "last_seen": self.coordinator.data.get("last_successful_update").isoformat()
            if self.coordinator.data.get("last_successful_update")
            else None,
        }
