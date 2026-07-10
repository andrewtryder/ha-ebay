"""Event entities for eBay automation triggers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BIDDING_EVENT_TYPES,
    DOMAIN,
    SELLING_EVENT_TYPES,
    WATCHING_EVENT_TYPES,
)
from .coordinator import EbayDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class EbayEventEntityDescription(EventEntityDescription):
    """Describe an eBay event entity."""

    kind: str


EVENTS = [
    EbayEventEntityDescription(
        key="selling_activity",
        translation_key="selling_activity",
        kind="selling",
        event_types=SELLING_EVENT_TYPES,
    ),
    EbayEventEntityDescription(
        key="watching_activity",
        translation_key="watching_activity",
        kind="watching",
        event_types=WATCHING_EVENT_TYPES,
    ),
    EbayEventEntityDescription(
        key="bidding_activity",
        translation_key="bidding_activity",
        kind="bidding",
        event_types=BIDDING_EVENT_TYPES,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up eBay event entities."""
    coordinator: EbayDataUpdateCoordinator = entry.runtime_data
    async_add_entities(EbayActivityEvent(coordinator, entry, description) for description in EVENTS)


class EbayActivityEvent(CoordinatorEntity[EbayDataUpdateCoordinator], EventEntity):
    """Fixed eBay activity event entity."""

    entity_description: EbayEventEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EbayDataUpdateCoordinator,
        entry: ConfigEntry,
        description: EbayEventEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
            "manufacturer": "eBay",
        }
        self._unsubscribe_event_callback: CALLBACK_TYPE | None = (
            coordinator.register_event_callback(description.kind, self._handle_event)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Deregister coordinator event callback."""
        if self._unsubscribe_event_callback is not None:
            self._unsubscribe_event_callback()
            self._unsubscribe_event_callback = None

    def _handle_event(self, event_type: str, event_data: dict[str, Any]) -> None:
        """Publish an event on this entity."""
        if event_type not in self.event_types:
            return
        self._trigger_event(event_type, event_data)
        self.async_write_ha_state()
