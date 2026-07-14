"""Sensor entities for eBay telemetry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import API_WARNINGS_STATE_ATTR_MAX
from .const import (
    CONF_BUYING_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLING_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    DEFAULT_OPTIONS,
    DOMAIN,
    REFRESH_RESULT_UNKNOWN,
)
from .coordinator import EbayDataUpdateCoordinator, SelectedItemKey

_LOGGER = logging.getLogger(__name__)

ENUM_UNKNOWN = "UNKNOWN"

SELLER_LEVEL_OPTIONS = [
    "TOP_RATED",
    "ABOVE_STANDARD",
    "BELOW_STANDARD",
    ENUM_UNKNOWN,
]

CUSTOMER_SERVICE_RATING_OPTIONS = [
    "VERY_HIGH",
    "HIGH",
    "AVERAGE",
    "LOW",
    "VERY_LOW",
    "ABOVE_STANDARD",
    "BELOW_STANDARD",
    ENUM_UNKNOWN,
]


@dataclass(frozen=True, kw_only=True)
class EbaySummarySensorDescription(SensorEntityDescription):
    """Describe a summary sensor."""

    value_key: str
    feature: str | None = None


@dataclass(frozen=True, kw_only=True)
class EbayDiagnosticSensorDescription(SensorEntityDescription):
    """Describe a disabled-by-default diagnostic sensor."""

    value_fn: Callable[[EbayDataUpdateCoordinator], StateType | datetime]
    attrs_fn: Callable[[EbayDataUpdateCoordinator], dict[str, Any]] | None = None


SUMMARY_SENSORS = [
    EbaySummarySensorDescription(
        key="active_selling_items",
        translation_key="active_selling_items",
        value_key="active_selling_items",
        feature=CONF_SELLING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="watched_items",
        translation_key="watched_items",
        value_key="watched_items",
        feature=CONF_BUYING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="bidding_items",
        translation_key="bidding_items",
        value_key="bidding_items",
        feature=CONF_BUYING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="selling_total_bids",
        translation_key="selling_total_bids",
        value_key="selling_total_bids",
        feature=CONF_SELLING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="selling_total_offers",
        translation_key="selling_total_offers",
        value_key="selling_total_offers",
        feature=CONF_SELLING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="active_listings_quantity_sold",
        translation_key="active_listings_quantity_sold",
        value_key="active_listings_quantity_sold",
        feature=CONF_SELLING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="sold_items_count",
        translation_key="sold_items_count",
        value_key="sold_items_count",
        feature=CONF_SELLING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="unsold_items_count",
        translation_key="unsold_items_count",
        value_key="unsold_items_count",
        feature=CONF_SELLING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="selling_total_watchers",
        translation_key="selling_total_watchers",
        value_key="selling_total_watchers",
        feature=CONF_SELLING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="selling_total_views",
        translation_key="selling_total_views",
        value_key="selling_total_views",
        feature=CONF_SELLING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="watched_ending_soon",
        translation_key="watched_ending_soon",
        value_key="watched_ending_soon",
        feature=CONF_BUYING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="selling_ending_soon",
        translation_key="selling_ending_soon",
        value_key="selling_ending_soon",
        feature=CONF_SELLING_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="orders_awaiting_shipment",
        translation_key="orders_awaiting_shipment",
        value_key="orders_awaiting_shipment",
        feature=CONF_FULFILLMENT_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    EbaySummarySensorDescription(
        key="orders_shipping_today",
        translation_key="orders_shipping_today",
        value_key="orders_shipping_today",
        feature=CONF_FULFILLMENT_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    EbaySummarySensorDescription(
        key="orders_overdue",
        translation_key="orders_overdue",
        value_key="orders_overdue",
        feature=CONF_FULFILLMENT_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="orders_shipped",
        translation_key="orders_shipped",
        value_key="orders_shipped",
        feature=CONF_FULFILLMENT_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    EbaySummarySensorDescription(
        key="orders_partially_fulfilled",
        translation_key="orders_partially_fulfilled",
        value_key="orders_partially_fulfilled",
        feature=CONF_FULFILLMENT_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    EbaySummarySensorDescription(
        key="open_payment_disputes",
        translation_key="open_payment_disputes",
        value_key="open_payment_disputes",
        feature=CONF_FULFILLMENT_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="seller_level",
        translation_key="seller_level",
        value_key="seller_level",
        feature=CONF_SELLER_STANDARDS_ENABLED,
        device_class=SensorDeviceClass.ENUM,
        options=SELLER_LEVEL_OPTIONS,
    ),
    EbaySummarySensorDescription(
        key="transaction_defect_rate",
        translation_key="transaction_defect_rate",
        value_key="transaction_defect_rate",
        feature=CONF_SELLER_STANDARDS_ENABLED,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="late_shipment_rate",
        translation_key="late_shipment_rate",
        value_key="late_shipment_rate",
        feature=CONF_SELLER_STANDARDS_ENABLED,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="cases_closed_without_seller_resolution",
        translation_key="cases_closed_without_seller_resolution",
        value_key="cases_closed_without_seller_resolution",
        feature=CONF_SELLER_STANDARDS_ENABLED,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="item_not_received_metric",
        translation_key="item_not_received_metric",
        value_key="item_not_received_metric",
        feature=CONF_SELLER_STANDARDS_ENABLED,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.ENUM,
        options=CUSTOMER_SERVICE_RATING_OPTIONS,
    ),
    EbaySummarySensorDescription(
        key="item_not_as_described_metric",
        translation_key="item_not_as_described_metric",
        value_key="item_not_as_described_metric",
        feature=CONF_SELLER_STANDARDS_ENABLED,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.ENUM,
        options=CUSTOMER_SERVICE_RATING_OPTIONS,
    ),
    EbaySummarySensorDescription(
        key="seller_next_evaluation_date",
        translation_key="seller_next_evaluation_date",
        value_key="seller_next_evaluation_date",
        feature=CONF_SELLER_STANDARDS_ENABLED,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    EbaySummarySensorDescription(
        key="feedback_score",
        translation_key="feedback_score",
        value_key="feedback_score",
        feature=CONF_FEEDBACK_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="positive_feedback_percent",
        translation_key="positive_feedback_percent",
        value_key="positive_feedback_percent",
        feature=CONF_FEEDBACK_ENABLED,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="recent_positive_feedback",
        translation_key="recent_positive_feedback",
        value_key="recent_positive_feedback",
        feature=CONF_FEEDBACK_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    EbaySummarySensorDescription(
        key="recent_neutral_feedback",
        translation_key="recent_neutral_feedback",
        value_key="recent_neutral_feedback",
        feature=CONF_FEEDBACK_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    EbaySummarySensorDescription(
        key="recent_negative_feedback",
        translation_key="recent_negative_feedback",
        value_key="recent_negative_feedback",
        feature=CONF_FEEDBACK_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    EbaySummarySensorDescription(
        key="items_awaiting_feedback",
        translation_key="items_awaiting_feedback",
        value_key="items_awaiting_feedback",
        feature=CONF_FEEDBACK_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    EbaySummarySensorDescription(
        key="unread_conversations",
        translation_key="unread_conversations",
        value_key="unread_conversations",
        feature=CONF_MESSAGES_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    EbaySummarySensorDescription(
        key="buyer_question_conversations",
        translation_key="buyer_question_conversations",
        value_key="buyer_question_conversations",
        feature=CONF_MESSAGES_ENABLED,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    EbaySummarySensorDescription(
        key="oldest_unanswered_message_hours",
        translation_key="oldest_unanswered_message_hours",
        value_key="oldest_unanswered_message_hours",
        feature=CONF_MESSAGES_ENABLED,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
]


def summary_sensors_for_options(
    options: dict[str, Any],
) -> list[EbaySummarySensorDescription]:
    """Return summary sensors whose feature group is enabled."""
    merged = {**DEFAULT_OPTIONS, **options}
    return [
        description
        for description in SUMMARY_SENSORS
        if description.feature is None or bool(merged.get(description.feature))
    ]


def _truncated_collection_names(coordinator: EbayDataUpdateCoordinator) -> list[str]:
    truncated = (coordinator.data or {}).get("truncated_collections") or {}
    return sorted(name for name, is_truncated in truncated.items() if is_truncated)


def _partial_failure_categories(coordinator: EbayDataUpdateCoordinator) -> list[str]:
    data = coordinator.data or {}
    return list(
        data.get("partial_failures")
        or data.get("summary", {}).get("partial_failures")
        or []
    )


def _api_warnings_all(coordinator: EbayDataUpdateCoordinator) -> list[Any]:
    """Return the full deduplicated warning list from coordinator payload."""
    data = coordinator.data or {}
    return list(data.get("api_warnings") or [])


def _api_warnings_for_attrs(coordinator: EbayDataUpdateCoordinator) -> list[Any]:
    """Return a capped warning list suitable for entity state attributes."""
    return _api_warnings_all(coordinator)[:API_WARNINGS_STATE_ATTR_MAX]


def _parse_successful_update(coordinator: EbayDataUpdateCoordinator) -> datetime | None:
    raw = (coordinator.data or {}).get("summary", {}).get("last_successful_update")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


DIAGNOSTIC_SENSORS = [
    EbayDiagnosticSensorDescription(
        key="last_successful_update",
        translation_key="last_successful_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_parse_successful_update,
    ),
    EbayDiagnosticSensorDescription(
        key="partial_failure_count",
        translation_key="partial_failure_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: len(_partial_failure_categories(coordinator)),
        attrs_fn=lambda coordinator: {
            "partial_failure_categories": _partial_failure_categories(coordinator)
        },
    ),
    EbayDiagnosticSensorDescription(
        key="truncated_collection_count",
        translation_key="truncated_collection_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: len(_truncated_collection_names(coordinator)),
        attrs_fn=lambda coordinator: {
            "truncated_collections": _truncated_collection_names(coordinator)
        },
    ),
    EbayDiagnosticSensorDescription(
        key="api_warning_count",
        translation_key="api_warning_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: len(_api_warnings_all(coordinator)),
        attrs_fn=lambda coordinator: {
            "api_warnings": _api_warnings_for_attrs(coordinator)
        },
    ),
    EbayDiagnosticSensorDescription(
        key="last_refresh_duration",
        translation_key="last_refresh_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: coordinator.last_refresh_duration_seconds,
    ),
    EbayDiagnosticSensorDescription(
        key="last_refresh_result",
        translation_key="last_refresh_result",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "unknown",
            "success",
            "partial",
            "error",
            "auth_error",
        ],
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: (
            coordinator.last_refresh_result or REFRESH_RESULT_UNKNOWN
        ),
        attrs_fn=lambda coordinator: {
            "error_category": coordinator.last_error_category
        },
    ),
    EbayDiagnosticSensorDescription(
        key="scheduled_ending_soon_timer_count",
        translation_key="scheduled_ending_soon_timer_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: coordinator.scheduled_ending_soon_count,
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

    async def _async_remove_item_sensor(unique_id: str) -> bool:
        if registry is None:
            return False
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            return False
        registry.async_remove(entity_id)
        if platform is not None and entity_id in platform.entities:
            try:
                await platform.async_remove_entity(entity_id)
            except KeyError:
                pass
        return True

    async def _async_remove_stale_item_sensors(selected_ids: set[str]) -> int:
        stale_ids = (
            known_item_ids
            | (
                _registered_item_sensor_unique_ids(registry, entry.entry_id)
                if registry is not None
                else set()
            )
        ) - selected_ids
        removed_count = 0
        for unique_id in stale_ids:
            if await _async_remove_item_sensor(unique_id):
                removed_count += 1
            known_item_ids.discard(unique_id)
        return removed_count

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
    initial_removed = await _async_remove_stale_item_sensors(
        _item_sensor_unique_ids(entry.entry_id, selected)
    )
    entities: list[SensorEntity] = [
        EbaySummarySensor(coordinator, entry, description)
        for description in summary_sensors_for_options(coordinator.options)
    ]
    entities.extend(
        EbayDiagnosticSensor(coordinator, entry, description)
        for description in DIAGNOSTIC_SENSORS
    )
    initial_item_sensors = _new_item_sensors(selected)
    entities.extend(initial_item_sensors)
    _LOGGER.debug(
        "Reconciled eBay item sensors entry_id=%s selected_count=%s added_count=%s removed_count=%s known_count=%s",
        entry.entry_id,
        len(selected),
        len(initial_item_sensors),
        initial_removed,
        len(known_item_ids),
    )
    async_add_entities(entities)

    async def _async_reconcile_item_sensors() -> None:
        selected = coordinator.refresh_selected_item_keys()
        removed_count = await _async_remove_stale_item_sensors(
            _item_sensor_unique_ids(entry.entry_id, selected)
        )
        new_sensors = _new_item_sensors(selected)
        _LOGGER.debug(
            "Reconciled eBay item sensors entry_id=%s selected_count=%s added_count=%s removed_count=%s known_count=%s",
            entry.entry_id,
            len(selected),
            len(new_sensors),
            removed_count,
            len(known_item_ids),
        )
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


_SUMMARY_SENSOR_KEYS = {description.key for description in SUMMARY_SENSORS}
_DIAGNOSTIC_SENSOR_KEYS = {description.key for description in DIAGNOSTIC_SENSORS}
_FIXED_SENSOR_KEYS = _SUMMARY_SENSOR_KEYS | _DIAGNOSTIC_SENSOR_KEYS


def _item_sensor_unique_ids(entry_id: str, selected: set[SelectedItemKey]) -> set[str]:
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
    if unique_id.removeprefix(f"{entry_id}_") in _FIXED_SENSOR_KEYS:
        return False
    for kind, fields in ITEM_SENSOR_FIELDS.items():
        prefix = f"{entry_id}_{kind}_"
        if not unique_id.startswith(prefix):
            continue
        remainder = unique_id[len(prefix) :]
        for suffix, _, _ in fields:
            ending = f"_{suffix}"
            if remainder.endswith(ending):
                item_id = remainder[: -len(ending)]
                if item_id:
                    return True
        return False
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
        }

    @property
    def native_value(self) -> StateType | datetime:
        """Return the summary value."""
        value = self.coordinator.data["summary"].get(self.entity_description.value_key)
        if (
            self.entity_description.device_class == SensorDeviceClass.TIMESTAMP
            and isinstance(value, str)
        ):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        if (
            self.entity_description.device_class == SensorDeviceClass.ENUM
            and value is not None
        ):
            text = str(value)
            options = self.entity_description.options or []
            if options and text not in options:
                return ENUM_UNKNOWN
            return text
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return bounded summary context."""
        summary = self.coordinator.data["summary"]
        attrs: dict[str, Any] = {
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
        if self.entity_description.device_class == SensorDeviceClass.ENUM:
            raw = summary.get(self.entity_description.value_key)
            if raw is not None:
                text = str(raw)
                options = self.entity_description.options or []
                if options and text not in options:
                    attrs["raw_value"] = text
        return attrs


class EbayDiagnosticSensor(CoordinatorEntity[EbayDataUpdateCoordinator], SensorEntity):
    """Disabled-by-default diagnostic health sensor."""

    entity_description: EbayDiagnosticSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EbayDataUpdateCoordinator,
        entry: ConfigEntry,
        description: EbayDiagnosticSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates and optional timer-count changes."""
        await super().async_added_to_hass()
        if self.entity_description.key != "scheduled_ending_soon_timer_count":
            return

        @callback
        def _timer_updated() -> None:
            self.async_write_ha_state()

        self.async_on_remove(self.coordinator.async_add_timer_listener(_timer_updated))

    @property
    def available(self) -> bool:
        """Keep health telemetry visible after a failed refresh."""
        return (
            self.coordinator.last_attempt_at is not None
            or self.coordinator.data is not None
        )

    @property
    def native_value(self) -> StateType | datetime:
        """Return the diagnostic value."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return safe diagnostic attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)


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
