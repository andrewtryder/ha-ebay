"""Binary sensors for eBay seller health."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SELLER_STANDARDS_ENABLED, DEFAULT_OPTIONS, DOMAIN
from .coordinator import EbayDataUpdateCoordinator

SELLER_AT_RISK = BinarySensorEntityDescription(
    key="seller_standard_at_risk",
    translation_key="seller_standard_at_risk",
    device_class=BinarySensorDeviceClass.PROBLEM,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up eBay binary sensors."""
    coordinator: EbayDataUpdateCoordinator = entry.runtime_data
    options = {**DEFAULT_OPTIONS, **coordinator.options}
    if not options.get(CONF_SELLER_STANDARDS_ENABLED):
        return
    async_add_entities([EbaySellerAtRiskBinarySensor(coordinator, entry)])


class EbaySellerAtRiskBinarySensor(
    CoordinatorEntity[EbayDataUpdateCoordinator], BinarySensorEntity
):
    """Binary sensor for seller-standard at-risk state."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EbayDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = SELLER_AT_RISK
        self._attr_unique_id = f"{entry.entry_id}_{SELLER_AT_RISK.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
        }

    @property
    def is_on(self) -> bool | None:
        """Return True when seller standards are at risk."""
        summary = (self.coordinator.data or {}).get("summary") or {}
        value = summary.get("seller_standard_at_risk")
        if value is None:
            standards = ((self.coordinator.data or {}).get("seller_ops") or {}).get(
                "standards"
            ) or {}
            value = standards.get("at_risk")
        if value is None:
            return None
        return bool(value)
