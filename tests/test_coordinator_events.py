"""Tests for coordinator event delivery."""

from __future__ import annotations

import logging
from collections import deque

import pytest

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

from custom_components.ebay.const import DEFAULT_OPTIONS, RECENT_EVENTS_MAX
from custom_components.ebay.coordinator import (
    EbayDataUpdateCoordinator,
    _normalized_end_time,
)


def _coordinator() -> EbayDataUpdateCoordinator:
    entry = Mock(entry_id="entry-1")
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.entry = entry
    coordinator.options = dict(DEFAULT_OPTIONS)
    coordinator.data = None
    coordinator.previous_payload = None
    coordinator._event_callbacks = {}
    coordinator._timer_listeners = []
    coordinator._scheduled = {}
    coordinator._fired_ending_soon = set()
    coordinator._price_drop_below_active = set()
    coordinator._recent_events = {
        "watching": deque(maxlen=RECENT_EVENTS_MAX),
        "bidding": deque(maxlen=RECENT_EVENTS_MAX),
        "selling": deque(maxlen=RECENT_EVENTS_MAX),
        "seller_ops": deque(maxlen=RECENT_EVENTS_MAX),
    }
    coordinator._recent_history_ending_soon = set()
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


def test_ending_soon_callback_uses_latest_item_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scheduled events should not emit stale data captured at schedule time."""
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.coordinator")
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
    assert (
        "Firing scheduled eBay ending-soon timer kind=watching item_id=123"
        in caplog.text
    )
    assert (
        "Emitted eBay event kind=watching event_type=watched_item_ending_soon item_id=123"
        in caplog.text
    )


def test_ending_soon_callback_ignores_changed_end_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale scheduled callback should not fire after eBay moves the end time."""
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.coordinator")
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
    assert (
        "Skipping stale eBay ending-soon timer kind=watching item_id=123" in caplog.text
    )


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
    field: str,
    event_type: str,
    old_value: int,
    new_value: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.coordinator")
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)
    old = {"item_id": "s1", "title": "Sell", field: old_value}
    new = {"item_id": "s1", "title": "Sell", field: new_value}
    coordinator._detect_selling_events(
        {"s1": old}, {"s1": new}, suppress_disappearance=False
    )
    assert events == [event_type]
    assert (
        f"Emitted eBay event kind=selling event_type={event_type} item_id=s1"
        in caplog.text
    )


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
    old = {
        "item_id": "w1",
        "current_price": 10.0,
        "currency": "USD",
        "bid_count": 1,
        "seconds_left": 100,
    }
    new = {
        "item_id": "w1",
        "current_price": 12.0,
        "currency": "USD",
        "bid_count": 2,
        "seconds_left": 90,
    }
    coordinator._detect_watching_events(
        {"w1": old}, {"w1": new}, suppress_disappearance=False
    )
    assert events == [
        "watched_item_price_changed",
        "watched_item_price_increased",
        "watched_item_bid_count_changed",
        "watched_item_bid_count_increased",
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


def test_selling_disappearance_classifies_sold_and_unsold() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)
    coordinator._detect_selling_events(
        {
            "sold1": {"item_id": "sold1", "title": "Sold"},
            "unsold1": {"item_id": "unsold1", "title": "Unsold"},
            "gone": {"item_id": "gone", "title": "Unknown"},
        },
        {},
        classification={"sold1": "sold", "unsold1": "unsold"},
        previous_sold_ids=set(),
        current_sold_ids={"sold1"},
        suppress_disappearance=False,
    )
    assert events == ["item_sold", "item_ended_unsold", "item_disappeared_unknown"]


def test_selling_sold_disappearance_does_not_double_fire_sale_completed() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)
    coordinator._detect_selling_events(
        {"sold1": {"item_id": "sold1", "title": "Sold"}},
        {},
        classification={"sold1": "sold"},
        previous_sold_ids=set(),
        current_sold_ids={"sold1"},
        suppress_disappearance=False,
    )
    assert events == ["item_sold"]


def test_selling_sale_completed_for_new_sold_list_member() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)
    coordinator._detect_selling_events(
        {"active": {"item_id": "active", "title": "Still active"}},
        {"active": {"item_id": "active", "title": "Still active"}},
        classification={"new_sold": "sold"},
        previous_sold_ids=set(),
        current_sold_ids={"new_sold"},
        suppress_disappearance=False,
    )
    assert events == ["item_sale_completed"]


def test_selling_sale_completed_skipped_without_previous_baseline() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)
    coordinator._detect_selling_events(
        {},
        {},
        classification={"new_sold": "sold"},
        previous_sold_ids=None,
        current_sold_ids={"new_sold"},
        suppress_disappearance=False,
    )
    assert events == []


