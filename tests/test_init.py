"""Integration lifecycle and migration tests."""

from __future__ import annotations

import asyncio
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
    data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client",
        CONF_CLIENT_SECRET: "secret",
        CONF_RUNAME: "runame",
        CONF_SITE_ID: "0",
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
    assert asyncio.run(ebay_integration.async_migrate_entry(hass, entry)) is True
    hass.config_entries.async_update_entry.assert_called_once()
    kwargs = hass.config_entries.async_update_entry.call_args.kwargs
    assert kwargs["version"] == 2
    assert "token" in kwargs["data"]
    assert kwargs["data"]["token"]["refresh_token"] == "legacy-refresh"


def test_migrate_rejects_future_version() -> None:
    hass = Mock()
    entry = _mock_entry(version=3)
    assert asyncio.run(ebay_integration.async_migrate_entry(hass, entry)) is False


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
    with _patch_setup(coordinator=coordinator):
        with pytest.raises(ConfigEntryNotReady):
            asyncio.run(ebay_integration.async_setup_entry(hass, entry))


def test_async_setup_entry_first_refresh_auth_failure() -> None:
    hass = Mock()
    entry = _mock_entry()
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=ConfigEntryAuthFailed("auth")
    )
    with _patch_setup(coordinator=coordinator):
        with pytest.raises(ConfigEntryAuthFailed):
            asyncio.run(ebay_integration.async_setup_entry(hass, entry))


def test_async_unload_cancels_timers_and_unloads_platforms() -> None:
    hass = Mock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    entry = _mock_entry()
    coordinator = Mock()
    entry.runtime_data = coordinator

    assert asyncio.run(ebay_integration.async_unload_entry(hass, entry)) is True
    coordinator.cancel_scheduled_events.assert_called_once()
    hass.config_entries.async_unload_platforms.assert_awaited()


def test_options_listener_reloads_entry() -> None:
    hass = Mock()
    hass.config_entries.async_reload = AsyncMock()
    entry = _mock_entry()
    asyncio.run(ebay_integration._async_update_listener(hass, entry))
    hass.config_entries.async_reload.assert_awaited_with(entry.entry_id)


def test_legacy_refresh_token_entry_setup_without_token_blob() -> None:
    """Legacy entries with only refresh_token still construct a client."""
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
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=Mock(),
        ),
    ):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True

    assert captured.get("refresh_token") == "legacy-refresh"
    assert captured.get("oauth_session") is None
