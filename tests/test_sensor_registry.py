"""Home Assistant fixture tests for entity-registry reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.ebay import sensor as sensor_module
from custom_components.ebay.button import (
    EbayCleanupInactiveItemEntitiesButton,
    async_setup_entry as async_setup_button_entry,
)
from custom_components.ebay.const import (
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENDING_SOON_THRESHOLD,
    CONF_ENTITY_MODE,
    CONF_ENVIRONMENT,
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    CONF_POLL_INTERVAL,
    CONF_RUNAME,
    CONF_SELLING_ENABLED,
    CONF_SITE_ID,
    DOMAIN,
    ENTITY_MODE_DETAILED,
)
from custom_components.ebay.coordinator import EbayDataUpdateCoordinator
from custom_components.ebay.sensor import ITEM_SENSOR_FIELDS

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _options() -> dict[str, Any]:
    return {
        CONF_POLL_INTERVAL: 30,
        CONF_ENDING_SOON_THRESHOLD: 60,
        CONF_ENTITY_MODE: ENTITY_MODE_DETAILED,
        CONF_PER_ITEM_CAP: 1,
        CONF_PINNED_ITEM_IDS: "",
        CONF_ANALYTICS_ENABLED: False,
        CONF_BUYING_ENABLED: False,
        CONF_SELLING_ENABLED: True,
    }


def _item(item_id: str) -> dict[str, Any]:
    end_time = datetime.now(timezone.utc) + timedelta(hours=2)
    return {
        "item_id": item_id,
        "title": f"Item {item_id}",
        "bid_count": 1,
        "current_price": 10.0,
        "currency": "USD",
        "seconds_left": 7200,
        "end_time": end_time,
        "views": 1,
        "watchers": 0,
        "offers": 0,
        "questions": 0,
        "quantity_available": 1,
        "quantity_sold": 0,
        "analytics_views_30d": None,
        "url": None,
        "image": None,
    }


def _payload(item_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    item = _item(item_id)
    return {
        "watched": {},
        "bidding": {},
        "selling": {item_id: item},
        "summary": {
            "active_selling_items": 1,
            "items_ending_soon": [],
            "items_with_offers": [],
            "items_with_bids": [item_id],
            "items_with_questions": [],
            "highest_watcher_count_item": None,
            "highest_view_count_item": None,
            "last_update": now.isoformat(),
            "last_successful_update": now.isoformat(),
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


def _selling_unique_ids(entry_id: str, item_id: str) -> set[str]:
    return {
        f"{entry_id}_selling_{item_id}_{suffix}"
        for suffix, _, _ in ITEM_SENSOR_FIELDS["selling"]
    }


def _registered_item_sensor_unique_ids(
    registry: er.EntityRegistry, entry_id: str
) -> set[str]:
    from custom_components.ebay.sensor import _is_item_sensor_unique_id

    return {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(registry, entry_id)
        if entry.domain == "sensor"
        and entry.platform == DOMAIN
        and _is_item_sensor_unique_id(entry_id, entry.unique_id)
    }


async def test_item_sensor_registry_retains_selection_change_during_grace(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Inactive item sensors stay registered during grace and drop after it."""
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.sensor")
    assert await async_setup_component(hass, "sensor", {})

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
        entry_id="entry-registry-1",
    )
    entry.add_to_hass(hass)

    first_payload = _payload("001")
    coordinator = EbayDataUpdateCoordinator(hass, entry, Mock(), _options())
    coordinator.update_interval = None
    coordinator.data = first_payload
    coordinator.refresh_selected_item_keys(first_payload)
    entry.runtime_data = coordinator

    platform = MockEntityPlatform(
        hass,
        domain="sensor",
        platform_name=DOMAIN,
        platform=sensor_module,
    )
    assert await platform.async_setup_entry(entry)
    await hass.async_block_till_done()

    first_ids = _selling_unique_ids(entry.entry_id, "001")
    assert (
        _registered_item_sensor_unique_ids(entity_registry, entry.entry_id) == first_ids
    )
    first_entity_ids = {
        entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        for unique_id in first_ids
    }
    assert None not in first_entity_ids
    for unique_id in first_ids:
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert entity_id is not None
        registry_entry = entity_registry.async_get(entity_id)
        assert registry_entry is not None
        if unique_id.endswith("_seconds_left"):
            assert registry_entry.disabled_by is not None
            assert hass.states.get(entity_id) is None
        else:
            assert hass.states.get(entity_id) is not None

    second_payload = _payload("002")
    caplog.clear()
    coordinator.async_set_updated_data(second_payload)
    await hass.async_block_till_done()

    second_ids = _selling_unique_ids(entry.entry_id, "002")
    assert first_ids & second_ids == set()
    assert first_ids.issubset(
        _registered_item_sensor_unique_ids(entity_registry, entry.entry_id)
    )
    assert second_ids.issubset(
        _registered_item_sensor_unique_ids(entity_registry, entry.entry_id)
    )
    assert first_ids <= set(coordinator.inactive_item_sensors)
    for unique_id in first_ids:
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert entity_id is not None
        if unique_id.endswith("_seconds_left"):
            assert hass.states.get(entity_id) is None
            continue
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "unavailable"

    # Still within grace: refresh should not purge inactive entities.
    caplog.clear()
    coordinator.async_set_updated_data(second_payload)
    await hass.async_block_till_done()
    assert first_ids.issubset(
        _registered_item_sensor_unique_ids(entity_registry, entry.entry_id)
    )

    # Past grace: auto-cleanup removes inactive sensors.
    past_grace = datetime.now(timezone.utc) - timedelta(days=8)
    for unique_id in first_ids:
        coordinator.inactive_item_sensors[unique_id] = past_grace
    coordinator.async_set_updated_data(second_payload)
    await hass.async_block_till_done()

    assert (
        _registered_item_sensor_unique_ids(entity_registry, entry.entry_id)
        == second_ids
    )
    for unique_id in first_ids:
        assert entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None
    for entity_id in first_entity_ids:
        assert hass.states.get(entity_id) is None

    await platform.async_reset()
    await coordinator.async_shutdown()
    await hass.async_block_till_done()
    assert platform.entities == {}


