"""Manual refresh button for the eBay integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EbayDataUpdateCoordinator

REFRESH_BUTTON = ButtonEntityDescription(
    key="refresh",
    translation_key="refresh",
    icon="mdi:refresh",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the eBay refresh button."""
    coordinator: EbayDataUpdateCoordinator = entry.runtime_data
    async_add_entities([EbayRefreshButton(coordinator, entry)])


class EbayRefreshButton(
    CoordinatorEntity[EbayDataUpdateCoordinator], ButtonEntity
):
    """Button that requests a rate-limited coordinator refresh."""

    _attr_has_entity_name = True
    entity_description = REFRESH_BUTTON

    def __init__(
        self, coordinator: EbayDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{REFRESH_BUTTON.key}"
        self._attr_translation_key = REFRESH_BUTTON.translation_key
        self._attr_icon = REFRESH_BUTTON.icon
        self._attr_entity_category = REFRESH_BUTTON.entity_category
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
            "manufacturer": "eBay",
        }

    async def async_press(self) -> None:
        """Request a manual refresh, ignoring presses during cooldown."""
        await self.coordinator.async_request_manual_refresh()
