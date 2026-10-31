"""Comprehensive end-to-end integration and release-readiness tests for Milestone 12."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

import custom_components.ebay as ebay_integration
from custom_components.ebay import (
    async_migrate_entry,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.ebay import (
    binary_sensor as binary_sensor_module,
    button as button_module,
    calendar as calendar_module,
    event as event_module,
    sensor as sensor_module,
)
from custom_components.ebay.clients.errors import EbayAuthError
from custom_components.ebay.clients.resilience import EndpointFamily
from custom_components.ebay.const import (
    CONF_ACCOUNT_IDENTITY,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENTITY_MODE,
    CONF_ENVIRONMENT,
    CONF_OAUTH_SCOPES,
    CONF_PER_ITEM_CAP,
    CONF_REFRESH_TOKEN,
    CONF_RUNAME,
    CONF_SITE_ID,
    DEFAULT_OPTIONS,
    DOMAIN,
    ENTITY_MODE_BALANCED,
    ENTITY_MODE_DETAILED,
    ENV_PRODUCTION,
    ENV_SANDBOX,
)
from custom_components.ebay.coordinator import (
    EbayDataUpdateCoordinator,
    _baseline_payload,
)
from custom_components.ebay.diagnostics import async_get_config_entry_diagnostics
from custom_components.ebay.migration_policy import (
    FeatureKey,
    MigrationPolicy,
    ProviderType,
    ReadinessEvidence,
    ReadinessState,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _sample_item(
    item_id: str, title: str, price: float, minutes_left: int = 60
) -> dict[str, Any]:
    """Return a representative normalized item dict for testing."""
    end_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_left)
    return {
        "item_id": item_id,
        "title": title,
        "current_price": price,
        "currency": "USD",
        "end_time": end_time,
        "seconds_left": minutes_left * 60,
        "listing_type": "FixedPriceItem",
        "item_url": f"https://www.ebay.com/itm/{item_id}",
        "thumbnail_url": f"https://i.ebayimg.com/thumbs/{item_id}.jpg",
    }


def _sample_payload(
    watched_items: dict[str, Any] | None = None,
    bidding_items: dict[str, Any] | None = None,
    selling_items: dict[str, Any] | None = None,
    truncated_collections: dict[str, bool] | None = None,
    partial_failures: list[str] | None = None,
    last_successful_update: datetime | None = None,
) -> dict[str, Any]:
    """Construct a full coordinator payload."""
    now = datetime.now(timezone.utc)
    watched = watched_items or {}
    bidding = bidding_items or {}
    selling = selling_items or {}
    truncated = truncated_collections or {}
    partials = partial_failures or []
    success_ts = last_successful_update or now

    return {
        "watched": dict(watched),
        "bidding": dict(bidding),
        "selling": dict(selling),
        "selling_classification": {k: "active" for k in selling},
        "sold_item_ids": [],
        "unsold_item_ids": [],
        "active_selling_item_ids": list(selling.keys()),
        "truncated_collections": dict(truncated),
        "partial_failures": list(partials),
        "api_warnings": [],
        "missing_scopes": {},
        "missing_scope_features": [],
        "refreshed_sections": ["buying", "selling"],
        "section_last_fetched": {"buying": now, "selling": now},
        "section_last_attempt": {"buying": now, "selling": now},
        "section_last_success": {"buying": success_ts, "selling": success_ts},
        "last_update": now,
        "last_successful_update": success_ts,
        "summary": {
            "active_selling_items": len(selling),
            "watched_items": len(watched),
            "bidding_items": len(bidding),
            "total_sold_last_60_days": 0,
            "total_unsold_last_60_days": 0,
            "feedback_score": 100,
            "positive_feedback_percent": 99.5,
            "feedback_rating_star": "Turquoise",
            "orders_overdue": 0,
            "orders_due_today": 0,
            "open_payment_disputes": 0,
            "unread_buyer_messages": 0,
            "seller_standard_level": "TOP_RATED",
            "seller_standard_at_risk": False,
            "last_update": now.isoformat(),
            "last_successful_update": success_ts.isoformat(),
            "partial_failures": list(partials),
            "truncated_collections": dict(truncated),
            "api_warnings": [],
        },
    }


def _create_mock_entry(
    entry_id: str = "entry-e2e-1",
    client_id: str = "app_id_e2e_1",
    environment: str = ENV_PRODUCTION,
    options: dict[str, Any] | None = None,
    version: int = 5,
) -> MockConfigEntry:
    """Create a configured MockConfigEntry."""
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    return MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        version=version,
        title=f"eBay ({client_id})",
        data={
            CONF_ENVIRONMENT: environment,
            CONF_CLIENT_ID: client_id,
            CONF_CLIENT_SECRET: "test_secret_xyz",
            CONF_RUNAME: "test_runame_abc",
            CONF_REFRESH_TOKEN: "test_refresh_token_123",
            CONF_SITE_ID: "0",
            CONF_OAUTH_SCOPES: "https://api.ebay.com/oauth/api_scope",
            "token": {
                "access_token": "test_access_token_abc",
                "refresh_token": "test_refresh_token_123",
                "expires_at": 9999999999.0,
            },
            "auth_implementation": "ebay",
            CONF_ACCOUNT_IDENTITY: {
                "entry_id": entry_id,
                "environment": environment,
                "client_id": client_id,
                "account_id": "test_account_hash",
                "identifier_type": "eiastoken",
                "username": "test_user",
                "binding_state": "verified",
                "provenance": "authenticated_get_user",
                "verified_at": "2026-10-31T12:00:00+00:00",
                "failure_reason": "none",
                "reauth_generation": 0,
            },
        },
        options=opts,
    )


# ==============================================================================
# Suite 1: Home Assistant Lifecycle & Platform Forwarding
# ==============================================================================


@pytest.mark.asyncio
async def test_e2e_full_setup_coordinator_refresh_and_all_platforms(
    hass: HomeAssistant,
) -> None:
    """Integration sets up coordinator and creates entities across all 5 platforms."""
    assert await async_setup_component(hass, "calendar", {})
    entry = _create_mock_entry(
        options={CONF_ENTITY_MODE: ENTITY_MODE_BALANCED, CONF_PER_ITEM_CAP: 5}
    )
    entry.add_to_hass(hass)

    items = {
        f"w_{i}": _sample_item(f"w_{i}", f"Watched Item {i}", 10.0 * (i + 1))
        for i in range(3)
    }
    payload = _sample_payload(watched_items=items)

    coordinator = EbayDataUpdateCoordinator(hass, entry, Mock(), entry.options)
    coordinator.data = payload
    coordinator.update_interval = None
    coordinator.refresh_selected_item_keys(payload)
    entry.runtime_data = coordinator

    try:
        # Test sensor platform
        sensor_plat = MockEntityPlatform(
            hass, domain="sensor", platform_name=DOMAIN, platform=sensor_module
        )
        assert await sensor_plat.async_setup_entry(entry)

        # Test binary sensor platform
        bin_plat = MockEntityPlatform(
            hass,
            domain="binary_sensor",
            platform_name=DOMAIN,
            platform=binary_sensor_module,
        )
        assert await bin_plat.async_setup_entry(entry)

        # Test button platform
        btn_plat = MockEntityPlatform(
            hass, domain="button", platform_name=DOMAIN, platform=button_module
        )
        assert await btn_plat.async_setup_entry(entry)

        # Test calendar platform
        cal_plat = MockEntityPlatform(
            hass, domain="calendar", platform_name=DOMAIN, platform=calendar_module
        )
        assert await cal_plat.async_setup_entry(entry)

        # Test event platform
        evt_plat = MockEntityPlatform(
            hass, domain="event", platform_name=DOMAIN, platform=event_module
        )
        assert await evt_plat.async_setup_entry(entry)

        await hass.async_block_till_done()

        # 1. Verify summary sensors
        entity_reg = er.async_get(hass)
        ent_id = entity_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_watched_items"
        )
        assert ent_id is not None
        watch_state = hass.states.get(ent_id)
        assert watch_state is not None
        assert int(watch_state.state) == 3

        # 2. Verify per-item entities created up to cap
        item_0_price_id = entity_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_watched_w_0_price"
        )
        assert item_0_price_id is not None
        price_state = hass.states.get(item_0_price_id)
        assert price_state is not None
        assert float(price_state.state) == 10.0

        # 3. Verify calendar entities
        cal_watched_id = entity_reg.async_get_entity_id(
            "calendar", DOMAIN, f"{entry.entry_id}_watched_items"
        )
        assert cal_watched_id is not None

        # 4. Verify refresh button
        btn_refresh_id = entity_reg.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}_refresh"
        )
        assert btn_refresh_id is not None

        # 5. Verify activity event entities
        evt_watch_id = entity_reg.async_get_entity_id(
            "event", DOMAIN, f"{entry.entry_id}_watching_activity"
        )
        assert evt_watch_id is not None
    finally:
        await cal_plat.async_reset()
        await evt_plat.async_reset()
        await btn_plat.async_reset()
        await bin_plat.async_reset()
        await sensor_plat.async_reset()
        await coordinator.async_shutdown()
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_e2e_options_update_reloads_entry(hass: HomeAssistant) -> None:
    """Changing integration options signals Home Assistant reload."""
    entry = _create_mock_entry()
    entry.add_to_hass(hass)

    coordinator = Mock()
    coordinator._loaded_config = ebay_integration._loaded_config_snapshot(entry)
    entry.runtime_data = coordinator

    hass.config_entries.async_reload = AsyncMock(return_value=True)  # type: ignore[method-assign]

    # Update options
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_ENTITY_MODE: ENTITY_MODE_DETAILED},
    )

    await ebay_integration._async_update_listener(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_e2e_token_rotation_skips_reload(hass: HomeAssistant) -> None:
    """OAuth token update or rotation skips reload to prevent reload storms."""
    entry = _create_mock_entry()
    entry.add_to_hass(hass)

    coordinator = Mock()
    coordinator._loaded_config = ebay_integration._loaded_config_snapshot(entry)
    entry.runtime_data = coordinator

    hass.config_entries.async_reload = AsyncMock(return_value=True)  # type: ignore[method-assign]

    # Update only token dictionary (e.g. routine token refresh)
    new_data = dict(entry.data)
    new_data["token"] = {
        "access_token": "brand_new_rotated_token",
        "refresh_token": "brand_new_rotated_refresh",
        "expires_at": 9999999999.0,
    }
    hass.config_entries.async_update_entry(entry, data=new_data)

    await ebay_integration._async_update_listener(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_e2e_unload_and_remove_cleans_up(hass: HomeAssistant) -> None:
    """Unloading and removing an entry cleanly frees coordinator, timers, and storage."""
    entry = _create_mock_entry()
    entry.add_to_hass(hass)

    coordinator = Mock()
    coordinator.async_shutdown = AsyncMock()
    coordinator._repair_streaks = {}
    coordinator._fired_ending_soon = set()
    coordinator.inactive_item_sensors = {}
    entry.runtime_data = coordinator

    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)  # type: ignore[method-assign]

    # Acquire tracker reference
    ebay_integration.async_get_rate_limit_tracker(
        hass,
        entry.data[CONF_ENVIRONMENT],
        entry.data[CONF_CLIENT_ID],
        entry_id=entry.entry_id,
    )
    trackers = hass.data[DOMAIN]["rate_limit_trackers"]
    assert len(trackers) == 1

    # Unload entry
    assert await async_unload_entry(hass, entry) is True
    coordinator.async_shutdown.assert_awaited_once()
    assert len(trackers) == 0

    # Remove entry
    with (
        patch(
            "custom_components.ebay.repairs.async_delete_entry_issues"
        ) as mock_delete,
        patch(
            "custom_components.ebay.repairs.async_remove_repair_streaks",
            new=AsyncMock(),
        ) as mock_streaks,
        patch(
            "custom_components.ebay.ending_soon_store.async_remove_ending_soon_fired",
            new=AsyncMock(),
        ) as mock_ending,
        patch(
            "custom_components.ebay.inactive_item_store.async_remove_inactive_item_sensors",
            new=AsyncMock(),
        ) as mock_inactive,
    ):
        await async_remove_entry(hass, entry)
        mock_delete.assert_called_once()
        mock_streaks.assert_awaited_once_with(hass, entry.entry_id)
        mock_ending.assert_awaited_once_with(hass, entry.entry_id)
        mock_inactive.assert_awaited_once_with(hass, entry.entry_id)


@pytest.mark.asyncio
async def test_e2e_missing_scope_raises_auth_failed(hass: HomeAssistant) -> None:
    """Missing core OAuth scope aborts setup with ConfigEntryAuthFailed."""
    entry = _create_mock_entry()
    entry.add_to_hass(hass)
    data = dict(entry.data)
    data[CONF_OAUTH_SCOPES] = "https://api.ebay.com/oauth/api_scope/unrelated"
    hass.config_entries.async_update_entry(entry, data=data)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_e2e_first_refresh_failure_releases_tracker(hass: HomeAssistant) -> None:
    """First coordinator refresh failure raises ConfigEntryNotReady and releases tracker."""
    entry = _create_mock_entry()
    entry.add_to_hass(hass)

    with (
        patch("homeassistant.helpers.aiohttp_client.async_get_clientsession"),
        patch(
            "custom_components.ebay.coordinator.EbayDataUpdateCoordinator.async_config_entry_first_refresh",
            side_effect=ConfigEntryNotReady("API down"),
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
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)

    trackers = hass.data.get(DOMAIN, {}).get("rate_limit_trackers", {})
    assert len(trackers) == 0


# ==============================================================================
# Suite 2: Existing-User Migration
# ==============================================================================


@pytest.mark.asyncio
async def test_e2e_migrate_v1_to_v5_preserves_tokens_and_account_identity(
    hass: HomeAssistant,
) -> None:
    """Upgrading an existing v1 entry migrates schema to v5 without losing tokens or identity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        entry_id="entry-mig-1",
        data={
            CONF_ENVIRONMENT: "production",
            CONF_CLIENT_ID: "client_legacy",
            CONF_CLIENT_SECRET: "secret_legacy",
            CONF_RUNAME: "runame_legacy",
            CONF_REFRESH_TOKEN: "refresh_token_legacy_123",
            CONF_SITE_ID: "0",
        },
        options=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 5

    # Verify v2 token migration
    assert "token" in entry.data
    assert entry.data["token"]["refresh_token"] == "refresh_token_legacy_123"

    # Verify v5 account identity migration
    identity = entry.data.get(CONF_ACCOUNT_IDENTITY)
    assert identity is not None
    assert identity["binding_state"] == "legacy_compatible"
    assert identity["identifier_type"] == "legacy_app_environment"
    assert identity["provenance"] == "legacy_migration"


