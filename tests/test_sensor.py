"""Tests for eBay sensor lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from unittest.mock import Mock

from custom_components.ebay.const import (
    DEFAULT_OPTIONS,
    ENTITY_MODE_BALANCED,
)
from custom_components.ebay.sensor import ITEM_SENSOR_FIELDS, SUMMARY_SENSORS, async_setup_entry


def test_item_sensors_are_added_after_late_coordinator_data() -> None:
    """Per-item sensors are created when selected items appear after setup."""
    listener: Callable[[], None] | None = None
    unloads: list[Callable[[], None]] = []

    class Coordinator:
        data: dict[str, Any] | None = None
        options = {**DEFAULT_OPTIONS, "entity_mode": ENTITY_MODE_BALANCED}
        last_update_success = True
        ending_soon_threshold_seconds = 3600

        def async_add_listener(
            self, update_callback: Callable[[], None], context: Any | None = None
        ) -> Callable[[], None]:
            nonlocal listener
            listener = update_callback
            return lambda: None

    coordinator = Coordinator()
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    entry.async_on_unload.side_effect = unloads.append
    added: list[list[Any]] = []

    def async_add_entities(entities: Any) -> None:
        added.append(list(entities))

    asyncio.run(async_setup_entry(Mock(), entry, async_add_entities))

    assert [len(batch) for batch in added] == [len(SUMMARY_SENSORS)]
    assert listener is not None
    assert len(unloads) == 1

    coordinator.data = {
        "summary": {},
        "selling": {
            "123": {
                "item_id": "123",
                "bid_count": 1,
                "end_time": datetime.now(timezone.utc) + timedelta(minutes=30),
                "seconds_left": 1800,
            }
        },
        "watched": {},
        "bidding": {},
        "last_successful_update": datetime.now(timezone.utc),
    }

    listener()
    listener()

    assert [len(batch) for batch in added] == [
        len(SUMMARY_SENSORS),
        len(ITEM_SENSOR_FIELDS["selling"]),
    ]
    assert {sensor.item_id for sensor in added[1]} == {"123"}