def test_selling_incomplete_classification_keeps_unknown() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)
    coordinator._detect_selling_events(
        {"s1": {"item_id": "s1", "title": "Gone"}},
        {},
        classification={"s1": "sold"},
        classification_incomplete=True,
        previous_sold_ids=set(),
        current_sold_ids={"s1"},
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
    coordinator._event_callbacks["selling"] = lambda event_type, _event_data: (
        events.append(event_type)
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


def test_initial_baseline_emits_no_added_events() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)
    coordinator._event_callbacks["bidding"] = lambda et, _ed: events.append(et)
    coordinator._event_callbacks["selling"] = lambda et, _ed: events.append(et)
    current = {
        "watched": {"w1": {"item_id": "w1", "title": "W", "seconds_left": 100}},
        "bidding": {"b1": {"item_id": "b1", "title": "B", "seconds_left": 100}},
        "selling": {"s1": {"item_id": "s1", "title": "S"}},
        "truncated_collections": {},
    }
    coordinator._detect_transition_events(None, current)
    assert events == []


def test_reload_baseline_emits_no_added_events() -> None:
    """Coordinator reconstruction (reload) starts with previous_payload None."""
    coordinator = _coordinator()
    assert coordinator.previous_payload is None
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)
    payload = {
        "watched": {"w1": {"item_id": "w1", "seconds_left": 100}},
        "bidding": {},
        "selling": {},
        "truncated_collections": {},
    }
    coordinator._detect_transition_events(coordinator.previous_payload, payload)
    assert events == []


def test_watched_item_added_after_baseline() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)
    previous = {"w1": {"item_id": "w1", "title": "Old", "seconds_left": 100}}
    current = {
        "w1": {"item_id": "w1", "title": "Old", "seconds_left": 90},
        "w2": {"item_id": "w2", "title": "New", "seconds_left": 200},
    }
    coordinator._detect_watching_events(previous, current, suppress_incomplete=False)
    assert events == ["watched_item_added"]


def test_bidding_and_selling_item_added_after_baseline() -> None:
    coordinator = _coordinator()
    events: list[tuple[str, str]] = []
    coordinator._event_callbacks["bidding"] = lambda et, _ed: events.append(
        ("bidding", et)
    )
    coordinator._event_callbacks["selling"] = lambda et, _ed: events.append(
        ("selling", et)
    )
    coordinator._detect_bidding_events(
        {},
        {"b1": {"item_id": "b1", "winning": False, "seconds_left": 100}},
        suppress_incomplete=False,
    )
    coordinator._detect_selling_events(
        {},
        {"s1": {"item_id": "s1", "title": "Sell"}},
        suppress_incomplete=False,
    )
    assert events == [
        ("bidding", "bid_item_added"),
        ("selling", "selling_item_added"),
    ]


def test_repeated_poll_does_not_duplicate_added_event() -> None:
    from custom_components.ebay.coordinator import _baseline_payload

    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)
    baseline = {
        "watched": {"w1": {"item_id": "w1", "seconds_left": 100}},
        "bidding": {},
        "selling": {},
        "truncated_collections": {},
    }
    with_new = {
        "watched": {
            "w1": {"item_id": "w1", "seconds_left": 90},
            "w2": {"item_id": "w2", "seconds_left": 200},
        },
        "bidding": {},
        "selling": {},
        "truncated_collections": {},
    }
    coordinator._detect_transition_events(baseline, with_new)
    assert events == ["watched_item_added"]
    events.clear()
    baseline = _baseline_payload(baseline, with_new)
    coordinator._detect_transition_events(baseline, with_new)
    assert events == []


def test_truncated_previous_or_current_suppresses_added_event() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)
    previous = {
        "watched": {"w1": {"item_id": "w1", "seconds_left": 100}},
        "bidding": {},
        "selling": {},
        "truncated_collections": {"watched": True},
    }
    current = {
        "watched": {
            "w1": {"item_id": "w1", "seconds_left": 90},
            "w2": {"item_id": "w2", "seconds_left": 200},
        },
        "bidding": {},
        "selling": {},
        "truncated_collections": {},
    }
    coordinator._detect_transition_events(previous, current)
    assert "watched_item_added" not in events

    events.clear()
    previous["truncated_collections"] = {}
    current["truncated_collections"] = {"watched": True}
    coordinator._detect_transition_events(previous, current)
    assert "watched_item_added" not in events


