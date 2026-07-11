"""Event entity callback registration tests."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any
from unittest.mock import Mock

from custom_components.ebay.const import DEFAULT_OPTIONS, RECENT_EVENTS_MAX, SELLING_EVENT_TYPES
from custom_components.ebay.coordinator import EbayDataUpdateCoordinator
from custom_components.ebay.event import EbayActivityEvent, EVENTS, async_setup_entry


def _coordinator() -> EbayDataUpdateCoordinator:
    entry = Mock(entry_id="entry-1")
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.entry = entry
    coordinator.options = dict(DEFAULT_OPTIONS)
    coordinator.data = None
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


def test_event_setup_creates_three_entities() -> None:
    coordinator = _coordinator()
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list[list[Any]] = []
    asyncio.run(async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities))))
    assert len(added[0]) == len(EVENTS)


def test_event_callback_registration_and_undeclared_filter() -> None:
    coordinator = _coordinator()
    entry = Mock(entry_id="entry-1")
    description = next(item for item in EVENTS if item.kind == "selling")
    entity = EbayActivityEvent(coordinator, entry, description)
    entity.hass = Mock()
    entity.async_write_ha_state = Mock()
    triggered: list[tuple[str, dict[str, Any]]] = []
    entity._trigger_event = lambda event_type, event_data=None: triggered.append(
        (event_type, event_data or {})
    )

    unsubscribe = coordinator.register_event_callback(
        description.kind, entity._handle_event
    )
    entity._unsubscribe_event_callback = unsubscribe
    assert "selling" in coordinator._event_callbacks

    entity._handle_event("bid_count_increased", {"item_id": "1"})
    entity._handle_event("not_a_real_event", {"item_id": "1"})
    assert [item[0] for item in triggered] == ["bid_count_increased"]
    assert "bid_count_increased" in SELLING_EVENT_TYPES

    unsubscribe()
    assert "selling" not in coordinator._event_callbacks
