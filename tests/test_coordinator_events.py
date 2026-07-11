"""Tests for coordinator event delivery."""

from __future__ import annotations

import pytest

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

    coordinator._detect_watching_events(
        {"123": old}, {"123": new}, suppress_disappearance=False
    )

    assert events == []


def test_ended_item_disappearance_does_not_emit_second_ended_event() -> None:
    """An item that already reached zero should not emit ended again when removed."""
    coordinator = _coordinator()
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["bidding"] = lambda *args: events.append(args)
    already_ended = {"item_id": "123", "title": "Ended", "seconds_left": 0}

    coordinator._detect_bidding_events(
        {"123": already_ended}, {}, suppress_disappearance=False
    )

    assert events == []


def test_watched_item_disappearance_emits_unknown_not_ended() -> None:
    """A missing watched item should not be reported as a confirmed ending."""
    coordinator = _coordinator()
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["watching"] = lambda *args: events.append(args)
    running = {"item_id": "123", "title": "Removed", "seconds_left": 600}

    coordinator._detect_watching_events(
        {"123": running}, {}, suppress_disappearance=False
    )

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

    coordinator._detect_bidding_events(
        {"123": running}, {}, suppress_disappearance=False
    )

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


def test_no_events_on_initial_startup() -> None:
    """First successful payload should not emit transition events."""
    coordinator = _coordinator()
    events: list[tuple[str, str]] = []

    def capture(kind: str):
        def _cb(event_type: str, event_data: dict[str, Any]) -> None:
            events.append((kind, event_type))

        return _cb

    coordinator._event_callbacks["selling"] = capture("selling")
    coordinator._event_callbacks["watching"] = capture("watching")
    coordinator._event_callbacks["bidding"] = capture("bidding")
    current = {
        "selling": {"s1": {"item_id": "s1", "bid_count": 2, "seconds_left": 100}},
        "watched": {"w1": {"item_id": "w1", "current_price": 5, "seconds_left": 100}},
        "bidding": {"b1": {"item_id": "b1", "winning": True, "seconds_left": 100}},
        "truncated_collections": {},
    }
    coordinator._detect_transition_events(None, current)
    assert events == []


@pytest.mark.parametrize(
    ("field", "event_type", "old_value", "new_value"),
    [
        ("bid_count", "bid_count_increased", 1, 2),
        ("offers", "offer_received", 0, 1),
        ("questions", "question_received", 0, 1),
        ("watchers", "watcher_count_increased", 3, 4),
        ("quantity_sold", "quantity_sold_increased", 1, 2),
    ],
)
def test_selling_increase_events(
    field: str, event_type: str, old_value: int, new_value: int
) -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)
    old = {"item_id": "s1", "title": "Sell", field: old_value}
    new = {"item_id": "s1", "title": "Sell", field: new_value}
    coordinator._detect_selling_events(
        {"s1": old}, {"s1": new}, suppress_disappearance=False
    )
    assert events == [event_type]


@pytest.mark.parametrize(
    ("field", "event_type", "old_value", "new_value"),
    [
        ("bid_count", "bid_count_increased", 2, 2),
        ("offers", "offer_received", 2, 1),
        ("questions", "question_received", 1, 0),
    ],
)
def test_selling_increase_only_counters_ignore_unchanged_or_decrease(
    field: str, event_type: str, old_value: int, new_value: int
) -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)
    old = {"item_id": "s1", field: old_value}
    new = {"item_id": "s1", field: new_value}
    coordinator._detect_selling_events(
        {"s1": old}, {"s1": new}, suppress_disappearance=False
    )
    assert events == []


def test_watched_price_and_bid_count_changes() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, ed: events.append(et)
    old = {"item_id": "w1", "current_price": 10.0, "bid_count": 1, "seconds_left": 100}
    new = {"item_id": "w1", "current_price": 12.0, "bid_count": 2, "seconds_left": 90}
    coordinator._detect_watching_events(
        {"w1": old}, {"w1": new}, suppress_disappearance=False
    )
    assert events == [
        "watched_item_price_changed",
        "watched_item_bid_count_changed",
    ]


def test_watched_item_ended_positive() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, ed: events.append(et)
    old = {"item_id": "w1", "seconds_left": 30}
    new = {"item_id": "w1", "seconds_left": 0}
    coordinator._detect_watching_events(
        {"w1": old}, {"w1": new}, suppress_disappearance=False
    )
    assert events == ["watched_item_ended"]


def test_winning_and_outbid_transitions() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["bidding"] = lambda et, ed: events.append(et)
    coordinator._detect_bidding_events(
        {"b1": {"item_id": "b1", "winning": True, "seconds_left": 100}},
        {"b1": {"item_id": "b1", "winning": False, "seconds_left": 90}},
        suppress_disappearance=False,
    )
    coordinator._detect_bidding_events(
        {"b2": {"item_id": "b2", "winning": False, "seconds_left": 100}},
        {"b2": {"item_id": "b2", "winning": True, "seconds_left": 90}},
        suppress_disappearance=False,
    )
    assert events == ["outbid", "winning"]