def test_failed_refresh_recovery_does_not_false_add() -> None:
    """Failed refresh leaves previous_payload untouched."""
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)
    baseline = {
        "watched": {"w1": {"item_id": "w1", "seconds_left": 100}},
        "bidding": {},
        "selling": {},
        "truncated_collections": {},
    }
    coordinator.previous_payload = baseline
    # Simulate failed refresh: previous_payload unchanged, then same data recovers.
    coordinator._detect_transition_events(coordinator.previous_payload, baseline)
    assert events == []


def test_active_watched_disappearance_remains_unknown() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    payloads: list[dict[str, Any]] = []
    coordinator._event_callbacks["watching"] = lambda et, ed: (
        events.append(et),
        payloads.append(ed),
    )
    running = {
        "item_id": "123",
        "title": "Still listed somewhere",
        "seconds_left": 600,
        "end_time": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    coordinator._detect_watching_events({"123": running}, {}, suppress_incomplete=False)
    assert events == ["watched_item_disappeared_unknown"]
    assert "watched_item_removed" not in events


def test_addition_and_disappearance_across_polls() -> None:
    from custom_components.ebay.coordinator import _baseline_payload

    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)
    poll1 = {
        "watched": {"w1": {"item_id": "w1", "seconds_left": 100}},
        "bidding": {},
        "selling": {},
        "truncated_collections": {},
    }
    poll2 = {
        "watched": {
            "w1": {"item_id": "w1", "seconds_left": 90},
            "w2": {"item_id": "w2", "seconds_left": 200},
        },
        "bidding": {},
        "selling": {},
        "truncated_collections": {},
    }
    poll3 = {
        "watched": {"w2": {"item_id": "w2", "seconds_left": 180}},
        "bidding": {},
        "selling": {},
        "truncated_collections": {},
    }
    baseline = poll1
    coordinator._detect_transition_events(baseline, poll2)
    assert events == ["watched_item_added"]
    baseline = _baseline_payload(baseline, poll2)
    events.clear()
    coordinator._detect_transition_events(baseline, poll3)
    assert events == ["watched_item_disappeared_unknown"]


def test_event_type_declarations_include_new_types() -> None:
    from custom_components.ebay.const import (
        BIDDING_EVENT_TYPES,
        SELLING_EVENT_TYPES,
        WATCHING_EVENT_TYPES,
    )

    assert "watched_item_added" in WATCHING_EVENT_TYPES
    assert "watched_item_removed" not in WATCHING_EVENT_TYPES
    assert "watched_item_price_increased" in WATCHING_EVENT_TYPES
    assert "watched_item_price_decreased" in WATCHING_EVENT_TYPES
    assert "watched_item_price_dropped_below" in WATCHING_EVENT_TYPES
    assert "watched_item_bid_count_increased" in WATCHING_EVENT_TYPES
    assert "bid_item_added" in BIDDING_EVENT_TYPES
    assert "selling_item_added" in SELLING_EVENT_TYPES


def test_price_increase_and_decrease_with_threshold() -> None:
    coordinator = _coordinator()
    coordinator.options["watched_price_change_min_percent"] = 10
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["watching"] = lambda et, ed: events.append((et, ed))

    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": 100,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 112,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=False,
    )
    assert [et for et, _ in events] == [
        "watched_item_price_changed",
        "watched_item_price_increased",
    ]
    assert events[-1][1]["direction"] == "increased"
    assert events[-1][1]["percent_change"] == 12

    events.clear()
    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": 100,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 105,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=False,
    )
    assert events == []

    events.clear()
    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": 100,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 80,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=False,
    )
    assert [et for et, _ in events] == [
        "watched_item_price_changed",
        "watched_item_price_decreased",
    ]


def test_price_unchanged_missing_zero_currency_mismatch() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)

    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": 10,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 10,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=False,
    )
    assert events == []

    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": None,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 5,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=False,
    )
    assert events == []

    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": 0,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 5,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=False,
    )
    assert events == []

    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": 10,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 5,
                "currency": "EUR",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=False,
    )
    assert events == []