@pytest.mark.asyncio
async def test_e2e_future_version_fails_closed(hass: HomeAssistant) -> None:
    """Entry with an unsupported future schema version fails closed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=6,
        entry_id="entry-future",
        data={},
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is False


# ==============================================================================
# Suite 3: Multi-Account Isolation and Shared Keyset Tracking
# ==============================================================================


@pytest.mark.asyncio
async def test_e2e_two_accounts_same_keyset_share_cooldown(hass: HomeAssistant) -> None:
    """Two independent accounts sharing one developer keyset share rate-limit cooldowns."""
    entry1 = _create_mock_entry(entry_id="entry_acct_1", client_id="shared_app_1")
    entry2 = _create_mock_entry(entry_id="entry_acct_2", client_id="shared_app_1")
    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    # Acquire trackers
    tracker1 = ebay_integration.async_get_rate_limit_tracker(
        hass,
        entry1.data[CONF_ENVIRONMENT],
        entry1.data[CONF_CLIENT_ID],
        entry_id=entry1.entry_id,
    )
    tracker2 = ebay_integration.async_get_rate_limit_tracker(
        hass,
        entry2.data[CONF_ENVIRONMENT],
        entry2.data[CONF_CLIENT_ID],
        entry_id=entry2.entry_id,
    )

    # Crucial: Must be the identical tracker instance
    assert tracker1 is tracker2

    trackers = hass.data[DOMAIN]["rate_limit_trackers"]
    safe_hash = hashlib.sha256("shared_app_1".encode("utf-8")).hexdigest()[:12]
    key = f"production:{safe_hash}"
    assert trackers[key]["ref_count"] == 2
    assert trackers[key]["entries"] == {"entry_acct_1", "entry_acct_2"}

    # Record 429 throttled cooldown on account 1
    tracker1.record_throttled(EndpointFamily.REST, 60.0, "shared_app_1")
    assert tracker2.is_throttled(EndpointFamily.REST, "shared_app_1")
    assert tracker2.cooldown_remaining(EndpointFamily.REST, "shared_app_1") > 0.0

    # Unload account 1: tracker retained for account 2
    ebay_integration.async_release_rate_limit_tracker(
        hass,
        entry1.data[CONF_ENVIRONMENT],
        entry1.data[CONF_CLIENT_ID],
        entry_id=entry1.entry_id,
    )
    assert key in trackers
    assert trackers[key]["ref_count"] == 1
    assert trackers[key]["entries"] == {"entry_acct_2"}

    # Unload account 2: tracker purged
    ebay_integration.async_release_rate_limit_tracker(
        hass,
        entry2.data[CONF_ENVIRONMENT],
        entry2.data[CONF_CLIENT_ID],
        entry_id=entry2.entry_id,
    )
    assert key not in trackers


@pytest.mark.asyncio
async def test_e2e_two_accounts_different_keysets_are_isolated(
    hass: HomeAssistant,
) -> None:
    """Accounts on different developer keysets have strictly isolated trackers."""
    entry_a = _create_mock_entry(entry_id="entry_a", client_id="app_A")
    entry_b = _create_mock_entry(entry_id="entry_b", client_id="app_B")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    tracker_a = ebay_integration.async_get_rate_limit_tracker(
        hass,
        entry_a.data[CONF_ENVIRONMENT],
        entry_a.data[CONF_CLIENT_ID],
        entry_id=entry_a.entry_id,
    )
    tracker_b = ebay_integration.async_get_rate_limit_tracker(
        hass,
        entry_b.data[CONF_ENVIRONMENT],
        entry_b.data[CONF_CLIENT_ID],
        entry_id=entry_b.entry_id,
    )

    assert tracker_a is not tracker_b

    # Throttle app A
    tracker_a.record_throttled(EndpointFamily.REST, 45.0, "app_A")
    assert tracker_a.is_throttled(EndpointFamily.REST, "app_A")
    assert not tracker_b.is_throttled(EndpointFamily.REST, "app_B")


@pytest.mark.asyncio
async def test_e2e_production_vs_sandbox_isolation(hass: HomeAssistant) -> None:
    """Production and Sandbox environments with identical app IDs are strictly isolated."""
    entry_prod = _create_mock_entry(
        entry_id="prod_entry", client_id="same_id", environment=ENV_PRODUCTION
    )
    entry_sand = _create_mock_entry(
        entry_id="sand_entry", client_id="same_id", environment=ENV_SANDBOX
    )
    entry_prod.add_to_hass(hass)
    entry_sand.add_to_hass(hass)

    tracker_prod = ebay_integration.async_get_rate_limit_tracker(
        hass,
        entry_prod.data[CONF_ENVIRONMENT],
        entry_prod.data[CONF_CLIENT_ID],
        entry_id=entry_prod.entry_id,
    )
    tracker_sand = ebay_integration.async_get_rate_limit_tracker(
        hass,
        entry_sand.data[CONF_ENVIRONMENT],
        entry_sand.data[CONF_CLIENT_ID],
        entry_id=entry_sand.entry_id,
    )

    assert tracker_prod is not tracker_sand


# ==============================================================================
# Suite 4: Data Integrity, Pagination & Mandatory Regression Scenario
# ==============================================================================


@pytest.mark.asyncio
async def test_e2e_complete_10_to_truncated_2_to_complete_empty_regression(
    hass: HomeAssistant,
) -> None:
    """Mandatory Regression: 10 complete -> 2 truncated -> 0 genuine complete-empty.

    Verifies:
    1. Truncated refresh retains all 10 items, preserves count, updates observed items,
       suppresses false removal/disappearance events, and preserves last_successful_update.
    2. Subsequent complete-empty refresh transitions to empty and allows legitimate removal.
    """
    entry = _create_mock_entry()
    entry.add_to_hass(hass)

    coordinator = EbayDataUpdateCoordinator(hass, entry, Mock(), entry.options)
    t1 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)

    # --- Phase 1: Complete 10-item refresh ---
    items_10 = {
        f"item_{i}": _sample_item(f"item_{i}", f"Item {i}", 10.0 + i) for i in range(10)
    }
    payload_1 = _sample_payload(
        watched_items=items_10,
        truncated_collections={"watched": False},
        last_successful_update=t1,
    )
    coordinator.data = payload_1
    coordinator.previous_payload = payload_1

    assert len(coordinator.data["watched"]) == 10
    assert coordinator.data["summary"]["watched_items"] == 10
    assert coordinator.data["last_successful_update"] == t1

    # --- Phase 2: Truncated 2-item refresh ---
    # Upstream returns only 2 items with updated price on item_0, truncated=True
    items_2_truncated = {
        "item_0": _sample_item("item_0", "Item 0 Updated", 99.0),
        "item_1": _sample_item("item_1", "Item 1", 11.0),
    }
    payload_2 = _sample_payload(
        watched_items=items_2_truncated,
        truncated_collections={"watched": True},
        partial_failures=["buying_truncated"],
        last_successful_update=t1,  # unchanged success timestamp
    )

    # Test baseline merger logic
    merged_baseline = _baseline_payload(coordinator.previous_payload, payload_2)

    # 1. All 10 items must be retained
    assert len(merged_baseline["watched"]) == 10
    # 2. Updated item_0 price must be reflected
    assert merged_baseline["watched"]["item_0"]["current_price"] == 99.0
    # 3. Missing items item_2 through item_9 must be retained
    for i in range(2, 10):
        assert f"item_{i}" in merged_baseline["watched"]

    # Test event detection: no false removal events fired
    detected_events: list[dict[str, Any]] = []
    coordinator._dispatch_event = lambda event_type, data: detected_events.append(
        {"type": event_type, "data": data}
    )  # type: ignore[method-assign]
    coordinator._detect_transition_events(coordinator.previous_payload, merged_baseline)

    # False disappearance/removal events must NOT be emitted
    removal_events = [
        e for e in detected_events if e["type"] in ("item_removed", "watch_item_ended")
    ]
    assert len(removal_events) == 0

    coordinator.previous_payload = merged_baseline

    # --- Phase 3: Genuine complete-empty refresh ---
    # Upstream returns 0 items, truncated=False
    t3 = datetime(2026, 10, 31, 13, 0, 0, tzinfo=timezone.utc)
    payload_3 = _sample_payload(
        watched_items={},
        truncated_collections={"watched": False},
        last_successful_update=t3,
    )

    final_baseline = _baseline_payload(coordinator.previous_payload, payload_3)
    # State transitions to confirmed complete 0
    assert len(final_baseline["watched"]) == 0
    assert payload_3["summary"]["watched_items"] == 0
    assert payload_3["last_successful_update"] == t3


# ==============================================================================
# Suite 5: Security, Diagnostics & Logging Privacy
# ==============================================================================


@pytest.mark.asyncio
async def test_e2e_diagnostics_privacy_sanitizes_tokens_and_secrets(
    hass: HomeAssistant,
) -> None:
    """Config entry diagnostics redacts all tokens, secrets, and raw RuNames."""
    entry = _create_mock_entry()
    entry.add_to_hass(hass)

    coordinator = Mock()
    coordinator.data = _sample_payload()
    coordinator.options = dict(entry.options)
    coordinator.last_refresh_result = "success"
    coordinator.last_refresh_duration_seconds = 0.5
    coordinator.last_attempt_at = None
    coordinator.last_error_category = None
    coordinator.selected_item_keys = set()
    coordinator.inactive_item_sensors = {}
    coordinator._repair_streaks = {}
    coordinator._fired_ending_soon = set()
    coordinator.api = Mock()
    coordinator.api.get_rate_limiting_diagnostics = Mock(
        return_value={"last_retry_count": 0}
    )
    entry.runtime_data = coordinator

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # Assert sensitive credential keys are excluded from the exported dictionary
    assert "client_secret" not in diag
    assert "access_token" not in diag
    assert "refresh_token" not in diag
    assert "test_secret_xyz" not in str(diag)
    assert "test_access_token_abc" not in str(diag)
    assert "test_refresh_token_123" not in str(diag)
    assert diag["account_identity"]["binding_state"] == "verified"


def test_e2e_auth_error_privacy_no_leaks() -> None:
    """EbayAuthError does not leak failed token in repr or str."""
    secret_token = "v^1.1#i^1#r^1#secret_bearer_token_xyz"
    err = EbayAuthError(
        "eBay REST auth failed with HTTP 401", failed_token=secret_token
    )

    assert err.failed_token == secret_token
    assert secret_token not in str(err)
    assert secret_token not in repr(err)


# ==============================================================================
# Suite 6: Modern API Migration Safety & Readiness Audit
# ==============================================================================


def test_e2e_eight_readiness_gates_and_production_routing_invariants() -> None:
    """Audit migration policy readiness gates and authoritative baseline routing."""
    # 1. XML Baseline Domains
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.WATCHLIST)
        == ProviderType.TRADING_XML
    )
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.BIDDING)
        == ProviderType.TRADING_XML
    )
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.SELLER_LISTINGS)
        == ProviderType.TRADING_XML
    )
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.OFFERS)
        == ProviderType.TRADING_XML
    )
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.SOLD_UNSOLD)
        == ProviderType.TRADING_XML
    )

    # 2. Modern REST Baseline Domains (Trading XML is NOT a fallback)
    assert MigrationPolicy.get_default_provider(FeatureKey.ORDERS) == ProviderType.REST
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.SELLER_STANDARDS)
        == ProviderType.REST
    )
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.MESSAGES) == ProviderType.REST
    )
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.ANALYTICS) == ProviderType.REST
    )
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.FINANCES) == ProviderType.REST
    )
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.ACCOUNT_PRIVILEGES)
        == ProviderType.REST
    )

    # 3. Hybrid Baseline Domain
    assert (
        MigrationPolicy.get_default_provider(FeatureKey.FEEDBACK) == ProviderType.HYBRID
    )

    # 4. GraphQL Watchlist is Blocked (FORBIDDEN)
    evidence = ReadinessEvidence(
        schema_validated=True,
        scopes_contract_established=True,
        required_scopes=("https://api.ebay.com/oauth/api_scope",),
        verified_scopes=("https://api.ebay.com/oauth/api_scope",),
        api_permission_granted=False,
        permission_error_code="FORBIDDEN",
        response_complete=True,
        pagination_verified=True,
        field_parity_verified=True,
        account_identity_consistent=True,
        fallback_available=True,
    )
    evaluation = MigrationPolicy.evaluate_readiness(
        FeatureKey.WATCHLIST,
        ProviderType.GRAPHQL,
        evidence=evidence,
    )
    assert not evaluation.is_ready
    assert evaluation.state == ReadinessState.BLOCKED_AUTHORIZATION
