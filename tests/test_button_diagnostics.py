"""Tests for the manual refresh button and refresh health metadata."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.ebay.api import EbayApiError, EbayAuthError
from custom_components.ebay.button import EbayRefreshButton, async_setup_entry
from custom_components.ebay.const import (
    DEFAULT_OPTIONS,
    MANUAL_REFRESH_COOLDOWN_SECONDS,
    RECENT_EVENTS_MAX,
    REFRESH_RESULT_AUTH_ERROR,
    REFRESH_RESULT_ERROR,
    REFRESH_RESULT_PARTIAL,
    REFRESH_RESULT_SUCCESS,
)
from custom_components.ebay.coordinator import EbayDataUpdateCoordinator
from custom_components.ebay.sensor import DIAGNOSTIC_SENSORS, EbayDiagnosticSensor


def _coordinator(api: Any | None = None) -> EbayDataUpdateCoordinator:
    entry = Mock(entry_id="entry-1")
    entry.data = {}
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.hass = Mock()
    coordinator.hass.async_create_task = lambda coro: coro
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
    coordinator.inactive_item_sensors = {}
    coordinator._item_entity_cleanup = None
    coordinator._fired_ending_soon_dirty = False
    coordinator._ending_soon_fired_save_task = None
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
    coordinator._manual_refresh_last_requested = None
    coordinator._force_full_refresh = False
    coordinator._force_sections = None
    coordinator._repair_streaks = {}
    coordinator._refresh_task = None
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


@pytest.fixture(autouse=True)
def _stub_repairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.ebay.repairs.async_sync_repair_issues",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "custom_components.ebay.repairs.async_sync_reauth_issue",
        AsyncMock(),
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload = {
        "selling": {},
        "watched": {},
        "bidding": {},
        "summary": {},
        "last_update": now,
        "last_successful_update": now,
        "partial_failures": [],
        "truncated_collections": {},
        "api_warnings": [],
    }
    payload.update(overrides)
    return payload


def test_button_setup_and_press_requests_refresh() -> None:
    coordinator = _coordinator()
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list[list[Any]] = []
    asyncio.run(
        async_setup_entry(Mock(), entry, lambda entities: added.append(list(entities)))
    )
    assert len(added[0]) == 1
    button = added[0][0]
    assert isinstance(button, EbayRefreshButton)
    assert button.unique_id == "entry-1_refresh"
    asyncio.run(button.async_press())
    coordinator.async_request_refresh.assert_awaited_once()


def test_rapid_repeated_presses_are_rate_limited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.coordinator")
    coordinator = _coordinator()
    assert asyncio.run(coordinator.async_request_manual_refresh()) is True
    assert asyncio.run(coordinator.async_request_manual_refresh()) is False
    assert "Ignoring eBay manual refresh during cooldown" in caplog.text
    coordinator.async_request_refresh.assert_awaited_once()


def test_press_after_cooldown_works() -> None:
    coordinator = _coordinator()
    assert asyncio.run(coordinator.async_request_manual_refresh()) is True
    coordinator._manual_refresh_last_requested = (
        __import__("time").monotonic() - MANUAL_REFRESH_COOLDOWN_SECONDS - 1
    )
    assert asyncio.run(coordinator.async_request_manual_refresh()) is True
    assert coordinator.async_request_refresh.await_count == 2


def test_concurrent_press_while_refresh_running_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.coordinator")
    coordinator = _coordinator()
    task = Mock()
    task.done.return_value = False
    coordinator._refresh_task = task
    assert asyncio.run(coordinator.async_request_manual_refresh()) is False
    assert "refresh already running" in caplog.text
    coordinator.async_request_refresh.assert_not_awaited()


def test_auth_and_error_refresh_record_health() -> None:
    api = Mock()
    api.async_fetch_data = AsyncMock(side_effect=EbayAuthError("nope"))
    coordinator = _coordinator(api)
    with pytest.raises(ConfigEntryAuthFailed):
        asyncio.run(coordinator._async_update_data())
    assert coordinator.last_refresh_result == REFRESH_RESULT_AUTH_ERROR
    assert coordinator.last_error_category == "auth"
    assert coordinator.last_refresh_duration_seconds is not None
    assert coordinator.last_refresh_duration_seconds >= 0
    assert coordinator.last_attempt_at is not None

    api.async_fetch_data = AsyncMock(side_effect=EbayApiError("boom"))
    with pytest.raises(UpdateFailed):
        asyncio.run(coordinator._async_update_data())
    assert coordinator.last_refresh_result == REFRESH_RESULT_ERROR
    assert coordinator.last_error_category == "EbayApiError"


def test_success_and_partial_refresh_results() -> None:
    api = Mock()
    api.async_fetch_data = AsyncMock(return_value=_payload())
    coordinator = _coordinator(api)
    asyncio.run(coordinator._async_update_data())
    assert coordinator.last_refresh_result == REFRESH_RESULT_SUCCESS
    assert coordinator.last_error_category is None

    api.async_fetch_data = AsyncMock(
        return_value=_payload(partial_failures=["buying_truncated"])
    )
    asyncio.run(coordinator._async_update_data())
    assert coordinator.last_refresh_result == REFRESH_RESULT_PARTIAL


def test_diagnostic_sensors_disabled_by_default() -> None:
    coordinator = _coordinator()
    coordinator.data = _payload(
        partial_failures=["trading_warning"],
        truncated_collections={"watched": True, "bidding": False},
        api_warnings=[{"code": "1", "short_message": "warn"}],
        summary={
            "last_successful_update": datetime.now(timezone.utc).isoformat(),
            "partial_failures": ["trading_warning"],
            "truncated_collections": {"watched": True},
            "api_warnings": [{"code": "1", "short_message": "warn"}],
        },
    )
    coordinator.last_refresh_duration_seconds = 1.25
    coordinator.last_refresh_result = REFRESH_RESULT_PARTIAL
    coordinator.last_error_category = None
    coordinator._scheduled[("entry-1", "watching", "1", 3600, "t")] = Mock()

    for description in DIAGNOSTIC_SENSORS:
        assert description.entity_registry_enabled_default is False
        assert description.entity_category.value == "diagnostic"
        entity = EbayDiagnosticSensor(
            coordinator, Mock(entry_id="entry-1"), description
        )
        value = entity.native_value
        attrs = entity.extra_state_attributes
        if description.key == "partial_failure_count":
            assert value == 1
            assert attrs["partial_failure_categories"] == ["trading_warning"]
        elif description.key == "truncated_collection_count":
            assert value == 1
            assert attrs["truncated_collections"] == ["watched"]
        elif description.key == "api_warning_count":
            assert value == 1
            assert attrs["api_warnings"] == [{"code": "1", "short_message": "warn"}]
        elif description.key == "last_refresh_duration":
            assert value == 1.25
        elif description.key == "last_refresh_result":
            assert value == REFRESH_RESULT_PARTIAL
        elif description.key == "scheduled_ending_soon_timer_count":
            assert value == 1
        secret_blob = str(value) + str(attrs)
        assert "access_token" not in secret_blob
        assert "refresh_token" not in secret_blob


def test_api_warning_count_uses_full_list_while_attrs_are_capped() -> None:
    from custom_components.ebay.api import API_WARNINGS_STATE_ATTR_MAX

    warnings = [
        {"code": str(index), "short_message": f"w{index}", "long_message": None}
        for index in range(API_WARNINGS_STATE_ATTR_MAX + 5)
    ]
    coordinator = _coordinator()
    coordinator.data = _payload(
        api_warnings=warnings,
        summary={"api_warnings": warnings[:API_WARNINGS_STATE_ATTR_MAX]},
    )
    description = next(
        item for item in DIAGNOSTIC_SENSORS if item.key == "api_warning_count"
    )
    entity = EbayDiagnosticSensor(coordinator, Mock(entry_id="entry-1"), description)
    assert entity.native_value == len(warnings)
    attrs = entity.extra_state_attributes
    assert attrs is not None
    assert len(attrs["api_warnings"]) == API_WARNINGS_STATE_ATTR_MAX


def test_timer_fire_notifies_timer_listeners_only() -> None:
    coordinator = _coordinator()
    key = ("entry-1", "watching", "1", 3600, "t")
    coordinator._scheduled[key] = Mock()
    notified: list[int] = []
    coordinator.async_add_timer_listener(
        lambda: notified.append(coordinator.scheduled_ending_soon_count)
    )
    fire = coordinator._scheduled_callback(
        "watching",
        "watched_item_ending_soon",
        "1",
        "t",
        key,
    )
    fire(datetime.now(timezone.utc))
    assert coordinator._scheduled == {}
    assert notified == [0]
    assert coordinator.async_request_refresh.await_count == 0


def test_diagnostic_and_button_available_after_failed_refresh() -> None:
    coordinator = _coordinator()
    coordinator.last_update_success = False
    coordinator.last_attempt_at = datetime.now(timezone.utc)
    coordinator.last_refresh_result = REFRESH_RESULT_ERROR
    coordinator.last_refresh_duration_seconds = 0.5
    coordinator.data = _payload()

    for description in DIAGNOSTIC_SENSORS:
        entity = EbayDiagnosticSensor(
            coordinator, Mock(entry_id="entry-1"), description
        )
        assert entity.available is True

    button = EbayRefreshButton(coordinator, Mock(entry_id="entry-1"))
    assert button.available is True

    coordinator.last_attempt_at = None
    coordinator.data = None
    for description in DIAGNOSTIC_SENSORS:
        entity = EbayDiagnosticSensor(
            coordinator, Mock(entry_id="entry-1"), description
        )
        assert entity.available is False
    assert EbayRefreshButton(coordinator, Mock(entry_id="entry-1")).available is False
