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
from custom_components.ebay.account_identity import (
    AccountBindingState,
    AccountIdentifierType,
    AccountIdentity,
    AccountIdentityProvenance,
)
from custom_components.ebay.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENVIRONMENT,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SITE_ID,
)


@pytest.fixture(autouse=True)
def _mock_identity_verification() -> Any:
    async def _mock_verify(*args: Any, **kwargs: Any) -> AccountIdentity:
        return AccountIdentity(
            account_id="client",
            username="client",
            binding_state=AccountBindingState.VERIFIED,
            provenance=AccountIdentityProvenance.AUTHENTICATED_GET_USER,
            identifier_type=AccountIdentifierType.EIASTOKEN,
        )

    with (
        patch(
            "custom_components.ebay.config_flow.async_verify_authenticated_account",
            side_effect=_mock_verify,
        ),
        patch(
            "custom_components.ebay.config_flow.async_verify_entry_existing_identity",
            side_effect=_mock_verify,
        ),
        patch(
            "custom_components.ebay.config_flow.async_get_clientsession",
            return_value=Mock(),
        ),
    ):
        yield


def _entry_data(**overrides: Any) -> dict[str, Any]:
    from custom_components.ebay.clients.scopes import DEFAULT_SCOPE
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
            "custom_components.ebay.storage.async_load_repair_streaks",
            new=AsyncMock(return_value={}),
        )
    )
    stack.enter_context(
        patch(
            "custom_components.ebay.storage.async_load_ending_soon_fired",
            new=AsyncMock(return_value=set()),
        )
    )
    stack.enter_context(
        patch(
            "custom_components.ebay.storage.async_load_inactive_item_sensors",
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

    async def _mock_verify(*args: Any, **kwargs: Any) -> AccountIdentity:
        return AccountIdentity(
            account_id="client",
            username="client",
            binding_state=AccountBindingState.VERIFIED,
            provenance=AccountIdentityProvenance.AUTHENTICATED_GET_USER,
            identifier_type=AccountIdentifierType.USER_ID,
        )

    stack.enter_context(
        patch(
            "custom_components.ebay.config_flow.async_verify_authenticated_account",
            side_effect=_mock_verify,
        )
    )
    stack.enter_context(
        patch(
            "custom_components.ebay.config_flow.async_get_clientsession",
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

    assert entry.version == 5
    assert "token" in entry.data
    assert entry.data["token"]["refresh_token"] == "legacy-refresh"
    assert hass.config_entries.async_update_entry.call_count == 4


def test_migrate_rejects_future_version() -> None:
    hass = Mock()
    entry = _mock_entry(version=6)
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
    assert entry.version == 5
    assert CONF_OAUTH_SCOPES not in entry.data


def test_migrate_v4_to_v5_adds_account_identity() -> None:
    from custom_components.ebay.const import CONF_ACCOUNT_IDENTITY

    hass = Mock()

    def _update_entry(entry: Mock, **kwargs: Any) -> None:
        if "data" in kwargs:
            entry.data = kwargs["data"]
        if "version" in kwargs:
            entry.version = kwargs["version"]

    hass.config_entries.async_update_entry = Mock(side_effect=_update_entry)
    entry = _mock_entry(version=4, data=_entry_data())
    assert asyncio.run(ebay_integration.async_migrate_entry(hass, entry)) is True
    assert entry.version == 5
    assert CONF_ACCOUNT_IDENTITY in entry.data
    assert entry.data[CONF_ACCOUNT_IDENTITY]["binding_state"] == "legacy_compatible"
    assert (
        entry.data[CONF_ACCOUNT_IDENTITY]["identifier_type"] == "legacy_app_environment"
    )


def test_async_setup_entry_requires_core_oauth_scope() -> None:
    hass = Mock()
    entry = _mock_entry(data=_entry_data(oauth_scopes="stale"))
    with pytest.raises(ConfigEntryAuthFailed):
        asyncio.run(ebay_integration.async_setup_entry(hass, entry))


def test_async_setup_entry_accepts_core_only_oauth_scopes() -> None:
    from custom_components.ebay.clients.scopes import CORE_SCOPE
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

    assert entry.version == 5
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
            "custom_components.ebay.storage.async_save_repair_streaks",
            new_callable=AsyncMock,
        ) as save_streaks,
        patch(
            "custom_components.ebay.storage.async_save_ending_soon_fired",
            new_callable=AsyncMock,
        ) as save_ending_soon,
        patch(
            "custom_components.ebay.storage.async_save_inactive_item_sensors",
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
            "custom_components.ebay.storage.async_remove_repair_streaks",
            new_callable=AsyncMock,
        ) as remove_streaks,
        patch(
            "custom_components.ebay.storage.async_remove_ending_soon_fired",
            new_callable=AsyncMock,
        ) as remove_ending_soon,
        patch(
            "custom_components.ebay.storage.async_remove_inactive_item_sensors",
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
    from custom_components.ebay.clients.scopes import DEFAULT_SCOPE
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
            "custom_components.ebay.storage.async_load_repair_streaks",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "custom_components.ebay.storage.async_load_ending_soon_fired",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "custom_components.ebay.storage.async_load_inactive_item_sensors",
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


def test_update_listener_skips_token_only_refresh_and_reloads_on_credential_reauth() -> (
    None
):
    """Token-only refresh triggers 0 reloads; credential reauth with unchanged options triggers 1."""
    from homeassistant import config_entries

    from custom_components.ebay.clients.scopes import CORE_SCOPE
    from custom_components.ebay.config_flow import EbayConfigFlow

    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock(return_value=True)

    entry = _mock_entry(version=4)
    listeners: list[Any] = []

    def _add_listener(listener: Any) -> Any:
        listeners.append(listener)
        return lambda: None

    entry.add_update_listener = Mock(side_effect=_add_listener)
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with _patch_setup(coordinator=coordinator):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True

    assert len(listeners) == 1
    listener = listeners[0]

    # 1. Simulate OAuth2Session routine token refresh (only access_token / expires_at change)
    entry.data = {
        **entry.data,
        "token": {
            "access_token": "refreshed-access-token",
            "refresh_token": "refresh",
            "expires_in": 7200,
            "expires_at": 9999999999,
            "token_type": "Bearer",
        },
    }
    asyncio.run(listener(hass, entry))
    assert hass.config_entries.async_reload.await_count == 0

    # 2. Simulate realistic reauth completion with changed credentials and UNCHANGED options
    flow = EbayConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client",
        CONF_CLIENT_SECRET: "rotated-client-secret",
        CONF_RUNAME: "updated-runame",
        CONF_SITE_ID: "3",  # Changed marketplace (EBAY_GB)
    }
    flow._requested_scopes = CORE_SCOPE

    def _update_and_abort(
        target_entry: Any,
        *,
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if data is not None:
            target_entry.data = dict(data)
        if options is not None:
            target_entry.options = dict(options)
        return {"type": "abort", "reason": "reauth_successful"}

    with (
        patch.object(flow, "_get_reauth_entry", return_value=entry),
        patch.object(flow, "async_update_and_abort", side_effect=_update_and_abort),
    ):
        result = asyncio.run(
            flow.async_oauth_create_entry(
                {
                    "auth_implementation": "ebay:production:client",
                    "token": {
                        "access_token": "new-access",
                        "refresh_token": "refresh",
                        "expires_in": 7200,
                        "token_type": "Bearer",
                    },
                }
            )
        )
    asyncio.run(listener(hass, entry))

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert hass.config_entries.async_reload.await_count == 1
    assert entry.data[CONF_CLIENT_SECRET] == "rotated-client-secret"
    assert entry.data[CONF_SITE_ID] == "3"


def test_update_listener_reauth_with_pending_options_reloads_once() -> None:
    """Staging pending options triggers 0 reloads; completing reauth triggers 1 reload."""
    from homeassistant import config_entries

    from custom_components.ebay.clients.scopes import scopes_for_options
    from custom_components.ebay.config_flow import EbayConfigFlow
    from custom_components.ebay.const import CONF_OAUTH_SCOPES, CONF_PENDING_OPTIONS

    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock(return_value=True)

    entry = _mock_entry(version=4)
    entry.options = {"feedback_enabled": False, "poll_interval": 30}
    listeners: list[Any] = []

    def _add_listener(listener: Any) -> Any:
        listeners.append(listener)
        return lambda: None

    entry.add_update_listener = Mock(side_effect=_add_listener)
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with _patch_setup(coordinator=coordinator):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True

    listener = listeners[0]
    pending = {"feedback_enabled": True, "poll_interval": 45}

    # 1. Stage pending options in entry.data (as EbayOptionsFlow._store_pending_options does)
    entry.data = {**entry.data, CONF_PENDING_OPTIONS: pending}
    asyncio.run(listener(hass, entry))
    assert hass.config_entries.async_reload.await_count == 0

    # 2. Complete reauth committing pending options and expanded scopes
    flow = EbayConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client",
        CONF_CLIENT_SECRET: "secret",
        CONF_RUNAME: "runame",
        CONF_SITE_ID: "0",
    }
    flow._requested_scopes = scopes_for_options(pending)

    def _update_and_abort(
        target_entry: Any,
        *,
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if data is not None:
            target_entry.data = dict(data)
        if options is not None:
            target_entry.options = dict(options)
        return {"type": "abort", "reason": "reauth_successful"}

    with (
        patch.object(flow, "_get_reauth_entry", return_value=entry),
        patch.object(flow, "async_update_and_abort", side_effect=_update_and_abort),
    ):
        result = asyncio.run(
            flow.async_oauth_create_entry(
                {
                    "auth_implementation": "ebay",
                    "token": {
                        "access_token": "new-access",
                        "refresh_token": "refresh",
                        "expires_in": 7200,
                        "token_type": "Bearer",
                    },
                }
            )
        )
    asyncio.run(listener(hass, entry))

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert hass.config_entries.async_reload.await_count == 1
    assert CONF_PENDING_OPTIONS not in entry.data
    assert entry.options == pending
    assert "commerce.feedback" in entry.data[CONF_OAUTH_SCOPES]


def test_update_listener_cancelled_or_failed_reauth_preserves_live_config() -> None:
    """Failed or cancelled reauth does not replace live credentials or reload."""
    from homeassistant import config_entries

    from custom_components.ebay.api import EbayAuthError
    from custom_components.ebay.config_flow import EbayConfigFlow
    from custom_components.ebay.const import CONF_PENDING_OPTIONS

    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock(return_value=True)

    entry = _mock_entry(version=4)
    entry.options = {"feedback_enabled": False}
    listeners: list[Any] = []

    def _add_listener(listener: Any) -> Any:
        listeners.append(listener)
        return lambda: None

    entry.add_update_listener = Mock(side_effect=_add_listener)
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with _patch_setup(coordinator=coordinator):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True

    listener = listeners[0]

    def _async_update_entry(target: Any, **kwargs: Any) -> bool:
        if "data" in kwargs:
            target.data = dict(kwargs["data"])
        if "options" in kwargs:
            target.options = dict(kwargs["options"])
        asyncio.run(listener(hass, target))
        return True

    hass.config_entries.async_update_entry = Mock(side_effect=_async_update_entry)

    # 1. Stage pending options
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_PENDING_OPTIONS: {"feedback_enabled": True}}
    )
    assert hass.config_entries.async_reload.await_count == 0

    # 2. Start reauth, enter different credentials in form, fail manual exchange, then cancel
    flow = EbayConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    flow._data = {
        CONF_ENVIRONMENT: "sandbox",
        CONF_CLIENT_ID: "unverified-client-id",
        CONF_CLIENT_SECRET: "unverified-secret",
        CONF_RUNAME: "unverified-runame",
        CONF_SITE_ID: "77",
    }

    mock_client = Mock()
    mock_client.async_exchange_authorization_code = AsyncMock(
        side_effect=EbayAuthError("invalid_grant")
    )
    with (
        patch.object(flow, "_get_reauth_entry", return_value=entry),
        patch(
            "custom_components.ebay.config_flow.EbayApiClient",
            return_value=mock_client,
        ),
        patch(
            "custom_components.ebay.config_flow.async_get_clientsession",
            return_value=Mock(),
        ),
    ):
        form_res = asyncio.run(flow.async_step_manual({"authorization_code": "bad"}))
        assert form_res["type"] == "form"
        assert form_res["errors"] == {"base": "cannot_connect"}
        flow.async_remove()

    # Live entry credentials and options must remain unchanged, with 0 reloads
    assert hass.config_entries.async_reload.await_count == 0
    assert CONF_PENDING_OPTIONS not in entry.data
    assert entry.data[CONF_ENVIRONMENT] == "production"
    assert entry.data[CONF_CLIENT_ID] == "client"
    assert entry.data[CONF_CLIENT_SECRET] == "secret"
    assert entry.data[CONF_RUNAME] == "runame"
    assert entry.data[CONF_SITE_ID] == "0"
    assert entry.options == {"feedback_enabled": False}


def test_update_listener_routine_oauth2_session_refresh_with_refresh_token_rotation_does_not_reload() -> (
    None
):
    """Routine OAuth2Session refresh that rotates both access_token and refresh_token triggers 0 reloads."""
    import time

    from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

    from custom_components.ebay.const import CONF_REAUTH_GENERATION

    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock(return_value=True)

    # Legacy entry without CONF_REAUTH_GENERATION and with an expired token
    entry = _mock_entry(
        version=4,
        data=_entry_data(
            token={
                "access_token": "expired-access",
                "refresh_token": "initial-refresh",
                "expires_at": time.time() - 60,
            }
        ),
    )
    assert CONF_REAUTH_GENERATION not in entry.data

    listeners: list[Any] = []
    entry.add_update_listener = Mock(
        side_effect=lambda cb: (listeners.append(cb), (lambda: None))[1]
    )
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with _patch_setup(coordinator=coordinator):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True

    listener = listeners[0]

    class _RotatingImplementation:
        async def async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
            return {
                **token,
                "access_token": "rotated-access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 7200,
                "expires_at": time.time() + 7200,
            }

    def _async_update_entry(target: Any, **kwargs: Any) -> bool:
        if "data" in kwargs:
            target.data = dict(kwargs["data"])
        if "options" in kwargs:
            target.options = dict(kwargs["options"])
        return True

    hass.config_entries.async_update_entry = Mock(side_effect=_async_update_entry)
    oauth_session = OAuth2Session(
        hass,
        entry,
        _RotatingImplementation(),  # type: ignore[arg-type]
    )

    asyncio.run(oauth_session.async_ensure_token_valid())
    asyncio.run(listener(hass, entry))

    assert entry.data["token"]["access_token"] == "rotated-access-token"
    assert entry.data["token"]["refresh_token"] == "rotated-refresh-token"
    assert CONF_REAUTH_GENERATION not in entry.data
    assert hass.config_entries.async_reload.await_count == 0


def test_update_listener_graphql_forced_refresh_with_refresh_token_rotation_does_not_reload() -> (
    None
):
    """GraphQL forced refresh rotating refresh_token triggers 0 reloads."""
    import time

    from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

    from custom_components.ebay.api import EbayApiClient
    from custom_components.ebay.const import CONF_REAUTH_GENERATION

    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock(return_value=True)

    entry = _mock_entry(
        version=4,
        data=_entry_data(
            token={
                "access_token": "rejected-access",
                "refresh_token": "initial-refresh",
                "expires_at": time.time() + 3600,
            }
        ),
    )
    assert CONF_REAUTH_GENERATION not in entry.data

    listeners: list[Any] = []
    entry.add_update_listener = Mock(
        side_effect=lambda cb: (listeners.append(cb), (lambda: None))[1]
    )
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with _patch_setup(coordinator=coordinator):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True

    listener = listeners[0]

    class _RotatingImplementation:
        async def async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
            return {
                "access_token": "graphql-new-access",
                "refresh_token": "graphql-rotated-refresh",
                "expires_in": 7200,
                "token_type": "Bearer",
            }

    def _async_update_entry(target: Any, **kwargs: Any) -> bool:
        if "data" in kwargs:
            target.data = dict(kwargs["data"])
        if "options" in kwargs:
            target.options = dict(kwargs["options"])
        return True

    hass.config_entries.async_update_entry = Mock(side_effect=_async_update_entry)
    oauth_session = OAuth2Session(
        hass,
        entry,
        _RotatingImplementation(),  # type: ignore[arg-type]
    )
    client = EbayApiClient(
        Mock(),
        environment="production",
        client_id="client",
        client_secret="secret",
        runame="runame",
        refresh_token=None,
        site_id="0",
        oauth_session=oauth_session,
    )

    refreshed = asyncio.run(
        client.async_refresh_access_token(failed_token="rejected-access")
    )
    asyncio.run(listener(hass, entry))

    assert refreshed == "graphql-new-access"
    assert entry.data["token"]["access_token"] == "graphql-new-access"
    assert entry.data["token"]["refresh_token"] == "graphql-rotated-refresh"
    assert CONF_REAUTH_GENERATION not in entry.data
    assert hass.config_entries.async_reload.await_count == 0


def test_update_listener_explicit_reauth_with_unchanged_credentials_and_options_reloads_once() -> (
    None
):
    """Explicit successful reauth with identical credentials/options increments reauth_generation and reloads once."""
    from homeassistant import config_entries

    from custom_components.ebay.clients.scopes import DEFAULT_SCOPE
    from custom_components.ebay.config_flow import EbayConfigFlow
    from custom_components.ebay.const import CONF_REAUTH_GENERATION

    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock(return_value=True)

    # Start with a legacy config entry that has no CONF_REAUTH_GENERATION marker
    entry = _mock_entry(version=4)
    assert CONF_REAUTH_GENERATION not in entry.data

    listeners: list[Any] = []
    entry.add_update_listener = Mock(
        side_effect=lambda cb: (listeners.append(cb), (lambda: None))[1]
    )
    coordinator = Mock()
    coordinator.async_config_entry_first_refresh = AsyncMock()

    with _patch_setup(coordinator=coordinator):
        assert asyncio.run(ebay_integration.async_setup_entry(hass, entry)) is True

    listener = listeners[0]

    flow = EbayConfigFlow()
    flow.hass = hass
    flow.context = {
        "source": config_entries.SOURCE_REAUTH,
        "entry_id": entry.entry_id,
    }
    # Identical app credentials, RuName, marketplace, environment, scopes, and options
    flow._data = {
        CONF_ENVIRONMENT: "production",
        CONF_CLIENT_ID: "client",
        CONF_CLIENT_SECRET: "secret",
        CONF_RUNAME: "runame",
        CONF_SITE_ID: "0",
    }
    flow._requested_scopes = DEFAULT_SCOPE

    def _update_and_abort(
        target_entry: Any,
        *,
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if data is not None:
            target_entry.data = dict(data)
        if options is not None:
            target_entry.options = dict(options)
        return {"type": "abort", "reason": "reauth_successful"}

    with (
        patch.object(flow, "_get_reauth_entry", return_value=entry),
        patch.object(flow, "async_update_and_abort", side_effect=_update_and_abort),
    ):
        result = asyncio.run(
            flow.async_oauth_create_entry(
                {
                    "auth_implementation": "ebay",
                    "token": {
                        "access_token": "reauth-access-1",
                        "refresh_token": "reauth-refresh-1",
                        "expires_in": 7200,
                        "token_type": "Bearer",
                    },
                }
            )
        )
    asyncio.run(listener(hass, entry))

    assert result == {"type": "abort", "reason": "reauth_successful"}
    assert entry.data[CONF_REAUTH_GENERATION] == 1
    assert hass.config_entries.async_reload.await_count == 1

    # Routine token rotation after reauth must NOT reload
    entry.data = {
        **entry.data,
        "token": {
            "access_token": "rotated-access-after-reauth",
            "refresh_token": "rotated-refresh-after-reauth",
            "expires_in": 7200,
            "expires_at": 9999999999,
            "token_type": "Bearer",
        },
    }
    asyncio.run(listener(hass, entry))
    assert hass.config_entries.async_reload.await_count == 1

    # A second explicit reauth increments reauth_generation to 2 and reloads once more
    with (
        patch.object(flow, "_get_reauth_entry", return_value=entry),
        patch.object(flow, "async_update_and_abort", side_effect=_update_and_abort),
    ):
        asyncio.run(
            flow.async_oauth_create_entry(
                {
                    "auth_implementation": "ebay",
                    "token": {
                        "access_token": "reauth-access-2",
                        "refresh_token": "reauth-refresh-2",
                        "expires_in": 7200,
                        "token_type": "Bearer",
                    },
                }
            )
        )
    asyncio.run(listener(hass, entry))
    assert entry.data[CONF_REAUTH_GENERATION] == 2
    assert hass.config_entries.async_reload.await_count == 2