def test_global_and_per_item_dropped_below() -> None:
    coordinator = _coordinator()
    coordinator.options["watched_price_drop_threshold"] = "100"
    coordinator.options["watched_price_drop_currency"] = "USD"
    coordinator.options["pinned_item_price_targets"] = "w2=50 USD"
    coordinator.options["pinned_item_ids"] = "w2"
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator._event_callbacks["watching"] = lambda et, ed: events.append((et, ed))

    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": 120,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 90,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=False,
    )
    drop_events = [ed for et, ed in events if et == "watched_item_price_dropped_below"]
    assert len(drop_events) == 1
    assert drop_events[0]["threshold_source"] == "global"
    assert drop_events[0]["threshold"] == 100

    events.clear()
    # No repeat while remaining below.
    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": 90,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 85,
                "currency": "USD",
                "seconds_left": 8,
            }
        },
        suppress_incomplete=False,
    )
    assert not any(et == "watched_item_price_dropped_below" for et, _ in events)

    events.clear()
    coordinator._detect_watching_events(
        {
            "w2": {
                "item_id": "w2",
                "current_price": 80,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w2": {
                "item_id": "w2",
                "current_price": 40,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=False,
    )
    drop_events = [ed for et, ed in events if et == "watched_item_price_dropped_below"]
    assert len(drop_events) == 1
    assert drop_events[0]["threshold_source"] == "pinned_item"
    assert drop_events[0]["threshold"] == 50


def test_price_drop_below_clears_on_unwatch_rewatch_recross() -> None:
    """Stale threshold-active state must not suppress a post-rewatch crossing."""
    coordinator = _coordinator()
    coordinator.options["watched_price_drop_threshold"] = "100"
    coordinator.options["watched_price_drop_currency"] = "USD"
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)

    above = {
        "item_id": "w1",
        "current_price": 120,
        "currency": "USD",
        "seconds_left": 10,
    }
    below = {
        "item_id": "w1",
        "current_price": 90,
        "currency": "USD",
        "seconds_left": 9,
    }

    coordinator._detect_watching_events(
        {"w1": above},
        {"w1": below},
        suppress_incomplete=False,
    )
    assert "watched_item_price_dropped_below" in events
    assert "w1" in coordinator._price_drop_below_active

    events.clear()
    coordinator._detect_watching_events(
        {"w1": below},
        {},
        suppress_incomplete=False,
    )
    assert events == ["watched_item_disappeared_unknown"]
    assert "w1" not in coordinator._price_drop_below_active

    events.clear()
    coordinator._detect_watching_events(
        {},
        {"w1": above},
        suppress_incomplete=False,
    )
    assert events == ["watched_item_added"]
    assert "w1" not in coordinator._price_drop_below_active

    events.clear()
    coordinator._detect_watching_events(
        {"w1": above},
        {"w1": below},
        suppress_incomplete=False,
    )
    assert "watched_item_price_dropped_below" in events
    assert "w1" in coordinator._price_drop_below_active


def test_bid_count_increase_versus_decrease() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)
    coordinator._detect_watching_events(
        {"w1": {"item_id": "w1", "bid_count": 2, "seconds_left": 10}},
        {"w1": {"item_id": "w1", "bid_count": 3, "seconds_left": 9}},
        suppress_incomplete=False,
    )
    assert events == [
        "watched_item_bid_count_changed",
        "watched_item_bid_count_increased",
    ]
    events.clear()
    coordinator._detect_watching_events(
        {"w1": {"item_id": "w1", "bid_count": 3, "seconds_left": 10}},
        {"w1": {"item_id": "w1", "bid_count": 1, "seconds_left": 9}},
        suppress_incomplete=False,
    )
    assert events == ["watched_item_bid_count_changed"]


def test_seller_ops_order_and_message_events() -> None:
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["seller_ops"] = lambda et, _ed: events.append(et)
    previous = {
        "orders": {"by_id": {}},
        "disputes": {"by_id": {}},
        "standards": {"seller_level": "TOP_RATED", "at_risk": False},
        "feedback": {
            "score": 10,
            "recent_positive": 1,
            "recent_neutral": 0,
            "recent_negative": 0,
        },
        "messages": {"by_id": {}},
    }
    current = {
        "orders": {
            "by_id": {
                "o1": {
                    "order_id": "o1",
                    "item_id": "111",
                    "paid": True,
                    "shipped": False,
                    "has_tracking": False,
                    "due_soon": True,
                    "overdue": False,
                }
            }
        },
        "disputes": {
            "by_id": {"d1": {"dispute_id": "d1", "order_id": "o1", "reason": "FRAUD"}}
        },
        "standards": {"seller_level": "ABOVE_STANDARD", "at_risk": True},
        "feedback": {
            "score": 11,
            "recent_positive": 1,
            "recent_neutral": 0,
            "recent_negative": 1,
        },
        "messages": {
            "by_id": {
                "c1": {
                    "conversation_id": "c1",
                    "listing_id": "111",
                    "subject": "Question",
                    "is_buyer_question": True,
                }
            }
        },
    }
    coordinator._detect_seller_ops_events(previous, current, suppress_incomplete=False)
    assert "order_received" in events
    assert "order_paid" in events
    assert "shipment_due_soon" in events
    assert "payment_dispute_opened" in events
    assert "seller_level_changed" in events
    assert "seller_standard_at_risk" in events
    assert "feedback_received" in events
    assert "negative_feedback_received" in events
    assert "feedback_rating_changed" in events
    assert "new_buyer_question" in events


