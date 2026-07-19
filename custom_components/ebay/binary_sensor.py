"""Binary sensors for eBay seller health."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ACCOUNT_PRIVILEGES_ENABLED,
    CONF_FEEDBACK_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from .coordinator import EbayDataUpdateCoordinator

# Updates are driven by the shared DataUpdateCoordinator.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EbayBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an eBay binary sensor."""

    feature: str | None = None
    is_on_fn: Callable[[EbayDataUpdateCoordinator], bool | None]
    attrs_fn: Callable[[EbayDataUpdateCoordinator], dict[str, Any]] | None = None


def _summary_count_on(
    coordinator: EbayDataUpdateCoordinator, value_key: str
) -> bool | None:
    """Return True when a summary count is greater than zero."""
    summary = (coordinator.data or {}).get("summary") or {}
    value = summary.get(value_key)
    if value is None:
        return None
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return bool(value)


def _seller_standard_at_risk_on(
    coordinator: EbayDataUpdateCoordinator,
) -> bool | None:
    """Return True when seller standards are at risk."""
    summary = (coordinator.data or {}).get("summary") or {}
    value = summary.get("seller_standard_at_risk")
    if value is None:
        standards = ((coordinator.data or {}).get("seller_ops") or {}).get(
            "standards"
        ) or {}
        value = standards.get("at_risk")
    if value is None:
        return None
    return bool(value)


def _summary_bool_on(
    coordinator: EbayDataUpdateCoordinator, value_key: str
) -> bool | None:
    """Return a summary boolean when present."""
    summary = (coordinator.data or {}).get("summary") or {}
    value = summary.get(value_key)
    if value is None:
        return None
    return bool(value)


def _partial_failure_categories(
    coordinator: EbayDataUpdateCoordinator,
) -> list[str]:
    data = coordinator.data or {}
    return list(
        data.get("partial_failures")
        or data.get("summary", {}).get("partial_failures")
        or []
    )


def _truncated_collection_names(
    coordinator: EbayDataUpdateCoordinator,
) -> list[str]:
    truncated = (coordinator.data or {}).get("truncated_collections") or {}
    return sorted(name for name, is_truncated in truncated.items() if is_truncated)


def _account_data_partially_unavailable_on(
    coordinator: EbayDataUpdateCoordinator,
) -> bool | None:
    if coordinator.data is None:
        return None
    return bool(_partial_failure_categories(coordinator))


def _api_data_truncated_on(
    coordinator: EbayDataUpdateCoordinator,
) -> bool | None:
    if coordinator.data is None:
        return None
    return bool(_truncated_collection_names(coordinator))


BINARY_SENSORS: tuple[EbayBinarySensorDescription, ...] = (
    EbayBinarySensorDescription(
        key="seller_standard_at_risk",
        translation_key="seller_standard_at_risk",
        device_class=BinarySensorDeviceClass.PROBLEM,
        feature=CONF_SELLER_STANDARDS_ENABLED,
        is_on_fn=_seller_standard_at_risk_on,
    ),
    EbayBinarySensorDescription(
        key="orders_overdue",
        translation_key="orders_overdue",
        device_class=BinarySensorDeviceClass.PROBLEM,
        feature=CONF_FULFILLMENT_ENABLED,
        entity_registry_enabled_default=False,
        is_on_fn=lambda coordinator: _summary_count_on(coordinator, "orders_overdue"),
    ),
    EbayBinarySensorDescription(
        key="orders_due_today",
        translation_key="orders_due_today",
        feature=CONF_FULFILLMENT_ENABLED,
        entity_registry_enabled_default=False,
        is_on_fn=lambda coordinator: _summary_count_on(
            coordinator, "orders_shipping_today"
        ),
    ),
    EbayBinarySensorDescription(
        key="open_payment_dispute",
        translation_key="open_payment_dispute",
        device_class=BinarySensorDeviceClass.PROBLEM,
        feature=CONF_FULFILLMENT_ENABLED,
        entity_registry_enabled_default=False,
        is_on_fn=lambda coordinator: _summary_count_on(
            coordinator, "open_payment_disputes"
        ),
    ),
    EbayBinarySensorDescription(
        key="unread_buyer_messages",
        translation_key="unread_buyer_messages",
        feature=CONF_MESSAGES_ENABLED,
        entity_registry_enabled_default=False,
        is_on_fn=lambda coordinator: _summary_count_on(
            coordinator, "unread_conversations"
        ),
    ),
    EbayBinarySensorDescription(
        key="negative_feedback_received_recently",
        translation_key="negative_feedback_received_recently",
        device_class=BinarySensorDeviceClass.PROBLEM,
        feature=CONF_FEEDBACK_ENABLED,
        entity_registry_enabled_default=False,
        is_on_fn=lambda coordinator: _summary_count_on(
            coordinator, "recent_negative_feedback"
        ),
    ),
    EbayBinarySensorDescription(
        key="selling_registered",
        translation_key="selling_registered",
        feature=CONF_ACCOUNT_PRIVILEGES_ENABLED,
        entity_registry_enabled_default=False,
        is_on_fn=lambda coordinator: _summary_bool_on(
            coordinator, "selling_registered"
        ),
    ),
    EbayBinarySensorDescription(
        key="selling_near_limit",
        translation_key="selling_near_limit",
        device_class=BinarySensorDeviceClass.PROBLEM,
        feature=CONF_ACCOUNT_PRIVILEGES_ENABLED,
        is_on_fn=lambda coordinator: _summary_bool_on(
            coordinator, "selling_near_limit"
        ),
    ),
    EbayBinarySensorDescription(
        key="account_data_partially_unavailable",
        translation_key="account_data_partially_unavailable",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_account_data_partially_unavailable_on,
        attrs_fn=lambda coordinator: {
            "partial_failure_categories": _partial_failure_categories(coordinator)
        },
    ),
    EbayBinarySensorDescription(
        key="api_data_truncated",
        translation_key="api_data_truncated",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_api_data_truncated_on,
        attrs_fn=lambda coordinator: {
            "truncated_collections": _truncated_collection_names(coordinator)
        },
    ),
)


def binary_sensors_for_options(
    options: dict[str, Any],
) -> list[EbayBinarySensorDescription]:
    """Return binary sensors whose feature group is enabled."""
    merged = {**DEFAULT_OPTIONS, **options}
    return [
        description
        for description in BINARY_SENSORS
        if description.feature is None or bool(merged.get(description.feature))
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up eBay binary sensors."""
    coordinator: EbayDataUpdateCoordinator = entry.runtime_data
    async_add_entities(
        [
            EbayBinarySensor(coordinator, entry, description)
            for description in binary_sensors_for_options(coordinator.options)
        ]
    )


class EbayBinarySensor(
    CoordinatorEntity[EbayDataUpdateCoordinator], BinarySensorEntity
):
    """Binary sensor derived from coordinator summary / health state."""

    entity_description: EbayBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EbayDataUpdateCoordinator,
        entry: ConfigEntry,
        description: EbayBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
        }

    @property
    def is_on(self) -> bool | None:
        """Return True when the described condition is active."""
        return self.entity_description.is_on_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return optional diagnostic attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator)
