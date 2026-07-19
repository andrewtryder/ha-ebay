"""Manual action buttons for the eBay integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SECTION_ACCOUNT_PRIVILEGES,
    SECTION_ANALYTICS,
    SECTION_BUYING,
    SECTION_FEEDBACK,
    SECTION_FINANCES,
    SECTION_FULFILLMENT,
    SECTION_SELLER_STANDARDS,
    SECTION_SELLING,
)
from .coordinator import EbayDataUpdateCoordinator

# Button presses are sequential; coordinator-backed platforms use 0.
PARALLEL_UPDATES = 1

REFRESH_BUTTON = ButtonEntityDescription(
    key="refresh",
    translation_key="refresh",
    icon="mdi:refresh",
    entity_category=EntityCategory.CONFIG,
)

CLEANUP_INACTIVE_ITEM_ENTITIES_BUTTON = ButtonEntityDescription(
    key="cleanup_inactive_item_entities",
    translation_key="cleanup_inactive_item_entities",
    icon="mdi:broom",
    entity_category=EntityCategory.CONFIG,
)


@dataclass(frozen=True, kw_only=True)
class EbaySectionRefreshButtonDescription(ButtonEntityDescription):
    """Describe a button that refreshes a subset of coordinator sections."""

    sections: frozenset[str]


SECTION_REFRESH_BUTTONS = (
    EbaySectionRefreshButtonDescription(
        key="refresh_buying",
        translation_key="refresh_buying",
        icon="mdi:cart-outline",
        entity_category=EntityCategory.CONFIG,
        sections=frozenset({SECTION_BUYING}),
    ),
    EbaySectionRefreshButtonDescription(
        key="refresh_selling",
        translation_key="refresh_selling",
        icon="mdi:storefront-outline",
        entity_category=EntityCategory.CONFIG,
        sections=frozenset({SECTION_SELLING}),
    ),
    EbaySectionRefreshButtonDescription(
        key="refresh_feedback",
        translation_key="refresh_feedback",
        icon="mdi:star-outline",
        entity_category=EntityCategory.CONFIG,
        sections=frozenset({SECTION_FEEDBACK}),
    ),
    EbaySectionRefreshButtonDescription(
        key="refresh_seller_operations",
        translation_key="refresh_seller_operations",
        icon="mdi:truck-delivery-outline",
        entity_category=EntityCategory.CONFIG,
        sections=frozenset(
            {
                SECTION_FULFILLMENT,
                SECTION_SELLER_STANDARDS,
                SECTION_ACCOUNT_PRIVILEGES,
                SECTION_FINANCES,
            }
        ),
    ),
    EbaySectionRefreshButtonDescription(
        key="refresh_analytics",
        translation_key="refresh_analytics",
        icon="mdi:chart-line",
        entity_category=EntityCategory.CONFIG,
        sections=frozenset({SECTION_ANALYTICS}),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up eBay action buttons."""
    coordinator: EbayDataUpdateCoordinator = entry.runtime_data
    entities: list[ButtonEntity] = [
        EbayRefreshButton(coordinator, entry),
        *[
            EbaySectionRefreshButton(coordinator, entry, description)
            for description in SECTION_REFRESH_BUTTONS
        ],
        EbayCleanupInactiveItemEntitiesButton(coordinator, entry),
    ]
    async_add_entities(entities)


class EbayRefreshButton(CoordinatorEntity[EbayDataUpdateCoordinator], ButtonEntity):
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
        }

    @property
    def available(self) -> bool:
        """Keep the refresh button usable after a failed coordinator update."""
        return (
            self.coordinator.last_attempt_at is not None
            or self.coordinator.data is not None
        )

    async def async_press(self) -> None:
        """Request a manual refresh, raising on cooldown or failure."""
        try:
            accepted = await self.coordinator.async_request_manual_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError("eBay refresh failed") from err
        if not accepted:
            raise HomeAssistantError(
                "eBay refresh ignored during cooldown or while a refresh is already running"
            )


class EbaySectionRefreshButton(
    CoordinatorEntity[EbayDataUpdateCoordinator], ButtonEntity
):
    """Button that forces refresh of a targeted set of sections."""

    _attr_has_entity_name = True
    entity_description: EbaySectionRefreshButtonDescription

    def __init__(
        self,
        coordinator: EbayDataUpdateCoordinator,
        entry: ConfigEntry,
        description: EbaySectionRefreshButtonDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_translation_key = description.translation_key
        self._attr_icon = description.icon
        self._attr_entity_category = description.entity_category
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
        }

    @property
    def available(self) -> bool:
        """Keep section refresh usable after a failed coordinator update."""
        return (
            self.coordinator.last_attempt_at is not None
            or self.coordinator.data is not None
        )

    async def async_press(self) -> None:
        """Request a targeted section refresh, raising on cooldown or failure."""
        try:
            accepted = await self.coordinator.async_request_manual_refresh(
                set(self.entity_description.sections)
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError("eBay section refresh failed") from err
        if not accepted:
            raise HomeAssistantError(
                "eBay refresh ignored during cooldown or while a refresh is already running"
            )


class EbayCleanupInactiveItemEntitiesButton(
    CoordinatorEntity[EbayDataUpdateCoordinator], ButtonEntity
):
    """Button that permanently removes inactive per-item sensors."""

    _attr_has_entity_name = True
    entity_description = CLEANUP_INACTIVE_ITEM_ENTITIES_BUTTON

    def __init__(
        self, coordinator: EbayDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{entry.entry_id}_{CLEANUP_INACTIVE_ITEM_ENTITIES_BUTTON.key}"
        )
        self._attr_translation_key = (
            CLEANUP_INACTIVE_ITEM_ENTITIES_BUTTON.translation_key
        )
        self._attr_icon = CLEANUP_INACTIVE_ITEM_ENTITIES_BUTTON.icon
        self._attr_entity_category = (
            CLEANUP_INACTIVE_ITEM_ENTITIES_BUTTON.entity_category
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
        }

    @property
    def available(self) -> bool:
        """Keep cleanup available whenever the coordinator has run or has data."""
        return (
            self.coordinator.last_attempt_at is not None
            or self.coordinator.data is not None
        )

    async def async_press(self) -> None:
        """Permanently remove inactive item sensors, raising HomeAssistantError on failure."""
        try:
            await self.coordinator.async_cleanup_inactive_item_entities(force=True)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                "Failed to clean up inactive eBay item entities"
            ) from err
