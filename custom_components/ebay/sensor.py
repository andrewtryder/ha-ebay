"""Sensor entities for eBay telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EbayDataUpdateCoordinator, SelectedItemKey


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
        key="active_listings_quantity_sold",
        translation_key="active_listings_quantity_sold",
        value_key="active_listings_quantity_sold",
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
        ("analytics_views_30d", "analytics_views_30d", None),
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
    registry = _entity_registry(hass)
    platform = _current_entity_platform()

    async def _async_remove_item_sensor(unique_id: str) -> None:
        if registry is None:
            return
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            return
        registry.async_remove(entity_id)
        if platform is not None and entity_id in platform.entities:
            try:
                await platform.async_remove_entity(entity_id)
            except KeyError:
                pass

    async def _async_remove_stale_item_sensors(selected_ids: set[str]) -> None:
        stale_ids = (
            known_item_ids
            | (
                _registered_item_sensor_unique_ids(registry, entry.entry_id)
                if registry is not None
                else set()
            )
        ) - selected_ids
        for unique_id in stale_ids:
            await _async_remove_item_sensor(unique_id)
            known_item_ids.discard(unique_id)

    def _new_item_sensors(selected: set[SelectedItemKey]) -> list[EbayItemSensor]:
        sensors: list[EbayItemSensor] = []
        for kind, item_id in selected:
            for suffix, field, unit in ITEM_SENSOR_FIELDS[kind]:
                unique_id = f"{entry.entry_id}_{kind}_{item_id}_{suffix}"
                if unique_id in known_item_ids:
                    continue
                known_item_ids.add(unique_id)
                sensors.append(
                    EbayItemSensor(
                        coordinator, entry, kind, item_id, suffix, field, unit
                    )
                )
        return sensors

    selected = coordinator.refresh_selected_item_keys()
    await _async_remove_stale_item_sensors(
        _item_sensor_unique_ids(entry.entry_id, selected)
    )
    entities: list[SensorEntity] = [
        EbaySummarySensor(coordinator, entry, description)
        for description in SUMMARY_SENSORS
    ]
    entities.extend(_new_item_sensors(selected))
    async_add_entities(entities)

    async def _async_reconcile_item_sensors() -> None:
        selected = coordinator.refresh_selected_item_keys()
        await _async_remove_stale_item_sensors(
            _item_sensor_unique_ids(entry.entry_id, selected)
        )
        new_sensors = _new_item_sensors(selected)
        if new_sensors:
            async_add_entities(new_sensors)

    @callback
    def _handle_coordinator_update() -> None:
        hass.async_create_task(_async_reconcile_item_sensors())

    entry.async_on_unload(coordinator.async_add_listener(_handle_coordinator_update))


def _current_entity_platform() -> entity_platform.EntityPlatform | None:
    """Return the current entity platform when running inside HA setup."""
    try:
        return entity_platform.async_get_current_platform()
    except RuntimeError:
        return None


def _entity_registry(hass: HomeAssistant) -> er.EntityRegistry | None:
    """Return the entity registry when a real Home Assistant object is available."""
    try:
        return er.async_get(hass)
    except (AttributeError, TypeError):
        return None


def _item_sensor_unique_ids(
    entry_id: str, selected: set[SelectedItemKey]
) -> set[str]:
    """Return unique IDs for the current capped selection without building entities."""
    unique_ids: set[str] = set()
    for kind, item_id in selected:
        for suffix, _, _ in ITEM_SENSOR_FIELDS[kind]:
            unique_ids.add(f"{entry_id}_{kind}_{item_id}_{suffix}")
    return unique_ids


def _registered_item_sensor_unique_ids(
    registry: er.EntityRegistry, entry_id: str
) -> set[str]:
    """Return per-item sensor unique IDs already registered for this entry."""
    return {
        registry_entry.unique_id
        for registry_entry in er.async_entries_for_config_entry(registry, entry_id)
        if registry_entry.domain == "sensor"
        and registry_entry.platform == DOMAIN
        and _is_item_sensor_unique_id(entry_id, registry_entry.unique_id)
    }


def _is_item_sensor_unique_id(entry_id: str, unique_id: str) -> bool:
    """Return whether a unique ID belongs to a generated per-item sensor."""
    for kind, fields in ITEM_SENSOR_FIELDS.items():
        if not unique_id.startswith(f"{entry_id}_{kind}_"):
            continue
        return any(unique_id.endswith(f"_{suffix}") for suffix, _, _ in fields)
    return False


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
            "truncated_collections": summary.get("truncated_collections"),
            "api_warnings": summary.get("api_warnings"),
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
        return (self.kind, self.item_id) in self.coordinator.selected_item_keys

    @property
    def available(self) -> bool:
        """Return whether item is currently inside the capped selection."""
        return super().available and self._item is not None

    @property
    def native_value(self) -> Any:
        """Return the item field value."""
        item = self._item
        if item is None:
            return None
        return item.get(self.field)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return item context attributes."""
        item = self._item
        if item is None:
            return {}
        end_time = item.get("end_time")
        return {
            "title": item.get("title"),
            "item_id": item.get("item_id"),
            "url": item.get("url"),
            "image": item.get("image"),
            "currency": item.get("currency"),
            "end_time": end_time.isoformat()
            if hasattr(end_time, "isoformat")
            else end_time,
            "time_left": item.get("time_left"),
        }
