"""Tests for bounded recent-activity history."""

from __future__ import annotations

from collections import deque
from typing import Any
from unittest.mock import Mock

from custom_components.ebay.const import DEFAULT_OPTIONS, RECENT_EVENTS_MAX
from custom_components.ebay.coordinator import EbayDataUpdateCoordinator
from custom_components.ebay.event import EbayActivityEvent, EVENTS


def _coordinator() -> EbayDataUpdateCoordinator:
    entry = Mock(entry_id="entry-1")
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.entry = entry
    coordinator.options = dict(DEFAULT_OPTIONS)
    coordinator.data = None
    coordinator.previous_payload = None
    coordinator._event_callbacks = {}
    coordinator._scheduled = {}
    coordinator._fired_ending_soon = set()
    coordinator._price_drop_below_active = set()
    coordinator._recent_events = {
        "watching": deque(maxlen=RECENT_EVENTS_MAX),
        "bidding": deque(maxlen=RECENT_EVENTS_MAX),
        "selling": deque(maxlen=RECENT_EVENTS_MAX),
    }
    coordinator._recent_history_ending_soon = set()
    return coordinator


def test_history_begins_empty() -> None:
    coordinator = _coordinator()
    assert coordinator.recent_events("watching") == []
    assert coordinator.recent_events("bidding") == []
    assert coordinator.recent_events("selling") == []


def test_event_appends_once_and_skips_legacy_alias() -> None:
    coordinator = _coordinator()
    item = {
        "item_id": "w1",
        "title": "Watch",
        "current_price": 12,
        "currency": "USD",
        "seconds_left": 100,
    }
    coordinator._emit(
        "watching",
        "watched_item_price_changed",
        item,
        old_value=10,
        new_value=12,
        direction="increased",
    )
    assert coordinator.recent_events("watching") == []
    coordinator._emit(
        "watching",
        "watched_item_price_increased",
        item,
        old_value=10,
        new_value=12,
        direction="increased",
    )
    history = coordinator.recent_events("watching")
    assert len(history) == 1
    assert history[0]["event_type"] == "watched_item_price_increased"
    assert "image" not in history[0]


def test_history_ordering_and_max_length() -> None:
    coordinator = _coordinator()
    for index in range(RECENT_EVENTS_MAX + 5):
        coordinator._emit(
            "selling",
            "selling_item_added",
            {"item_id": str(index), "title": f"Item {index}"},
        )
    history = coordinator.recent_events("selling")
    assert len(history) == RECENT_EVENTS_MAX
    assert history[0]["item_id"] == str(RECENT_EVENTS_MAX + 4)
    assert history[-1]["item_id"] == "5"


def test_histories_remain_isolated() -> None:
    coordinator = _coordinator()
    coordinator._emit("watching", "watched_item_added", {"item_id": "w1", "title": "W"})
    coordinator._emit("bidding", "bid_item_added", {"item_id": "b1", "title": "B"})
    assert [item["item_id"] for item in coordinator.recent_events("watching")] == ["w1"]
    assert [item["item_id"] for item in coordinator.recent_events("bidding")] == ["b1"]
    assert coordinator.recent_events("selling") == []


def test_reconstruction_resets_history() -> None:
    first = _coordinator()
    first._emit("watching", "watched_item_added", {"item_id": "w1", "title": "W"})
    second = _coordinator()
    assert second.recent_events("watching") == []


def test_disabled_entity_still_records_history() -> None:
    """History must not depend on an EventEntity callback being registered."""
    coordinator = _coordinator()
    assert coordinator._event_callbacks == {}
    coordinator._emit(
        "watching",
        "watched_item_added",
        {"item_id": "w1", "title": "W", "seconds_left": 10},
    )
    assert len(coordinator.recent_events("watching")) == 1


def test_startup_baseline_does_not_add_history() -> None:
    coordinator = _coordinator()
    coordinator._detect_transition_events(
        None,
        {
            "watched": {"w1": {"item_id": "w1", "seconds_left": 10}},
            "bidding": {},
            "selling": {},
            "truncated_collections": {},
        },
    )
    assert coordinator.recent_events("watching") == []


def test_event_entity_exposes_kind_history_only() -> None:
    coordinator = _coordinator()
    coordinator._emit("watching", "watched_item_added", {"item_id": "w1", "title": "W"})
    coordinator._emit("selling", "selling_item_added", {"item_id": "s1", "title": "S"})
    description = next(item for item in EVENTS if item.kind == "watching")
    entity = EbayActivityEvent(coordinator, Mock(entry_id="entry-1"), description)
    attrs = entity.extra_state_attributes
    assert [item["item_id"] for item in attrs["recent_events"]] == ["w1"]
    assert EbayActivityEvent._unrecorded_attributes == frozenset({"recent_events"})


def test_safe_history_fields_only() -> None:
    coordinator = _coordinator()
    long_title = "X" * 200
    coordinator._emit(
        "watching",
        "watched_item_added",
        {
            "item_id": "w1",
            "title": long_title,
            "current_price": 1,
            "currency": "USD",
            "url": "https://ebay.example/item",
            "image": "https://ebay.example/secret.png",
            "access_token": "should-not-appear",
        },
    )
    entry: dict[str, Any] = coordinator.recent_events("watching")[0]
    assert len(entry["title"]) <= 120
    assert "image" not in entry
    assert "access_token" not in entry
    assert entry["url"] == "https://ebay.example/item"
