"""Coordinator update error and timer behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.ebay.api import EbayApiError, EbayAuthError
from custom_components.ebay.const import DEFAULT_OPTIONS
from custom_components.ebay.coordinator import EbayDataUpdateCoordinator


def _coordinator(api: Any | None = None) -> EbayDataUpdateCoordinator:
    entry = Mock(entry_id="entry-1")
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.hass = Mock()
    coordinator.entry = entry
    coordinator.api = api or Mock()
    coordinator.options = dict(DEFAULT_OPTIONS)
    coordinator.data = None
    coordinator.previous_payload = None
    coordinator.last_error_category = None
    coordinator.last_attempt_at = None
    coordinator.last_refresh_duration_seconds = None
    coordinator.last_refresh_result = "unknown"
    coordinator.selected_item_keys = set()
    coordinator._event_callbacks = {}
    coordinator._timer_listeners = []
    coordinator._scheduled = {}
    coordinator._fired_ending_soon = set()
    coordinator._price_drop_below_active = set()
    from collections import deque

    from custom_components.ebay.const import RECENT_EVENTS_MAX

    coordinator._recent_events = {
        "watching": deque(maxlen=RECENT_EVENTS_MAX),
        "bidding": deque(maxlen=RECENT_EVENTS_MAX),
        "selling": deque(maxlen=RECENT_EVENTS_MAX),
        "seller_ops": deque(maxlen=RECENT_EVENTS_MAX),
    }
    coordinator._recent_history_ending_soon = set()
    coordinator._manual_refresh_last_requested = None
    return coordinator


def test_auth_failure_becomes_config_entry_auth_failed() -> None:
    api = Mock()
    api.async_fetch_data = AsyncMock(side_effect=EbayAuthError("nope"))
    coordinator = _coordinator(api)

    with pytest.raises(ConfigEntryAuthFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.last_error_category == "auth"
    assert coordinator.previous_payload is None


def test_other_ebay_failure_becomes_update_failed() -> None:
    api = Mock()
    api.async_fetch_data = AsyncMock(side_effect=EbayApiError("boom"))
    coordinator = _coordinator(api)
    previous = {
        "selling": {"s1": {"item_id": "s1"}},
        "watched": {},
        "bidding": {},
        "truncated_collections": {},
    }
    coordinator.previous_payload = previous
    events: list[str] = []
    coordinator._event_callbacks["selling"] = lambda et, ed: events.append(et)

    with pytest.raises(UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.last_error_category == "EbayApiError"
    assert coordinator.previous_payload is previous
    assert events == []


def test_successful_update_clears_last_error_and_sets_baseline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.coordinator")
    now = datetime.now(timezone.utc)
    payload = {
        "selling": {"s1": {"item_id": "s1", "bid_count": 1}},
        "watched": {},
        "bidding": {},
        "summary": {},
        "last_update": now,
        "last_successful_update": now,
        "partial_failures": [],
        "truncated_collections": {
            "watched": False,
            "bidding": False,
            "selling": False,
            "seller_list_views": False,
            "active_offers": False,
        },
        "api_warnings": [],
    }
    api = Mock()
    api.async_fetch_data = AsyncMock(return_value=payload)
    coordinator = _coordinator(api)
    coordinator.last_error_category = "auth"

    result = asyncio.run(coordinator._async_update_data())

    assert result["summary"]["partial_failures"] == []
    assert coordinator.last_error_category is None
    assert coordinator.previous_payload is not None
    assert coordinator.previous_payload["selling"] == payload["selling"]
    assert "eBay coordinator refresh started entry_id=entry-1" in caplog.text
    assert "eBay coordinator refresh completed entry_id=entry-1" in caplog.text
    assert "watched_count=0 bidding_count=0 selling_count=1" in caplog.text
    assert "duration_ms=" in caplog.text


def test_summary_api_warnings_are_capped_for_state_attributes() -> None:
    from custom_components.ebay.api import API_WARNINGS_STATE_ATTR_MAX

    now = datetime.now(timezone.utc)
    warnings = [
        {"code": str(index), "short_message": f"w{index}", "long_message": None}
        for index in range(API_WARNINGS_STATE_ATTR_MAX + 5)
    ]
    payload = {
        "selling": {},
        "watched": {},
        "bidding": {},
        "summary": {},
        "last_update": now,
        "last_successful_update": now,
        "partial_failures": ["trading_warning"],
        "truncated_collections": {},
        "api_warnings": warnings,
    }
    api = Mock()
    api.async_fetch_data = AsyncMock(return_value=payload)
    coordinator = _coordinator(api)

    result = asyncio.run(coordinator._async_update_data())

    assert result["api_warnings"] == warnings
    assert result["summary"]["api_warnings"] == warnings[:API_WARNINGS_STATE_ATTR_MAX]


def test_cancel_scheduled_events_clears_timers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.coordinator")
    coordinator = _coordinator()
    cancelled: list[bool] = []
    coordinator._scheduled[("entry-1", "watching", "1", 3600, "t")] = lambda: (
        cancelled.append(True)
    )
    coordinator.cancel_scheduled_events()
    assert cancelled == [True]
    assert coordinator._scheduled == {}
    assert "Cancelling eBay ending-soon timer kind=watching item_id=1" in caplog.text


def test_timers_reschedule_when_end_time_changes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.coordinator")
    coordinator = _coordinator()
    events: list[str] = []
    coordinator._event_callbacks["watching"] = lambda et, ed: events.append(et)
    cancels: list[Mock] = []

    def fake_track(hass: Any, action: Any, point_in_time: datetime) -> Mock:
        cancel = Mock()
        cancels.append(cancel)
        return cancel

    monkeypatch.setattr(
        "custom_components.ebay.coordinator.async_track_point_in_utc_time",
        fake_track,
    )
    end_a = datetime.now(timezone.utc) + timedelta(hours=3)
    end_b = datetime.now(timezone.utc) + timedelta(hours=4)
    first = {
        "watched": {
            "123": {
                "item_id": "123",
                "title": "Item",
                "end_time": end_a,
                "seconds_left": 3 * 3600,
            }
        },
        "bidding": {},
        "selling": {},
        "summary": {},
    }
    second = {
        "watched": {
            "123": {
                "item_id": "123",
                "title": "Item",
                "end_time": end_b,
                "seconds_left": 4 * 3600,
            }
        },
        "bidding": {},
        "selling": {},
        "summary": {},
    }
    coordinator._rebuild_ending_soon_timers(first)
    first_keys = set(coordinator._scheduled)
    assert len(first_keys) == 1
    coordinator._rebuild_ending_soon_timers(second)
    assert set(coordinator._scheduled) != first_keys
    assert len(coordinator._scheduled) == 1
    assert cancels[0].called
    assert "Scheduling eBay ending-soon timer kind=watching item_id=123" in caplog.text
    assert (
        "Rescheduling eBay ending-soon timer kind=watching item_id=123" in caplog.text
    )
    assert "Cancelling eBay ending-soon timer kind=watching item_id=123" in caplog.text