def test_bid_item_ended_positive() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["bidding"] = lambda et, ed: events.append(et)
    coordinator._detect_bidding_events(
        {"b1": {"item_id": "b1", "winning": True, "seconds_left": 10}},
        {"b1": {"item_id": "b1", "winning": True, "seconds_left": 0}},
        suppress_disappearance=False,
    )
    assert events == ["bid_item_ended"]


def test_selling_disappearance_emits_unknown() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)
    coordinator._detect_selling_events(
        {"s1": {"item_id": "s1", "title": "Gone"}},
        {},
        suppress_disappearance=False,
    )
    assert events == ["item_disappeared_unknown"]


def test_truncated_snapshot_suppresses_disappearance_events() -> None:
    coordinator = _coordinator()
    events: list[tuple[str, str]] = []

    def capture(kind: str):
        def _cb(event_type: str, event_data: dict[str, Any]) -> None:
            events.append((kind, event_type))

        return _cb

    coordinator._event_callbacks["selling"] = capture("selling")
    coordinator._event_callbacks["watching"] = capture("watching")
    coordinator._event_callbacks["bidding"] = capture("bidding")
    previous = {
        "selling": {"s1": {"item_id": "s1", "bid_count": 1}, "s2": {"item_id": "s2"}},
        "watched": {"w1": {"item_id": "w1", "seconds_left": 100}},
        "bidding": {"b1": {"item_id": "b1", "seconds_left": 100}},
        "truncated_collections": {},
    }
    current = {
        "selling": {"s1": {"item_id": "s1", "bid_count": 2}},
        "watched": {},
        "bidding": {},
        "truncated_collections": {
            "selling": True,
            "watched": True,
            "bidding": True,
        },
    }
    coordinator._detect_transition_events(previous, current)
    assert ("selling", "item_disappeared_unknown") not in events
    assert ("watching", "watched_item_disappeared_unknown") not in events
    assert ("bidding", "bid_item_disappeared_unknown") not in events
    assert ("selling", "bid_count_increased") in events


def test_truncated_baseline_merges_overlapping_items() -> None:
    from custom_components.ebay.coordinator import _baseline_payload

    previous = {
        "selling": {
            "s1": {"item_id": "s1", "bid_count": 1},
            "s2": {"item_id": "s2", "bid_count": 0},
        },
        "watched": {},
        "bidding": {},
        "truncated_collections": {},
    }
    current = {
        "selling": {"s1": {"item_id": "s1", "bid_count": 2}},
        "watched": {},
        "bidding": {},
        "summary": {"x": 1},
        "truncated_collections": {"selling": True, "watched": False, "bidding": False},
        "partial_failures": ["selling_truncated"],
    }
    baseline = _baseline_payload(previous, current)
    assert baseline["selling"] == {
        "s1": {"item_id": "s1", "bid_count": 2},
        "s2": {"item_id": "s2", "bid_count": 0},
    }
    assert baseline["summary"] == current["summary"]
    assert baseline["truncated_collections"]["selling"] is True


def test_truncated_baseline_merge_avoids_repeated_change_events() -> None:
    """Overlapping truncated polls must not re-emit the same increase."""
    from custom_components.ebay.coordinator import _baseline_payload

    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = (
        lambda event_type, _event_data: events.append(event_type)
    )

    complete_1 = {
        "selling": {"s1": {"item_id": "s1", "bid_count": 1}},
        "watched": {},
        "bidding": {},
        "truncated_collections": {},
    }
    truncated_2 = {
        "selling": {"s1": {"item_id": "s1", "bid_count": 2}},
        "watched": {},
        "bidding": {},
        "truncated_collections": {"selling": True},
    }
    complete_2 = {
        "selling": {"s1": {"item_id": "s1", "bid_count": 2}},
        "watched": {},
        "bidding": {},
        "truncated_collections": {},
    }
    complete_3 = {
        "selling": {"s1": {"item_id": "s1", "bid_count": 3}},
        "watched": {},
        "bidding": {},
        "truncated_collections": {},
    }

    baseline = complete_1

    coordinator._detect_transition_events(baseline, truncated_2)
    assert events == ["bid_count_increased"]
    baseline = _baseline_payload(baseline, truncated_2)
    events.clear()

    coordinator._detect_transition_events(baseline, truncated_2)
    assert events == []
    baseline = _baseline_payload(baseline, truncated_2)

    coordinator._detect_transition_events(baseline, complete_2)
    assert events == []
    baseline = _baseline_payload(baseline, complete_2)

    coordinator._detect_transition_events(baseline, complete_3)
    assert events == ["bid_count_increased"]
