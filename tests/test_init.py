"""Integration lifecycle and migration tests."""

from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

import custom_components.ebay as ebay_integration
from custom_components.ebay.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SITE_ID,
)


def _entry_data(**overrides: Any) -> dict[str, Any]:
    from custom_components.ebay.api import DEFAULT_SCOPE
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client",
        CONF_CLIENT_SECRET: "secret",
        CONF_RUNAME: "runame",
        CONF_SITE_ID: "0",
        CONF_OAUTH_SCOPES: DEFAULT_SCOPE,
        "token": {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 9999999999,
        },
        "auth_implementation": "ebay",
    }
    data.update(overrides)
    return data


def _mock_entry(*, version: int = 2, data: dict[str, Any] | None = None) -> Mock:
    entry = Mock()
    entry.entry_id = "entry-1"
    entry.version = version
    entry.data = data or _entry_data()
    entry.options = {}
    entry.runtime_data = None
    entry.add_update_listener = Mock(return_value=lambda: None)
    entry.async_on_unload = Mock()
    return entry


def _patch_setup(*, coordinator: Mock, client: Any = None) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(
        patch(
            "custom_components.ebay.api.EbayApiClient",
            return_value=client if client is not None else Mock(),
        )
    )
    stack.enter_context(
        patch(
            "custom_components.ebay.coordinator.EbayDataUpdateCoordinator",
            return_value=coordinator,
        )
    )
    stack.enter_context(
        patch(
            "custom_components.ebay.repairs.async_load_repair_streaks",
            new=AsyncMock(return_value={}),
        )
    )
    stack.enter_context(
        patch(
            "custom_components.ebay.ending_soon_store.async_load_ending_soon_fired",
            new=AsyncMock(return_value=set()),
        )
    )
    stack.enter_context(
        patch(
            "custom_components.ebay.inactive_item_store.async_load_inactive_item_sensors",
            new=AsyncMock(return_value={}),
        )
    )
    stack.enter_context(
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_register_implementation"
        )
    )
    stack.enter_context(
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.OAuth2Session",
            return_value=Mock(),
        )
    )
    stack.enter_context(
        patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=Mock(),
        )
    )
    stack.enter_context(
        patch(
            "custom_components.ebay.oauth2.EbayOAuth2Implementation",
            return_value=Mock(),
        )
    )
    return stack


def test_migrate_v1_refresh_token_to_v2() -> None:
    hass = Mock()

    def _update_entry(entry: Mock, **kwargs: Any) -> None:
        if "data" in kwargs:
            entry.data = kwargs["data"]
        if "version" in kwargs:
            entry.version = kwargs["version"]

    hass.config_entries.async_update_entry = Mock(side_effect=_update_entry)
    entry = _mock_entry(
        version=1,
        data={
            CONF_ENVIRONMENT: "production",
            CONF_CLIENT_ID: "client",
            CONF_CLIENT_SECRET: "secret",
            CONF_RUNAME: "runame",
            CONF_SITE_ID: "0",
            CONF_REFRESH_TOKEN: "legacy-refresh",
        },
    )

    async def _noop_migrate_entries(*_args: Any, **_kwargs: Any) -> None:
        return None

    with patch(
        "homeassistant.helpers.entity_registry.async_migrate_entries",
        new=_noop_migrate_entries,
    ):
        assert asyncio.run(ebay_integration.async_migrate_entry(hass, entry)) is True

    assert entry.version == 4
    assert "token" in entry.data
    assert entry.data["token"]["refresh_token"] == "legacy-refresh"
    assert hass.config_entries.async_update_entry.call_count == 3


def test_migrate_rejects_future_version() -> None:
    hass = Mock()
    entry = _mock_entry(version=5)
    assert asyncio.run(ebay_integration.async_migrate_entry(hass, entry)) is False


def test_migrate_v3_to_v4_clears_oauth_scopes() -> None:
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    hass = Mock()

    def _update_entry(entry: Mock, **kwargs: Any) -> None:
        if "data" in kwargs:
            entry.data = kwargs["data"]
        if "version" in kwargs:
            entry.version = kwargs["version"]

    hass.config_entries.async_update_entry = Mock(side_effect=_update_entry)
    entry = _mock_entry(
        version=3,
        data=_entry_data(**{CONF_OAUTH_SCOPES: "old-scope"}),
    )
    assert asyncio.run(ebay_integration.async_migrate_entry(hass, entry)) is True
    assert entry.version == 4
    assert CONF_OAUTH_SCOPES not in entry.data