async def test_cleanup_button_removes_inactive_item_entities_immediately(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Cleanup button force-removes inactive item sensors before grace expires."""
    assert await async_setup_component(hass, "sensor", {})

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
        entry_id="entry-registry-cleanup",
    )
    entry.add_to_hass(hass)

    first_payload = _payload("001")
    coordinator = EbayDataUpdateCoordinator(hass, entry, Mock(), _options())
    coordinator.update_interval = None
    coordinator.data = first_payload
    coordinator.last_attempt_at = datetime.now(timezone.utc)
    coordinator.refresh_selected_item_keys(first_payload)
    entry.runtime_data = coordinator

    sensor_platform = MockEntityPlatform(
        hass,
        domain="sensor",
        platform_name=DOMAIN,
        platform=sensor_module,
    )
    assert await sensor_platform.async_setup_entry(entry)
    await hass.async_block_till_done()

    first_ids = _selling_unique_ids(entry.entry_id, "001")
    coordinator.async_set_updated_data(_payload("002"))
    await hass.async_block_till_done()
    assert first_ids.issubset(
        _registered_item_sensor_unique_ids(entity_registry, entry.entry_id)
    )

    cleanup_button = EbayCleanupInactiveItemEntitiesButton(coordinator, entry)
    await cleanup_button.async_press()
    await hass.async_block_till_done()

    second_ids = _selling_unique_ids(entry.entry_id, "002")
    assert (
        _registered_item_sensor_unique_ids(entity_registry, entry.entry_id)
        == second_ids
    )
    for unique_id in first_ids:
        assert entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None
    assert first_ids.isdisjoint(coordinator.inactive_item_sensors)

    await sensor_platform.async_reset()
    await coordinator.async_shutdown()
    await hass.async_block_till_done()


def test_cleanup_button_setup_registers_entity() -> None:
    """Button platform registers refresh and cleanup actions."""
    import asyncio
    from unittest.mock import AsyncMock

    coordinator = Mock()
    coordinator.last_attempt_at = datetime.now(timezone.utc)
    coordinator.data = {}
    coordinator.async_cleanup_inactive_item_entities = AsyncMock(return_value=0)
    entry = Mock(entry_id="entry-1", runtime_data=coordinator)
    added: list[list[Any]] = []
    asyncio.run(
        async_setup_button_entry(
            Mock(), entry, lambda entities: added.append(list(entities))
        )
    )
    assert len(added[0]) == 7
    cleanup = next(
        entity
        for entity in added[0]
        if isinstance(entity, EbayCleanupInactiveItemEntitiesButton)
    )
    assert cleanup.unique_id == "entry-1_cleanup_inactive_item_entities"
    asyncio.run(cleanup.async_press())
    coordinator.async_cleanup_inactive_item_entities.assert_awaited_once_with(
        force=True
    )
