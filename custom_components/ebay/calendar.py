"""Calendar entities backed by coordinator listing end times."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityDescription,
    CalendarEvent,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CALENDAR_MARKER_MINUTES,
    CONF_BUYING_ENABLED,
    CONF_SELLING_ENABLED,
    DOMAIN,
)
from .coordinator import EbayDataUpdateCoordinator

# Updates are driven by the shared DataUpdateCoordinator.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EbayCalendarDescription(CalendarEntityDescription):
    """Describe a fixed eBay calendar entity."""

    collection: str
    listing_kind: str
    requires_buying: bool = False
    requires_selling: bool = False


CALENDARS = [
    EbayCalendarDescription(
        key="watched_items",
        translation_key="watched_items",
        collection="watched",
        listing_kind="watching",
        requires_buying=True,
    ),
    EbayCalendarDescription(
        key="bidding_items",
        translation_key="bidding_items",
        collection="bidding",
        listing_kind="bidding",
        requires_buying=True,
    ),
    EbayCalendarDescription(
        key="selling_items",
        translation_key="selling_items",
        collection="selling",
        listing_kind="selling",
        requires_selling=True,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up eBay calendar entities."""
    coordinator: EbayDataUpdateCoordinator = entry.runtime_data
    async_add_entities(
        EbayListingCalendar(coordinator, entry, description)
        for description in CALENDARS
    )


class EbayListingCalendar(CoordinatorEntity[EbayDataUpdateCoordinator], CalendarEntity):
    """Calendar of listing end times from already-fetched coordinator data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EbayDataUpdateCoordinator,
        entry: ConfigEntry,
        description: EbayCalendarDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_translation_key = description.translation_key
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "eBay",
        }

    @property
    def available(self) -> bool:
        """Return False when the related buying/selling section is disabled."""
        if not super().available:
            return False
        options = self.coordinator.options
        if self._description.requires_buying and not options.get(
            CONF_BUYING_ENABLED, True
        ):
            return False
        if self._description.requires_selling and not options.get(
            CONF_SELLING_ENABLED, True
        ):
            return False
        return True

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next upcoming listing end event."""
        now = datetime.now(timezone.utc)
        events = self._build_events(
            start_date=now - timedelta(minutes=CALENDAR_MARKER_MINUTES),
            end_date=now + timedelta(days=366),
            include_past_in_window=True,
        )
        for event in events:
            if event.end >= now:
                return event
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return listing end markers within the requested range."""
        return self._build_events(
            start_date=start_date,
            end_date=end_date,
            include_past_in_window=True,
        )

    def _build_events(
        self,
        *,
        start_date: datetime,
        end_date: datetime,
        include_past_in_window: bool,
    ) -> list[CalendarEvent]:
        if not self.available:
            return []
        data = self.coordinator.data or {}
        collection = data.get(self._description.collection) or {}
        marker = timedelta(minutes=CALENDAR_MARKER_MINUTES)
        events: list[CalendarEvent] = []
        seen: set[str] = set()
        for item_id, item in collection.items():
            if item_id in seen:
                continue
            end_time = item.get("end_time")
            if not isinstance(end_time, datetime):
                continue
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
            else:
                end_time = end_time.astimezone(timezone.utc)
            event_end = end_time + marker
            # Range overlap: event.end > start_date and event.start < end_date
            if event_end <= start_date or end_time >= end_date:
                continue
            if not include_past_in_window and event_end <= datetime.now(timezone.utc):
                continue
            seen.add(item_id)
            events.append(
                CalendarEvent(
                    start=end_time,
                    end=event_end,
                    summary=str(item.get("title") or f"Item {item_id}"),
                    description=_event_description(
                        item, self._description.listing_kind
                    ),
                    location="eBay",
                    uid=f"{self._description.collection}:{item_id}",
                )
            )
        events.sort(key=lambda event: (event.start, event.uid or ""))
        return events


def _event_description(item: dict[str, Any], listing_kind: str) -> str:
    """Build a compact calendar description from safe item fields."""
    lines = [
        f"Item ID: {item.get('item_id')}",
        f"Kind: {listing_kind}",
    ]
    price = item.get("current_price")
    currency = item.get("currency")
    if price is not None:
        if currency:
            lines.append(f"Price: {price} {currency}")
        else:
            lines.append(f"Price: {price}")
    if listing_kind == "bidding" and item.get("winning") is not None:
        lines.append(f"Winning: {bool(item.get('winning'))}")
    url = item.get("url")
    if url:
        lines.append(f"URL: {url}")
    return "\n".join(lines)