def test_async_setup_entry_requires_core_oauth_scope() -> None:
    hass = Mock()
    entry = _mock_entry(data=_entry_data(oauth_scopes="stale"))
    with pytest.raises(ConfigEntryAuthFailed):
        asyncio.run(ebay_integration.async_setup_entry(hass, entry))


def test_async_setup_entry_accepts_core_only_oauth_scopes() -> None:
    from custom_components.ebay.api import CORE_SCOPE
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = _mock_entry(data=_entry_data(**{CONF_OAUTH_SCOPES: CORE_SCOPE}))
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    with _patch_setup(coordinator=coordinator):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True


def test_migrate_v2_renames_legacy_sensor_unique_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy summary and analytics unique IDs must become the release names."""
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay")
    from types import SimpleNamespace

    hass = Mock()

    def _update_entry(entry: Mock, **kwargs: Any) -> None:
        if "data" in kwargs:
            entry.data = kwargs["data"]
        if "version" in kwargs:
            entry.version = kwargs["version"]

    hass.config_entries.async_update_entry = Mock(side_effect=_update_entry)
    entry = _mock_entry(version=2)
    migrated: list[tuple[str, str]] = []

    async def _fake_migrate_entries(
        _hass: Any,
        config_entry_id: str,
        callback: Any,
    ) -> None:
        assert config_entry_id == entry.entry_id
        for unique_id in (
            f"{entry.entry_id}_selling_total_sold",
            f"{entry.entry_id}_selling_abc_analytics_views",
            f"{entry.entry_id}_selling_total_bids",
            f"{entry.entry_id}_selling_abc_analytics_views_30d",
            f"{entry.entry_id}_selling_abc_views",
        ):
            updates = callback(SimpleNamespace(unique_id=unique_id))
            if updates is not None:
                migrated.append((unique_id, updates["new_unique_id"]))

    with patch(
        "homeassistant.helpers.entity_registry.async_migrate_entries",
        new=_fake_migrate_entries,
    ):
        assert asyncio.run(ebay_integration.async_migrate_entry(hass, entry)) is True

    assert entry.version == 4
    assert migrated == [
        (
            f"{entry.entry_id}_selling_total_sold",
            f"{entry.entry_id}_active_listings_quantity_sold",
        ),
        (
            f"{entry.entry_id}_selling_abc_analytics_views",
            f"{entry.entry_id}_selling_abc_analytics_views_30d",
        ),
    ]
    assert (
        "Migrated eBay config entry sensor unique IDs entry_id=entry-1 "
        "renamed_count=2 to_version=3" in caplog.text
    )


def test_renamed_sensor_unique_id_mapping() -> None:
    assert (
        ebay_integration._renamed_sensor_unique_id(
            "entry-1", "entry-1_selling_total_sold"
        )
        == "entry-1_active_listings_quantity_sold"
    )
    assert (
        ebay_integration._renamed_sensor_unique_id(
            "entry-1", "entry-1_selling_123_analytics_views"
        )
        == "entry-1_selling_123_analytics_views_30d"
    )
    assert (
        ebay_integration._renamed_sensor_unique_id(
            "entry-1", "entry-1_selling_123_analytics_views_30d"
        )
        is None
    )
    assert (
        ebay_integration._renamed_sensor_unique_id(
            "entry-1", "entry-1_selling_total_bids"
        )
        is None
    )


def test_async_setup_entry_success_forwards_platforms() -> None:
    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    entry = _mock_entry()
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with _patch_setup(coordinator=coordinator):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True

    coordinator.async_config_entry_first_refresh.assert_awaited_once()
    hass.config_entries.async_forward_entry_setups.assert_awaited()
    assert entry.runtime_data is coordinator
    entry.async_on_unload.assert_called()


def test_async_setup_entry_first_refresh_api_failure() -> None:
    hass = Mock()
    entry = _mock_entry()
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=ConfigEntryNotReady("fail")
    )
    with _patch_setup(coordinator=coordinator), pytest.raises(ConfigEntryNotReady):
        asyncio.run(ebay_integration.async_setup_entry(hass, entry))


def test_async_setup_entry_first_refresh_auth_failure() -> None:
    hass = Mock()
    entry = _mock_entry()
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=ConfigEntryAuthFailed("auth")
    )
    with (
        _patch_setup(coordinator=coordinator),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        asyncio.run(ebay_integration.async_setup_entry(hass, entry))


def test_async_unload_cancels_timers_and_unloads_platforms() -> None:
    hass = Mock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    entry = _mock_entry()
    coordinator = Mock()
    coordinator._repair_streaks = {"truncation:watched": 3}
    coordinator._fired_ending_soon = {("entry-1", "watching", "1", 3600, "t")}
    coordinator.inactive_item_sensors = {"entry-1_selling_1_bids": Mock()}
    entry.runtime_data = coordinator

    with (
        patch(
            "custom_components.ebay.repairs.async_save_repair_streaks",
            new_callable=AsyncMock,
        ) as save_streaks,
        patch(
            "custom_components.ebay.ending_soon_store.async_save_ending_soon_fired",
            new_callable=AsyncMock,
        ) as save_ending_soon,
        patch(
            "custom_components.ebay.inactive_item_store.async_save_inactive_item_sensors",
            new_callable=AsyncMock,
        ) as save_inactive,
        patch(
            "custom_components.ebay.repairs.async_delete_entry_issues"
        ) as delete_issues,
    ):
        assert asyncio.run(ebay_integration.async_unload_entry(hass, entry)) is True

    coordinator.cancel_scheduled_events.assert_called_once()
    save_streaks.assert_awaited_once_with(
        hass, entry.entry_id, coordinator._repair_streaks
    )
    save_ending_soon.assert_awaited_once_with(
        hass, entry.entry_id, coordinator._fired_ending_soon
    )
    save_inactive.assert_awaited_once_with(
        hass, entry.entry_id, coordinator.inactive_item_sensors
    )
    assert coordinator._fired_ending_soon_dirty is False
    delete_issues.assert_not_called()
    hass.config_entries.async_unload_platforms.assert_awaited()


def test_async_remove_entry_clears_issues_and_streaks() -> None:
    hass = Mock()
    entry = _mock_entry()

    with (
        patch(
            "custom_components.ebay.repairs.async_delete_entry_issues"
        ) as delete_issues,
        patch(
            "custom_components.ebay.repairs.async_remove_repair_streaks",
            new_callable=AsyncMock,
        ) as remove_streaks,
        patch(
            "custom_components.ebay.ending_soon_store.async_remove_ending_soon_fired",
            new_callable=AsyncMock,
        ) as remove_ending_soon,
        patch(
            "custom_components.ebay.inactive_item_store.async_remove_inactive_item_sensors",
            new_callable=AsyncMock,
        ) as remove_inactive,
    ):
        asyncio.run(ebay_integration.async_remove_entry(hass, entry))

    delete_issues.assert_called_once_with(hass, entry)
    remove_streaks.assert_awaited_once_with(hass, entry.entry_id)
    remove_ending_soon.assert_awaited_once_with(hass, entry.entry_id)
    remove_inactive.assert_awaited_once_with(hass, entry.entry_id)


def test_options_listener_reloads_entry() -> None:
    hass = Mock()
    hass.config_entries.async_reload = AsyncMock()
    entry = _mock_entry()
    asyncio.run(ebay_integration._async_update_listener(hass, entry))
    hass.config_entries.async_reload.assert_awaited_with(entry.entry_id)


def test_legacy_refresh_token_entry_setup_without_token_blob() -> None:
    """Legacy entries with only refresh_token still construct a client."""
    from custom_components.ebay.api import DEFAULT_SCOPE
    from custom_components.ebay.const import CONF_OAUTH_SCOPES

    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    entry = _mock_entry(
        data={
            CONF_ENVIRONMENT: "production",
            CONF_CLIENT_ID: "client",
            CONF_CLIENT_SECRET: "secret",
            CONF_RUNAME: "runame",
            CONF_SITE_ID: "0",
            CONF_REFRESH_TOKEN: "legacy-refresh",
            CONF_OAUTH_SCOPES: DEFAULT_SCOPE,
        }
    )
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    captured: dict[str, Any] = {}

    def capture_client(*args: Any, **kwargs: Any) -> Mock:
        captured.update(kwargs)
        return Mock()

    with (
        patch("custom_components.ebay.api.EbayApiClient", side_effect=capture_client),
        patch(
            "custom_components.ebay.coordinator.EbayDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.ebay.repairs.async_load_repair_streaks",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "custom_components.ebay.ending_soon_store.async_load_ending_soon_fired",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ebay.inactive_item_store.async_load_inactive_item_sensors",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=Mock(),
        ),
    ):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True

    assert captured.get("refresh_token") == "legacy-refresh"
    assert captured.get("oauth_session") is None
