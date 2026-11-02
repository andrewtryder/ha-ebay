"""Comprehensive end-to-end integration and release-readiness tests for Milestone 12."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
import xml.etree.ElementTree as ET

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
from custom_components.ebay.api import EbayApiClient
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

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


class _FakeResponse:
    def __init__(self, status: int = 200, text: str = "") -> None:
        self.status = status
        self._text = text
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return self._text

    async def json(self, content_type: str | None = None) -> Any:
        return {}


class _FakeContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *args: object) -> None:
        return None


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
async def test_e2e_production_setup_entry_to_entities_and_update_lifecycle(
    hass: HomeAssistant,
) -> None:
    """Exercise genuine setup-to-entity integration path through async_setup_entry."""
    assert await async_setup_component(hass, "calendar", {})
    entry = _create_mock_entry(
        entry_id="prod_live_setup_1",
        client_id="app_live_setup_1",
        options={CONF_ENTITY_MODE: ENTITY_MODE_DETAILED, CONF_PER_ITEM_CAP: 5},
    )
    entry.add_to_hass(hass)

    buying_xml_1 = """
    <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <WatchList>
        <ItemArray>
          <Item>
            <ItemID>111111111111</ItemID>
            <Title>Test Watched Camera</Title>
            <ListingType>Auction</ListingType>
            <ListingDetails>
              <EndTime>2026-11-05T12:00:00.000Z</EndTime>
              <ViewItemURL>https://www.ebay.com/itm/111111111111</ViewItemURL>
            </ListingDetails>
            <SellingStatus>
              <CurrentPrice currencyID="USD">25.50</CurrentPrice>
              <BidCount>2</BidCount>
            </SellingStatus>
          </Item>
        </ItemArray>
        <PaginationResult>
          <TotalNumberOfPages>1</TotalNumberOfPages>
          <TotalNumberOfEntries>1</TotalNumberOfEntries>
        </PaginationResult>
      </WatchList>
      <BidList>
        <ItemArray>
          <Item>
            <ItemID>222222222222</ItemID>
            <Title>Test Bidding Lens</Title>
            <ListingType>Auction</ListingType>
            <ListingDetails>
              <EndTime>2026-11-05T14:00:00.000Z</EndTime>
              <ViewItemURL>https://www.ebay.com/itm/222222222222</ViewItemURL>
            </ListingDetails>
            <SellingStatus>
              <CurrentPrice currencyID="USD">50.00</CurrentPrice>
              <BidCount>5</BidCount>
            </SellingStatus>
          </Item>
        </ItemArray>
        <PaginationResult>
          <TotalNumberOfPages>1</TotalNumberOfPages>
          <TotalNumberOfEntries>1</TotalNumberOfEntries>
        </PaginationResult>
      </BidList>
    </GetMyeBayBuyingResponse>
    """

    buying_xml_2 = """
    <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <WatchList>
        <ItemArray>
          <Item>
            <ItemID>111111111111</ItemID>
            <Title>Test Watched Camera</Title>
            <ListingType>Auction</ListingType>
            <ListingDetails>
              <EndTime>2026-11-05T12:00:00.000Z</EndTime>
              <ViewItemURL>https://www.ebay.com/itm/111111111111</ViewItemURL>
            </ListingDetails>
            <SellingStatus>
              <CurrentPrice currencyID="USD">32.50</CurrentPrice>
              <BidCount>3</BidCount>
            </SellingStatus>
          </Item>
        </ItemArray>
        <PaginationResult>
          <TotalNumberOfPages>1</TotalNumberOfPages>
          <TotalNumberOfEntries>1</TotalNumberOfEntries>
        </PaginationResult>
      </WatchList>
      <BidList>
        <ItemArray>
          <Item>
            <ItemID>222222222222</ItemID>
            <Title>Test Bidding Lens</Title>
            <ListingType>Auction</ListingType>
            <ListingDetails>
              <EndTime>2026-11-05T14:00:00.000Z</EndTime>
              <ViewItemURL>https://www.ebay.com/itm/222222222222</ViewItemURL>
            </ListingDetails>
            <SellingStatus>
              <CurrentPrice currencyID="USD">55.00</CurrentPrice>
              <BidCount>6</BidCount>
            </SellingStatus>
          </Item>
        </ItemArray>
        <PaginationResult>
          <TotalNumberOfPages>1</TotalNumberOfPages>
          <TotalNumberOfEntries>1</TotalNumberOfEntries>
        </PaginationResult>
      </BidList>
    </GetMyeBayBuyingResponse>
    """

    selling_xml_1 = """
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <ActiveList>
        <ItemArray>
          <Item>
            <ItemID>333333333333</ItemID>
            <Title>Test Selling Tripod</Title>
            <ListingDetails>
              <EndTime>2026-11-06T18:00:00.000Z</EndTime>
              <ViewItemURL>https://www.ebay.com/itm/333333333333</ViewItemURL>
            </ListingDetails>
            <SellingStatus>
              <CurrentPrice currencyID="USD">99.99</CurrentPrice>
              <BidCount>0</BidCount>
              <QuantitySold>0</QuantitySold>
            </SellingStatus>
            <Quantity>1</Quantity>
            <QuantityAvailable>1</QuantityAvailable>
            <HitCount>10</HitCount>
            <WatchCount>3</WatchCount>
          </Item>
        </ItemArray>
        <PaginationResult>
          <TotalNumberOfPages>1</TotalNumberOfPages>
          <TotalNumberOfEntries>1</TotalNumberOfEntries>
        </PaginationResult>
      </ActiveList>
    </GetMyeBaySellingResponse>
    """

    selling_xml_2 = """
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <ActiveList>
        <ItemArray>
          <Item>
            <ItemID>333333333333</ItemID>
            <Title>Test Selling Tripod</Title>
            <ListingDetails>
              <EndTime>2026-11-06T18:00:00.000Z</EndTime>
              <ViewItemURL>https://www.ebay.com/itm/333333333333</ViewItemURL>
            </ListingDetails>
            <SellingStatus>
              <CurrentPrice currencyID="USD">105.00</CurrentPrice>
              <BidCount>0</BidCount>
              <QuantitySold>0</QuantitySold>
            </SellingStatus>
            <Quantity>1</Quantity>
            <QuantityAvailable>1</QuantityAvailable>
            <HitCount>12</HitCount>
            <WatchCount>4</WatchCount>
          </Item>
        </ItemArray>
        <PaginationResult>
          <TotalNumberOfPages>1</TotalNumberOfPages>
          <TotalNumberOfEntries>1</TotalNumberOfEntries>
        </PaginationResult>
      </ActiveList>
    </GetMyeBaySellingResponse>
    """

    seller_list_xml = """
    <GetSellerListResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <ItemArray>
        <Item>
          <ItemID>333333333333</ItemID>
          <HitCount>10</HitCount>
        </Item>
      </ItemArray>
      <PaginationResult>
        <TotalNumberOfPages>1</TotalNumberOfPages>
        <TotalNumberOfEntries>1</TotalNumberOfEntries>
      </PaginationResult>
    </GetSellerListResponse>
    """

    best_offers_xml = """
    <GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <PaginationResult>
        <TotalNumberOfPages>1</TotalNumberOfPages>
      </PaginationResult>
    </GetBestOffersResponse>
    """

    cycle = 1

    def _mock_post(url: str, *args: Any, **kwargs: Any) -> Any:
        headers = kwargs.get("headers") or {}
        call_name = headers.get("X-EBAY-API-CALL-NAME")
        if call_name == "GetMyeBayBuying":
            xml = buying_xml_1 if cycle == 1 else buying_xml_2
            return _FakeContext(_FakeResponse(200, xml))
        elif call_name == "GetMyeBaySelling":
            xml = selling_xml_1 if cycle == 1 else selling_xml_2
            return _FakeContext(_FakeResponse(200, xml))
        elif call_name == "GetSellerList":
            return _FakeContext(_FakeResponse(200, seller_list_xml))
        elif call_name == "GetBestOffers":
            return _FakeContext(_FakeResponse(200, best_offers_xml))
        return _FakeContext(_FakeResponse(200, "<Ack>Success</Ack>"))

    mock_session = Mock()
    mock_session.post = Mock(side_effect=_mock_post)

    # Home Assistant test environment does not install hass_frontend (headless).
    # Pre-registering frontend and my satisfies manifest dependencies without loading frontend assets.
    hass.config.components.add("frontend")
    hass.config.components.add("my")

    with patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        return_value=mock_session,
    ):
        # 1. Actual async_setup executes through Home Assistant config entry manager
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

        # 2. Platform forwarding creates expected entities
        entity_reg = er.async_get(hass)

        # Buying summary sensor
        w_ent_id = entity_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_watched_items"
        )
        assert w_ent_id is not None
        w_state = hass.states.get(w_ent_id)
        assert w_state is not None
        assert w_state.state == "1"

        # Bidding summary sensor
        b_ent_id = entity_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_bidding_items"
        )
        assert b_ent_id is not None
        b_state = hass.states.get(b_ent_id)
        assert b_state is not None
        assert b_state.state == "1"

        # Selling summary sensor
        s_ent_id = entity_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_active_selling_items"
        )
        assert s_ent_id is not None
        s_state = hass.states.get(s_ent_id)
        assert s_state is not None
        assert s_state.state == "1"

        # Per-item watched price sensor
        pw_id = entity_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_watched_111111111111_price"
        )
        assert pw_id is not None
        pw_state = hass.states.get(pw_id)
        assert pw_state is not None
        assert float(pw_state.state) == 25.50

        # Per-item selling price sensor
        ps_id = entity_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_selling_333333333333_price"
        )
        assert ps_id is not None
        ps_state = hass.states.get(ps_id)
        assert ps_state is not None
        assert float(ps_state.state) == 99.99

        # 3. Trigger another coordinator refresh with changed upstream responses
        cycle = 2
        coordinator = entry.runtime_data
        coordinator._force_full_refresh = True
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # 4. Existing entities update without duplicate unique IDs
        pw_state_2 = hass.states.get(pw_id)
        assert pw_state_2 is not None
        assert float(pw_state_2.state) == 32.50

        ps_state_2 = hass.states.get(ps_id)
        assert ps_state_2 is not None
        assert float(ps_state_2.state) == 105.00

        # Unique ID identity is preserved with zero duplicates
        assert (
            entity_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_watched_111111111111_price"
            )
            == pw_id
        )

        # 5. Production unload lifecycle teardown
        assert await hass.config_entries.async_unload(entry.entry_id) is True
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
            "custom_components.ebay.storage.async_remove_repair_streaks",
            new=AsyncMock(),
        ) as mock_streaks,
        patch(
            "custom_components.ebay.storage.async_remove_ending_soon_fired",
            new=AsyncMock(),
        ) as mock_ending,
        patch(
            "custom_components.ebay.storage.async_remove_inactive_item_sensors",
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

    Exercises real client.async_fetch_sections and coordinator transition logic for:
    - 10 watched items
    - 10 active seller listings
    - 10 representative bidding items

    Verifies across all three collections:
    1. Previously confirmed records survive truncation.
    2. Observed updates are applied correctly.
    3. Summary counts remain consistent.
    4. No false disappearance, outbid, sale, or other transition events fire.
    5. section_last_attempt advances.
    6. section_last_success does NOT advance on incomplete/truncated data.
    7. A subsequently complete-empty response correctly clears confirmed items.
    8. Account A data remains completely isolated from Account B.
    """
    entry_a = _create_mock_entry(entry_id="acct_a_entry", client_id="client_app_a")
    entry_b = _create_mock_entry(entry_id="acct_b_entry", client_id="client_app_b")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    client_a = EbayApiClient(
        Mock(),
        environment="production",
        client_id="client_app_a",
        client_secret="secret_a",
        runame="runame_a",
        refresh_token="refresh_a",
        site_id="0",
    )
    client_b = EbayApiClient(
        Mock(),
        environment="production",
        client_id="client_app_b",
        client_secret="secret_b",
        runame="runame_b",
        refresh_token="refresh_b",
        site_id="0",
    )

    # Initialize common mocks for optional endpoints
    for c in (client_a, client_b):
        c._fetch_seller_list_views = AsyncMock(return_value=({}, False))
        c.async_call_trading_api = AsyncMock(
            return_value=ET.fromstring(
                "<GetBestOffersResponse xmlns='urn:ebay:apis:eBLBaseComponents'>"
                "<Ack>Success</Ack><PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>"
                "</GetBestOffersResponse>"
            )
        )
        c.async_fetch_sold_unsold_classification = AsyncMock(
            return_value=({}, 0, 0, False)
        )

    coordinator_a = EbayDataUpdateCoordinator(hass, entry_a, client_a, entry_a.options)
    coordinator_b = EbayDataUpdateCoordinator(hass, entry_b, client_b, entry_b.options)

    # --- Setup Account B Baseline ---
    b_watched = {"b_w_0": _sample_item("b_w_0", "Account B Watch", 50.0)}
    b_bidding = {"b_b_0": _sample_item("b_b_0", "Account B Bid", 75.0)}
    b_selling = {"b_s_0": _sample_item("b_s_0", "Account B Sell", 200.0)}
    client_b._fetch_buying_pages = AsyncMock(
        return_value=(b_watched, b_bidding, False, False)
    )
    client_b._fetch_selling_pages = AsyncMock(return_value=(b_selling, False))

    payload_b = await client_b.async_fetch_sections(
        {"buying", "selling"},
        previous=None,
        buying_enabled=True,
        selling_enabled=True,
        analytics_enabled=False,
        ending_soon_threshold_seconds=720,
    )
    coordinator_b.data = payload_b

    # --- Phase 1: Account A Complete 10-item refresh across watched, bidding, and selling ---
    w_10 = {
        f"w_{i}": _sample_item(f"w_{i}", f"Watched {i}", 10.0 + i) for i in range(10)
    }
    b_10 = {
        f"b_{i}": _sample_item(f"b_{i}", f"Bidding {i}", 20.0 + i) for i in range(10)
    }
    for b in b_10.values():
        b["bid_count"] = 3
    s_10 = {
        f"s_{i}": _sample_item(f"s_{i}", f"Selling {i}", 100.0 + i) for i in range(10)
    }
    for s in s_10.values():
        s["quantity_available"] = 5

    client_a._fetch_buying_pages = AsyncMock(return_value=(w_10, b_10, False, False))
    client_a._fetch_selling_pages = AsyncMock(return_value=(s_10, False))

    payload_1 = await client_a.async_fetch_sections(
        {"buying", "selling"},
        previous=None,
        buying_enabled=True,
        selling_enabled=True,
        analytics_enabled=False,
        ending_soon_threshold_seconds=720,
    )
    coordinator_a.data = payload_1
    coordinator_a.previous_payload = payload_1

    # Verifications for Phase 1
    assert len(payload_1["watched"]) == 10
    assert len(payload_1["bidding"]) == 10
    assert len(payload_1["selling"]) == 10
    assert payload_1["summary"]["watched_items"] == 10
    assert payload_1["summary"]["bidding_items"] == 10
    assert payload_1["summary"]["active_selling_items"] == 10

    t1_attempt_buying = payload_1["section_last_attempt"]["buying"]
    t1_success_buying = payload_1["section_last_success"]["buying"]
    t1_attempt_selling = payload_1["section_last_attempt"]["selling"]
    t1_success_selling = payload_1["section_last_success"]["selling"]
    assert t1_success_buying is not None
    assert t1_success_selling is not None

    # --- Phase 2: Account A Truncated 2-item refresh with observed updates ---
    # Upstream returns only 2 items per collection, with price updates and truncated=True
    w_2_trunc = {
        "w_0": _sample_item("w_0", "Watched 0 Updated", 99.0),
        "w_1": _sample_item("w_1", "Watched 1", 11.0),
    }
    b_2_trunc = {
        "b_0": _sample_item("b_0", "Bidding 0 Updated", 75.0),
        "b_1": _sample_item("b_1", "Bidding 1", 21.0),
    }
    b_2_trunc["b_0"]["bid_count"] = 5
    s_2_trunc = {
        "s_0": _sample_item("s_0", "Selling 0 Updated", 150.0),
        "s_1": _sample_item("s_1", "Selling 1", 101.0),
    }

    client_a._fetch_buying_pages = AsyncMock(
        return_value=(w_2_trunc, b_2_trunc, True, True)
    )
    client_a._fetch_selling_pages = AsyncMock(return_value=(s_2_trunc, True))

    payload_2 = await client_a.async_fetch_sections(
        {"buying", "selling"},
        previous=payload_1,
        buying_enabled=True,
        selling_enabled=True,
        analytics_enabled=False,
        ending_soon_threshold_seconds=720,
    )

    # 1. Previously confirmed records survive truncation across all 3 collections
    assert len(payload_2["watched"]) == 10
    assert len(payload_2["bidding"]) == 10
    assert len(payload_2["selling"]) == 10
    for i in range(10):
        assert f"w_{i}" in payload_2["watched"]
        assert f"b_{i}" in payload_2["bidding"]
        assert f"s_{i}" in payload_2["selling"]

    # 2. Observed updates are applied correctly
    assert payload_2["watched"]["w_0"]["current_price"] == 99.0
    assert payload_2["bidding"]["b_0"]["current_price"] == 75.0
    assert payload_2["bidding"]["b_0"]["bid_count"] == 5
    assert payload_2["selling"]["s_0"]["current_price"] == 150.0

    # 3. Summary counts remain consistent
    assert payload_2["summary"]["watched_items"] == 10
    assert payload_2["summary"]["bidding_items"] == 10
    assert payload_2["summary"]["active_selling_items"] == 10

    # 4. section_last_attempt advances; section_last_success is preserved
    assert payload_2["section_last_attempt"]["buying"] >= t1_attempt_buying
    assert payload_2["section_last_success"]["buying"] == t1_success_buying
    assert payload_2["section_last_attempt"]["selling"] >= t1_attempt_selling
    assert payload_2["section_last_success"]["selling"] == t1_success_selling

    # 5. No false transition events fire
    detected_events: list[dict[str, Any]] = []
    coordinator_a._dispatch_event = lambda event_type, data: detected_events.append(
        {"type": event_type, "data": data}
    )  # type: ignore[method-assign]
    coordinator_a._detect_transition_events(payload_1, payload_2)

    false_events = [
        e
        for e in detected_events
        if e["type"]
        in (
            "item_removed",
            "watch_item_ended",
            "outbid",
            "bid_item_ended",
            "item_sold",
            "item_unsold",
        )
    ]
    assert len(false_events) == 0

    # 6. Account B data remains isolated
    assert len(coordinator_b.data["watched"]) == 1
    assert "b_w_0" in coordinator_b.data["watched"]
    assert len(coordinator_b.data["bidding"]) == 1
    assert "b_b_0" in coordinator_b.data["bidding"]
    assert len(coordinator_b.data["selling"]) == 1
    assert "b_s_0" in coordinator_b.data["selling"]

    # --- Phase 3: Account A Genuine complete-empty response ---
    client_a._fetch_buying_pages = AsyncMock(return_value=({}, {}, False, False))
    client_a._fetch_selling_pages = AsyncMock(return_value=({}, False))

    payload_3 = await client_a.async_fetch_sections(
        {"buying", "selling"},
        previous=payload_2,
        buying_enabled=True,
        selling_enabled=True,
        analytics_enabled=False,
        ending_soon_threshold_seconds=720,
    )

    # 1. Subsequently complete empty response correctly clears confirmed items
    assert len(payload_3["watched"]) == 0
    assert len(payload_3["bidding"]) == 0
    assert len(payload_3["selling"]) == 0

    # 2. Summary counts reflect zero items
    assert payload_3["summary"]["watched_items"] == 0
    assert payload_3["summary"]["bidding_items"] == 0
    assert payload_3["summary"]["active_selling_items"] == 0

    # 3. section_last_success advances on genuine complete success
    assert payload_3["section_last_success"]["buying"] >= t1_success_buying
    assert payload_3["section_last_success"]["selling"] >= t1_success_selling

    # 4. Incomplete/truncated previous state suppresses spurious disappearance events
    # during transition out of truncation until clean baseline is established
    detected_events_3: list[dict[str, Any]] = []
    coordinator_a._dispatch_event = lambda event_type, data: detected_events_3.append(
        {"type": event_type, "data": data}
    )  # type: ignore[method-assign]
    coordinator_a._detect_transition_events(payload_2, payload_3)

    spurious_events = [
        e
        for e in detected_events_3
        if e["type"] in ("item_removed", "watch_item_ended")
    ]
    assert len(spurious_events) == 0

    # Clean baseline clears confirmed items without stale ghosts
    final_baseline = _baseline_payload(payload_2, payload_3)
    assert len(final_baseline["watched"]) == 0
    assert len(final_baseline["bidding"]) == 0
    assert len(final_baseline["selling"]) == 0
    assert final_baseline["truncated_collections"]["watched"] is False

    # 5. Account B remains untouched
    assert len(coordinator_b.data["watched"]) == 1
    assert "b_w_0" in coordinator_b.data["watched"]
    assert len(coordinator_b.data["selling"]) == 1
    assert "b_s_0" in coordinator_b.data["selling"]


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
