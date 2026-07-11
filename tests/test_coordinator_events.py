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


def _coordinator() -> EbayDataUpdateCoordinator:
    entry = Mock(entry_id="entry-1")
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.entry = entry
    coordinator.options = DEFAULT_OPTIONS
    coordinator.data = None
    coordinator._event_callbacks = {}
    coordinator._scheduled = {}
    coordinator._fired_ending_soon = set()
    return coordinator


def test_ending_soon_event_is_not_lost_before_callback_registers() -> None:
    """Items already inside the threshold should fire after event setup."""
    coordinator = _coordinator()
    entry = coordinator.entry
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
    coordinator = _coordinator()
    entry = coordinator.entry
    scheduled_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
        minutes=30
    )
    end_time = scheduled_at + timedelta(
        seconds=coordinator.ending_soon_threshold_seconds
    )
    item = {
        "item_id": "123",
        "title": "Updated title",
        "end_time": end_time,
        "seconds_left": coordinator.ending_soon_threshold_seconds + (15 * 60),
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
    callback(scheduled_at)

    assert len(events) == 1
    assert events[0][1]["title"] == "Updated title"
    assert events[0][1]["price"] == 12.5
    assert events[0][1]["seconds_left"] == coordinator.ending_soon_threshold_seconds


def test_ending_soon_callback_ignores_changed_end_time() -> None:
    """A stale scheduled callback should not fire after eBay moves the end time."""
    coordinator = _coordinator()
    entry = coordinator.entry
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


def test_ended_items_do_not_emit_repeated_zero_to_zero_events() -> None:
    """Polling an already-ended item again should not emit another ended event."""
    coordinator = _coordinator()
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["watching"] = lambda *args: events.append(args)
    old = {"item_id": "123", "title": "Ended", "seconds_left": 0}
    new = {"item_id": "123", "title": "Ended", "seconds_left": 0}

    coordinator._detect_watching_events({"123": old}, {"123": new})

    assert events == []


def test_ended_item_disappearance_does_not_emit_second_ended_event() -> None:
    """An item that already reached zero should not emit ended again when removed."""
    coordinator = _coordinator()
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["bidding"] = lambda *args: events.append(args)
    already_ended = {"item_id": "123", "title": "Ended", "seconds_left": 0}

    coordinator._detect_bidding_events({"123": already_ended}, {})

    assert events == []


def test_watched_item_disappearance_emits_unknown_not_ended() -> None:
    """A missing watched item should not be reported as a confirmed ending."""
    coordinator = _coordinator()
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["watching"] = lambda *args: events.append(args)
    running = {"item_id": "123", "title": "Removed", "seconds_left": 600}

    coordinator._detect_watching_events({"123": running}, {})

    assert len(events) == 1
    assert events[0][0] == "watched_item_disappeared_unknown"
    assert events[0][1]["event_type"] == "watched_item_disappeared_unknown"
    assert events[0][1]["item_id"] == "123"


def test_bid_item_disappearance_emits_unknown_not_ended() -> None:
    """A missing bid item should not be reported as a confirmed ending."""
    coordinator = _coordinator()
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["bidding"] = lambda *args: events.append(args)
    running = {"item_id": "123", "title": "Removed", "seconds_left": 600}

    coordinator._detect_bidding_events({"123": running}, {})

    assert len(events) == 1
    assert events[0][0] == "bid_item_disappeared_unknown"
    assert events[0][1]["event_type"] == "bid_item_disappeared_unknown"
    assert events[0][1]["item_id"] == "123"


def test_ending_soon_ignores_items_already_ended() -> None:
    """Already-ended items should not qualify for ending-soon timers."""
    coordinator = _coordinator()
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["watching"] = lambda *args: events.append(args)
    end_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    payload: dict[str, Any] = {
        "watched": {
            "123": {
                "item_id": "123",
                "title": "Ended item",
                "end_time": end_time,
                "seconds_left": 0,
            }
        },
        "bidding": {},
        "selling": {},
        "summary": {},
    }

    coordinator._rebuild_ending_soon_timers(payload)

    assert events == []
    assert coordinator._scheduled == {}
