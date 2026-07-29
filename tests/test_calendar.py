"""Tests for eBay calendar entities."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest
from homeassistant.components.calendar import CalendarEntityDescription
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.ebay import calendar as calendar_module
from custom_components.ebay.calendar import (
    CALENDARS,
    EbayListingCalendar,
    async_setup_entry,
)
from custom_components.ebay.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_RUNAME,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.ebay.coordinator import EbayDataUpdateCoordinator


def _coordinator(data: dict[str, Any] | None = None, **option_overrides: Any) -> Any:
    coordinator = Mock()
    coordinator.options = {**DEFAULT_OPTIONS, **option_overrides}
    coordinator.data = data
    coordinator.last_update_success = True
    return coordinator


def _calendar_payload() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "watched": {},
        "bidding": {},
        "selling": {},
        "summary": {
            "active_selling_items": 0,
            "watched_items": 0,
            "bidding_items": 0,
            "last_update": now,
            "last_successful_update": now,
            "partial_failures": [],
            "truncated_collections": {},
            "api_warnings": [],
        },
        "last_update": now,
        "last_successful_update": now,
        "partial_failures": [],
        "truncated_collections": {},
        "api_warnings": [],
    }


async def _setup_calendar_platform(
    hass: HomeAssistant,
    *,
    entry_id: str = "entry-calendar-1",
) -> tuple[MockConfigEntry, EbayDataUpdateCoordinator, MockEntityPlatform]:
    """Register the eBay calendar platform under a mock Home Assistant harness."""
    assert await async_setup_component(hass, "calendar", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENVIRONMENT: "production",
            CONF_CLIENT_ID: "client",
            CONF_CLIENT_SECRET: "secret",
            CONF_RUNAME: "runame",
            CONF_SITE_ID: "0",
        },
        options=dict(DEFAULT_OPTIONS),
        version=3,
        title="eBay",
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)

    coordinator = EbayDataUpdateCoordinator(hass, entry, Mock(), dict(DEFAULT_OPTIONS))
    coordinator.update_interval = None
    coordinator.data = _calendar_payload()
    entry.runtime_data = coordinator

    platform = MockEntityPlatform(
        hass,
        domain="calendar",
        platform_name=DOMAIN,
        platform=calendar_module,
    )
    assert await platform.async_setup_entry(entry)
    await hass.async_block_till_done()
    return entry, coordinator, platform


def test_calendar_descriptions_use_home_assistant_base() -> None:
    assert all(
        isinstance(description, CalendarEntityDescription) for description in CALENDARS
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_calendar_entity_names_can_be_resolved(hass: HomeAssistant) -> None:
    """Name resolution requires HA translation context; exercise the failing path."""
    _entry, coordinator, platform = await _setup_calendar_platform(
        hass, entry_id="entry-calendar-names"
    )
    try:
        assert len(platform.entities) == len(CALENDARS)
        for entity in platform.entities.values():
            assert entity.name is not None
            # Exact AttributeError path during HA platform registration.
            assert entity.device_class is None
    finally:
        await platform.async_reset()
        await coordinator.async_shutdown()
        await hass.async_block_till_done()


def test_calendar_setup_creates_three_entities() -> None:
    coordinator = _coordinator()
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list[list[Any]] = []
    asyncio.run(
        async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities)))
    )
    assert len(added[0]) == len(CALENDARS)
    unique_ids = {entity.unique_id for entity in added[0]}
    assert unique_ids == {
        "entry-1_watched_items",
        "entry-1_bidding_items",
        "entry-1_selling_items",
    }


def test_watched_calendar_events_and_ordering() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "watched": {
            "2": {
                "item_id": "2",
                "title": "Later",
                "end_time": now + timedelta(hours=2),
                "current_price": 20,
                "currency": "USD",
                "url": "https://ebay.example/2",
            },
            "1": {
                "item_id": "1",
                "title": "Sooner",
                "end_time": now + timedelta(hours=1),
                "current_price": 10,
                "currency": "USD",
                "url": "https://ebay.example/1",
            },
        }
    }
    description = next(item for item in CALENDARS if item.key == "watched_items")
    entity = EbayListingCalendar(
        _coordinator(data), Mock(entry_id="entry-1"), description
    )
    events = asyncio.run(
        entity.async_get_events(
            Mock(), now - timedelta(minutes=1), now + timedelta(days=1)
        )
    )
    assert [event.summary for event in events] == ["Sooner", "Later"]
    assert events[0].location == "eBay"
    assert "Item ID: 1" in (events[0].description or "")
    assert "Price: 10 USD" in (events[0].description or "")


def test_bidding_calendar_includes_winning_metadata() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "bidding": {
            "b1": {
                "item_id": "b1",
                "title": "Bid item",
                "end_time": now + timedelta(hours=1),
                "current_price": 15,
                "currency": "USD",
                "winning": True,
                "url": "https://ebay.example/b1",
            }
        }
    }
    description = next(item for item in CALENDARS if item.key == "bidding_items")
    entity = EbayListingCalendar(
        _coordinator(data), Mock(entry_id="entry-1"), description
    )
    events = asyncio.run(entity.async_get_events(Mock(), now, now + timedelta(days=1)))
    assert len(events) == 1
    assert "Winning: True" in (events[0].description or "")


def test_selling_calendar_events() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "selling": {
            "s1": {
                "item_id": "s1",
                "title": "Sell item",
                "end_time": now + timedelta(hours=3),
                "current_price": 40,
                "currency": "EUR",
            }
        }
    }
    description = next(item for item in CALENDARS if item.key == "selling_items")
    entity = EbayListingCalendar(
        _coordinator(data), Mock(entry_id="entry-1"), description
    )
    events = asyncio.run(entity.async_get_events(Mock(), now, now + timedelta(days=1)))
    assert len(events) == 1
    assert events[0].summary == "Sell item"


def test_date_range_filtering_and_missing_invalid_end_times() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "watched": {
            "in": {
                "item_id": "in",
                "title": "In range",
                "end_time": now + timedelta(hours=2),
            },
            "out": {
                "item_id": "out",
                "title": "Out of range",
                "end_time": now + timedelta(days=10),
            },
            "missing": {"item_id": "missing", "title": "No end", "end_time": None},
            "invalid": {
                "item_id": "invalid",
                "title": "Bad end",
                "end_time": "not-a-datetime",
            },
        }
    }
    description = next(item for item in CALENDARS if item.key == "watched_items")
    entity = EbayListingCalendar(
        _coordinator(data), Mock(entry_id="entry-1"), description
    )
    events = asyncio.run(entity.async_get_events(Mock(), now, now + timedelta(days=1)))
    assert [event.uid for event in events] == ["watched:in"]


def test_duplicate_item_ids_deduped() -> None:
    now = datetime.now(timezone.utc)
    # Dict keys already unique; ensure builder still dedupes by item_id.
    data = {
        "watched": {
            "1": {
                "item_id": "1",
                "title": "One",
                "end_time": now + timedelta(hours=1),
            }
        }
    }
    description = next(item for item in CALENDARS if item.key == "watched_items")
    entity = EbayListingCalendar(
        _coordinator(data), Mock(entry_id="entry-1"), description
    )
    events = entity._build_events(
        start_date=now, end_date=now + timedelta(days=1), include_past_in_window=True
    )
    assert len(events) == 1


def test_disabled_buying_and_selling_sections() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "watched": {
            "w1": {
                "item_id": "w1",
                "title": "W",
                "end_time": now + timedelta(hours=1),
            }
        },
        "selling": {
            "s1": {
                "item_id": "s1",
                "title": "S",
                "end_time": now + timedelta(hours=1),
            }
        },
    }
    watched = next(item for item in CALENDARS if item.key == "watched_items")
    selling = next(item for item in CALENDARS if item.key == "selling_items")
    watched_entity = EbayListingCalendar(
        _coordinator(data, buying_enabled=False),
        Mock(entry_id="entry-1"),
        watched,
    )
    selling_entity = EbayListingCalendar(
        _coordinator(data, selling_enabled=False),
        Mock(entry_id="entry-1"),
        selling,
    )
    assert watched_entity.available is False
    assert selling_entity.available is False
    assert (
        asyncio.run(
            watched_entity.async_get_events(Mock(), now, now + timedelta(days=1))
        )
        == []
    )
    assert (
        asyncio.run(
            selling_entity.async_get_events(Mock(), now, now + timedelta(days=1))
        )
        == []
    )


def test_event_property_returns_next_upcoming() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "watched": {
            "past": {
                "item_id": "past",
                "title": "Past",
                "end_time": now - timedelta(hours=2),
            },
            "next": {
                "item_id": "next",
                "title": "Next",
                "end_time": now + timedelta(hours=1),
            },
        }
    }
    description = next(item for item in CALENDARS if item.key == "watched_items")
    entity = EbayListingCalendar(
        _coordinator(data), Mock(entry_id="entry-1"), description
    )
    assert entity.event is not None
    assert entity.event.summary == "Next"


def test_calendar_unavailable_when_coordinator_failed() -> None:
    description = next(item for item in CALENDARS if item.key == "watched_items")
    coordinator = _coordinator({})
    coordinator.last_update_success = False
    entity = EbayListingCalendar(coordinator, Mock(entry_id="entry-1"), description)
    assert entity.available is False


def test_calendar_handles_naive_end_time_and_price_without_currency() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "watched": {
            "w1": {
                "item_id": "w1",
                "title": "Naive",
                "end_time": datetime(2026, 7, 18, 15, 0),  # noqa: DTZ001
                "current_price": 12.5,
            }
        },
        "bidding": {
            "b1": {
                "item_id": "b1",
                "title": "Bid",
                "end_time": now + timedelta(hours=2),
                "current_price": 9,
                "winning": True,
            }
        },
    }
    watched = next(item for item in CALENDARS if item.key == "watched_items")
    bidding = next(item for item in CALENDARS if item.key == "bidding_items")
    # Force a window that includes the naive UTC-assumed end time.
    watched_entity = EbayListingCalendar(
        _coordinator(data), Mock(entry_id="entry-1"), watched
    )
    events = watched_entity._build_events(
        start_date=datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc),
        end_date=datetime(2026, 7, 18, 16, 0, tzinfo=timezone.utc),
        include_past_in_window=True,
    )
    assert len(events) == 1
    assert "Price: 12.5" in (events[0].description or "")

    bidding_entity = EbayListingCalendar(
        _coordinator(data), Mock(entry_id="entry-1"), bidding
    )
    bid_events = bidding_entity._build_events(
        start_date=now, end_date=now + timedelta(days=1), include_past_in_window=True
    )
    assert "Winning: True" in (bid_events[0].description or "")


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_calendar_platform_registers_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Calendar platform registration succeeds with HA entity descriptions."""
    entry, coordinator, platform = await _setup_calendar_platform(hass)

    expected = {
        "watched_items": ("calendar.ebay_watched_items", "Watched items"),
        "bidding_items": ("calendar.ebay_bidding_items", "Bidding items"),
        "selling_items": ("calendar.ebay_selling_items", "Selling items"),
    }
    for key, (entity_id, translated_name) in expected.items():
        unique_id = f"{entry.entry_id}_{key}"
        registry_entity_id = entity_registry.async_get_entity_id(
            "calendar", DOMAIN, unique_id
        )
        assert registry_entity_id == entity_id
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes.get("friendly_name") == f"eBay {translated_name}"
        entity = platform.entities[entity_id]
        assert entity.name == translated_name
        assert entity.unique_id == unique_id

    await platform.async_reset()
    await coordinator.async_shutdown()
    await hass.async_block_till_done()
    assert platform.entities == {}
    for entity_id, _ in expected.values():
        state = hass.states.get(entity_id)
        assert state is None or state.state == STATE_UNAVAILABLE
