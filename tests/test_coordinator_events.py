"""Tests for coordinator event delivery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

from custom_components.ebay.const import DEFAULT_OPTIONS
from custom_components.ebay.coordinator import EbayDataUpdateCoordinator


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
    )
    assert key not in coordinator._fired_ending_soon

    events: list[tuple[str, dict[str, Any]]] = []
    coordinator.data = payload
    coordinator.register_event_callback("watching", lambda *args: events.append(args))

    assert len(events) == 1
    assert events[0][0] == "watched_item_ending_soon"
    assert events[0][1]["item_id"] == "123"
    assert key in coordinator._fired_ending_soon
