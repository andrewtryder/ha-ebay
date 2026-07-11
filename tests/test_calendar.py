"""Tests for eBay calendar entities."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

from custom_components.ebay.calendar import (
    CALENDARS,
    EbayListingCalendar,
    async_setup_entry,
)
from custom_components.ebay.const import DEFAULT_OPTIONS


def _coordinator(data: dict[str, Any] | None = None, **option_overrides: Any) -> Any:
    coordinator = Mock()
    coordinator.options = {**DEFAULT_OPTIONS, **option_overrides}
    coordinator.data = data
    coordinator.last_update_success = True
    return coordinator


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
    events = asyncio.run(
        entity.async_get_events(Mock(), now, now + timedelta(days=1))
    )
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
    events = asyncio.run(
        entity.async_get_events(Mock(), now, now + timedelta(days=1))
    )
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
    events = asyncio.run(
        entity.async_get_events(Mock(), now, now + timedelta(days=1))
    )
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
