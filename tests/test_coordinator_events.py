"""Tests for coordinator event delivery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

from custom_components.ebay.const import DEFAULT_OPTIONS
from custom_components.ebay.coordinator import (
    EbayDataUpdateCoordinator,
    _normalized_end_time,
)


def test_ending_soon_event_is_not_lost_before_callback_registers() -> None:
    """Items already inside the threshold should fire after event setup."""
    entry = Mock(entry_id="entry-1")
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.entry = entry
    coordinator.options = DEFAULT_OPTIONS
    coordinator.data = None
    coordinator._event_callbacks = {}
    coordinator._scheduled = {}
    coordinator._fired_ending_soon = set()
    item = {
        "item_id": "123",
        "title": "Watched item",
        "end_time": datetime.now(timezone.utc) + timedelta(minutes=30),
        "seconds_left": 30 * 60,
    }
    payload: dict[str, Any] = {
        "watched": {"123": item},
        "bidding": {},
        "selling": {},
        "summary": {},
    }

    coordinator._rebuild_ending_soon_timers(payload)

    key = (
        entry.entry_id,
        "watching",
        item["item_id"],
        coordinator.ending_soon_threshold_seconds,
        _normalized_end_time(item["end_time"]),
    )
    assert key not in coordinator._fired_ending_soon

    events: list[tuple[str, dict[str, Any]]] = []
    coordinator.data = payload
    coordinator.register_event_callback("watching", lambda *args: events.append(args))

    assert len(events) == 1
    assert events[0][0] == "watched_item_ending_soon"
    assert events[0][1]["item_id"] == "123"
    assert key in coordinator._fired_ending_soon


def test_ending_soon_callback_uses_latest_item_data() -> None:
    """Scheduled events should not emit stale data captured at schedule time."""
    entry = Mock(entry_id="entry-1")
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.entry = entry
    coordinator.options = DEFAULT_OPTIONS
    coordinator._event_callbacks = {}
    coordinator._scheduled = {}
    coordinator._fired_ending_soon = set()
    end_time = datetime.now(timezone.utc) + timedelta(minutes=90)
    item = {
        "item_id": "123",
        "title": "Updated title",
        "end_time": end_time,
        "seconds_left": 60 * 60,
        "current_price": 12.5,
    }
    coordinator.data = {
        "watched": {"123": item},
        "bidding": {},
        "selling": {},
        "summary": {},
    }
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["watching"] = lambda *args: events.append(args)
    key = (
        entry.entry_id,
        "watching",
        "123",
        coordinator.ending_soon_threshold_seconds,
        _normalized_end_time(end_time),
    )

    callback = coordinator._scheduled_callback(
        "watching",
        "watched_item_ending_soon",
        "123",
        _normalized_end_time(end_time),
        key,
    )
    callback(datetime.now(timezone.utc))

    assert len(events) == 1
    assert events[0][1]["title"] == "Updated title"
    assert events[0][1]["price"] == 12.5


def test_ending_soon_callback_ignores_changed_end_time() -> None:
    """A stale scheduled callback should not fire after eBay moves the end time."""
    entry = Mock(entry_id="entry-1")
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.entry = entry
    coordinator.options = DEFAULT_OPTIONS
    coordinator._event_callbacks = {}
    coordinator._scheduled = {}
    coordinator._fired_ending_soon = set()
    original_end_time = datetime.now(timezone.utc) + timedelta(minutes=60)
    current_end_time = original_end_time + timedelta(days=1)
    coordinator.data = {
        "watched": {
            "123": {
                "item_id": "123",
                "title": "Extended auction",
                "end_time": current_end_time,
                "seconds_left": 25 * 60 * 60,
            }
        },
        "bidding": {},
        "selling": {},
        "summary": {},
    }
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["watching"] = lambda *args: events.append(args)
    key = (
        entry.entry_id,
        "watching",
        "123",
        coordinator.ending_soon_threshold_seconds,
        _normalized_end_time(original_end_time),
    )

    callback = coordinator._scheduled_callback(
        "watching",
        "watched_item_ending_soon",
        "123",
        _normalized_end_time(original_end_time),
        key,
    )
    callback(datetime.now(timezone.utc))

    assert events == []