def test_seller_ops_suppress_incomplete_blocks_new_events() -> None:
    """Truncated seller-ops collections must not emit new-entity events."""
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["seller_ops"] = lambda et, _ed: events.append(et)
    previous = {
        "orders": {"by_id": {}},
        "disputes": {"by_id": {}},
        "standards": {},
        "feedback": {},
        "messages": {"by_id": {}},
    }
    current = {
        "orders": {
            "by_id": {
                "o1": {
                    "order_id": "o1",
                    "paid": True,
                    "shipped": False,
                    "has_tracking": False,
                    "due_soon": True,
                    "overdue": False,
                }
            }
        },
        "disputes": {"by_id": {"d1": {"dispute_id": "d1", "order_id": "o1"}}},
        "standards": {},
        "feedback": {},
        "messages": {
            "by_id": {
                "c1": {
                    "conversation_id": "c1",
                    "is_buyer_question": False,
                    "subject": "Hello",
                }
            }
        },
    }
    coordinator._detect_seller_ops_events(previous, current, suppress_incomplete=True)
    assert events == []


def test_seller_ops_existing_order_and_message_variants() -> None:
    """Existing-order transitions and non-question messages emit distinct events."""
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["seller_ops"] = lambda et, _ed: events.append(et)
    previous = {
        "orders": {
            "by_id": {
                "o1": {
                    "order_id": "o1",
                    "paid": False,
                    "shipped": False,
                    "has_tracking": False,
                    "due_soon": False,
                    "overdue": False,
                }
            }
        },
        "disputes": {"by_id": {}},
        "standards": {"item_not_received_above_benchmark": False},
        "feedback": {},
        "messages": {"by_id": {}},
    }
    current = {
        "orders": {
            "by_id": {
                "o1": {
                    "order_id": "o1",
                    "paid": True,
                    "shipped": True,
                    "has_tracking": True,
                    "due_soon": False,
                    "overdue": True,
                }
            }
        },
        "disputes": {"by_id": {}},
        "standards": {"item_not_received_above_benchmark": True},
        "feedback": {},
        "messages": {
            "by_id": {
                "c1": {
                    "conversation_id": "c1",
                    "listing_id": "111",
                    "subject": "Thanks",
                    "is_buyer_question": False,
                }
            }
        },
    }
    coordinator._detect_seller_ops_events(previous, current, suppress_incomplete=False)
    assert "order_paid" in events
    assert "order_marked_shipped" in events
    assert "tracking_added" in events
    assert "shipment_overdue" in events
    assert "service_metric_above_peer_benchmark" in events
    assert "new_message_received" in events
    assert "new_buyer_question" not in events


def test_watched_truncation_suppresses_price_dropped_below() -> None:
    """Incomplete watched snapshots must not emit drop-below threshold events."""
    coordinator = _coordinator()
    coordinator.options["watched_price_drop_threshold"] = "100"
    coordinator.options["watched_price_drop_currency"] = "USD"
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, _ed: events.append(et)

    coordinator._detect_watching_events(
        {
            "w1": {
                "item_id": "w1",
                "current_price": 120,
                "currency": "USD",
                "seconds_left": 10,
            }
        },
        {
            "w1": {
                "item_id": "w1",
                "current_price": 90,
                "currency": "USD",
                "seconds_left": 9,
            }
        },
        suppress_incomplete=True,
    )
    assert "watched_item_price_dropped_below" not in events


def test_sold_unsold_classification_partial_keeps_unknown() -> None:
    """partial_failures sold_unsold_classification marks classification incomplete."""
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, _ed: events.append(et)
    previous = {
        "selling": {"s1": {"item_id": "s1", "title": "Gone"}},
        "watched": {},
        "bidding": {},
        "sold_item_ids": [],
        "truncated_collections": {},
        "partial_failures": [],
    }
    current = {
        "selling": {},
        "watched": {},
        "bidding": {},
        "selling_classification": {"s1": "sold"},
        "sold_item_ids": ["s1"],
        "truncated_collections": {},
        "partial_failures": ["sold_unsold_classification"],
    }
    coordinator._detect_transition_events(previous, current)
    assert events == ["item_disappeared_unknown"]
    assert "item_sold" not in events
