"""Home Assistant fixture tests for diagnostic health availability."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.ebay import button as button_module
from custom_components.ebay import sensor as sensor_module
from custom_components.ebay.api import EbayApiError
from custom_components.ebay.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENTITY_MODE,
    CONF_ENVIRONMENT,
    CONF_PER_ITEM_CAP,
    CONF_RUNAME,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    ENTITY_MODE_MINIMAL,
    REFRESH_RESULT_ERROR,
    REFRESH_RESULT_SUCCESS,
)
from custom_components.ebay.coordinator import EbayDataUpdateCoordinator

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _options() -> dict[str, Any]:
    return {
        **DEFAULT_OPTIONS,
        CONF_ENTITY_MODE: ENTITY_MODE_MINIMAL,
        CONF_PER_ITEM_CAP: 0,
    }


def _payload(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "selling": {},
        "watched": {},
        "bidding": {},
        "summary": {
            "active_selling_items": 0,
            "watched_items": 0,
            "bidding_items": 0,
            "last_update": now,
            "last_successful_update": now,
            "partial_failures": [],
            "truncated_collections": {},
            "api_warnings": [],
        },
        "last_update": now,
        "last_successful_update": now,
        "partial_failures": [],
        "truncated_collections": {},
        "api_warnings": [],
    }
    payload.update(overrides)
    return payload


async def _enable_unique_ids(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    unique_ids: set[str],
) -> dict[str, str]:
    entity_ids: dict[str, str] = {}
    for unique_id in unique_ids:
        entry = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entry is None:
            entry = entity_registry.async_get_entity_id("button", DOMAIN, unique_id)
        assert entry is not None, unique_id
        entity_registry.async_update_entity(entry, disabled_by=None)
        entity_ids[unique_id] = entry
    await hass.async_block_till_done()
    return entity_ids


async def test_health_entities_remain_available_across_failed_refresh(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Diagnostic health stays visible when the coordinator update fails."""
    assert await async_setup_component(hass, "sensor", {})
    assert await async_setup_component(hass, "button", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ENVIRONMENT: "production",
            CONF_CLIENT_ID: "client",
            CONF_CLIENT_SECRET: "secret",
            CONF_RUNAME: "runame",
            CONF_SITE_ID: "0",
        },
        options=_options(),
        version=3,
        title="eBay",
        entry_id="entry-health-1",
    )
    entry.add_to_hass(hass)

    first_payload = _payload()
    api = Mock()
    api.async_fetch_data = AsyncMock(return_value=first_payload)
    coordinator = EbayDataUpdateCoordinator(hass, entry, api, _options())
    coordinator.update_interval = None
    coordinator.data = first_payload
    coordinator.last_attempt_at = datetime.now(timezone.utc)
    coordinator.last_refresh_result = REFRESH_RESULT_SUCCESS
    coordinator.last_refresh_duration_seconds = 0.1
    entry.runtime_data = coordinator

    sensor_platform = MockEntityPlatform(
        hass,
        domain="sensor",
        platform_name=DOMAIN,
        platform=sensor_module,
    )
    button_platform = MockEntityPlatform(
        hass,
        domain="button",
        platform_name=DOMAIN,
        platform=button_module,
    )
    assert await sensor_platform.async_setup_entry(entry)
    assert await button_platform.async_setup_entry(entry)
    await hass.async_block_till_done()

    result_uid = f"{entry.entry_id}_last_refresh_result"
    duration_uid = f"{entry.entry_id}_last_refresh_duration"
    summary_uid = f"{entry.entry_id}_active_selling_items"
    button_uid = f"{entry.entry_id}_refresh"
    entity_ids = await _enable_unique_ids(
        hass,
        entity_registry,
        {result_uid, duration_uid, summary_uid, button_uid},
    )
    await sensor_platform.async_reset()
    await button_platform.async_reset()
    assert await sensor_platform.async_setup_entry(entry)
    assert await button_platform.async_setup_entry(entry)
    await hass.async_block_till_done()

    result_state = hass.states.get(entity_ids[result_uid])
    duration_state = hass.states.get(entity_ids[duration_uid])
    summary_state = hass.states.get(entity_ids[summary_uid])
    button_state = hass.states.get(entity_ids[button_uid])
    assert result_state is not None
    assert duration_state is not None
    assert summary_state is not None
    assert button_state is not None
    assert result_state.state == REFRESH_RESULT_SUCCESS
    assert summary_state.state != STATE_UNAVAILABLE
    assert button_state.state != STATE_UNAVAILABLE

    api.async_fetch_data = AsyncMock(side_effect=EbayApiError("boom"))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    result_state = hass.states.get(entity_ids[result_uid])
    duration_state = hass.states.get(entity_ids[duration_uid])
    summary_state = hass.states.get(entity_ids[summary_uid])
    button_state = hass.states.get(entity_ids[button_uid])
    assert result_state is not None
    assert duration_state is not None
    assert summary_state is not None
    assert button_state is not None
    assert result_state.state == REFRESH_RESULT_ERROR
    assert result_state.state != STATE_UNAVAILABLE
    assert duration_state.state != STATE_UNAVAILABLE
    assert float(duration_state.state) >= 0
    assert summary_state.state == STATE_UNAVAILABLE
    assert button_state.state != STATE_UNAVAILABLE

    api.async_fetch_data = AsyncMock(return_value=_payload())
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    result_state = hass.states.get(entity_ids[result_uid])
    summary_state = hass.states.get(entity_ids[summary_uid])
    assert result_state is not None
    assert summary_state is not None
    assert result_state.state == REFRESH_RESULT_SUCCESS
    assert summary_state.state != STATE_UNAVAILABLE

    await sensor_platform.async_reset()
    await button_platform.async_reset()
    await coordinator.async_shutdown()
    await hass.async_block_till_done()
