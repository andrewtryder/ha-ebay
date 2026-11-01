"""Comprehensive reliability, rate limit, and lifecycle regression tests for Milestone 11."""

from __future__ import annotations

import asyncio
import email.utils
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import xml.etree.ElementTree as ET

import aiohttp
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session
import pytest

from custom_components.ebay.api import (
    EbayApiClient,
    EbayApiError,
    EbayAuthError,
    EbayAuthTransientError,
    EbayPartialFailure,
)
from custom_components.ebay.clients.resilience import (
    BoundedRetryPolicy,
    EndpointFamily,
    RateLimitTracker,
    parse_retry_after,
)
from custom_components.ebay.coordinator import EbayDataUpdateCoordinator
from custom_components.ebay.diagnostics import async_get_config_entry_diagnostics
from custom_components.ebay.events import EbayEventMixin
from custom_components.ebay.oauth2 import EbayOAuth2Implementation
from custom_components.ebay.oauth_errors import (
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)
from custom_components.ebay.parsers.xml import _rate_limit_error


# --- 1. parse_retry_after tests ---


def test_parse_retry_after_delta_seconds() -> None:
    """parse_retry_after parses integer and float delta-seconds."""
    assert parse_retry_after("15") == 15.0
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("-5") == 0.0
    assert parse_retry_after("120", max_delay=30.0) == 30.0
    assert parse_retry_after(None, default_delay=5.0) == 5.0
    assert parse_retry_after("", default_delay=2.0) == 2.0


def test_parse_retry_after_http_date() -> None:
    """parse_retry_after parses RFC 7231 / 9110 HTTP-date strings."""
    fixed_now = 1700000000.0  # reference epoch
    target_future = fixed_now + 20.0
    http_date = email.utils.formatdate(target_future, usegmt=True)

    parsed = parse_retry_after(http_date, now_ts=fixed_now)
    assert abs(parsed - 20.0) < 0.1

    # Date in the past clamps to 0.0
    target_past = fixed_now - 10.0
    past_date = email.utils.formatdate(target_past, usegmt=True)
    assert parse_retry_after(past_date, now_ts=fixed_now) == 0.0

    # Date exceeding max_delay is capped
    target_far = fixed_now + 100.0
    far_date = email.utils.formatdate(target_far, usegmt=True)
    assert parse_retry_after(far_date, max_delay=30.0, now_ts=fixed_now) == 30.0

    # Malformed date string falls back to default_delay
    assert parse_retry_after("invalid-date-string", default_delay=3.0) == 3.0


# --- 2. RateLimitTracker tests ---


def test_rate_limit_tracker_cooldown_and_isolation() -> None:
    """RateLimitTracker tracks cooldowns per endpoint family and client_id."""
    tracker = RateLimitTracker()

    assert not tracker.is_throttled(EndpointFamily.REST, "client-a")
    assert tracker.cooldown_remaining(EndpointFamily.REST, "client-a") == 0.0

    # Record cooldown for client-a
    delay = tracker.record_throttled(EndpointFamily.REST, 10.0, "client-a")
    assert delay == 10.0
    assert tracker.is_throttled(EndpointFamily.REST, "client-a")
    assert tracker.cooldown_remaining(EndpointFamily.REST, "client-a") > 0.0

    # Client-b on REST is isolated and NOT throttled
    assert not tracker.is_throttled(EndpointFamily.REST, "client-b")

    # Trading on client-a is NOT throttled (separate endpoint family)
    assert not tracker.is_throttled(EndpointFamily.TRADING, "client-a")

    # Clear resets state
    tracker.clear()
    assert not tracker.is_throttled(EndpointFamily.REST, "client-a")


# --- 3. BoundedRetryPolicy tests ---


def test_bounded_retry_policy_progression() -> None:
    """BoundedRetryPolicy computes deterministic backoff and respects caps."""
    policy = BoundedRetryPolicy(base_delay=0.5, max_delay=5.0)

    # Exponential: 0.5 * 2^attempt
    assert policy.compute_backoff(0) == 0.5
    assert policy.compute_backoff(1) == 1.0
    assert policy.compute_backoff(2) == 2.0
    assert policy.compute_backoff(3) == 4.0
    assert policy.compute_backoff(4) == 5.0  # capped at max_delay

    # retry_after override capped at max_delay
    assert policy.compute_backoff(0, retry_after=1.5) == 1.5
    assert policy.compute_backoff(0, retry_after=10.0) == 5.0


def test_bounded_retry_policy_transient_classification() -> None:
    """BoundedRetryPolicy accurately classifies transient status codes and exceptions."""
    policy = BoundedRetryPolicy()

    assert policy.is_transient_status(429)
    assert policy.is_transient_status(500)
    assert policy.is_transient_status(502)
    assert policy.is_transient_status(503)
    assert policy.is_transient_status(504)
    assert not policy.is_transient_status(400)
    assert not policy.is_transient_status(401)
    assert not policy.is_transient_status(403)
    assert not policy.is_transient_status(404)

    assert policy.is_transient_exception(TimeoutError())
    assert policy.is_transient_exception(asyncio.TimeoutError())
    assert policy.is_transient_exception(aiohttp.ClientConnectorError(Mock(), Mock()))
    assert policy.is_transient_exception(aiohttp.ServerDisconnectedError())
    assert policy.is_transient_exception(aiohttp.ClientPayloadError())
    assert policy.is_transient_exception(aiohttp.ClientOSError())
    assert not policy.is_transient_exception(ValueError())


@pytest.mark.asyncio
async def test_bounded_retry_policy_cancellation_aware() -> None:
    """BoundedRetryPolicy async_wait responds to task cancellation."""
    policy = BoundedRetryPolicy()
    task = asyncio.create_task(policy.async_wait(10.0))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- 4. Fast-fail on active cooldown in RestClientMixin & TradingClientMixin ---


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        json_data: Any = None,
        json_payload: Any = None,
        text: str | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._json_data = json_data if json_data is not None else (json_payload or {})
        self._text = text

    async def json(self, content_type: str | None = None) -> Any:
        return self._json_data

    async def text(self) -> str:
        if self._text is not None:
            return self._text
        if self._json_data:
            import json

            return json.dumps(self._json_data)
        return ""


class _FakeContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeBoomContext:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> Any:
        raise self._exc

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_rest_fast_fail_during_cooldown() -> None:
    """REST calls fast-fail without hitting network when cooldown > 2.0s."""
    session = Mock()
    session.get = Mock()

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    # Seed cooldown
    client.rate_limit_tracker.record_throttled(EndpointFamily.REST, 15.0, "test_client")

    with pytest.raises(EbayPartialFailure) as exc_info:
        await client._async_rest_get(
            "https://api.ebay.com/test",
            partial_category="orders",
        )

    assert exc_info.value.category == "orders"
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "transient"
    # Network request was NOT made
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_trading_fast_fail_during_cooldown() -> None:
    """Trading API fast-fails without hitting network when cooldown > 2.0s."""
    session = Mock()
    session.post = Mock()

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    # Seed cooldown
    client.rate_limit_tracker.record_throttled(
        EndpointFamily.TRADING, 10.0, "test_client"
    )

    with pytest.raises(EbayApiError) as exc_info:
        await client._async_call_trading_api_once("GetMyeBaySelling", "<xml/>")

    assert "in cooldown" in str(exc_info.value)
    assert exc_info.value.status == 429
    # Network request was NOT made
    session.post.assert_not_called()


def test_trading_xml_rate_limit_error_classification() -> None:
    """_rate_limit_error recognizes ErrorCode 518, 17, and quota phrases."""
    xml_518 = """
    <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
        <Ack>Failure</Ack>
        <Errors>
            <ErrorCode>518</ErrorCode>
            <ShortMessage>Call usage limit reached</ShortMessage>
            <SeverityCode>Error</SeverityCode>
        </Errors>
    </GetMyeBayBuyingResponse>
    """
    root_518 = ET.fromstring(xml_518)
    assert _rate_limit_error(root_518) is True

    xml_17 = """
    <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
        <Ack>Failure</Ack>
        <Errors>
            <ErrorCode>17</ErrorCode>
            <ShortMessage>Daily limit exceeded</ShortMessage>
        </Errors>
    </GetMyeBayBuyingResponse>
    """
    root_17 = ET.fromstring(xml_17)
    assert _rate_limit_error(root_17) is True

    xml_auth = """
    <GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
        <Ack>Failure</Ack>
        <Errors>
            <ErrorCode>931</ErrorCode>
            <ShortMessage>Auth token is invalid</ShortMessage>
        </Errors>
    </GetMyeBayBuyingResponse>
    """
    root_auth = ET.fromstring(xml_auth)
    assert _rate_limit_error(root_auth) is False


# --- 5. Preservation of Last-Good Data in section_fetch ---


@pytest.mark.asyncio
async def test_section_fetch_preserves_sold_unsold_on_failure() -> None:
    """section_fetch retains prior selling_classification and counts when classification fails."""
    client = EbayApiClient(
        Mock(),
        environment="production",
        client_id="client",
        client_secret="secret",
        runame="runame",
        refresh_token="refresh",
        site_id="0",
    )

    prior_data = {
        "selling": {"item-1": {"title": "Item 1"}},
        "selling_classification": {"item-old": "sold"},
        "summary": {"sold_items_count": 5, "unsold_items_count": 3},
        "truncated_collections": {},
    }

    # Mock _fetch_selling_pages to return current active listing
    client._fetch_selling_pages = AsyncMock(
        return_value=({"item-1": {"title": "Item 1"}}, False)
    )
    # Mock classification to fail
    client.async_fetch_sold_unsold_classification = AsyncMock(
        side_effect=EbayApiError("API limit exceeded")
    )
    client._fetch_seller_list_views = AsyncMock(return_value=({}, False))
    client.async_call_trading_api = AsyncMock(side_effect=EbayApiError("No offers"))

    result = await client.async_fetch_data(
        buying_enabled=False,
        selling_enabled=True,
        analytics_enabled=False,
        ending_soon_threshold_seconds=3600,
        previous=prior_data,
    )

    # Classification and counts are preserved from prior_data rather than wiped
    assert result["selling_classification"] == {"item-old": "sold"}
    assert result["summary"]["sold_items_count"] == 5
    assert result["summary"]["unsold_items_count"] == 3
    assert "sold_unsold_classification" in result["partial_failures"]
    assert result["truncated_collections"]["sold_unsold"] is True


@pytest.mark.asyncio
async def test_section_fetch_preserves_item_attributes_on_partial_failure() -> None:
    """section_fetch preserves prior views, offers, and analytics on transient failures."""
    client = EbayApiClient(
        Mock(),
        environment="production",
        client_id="client",
        client_secret="secret",
        runame="runame",
        refresh_token="refresh",
        site_id="0",
    )

    prior_data = {
        "selling": {
            "item-1": {
                "title": "Item 1",
                "views": 42,
                "offers": 3,
                "analytics_views_30d": 150,
            }
        },
        "selling_classification": {},
        "summary": {},
        "truncated_collections": {},
    }

    client._fetch_selling_pages = AsyncMock(
        return_value=({"item-1": {"title": "Item 1"}}, False)
    )
    # views, offers, and analytics fail
    client._fetch_seller_list_views = AsyncMock(
        side_effect=EbayApiError("Views failed")
    )
    client.async_call_trading_api = AsyncMock(side_effect=EbayApiError("Offers failed"))
    client.async_fetch_analytics_views = AsyncMock(
        side_effect=EbayApiError("Analytics failed")
    )

    result = await client.async_fetch_data(
        buying_enabled=False,
        selling_enabled=True,
        analytics_enabled=True,
        ending_soon_threshold_seconds=3600,
        previous=prior_data,
        granted_scopes="https://api.ebay.com/oauth/api_scope/sell.analytics.readonly",
    )

    item = result["selling"]["item-1"]
    assert item["views"] == 42
    assert item["offers"] == 3
    assert item["analytics_views_30d"] == 150
    assert "seller_list_views" in result["partial_failures"]
    assert "active_offers" in result["partial_failures"]
    assert "analytics_views" in result["partial_failures"]


# --- 6. Event Suppression during Partial Failures ---


def test_events_suppressed_on_partial_failure() -> None:
    """Transition events are suppressed when partial failures indicate incomplete data."""
    mixin = EbayEventMixin()
    mixin._event_callbacks = {"selling": Mock(), "watching": Mock(), "bidding": Mock()}
    mixin._recent_events = {"selling": Mock(), "watching": Mock(), "bidding": Mock()}
    mixin._recent_history_ending_soon = set()
    mixin._price_drop_below_active = set()

    previous = {
        "selling": {"item-1": {"title": "Item 1", "seconds_left": 100}},
        "watched": {"item-2": {"title": "Item 2", "seconds_left": 100}},
        "bidding": {"item-3": {"title": "Item 3", "seconds_left": 100}},
        "sold_item_ids": [],
    }
    # Current snapshot is empty because of a partial failure in selling and buying
    current = {
        "selling": {},
        "watched": {},
        "bidding": {},
        "sold_item_ids": [],
        "partial_failures": ["selling", "buying_truncated"],
        "truncated_collections": {"selling": True},
    }

    mixin._detect_transition_events(previous, current)

    # No spurious item disappearance or sale events should be emitted
    mixin._event_callbacks["selling"].assert_not_called()
    mixin._event_callbacks["watching"].assert_not_called()
    mixin._event_callbacks["bidding"].assert_not_called()


# --- 7. Coordinator async_shutdown lifecycle ---


@pytest.mark.asyncio
async def test_coordinator_async_shutdown() -> None:
    """coordinator.async_shutdown cancels timers, clears callbacks, and cancels save tasks."""
    coordinator = object.__new__(EbayDataUpdateCoordinator)
    coordinator.hass = Mock()
    coordinator.data = None
    coordinator._unsub_refresh = None
    coordinator._unsub_shutdown = None
    coordinator._listeners = {}
    coordinator._timer_listeners = []
    coordinator._event_callbacks = {}
    coordinator._scheduled = {}

    # Register mock listeners
    timer_listener = Mock()
    coordinator.async_add_timer_listener(timer_listener)
    event_cb = Mock()
    coordinator.register_event_callback("selling", event_cb)

    # Schedule a dummy ending-soon timer
    cancel_mock = Mock()
    key = ("entry", "selling", "1", 3600, "2026-10-31T12:00:00Z")
    coordinator._scheduled[key] = cancel_mock

    # Create dummy running save task
    save_task = asyncio.create_task(asyncio.sleep(100))
    coordinator._ending_soon_fired_save_task = save_task

    await coordinator.async_shutdown()

    # Verify everything cleaned up
    cancel_mock.assert_called_once()
    assert len(coordinator._scheduled) == 0
    assert len(coordinator._timer_listeners) == 0
    assert len(coordinator._event_callbacks) == 0
    assert save_task.cancelled() or save_task.done()


# --- 8. OAuth2 Implementation ClientTimeout ---


@pytest.mark.asyncio
async def test_oauth2_token_request_has_timeout() -> None:
    """_token_request specifies a 30s ClientTimeout to prevent indefinite hangs."""
    hass = Mock()
    session = Mock()
    session.post = AsyncMock()

    resp_mock = Mock()
    resp_mock.status = 200
    resp_mock.json = AsyncMock(return_value={"access_token": "abc", "expires_in": 7200})
    session.post.return_value = resp_mock

    with patch(
        "custom_components.ebay.oauth2.async_get_clientsession", return_value=session
    ):
        oauth = EbayOAuth2Implementation(
            hass,
            {
                "environment": "production",
                "client_id": "cid",
                "client_secret": "csec",
                "runame": "cruname",
            },
        )
        res = await oauth._token_request(
            {"grant_type": "authorization_code", "code": "code"}
        )
        assert res["access_token"] == "abc"

        # Check call arguments
        _, kwargs = session.post.call_args
        assert "timeout" in kwargs
        assert isinstance(kwargs["timeout"], aiohttp.ClientTimeout)
        assert kwargs["timeout"].total == 30


# --- 9. Diagnostics Rate Limiting Block & Privacy ---


@pytest.mark.asyncio
async def test_diagnostics_rate_limiting_block_and_privacy() -> None:
    """async_get_config_entry_diagnostics returns safe rate_limiting telemetry."""
    hass = Mock()
    entry = Mock()
    entry.data = {
        "environment": "production",
        "site_id": "0",
        "client_id": "secret_id",
        "client_secret": "secret_val",
        "refresh_token": "secret_tok",
    }

    coordinator = Mock()
    coordinator.data = {"summary": {}}
    coordinator.options = {}
    coordinator.last_attempt_at = None
    coordinator.last_refresh_duration_seconds = 1.2
    coordinator.last_refresh_result = "success"
    coordinator.last_error_category = None
    coordinator.scheduled_ending_soon_count = 0

    client = Mock()
    client.get_rate_limiting_diagnostics = Mock(
        return_value={
            "throttled": True,
            "rest_cooldown_remaining_seconds": 12.5,
            "trading_cooldown_remaining_seconds": 0.0,
            "last_retry_count": 1,
        }
    )
    coordinator.api = client
    entry.runtime_data = coordinator

    with patch(
        "custom_components.ebay.diagnostics.get_entry_account_identity"
    ) as mock_identity:
        mock_identity.return_value.to_diagnostic_dict.return_value = {
            "provenance": "test"
        }
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert "rate_limiting" in diagnostics
    assert diagnostics["rate_limiting"] == {
        "throttled": True,
        "rest_cooldown_remaining_seconds": 12.5,
        "trading_cooldown_remaining_seconds": 0.0,
        "last_retry_count": 1,
    }

    # Verify no raw secrets in diagnostics keys/values
    diag_str = str(diagnostics)
    assert "secret_val" not in diag_str
    assert "secret_tok" not in diag_str


# --- 10. Additional Edge Cases & Granular Coverage ---


@pytest.mark.asyncio
async def test_rest_short_cooldown_waits_and_proceeds() -> None:
    """REST calls with cooldown <= 2.0s wait for cooldown before proceeding."""
    session = Mock()
    resp = _FakeResponse(status=200, json_data={"orders": []})
    session.get = Mock(return_value=_FakeContext(resp))

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    # Record 0.05s short cooldown
    client.rate_limit_tracker.record_throttled(EndpointFamily.REST, 0.05, "test_client")

    with patch(
        "custom_components.ebay.clients.http.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        res = await client._async_rest_get_once(
            "https://api.ebay.com/test",
            partial_category="orders",
        )
        mock_sleep.assert_awaited_once()
        assert res == {"orders": []}


@pytest.mark.asyncio
async def test_trading_short_cooldown_waits_and_proceeds() -> None:
    """Trading API calls with cooldown <= 2.0s wait for cooldown before proceeding."""
    session = Mock()
    trading_xml = """
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
        <Ack>Success</Ack>
    </GetMyeBaySellingResponse>
    """
    resp = _FakeResponse(status=200)
    resp.text = AsyncMock(return_value=trading_xml)
    session.post = Mock(return_value=_FakeContext(resp))

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    # Record 0.05s short cooldown
    client.rate_limit_tracker.record_throttled(
        EndpointFamily.TRADING, 0.05, "test_client"
    )

    with patch(
        "custom_components.ebay.clients.trading.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        root = await client.async_call_trading_api("GetMyeBaySelling", "<xml/>")
        mock_sleep.assert_awaited_once()
        assert root is not None


def test_rate_limit_tracker_no_scope_key() -> None:
    """RateLimitTracker handles global (no scope_key) cooldowns."""
    tracker = RateLimitTracker()
    tracker.record_throttled(EndpointFamily.REST, 5.0)
    assert tracker.is_throttled(EndpointFamily.REST)
    assert tracker.cooldown_remaining(EndpointFamily.REST) > 0.0
    tracker.clear()
    assert not tracker.is_throttled(EndpointFamily.REST)


@pytest.mark.asyncio
async def test_bounded_retry_zero_delay() -> None:
    """BoundedRetryPolicy async_wait returns immediately when delay <= 0."""
    sleep_mock = AsyncMock()
    policy = BoundedRetryPolicy(sleep_func=sleep_mock)
    await policy.async_wait(0.0)
    await policy.async_wait(-1.0)
    sleep_mock.assert_not_called()


def test_rate_limiting_diagnostics_defaults() -> None:
    """RestClientMixin returns non-throttled default diagnostics when idle."""
    client = EbayApiClient(
        Mock(),
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    diag = client.get_rate_limiting_diagnostics()
    assert diag == {
        "throttled": False,
        "rest_cooldown_remaining_seconds": 0.0,
        "trading_cooldown_remaining_seconds": 0.0,
        "last_retry_count": 0,
    }


# --- 9. Bounded Retry Production Boundary Regression Tests ---


@pytest.mark.asyncio
async def test_rest_bounded_retry_500_then_success() -> None:
    """REST GET retries on transient 500 and succeeds, incrementing retry count."""
    session = Mock()
    resp_500 = _FakeResponse(status=500, json_payload={"error": "server error"})
    resp_200 = _FakeResponse(status=200, json_payload={"orders": [{"orderId": "123"}]})
    session.get = Mock(side_effect=[_FakeContext(resp_500), _FakeContext(resp_200)])

    policy = BoundedRetryPolicy(base_delay=0.01, sleep_func=AsyncMock())
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        retry_policy=policy,
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    data = await client._async_rest_get(
        "https://api.ebay.com/sell/fulfillment/v1/order",
        partial_category="orders",
    )
    assert data == {"orders": [{"orderId": "123"}]}
    assert client._last_retry_count == 1
    assert client.get_rate_limiting_diagnostics()["last_retry_count"] == 1


@pytest.mark.asyncio
async def test_rest_bounded_retry_timeout_then_success() -> None:
    """REST GET retries on TimeoutError and recovers."""
    session = Mock()
    resp_200 = _FakeResponse(status=200, json_payload={"data": "ok"})
    boom = _FakeBoomContext(TimeoutError("socket timeout"))
    session.get = Mock(side_effect=[boom, _FakeContext(resp_200)])

    policy = BoundedRetryPolicy(base_delay=0.01, sleep_func=AsyncMock())
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        retry_policy=policy,
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    data = await client._async_rest_get(
        "https://api.ebay.com/test",
        partial_category="test",
    )
    assert data == {"data": "ok"}
    assert client._last_retry_count == 1


@pytest.mark.asyncio
async def test_rest_bounded_retry_repeated_503_exhausts_and_fails() -> None:
    """REST GET exhausts bounded retries (2 retries, 3 attempts) and raises partial failure."""
    session = Mock()
    resp_503 = _FakeResponse(status=503, json_payload={"error": "service unavailable"})
    session.get = Mock(
        side_effect=[
            _FakeContext(resp_503),
            _FakeContext(resp_503),
            _FakeContext(resp_503),
        ]
    )

    policy = BoundedRetryPolicy(base_delay=0.01, sleep_func=AsyncMock())
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        retry_policy=policy,
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    with pytest.raises(EbayPartialFailure) as exc_info:
        await client._async_rest_get(
            "https://api.ebay.com/test",
            partial_category="orders",
        )
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.http_status == 503
    assert client._last_retry_count == 2
    assert session.get.call_count == 3


@pytest.mark.asyncio
async def test_rest_401_raises_immediately_without_retrying_in_once() -> None:
    """HTTP 401 raises EbayAuthError immediately in _async_rest_get_once without multiplying retries."""
    session = Mock()
    resp_401 = _FakeResponse(status=401, text="Unauthorized")
    session.get = Mock(return_value=_FakeContext(resp_401))

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(return_value="expired_token")

    with pytest.raises(EbayAuthError):
        await client._async_rest_get_once(
            "https://api.ebay.com/test",
            partial_category="test",
        )
    assert session.get.call_count == 1
    assert getattr(client, "_last_retry_count", 0) == 0


@pytest.mark.asyncio
async def test_rest_403_permanent_failure_no_retry() -> None:
    """HTTP 403 fails immediately without bounded retry attempts."""
    session = Mock()
    resp_403 = _FakeResponse(status=403, json_payload={"error": "forbidden"})
    session.get = Mock(return_value=_FakeContext(resp_403))

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    with pytest.raises(EbayPartialFailure):
        await client._async_rest_get_once(
            "https://api.ebay.com/test",
            partial_category="orders",
        )
    assert session.get.call_count == 1
    assert getattr(client, "_last_retry_count", 0) == 0


@pytest.mark.asyncio
async def test_trading_bounded_retry_502_then_success() -> None:
    """Trading API retries on 502 and parses valid XML on second attempt."""
    session = Mock()
    resp_502 = _FakeResponse(status=502, text="Bad Gateway")
    resp_200 = _FakeResponse(
        status=200,
        text="""<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
            <Ack>Success</Ack>
        </GetMyeBaySellingResponse>""",
    )
    session.post = Mock(side_effect=[_FakeContext(resp_502), _FakeContext(resp_200)])

    policy = BoundedRetryPolicy(base_delay=0.01, sleep_func=AsyncMock())
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        retry_policy=policy,
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    root = await client.async_call_trading_api("GetMyeBaySelling", "<xml/>")
    assert root is not None
    assert client._last_retry_count == 1
    assert session.post.call_count == 2


@pytest.mark.asyncio
async def test_trading_429_missing_retry_after_records_default_cooldown() -> None:
    """Trading 429 without Retry-After header records a safe 60s default cooldown."""
    session = Mock()
    resp_429 = _FakeResponse(status=429, text="Rate Limited")  # no headers
    session.post = Mock(return_value=_FakeContext(resp_429))

    tracker = RateLimitTracker()
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        rate_limit_tracker=tracker,
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    with pytest.raises(EbayApiError, match="HTTP 429"):
        await client._async_call_trading_api_once("GetMyeBaySelling", "<xml/>")

    assert tracker.is_throttled(EndpointFamily.TRADING, "test_client")
    remaining = tracker.cooldown_remaining(EndpointFamily.TRADING, "test_client")
    assert remaining >= 55.0  # safe default is 60.0s


def test_parse_retry_after_storage_bound_vs_polling_wait() -> None:
    """Server cooldown up to 24h is stored truthfully in tracker."""
    assert parse_retry_after("3600") == 3600.0
    tracker = RateLimitTracker()
    tracker.record_throttled(EndpointFamily.REST, 3600.0)
    assert tracker.cooldown_remaining(EndpointFamily.REST) > 3500.0


def test_shared_keyset_tracker_registry_isolation_and_lifecycle() -> None:
    """HA-instance registry shares tracker for same keyset, isolates others, and tracks ref count."""
    from custom_components.ebay import (
        async_get_rate_limit_tracker,
        async_release_rate_limit_tracker,
    )
    from custom_components.ebay.const import DOMAIN

    hass = Mock()
    hass.data = {}

    # Account 1 and Account 2 share developer keyset on production
    t1 = async_get_rate_limit_tracker(hass, "production", "shared_app_id")
    t2 = async_get_rate_limit_tracker(hass, "production", "shared_app_id")
    assert t1 is t2

    # Account 3 has a different developer keyset on production
    t3 = async_get_rate_limit_tracker(hass, "production", "other_app_id")
    assert t3 is not t1

    # Account 4 uses shared keyset but in sandbox environment
    t4 = async_get_rate_limit_tracker(hass, "sandbox", "shared_app_id")
    assert t4 is not t1

    # Throttling t1 affects t2
    t1.record_throttled(EndpointFamily.REST, 60.0)
    assert t2.is_throttled(EndpointFamily.REST)
    assert not t3.is_throttled(EndpointFamily.REST)
    assert not t4.is_throttled(EndpointFamily.REST)

    # Release ref counts
    async_release_rate_limit_tracker(hass, "production", "shared_app_id")
    assert len(hass.data[DOMAIN]["rate_limit_trackers"]) == 3
    async_release_rate_limit_tracker(hass, "production", "shared_app_id")
    # Cleaned up from registry when ref_count reaches 0
    assert len(hass.data[DOMAIN]["rate_limit_trackers"]) == 2


@pytest.mark.asyncio
async def test_truncated_pagination_preserves_confirmed_items_and_separates_success_stamp() -> (
    None
):
    """Truncated pagination retains previously confirmed items and only updates attempt timestamp."""
    from custom_components.ebay.const import SECTION_BUYING

    session = Mock()
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )

    # Cycle 1: Complete fetch with 3 watched items
    full_watched = {
        "item_1": {"title": "Item 1", "current_price": 10.0},
        "item_2": {"title": "Item 2", "current_price": 20.0},
        "item_3": {"title": "Item 3", "current_price": 30.0},
    }
    client._fetch_buying_pages = AsyncMock(
        return_value=(full_watched, {}, False, False)
    )

    p1 = await client.async_fetch_sections(
        {SECTION_BUYING},
        buying_enabled=True,
        selling_enabled=False,
        analytics_enabled=False,
        ending_soon_threshold_seconds=1800,
    )
    assert len(p1["watched"]) == 3
    assert p1["summary"]["watched_items"] == 3
    assert SECTION_BUYING in p1["section_last_success"]
    success_stamp_1 = p1["section_last_success"][SECTION_BUYING]

    # Cycle 2: Truncated fetch with only 1 item returned
    partial_watched = {"item_1": {"title": "Item 1 Updated", "current_price": 12.0}}
    client._fetch_buying_pages = AsyncMock(
        return_value=(partial_watched, {}, True, False)  # watched_truncated = True
    )

    p2 = await client.async_fetch_sections(
        {SECTION_BUYING},
        previous=p1,
        buying_enabled=True,
        selling_enabled=False,
        analytics_enabled=False,
        ending_soon_threshold_seconds=1800,
    )
    # Critical: All 3 items preserved, item_1 updated!
    assert len(p2["watched"]) == 3
    assert p2["watched"]["item_1"]["current_price"] == 12.0
    assert "item_2" in p2["watched"]
    assert "item_3" in p2["watched"]
    assert p2["summary"]["watched_items"] == 3
    assert "buying_truncated" in p2["partial_failures"]
    # Success stamp must NOT advance
    assert p2["section_last_success"][SECTION_BUYING] == success_stamp_1
    # Attempt stamp MUST advance
    assert p2["section_last_attempt"][SECTION_BUYING] >= success_stamp_1

    # Cycle 3: Genuine untruncated fetch with 0 items
    client._fetch_buying_pages = AsyncMock(
        return_value=({}, {}, False, False)  # watched_truncated = False
    )

    p3 = await client.async_fetch_sections(
        {SECTION_BUYING},
        previous=p2,
        buying_enabled=True,
        selling_enabled=False,
        analytics_enabled=False,
        ending_soon_threshold_seconds=1800,
    )
    # Legitimate 0 count!
    assert len(p3["watched"]) == 0
    assert p3["summary"]["watched_items"] == 0
    assert "buying_truncated" not in p3["partial_failures"]
    assert p3["section_last_success"][SECTION_BUYING] > success_stamp_1


# ==============================================================================
# Additional Tests: 401/403/400/404 Semantics, Retry Budget, and Tracker Lifecycle
# ==============================================================================


@pytest.mark.asyncio
async def test_trading_401_refreshes_token_and_succeeds() -> None:
    """Trading 401 refreshes token once and succeeds on second request."""
    session = Mock()
    resp_401 = _FakeResponse(status=401, text="Unauthorized")
    resp_200 = _FakeResponse(
        status=200,
        text="""<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
            <Ack>Success</Ack>
        </GetMyeBaySellingResponse>""",
    )
    session.post = Mock(side_effect=[_FakeContext(resp_401), _FakeContext(resp_200)])

    tokens = ["expired_token", "fresh_token"]

    async def _mock_get_token() -> str:
        return tokens.pop(0) if len(tokens) > 1 else tokens[0]

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(side_effect=_mock_get_token)

    root = await client.async_call_trading_api("GetMyeBaySelling", "<xml/>")
    assert root is not None
    assert session.post.call_count == 2
    assert client._last_retry_count == 1


@pytest.mark.asyncio
async def test_trading_401_refresh_fails_bounded_error() -> None:
    """Trading 401 refresh failure raises bounded EbayAuthError without infinite loop."""
    session = Mock()
    resp_401 = _FakeResponse(status=401, text="Unauthorized")
    session.post = Mock(return_value=_FakeContext(resp_401))

    call_count = 0

    async def _mock_get_token() -> str:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise EbayAuthError("OAuth refresh failed")
        return "expired_token"

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(side_effect=_mock_get_token)

    with pytest.raises(EbayAuthError, match="OAuth refresh failed"):
        await client.async_call_trading_api("GetMyeBaySelling", "<xml/>")
    assert session.post.call_count == 1


@pytest.mark.asyncio
async def test_trading_403_fails_immediately_no_refresh_no_retry() -> None:
    """Trading HTTP 403 fails immediately with status=403, zero refreshes and zero retries."""
    session = Mock()
    resp_403 = _FakeResponse(status=403, text="Forbidden")
    session.post = Mock(return_value=_FakeContext(resp_403))

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    with pytest.raises(EbayApiError) as exc_info:
        await client.async_call_trading_api("GetMyeBaySelling", "<xml/>")

    assert exc_info.value.status == 403
    assert session.post.call_count == 1
    assert client._last_retry_count == 0
    assert client.async_get_access_token.call_count == 1


@pytest.mark.asyncio
async def test_trading_400_and_404_fail_immediately() -> None:
    """Trading HTTP 400 and 404 fail immediately with zero retries."""
    session = Mock()
    resp_400 = _FakeResponse(status=400, text="Bad Request")
    session.post = Mock(return_value=_FakeContext(resp_400))

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    with pytest.raises(EbayApiError) as exc_400:
        await client.async_call_trading_api("GetMyeBaySelling", "<xml/>")
    assert exc_400.value.status == 400
    assert session.post.call_count == 1

    resp_404 = _FakeResponse(status=404, text="Not Found")
    session.post = Mock(return_value=_FakeContext(resp_404))
    with pytest.raises(EbayApiError) as exc_404:
        await client.async_call_trading_api("GetMyeBaySelling", "<xml/>")
    assert exc_404.value.status == 404
    assert session.post.call_count == 1


@pytest.mark.asyncio
async def test_rest_403_fails_immediately_no_refresh_no_retry() -> None:
    """REST HTTP 403 fails immediately with zero retries and zero refreshes."""
    session = Mock()
    resp_403 = _FakeResponse(status=403, json_payload={"error": "forbidden"})
    session.get = Mock(return_value=_FakeContext(resp_403))

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    with pytest.raises(EbayPartialFailure) as exc_info:
        await client._async_rest_get(
            "https://api.ebay.com/sell/fulfillment/v1/order",
            partial_category="orders",
        )

    assert exc_info.value.failure is not None
    assert exc_info.value.failure.http_status == 403
    assert session.get.call_count == 1
    assert client._last_retry_count == 0
    assert client.async_get_access_token.call_count == 1


@pytest.mark.asyncio
async def test_rest_scenario_500_401_200_capped_at_three_attempts() -> None:
    """Scenario 500 -> 401 -> 200 executes 3 total endpoint requests, 1 refresh, and succeeds."""
    session = Mock()
    resp_500 = _FakeResponse(status=500, json_payload={"error": "server error"})
    resp_401 = _FakeResponse(status=401, text="Unauthorized")
    resp_200 = _FakeResponse(status=200, json_payload={"data": "ok"})
    session.get = Mock(
        side_effect=[
            _FakeContext(resp_500),
            _FakeContext(resp_401),
            _FakeContext(resp_200),
        ]
    )

    tokens = ["token_1", "token_2"]

    async def _mock_get_token() -> str:
        return tokens.pop(0) if len(tokens) > 1 else tokens[0]

    policy = BoundedRetryPolicy(base_delay=0.01, sleep_func=AsyncMock())
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        retry_policy=policy,
    )
    client.async_get_access_token = AsyncMock(side_effect=_mock_get_token)

    data = await client._async_rest_get(
        "https://api.ebay.com/test",
        partial_category="test",
    )
    assert data == {"data": "ok"}
    assert session.get.call_count == 3
    assert client._last_retry_count == 2


@pytest.mark.asyncio
async def test_rest_scenario_500_503_401_no_fourth_attempt() -> None:
    """Scenario 500 -> 503 -> 401 hits 3 total endpoint attempts and halts without 4th attempt."""
    session = Mock()
    resp_500 = _FakeResponse(status=500, json_payload={"error": "server error 1"})
    resp_503 = _FakeResponse(status=503, json_payload={"error": "server error 2"})
    resp_401 = _FakeResponse(status=401, text="Unauthorized")
    session.get = Mock(
        side_effect=[
            _FakeContext(resp_500),
            _FakeContext(resp_503),
            _FakeContext(resp_401),
        ]
    )

    policy = BoundedRetryPolicy(base_delay=0.01, sleep_func=AsyncMock())
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        retry_policy=policy,
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    with pytest.raises(EbayAuthError):
        await client._async_rest_get(
            "https://api.ebay.com/test",
            partial_category="test",
        )
    assert session.get.call_count == 3
    assert client._last_retry_count == 2


@pytest.mark.asyncio
async def test_rest_scenario_budget_expires_before_backoff_no_phantom_retry() -> None:
    """Budget expiring before backoff completes raises bounded error and records 0 retries."""
    session = Mock()
    resp_500 = _FakeResponse(status=500, json_payload={"error": "server error"})
    session.get = Mock(return_value=_FakeContext(resp_500))

    # Total timeout 0.1s, base delay 1.0s -> backoff will exceed deadline
    policy = BoundedRetryPolicy(base_delay=1.0, total_timeout=0.1)
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        retry_policy=policy,
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    with pytest.raises(EbayPartialFailure) as exc_info:
        await client._async_rest_get(
            "https://api.ebay.com/test",
            partial_category="orders",
        )
    assert exc_info.value.failure is not None
    assert session.get.call_count == 1
    # Crucial: _last_retry_count must NOT be incremented because attempt 1 was never dispatched!
    assert client._last_retry_count == 0


@pytest.mark.asyncio
async def test_rest_scenario_cancellation_during_backoff() -> None:
    """Cancellation during backoff sleep cleanly halts without dispatching another request."""
    session = Mock()
    resp_500 = _FakeResponse(status=500, json_payload={"error": "server error"})
    session.get = Mock(return_value=_FakeContext(resp_500))

    async def _cancel_sleep(delay: float) -> None:
        raise asyncio.CancelledError()

    policy = BoundedRetryPolicy(base_delay=0.5, sleep_func=_cancel_sleep)
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        retry_policy=policy,
    )
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    with pytest.raises(asyncio.CancelledError):
        await client._async_rest_get(
            "https://api.ebay.com/test",
            partial_category="orders",
        )
    assert session.get.call_count == 1
    assert client._last_retry_count == 0


@pytest.mark.asyncio
async def test_rest_scenario_budget_exhausted_during_request() -> None:
    """Request timeout when budget is zero raises 504 bounded failure."""
    session = Mock()
    policy = BoundedRetryPolicy(total_timeout=-1.0)
    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
        retry_policy=policy,
    )

    with pytest.raises(EbayPartialFailure) as exc_info:
        await client._async_rest_get(
            "https://api.ebay.com/test",
            partial_category="orders",
        )
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.http_status == 504
    assert exc_info.value.failure.classification == "transient"
    assert session.get.call_count == 0


@pytest.mark.asyncio
async def test_multiple_concurrent_requests_share_token_refresh() -> None:
    """Multiple concurrent requests encountering 401 coordinate under token refresh lock."""
    session = Mock()
    resp_401 = _FakeResponse(status=401, text="Unauthorized")
    resp_200 = _FakeResponse(status=200, json_payload={"result": "ok"})

    session.get = Mock(
        side_effect=[
            _FakeContext(resp_401),
            _FakeContext(resp_200),
            _FakeContext(resp_200),
            _FakeContext(resp_200),
        ]
    )

    refresh_count = 0

    client = EbayApiClient(
        session,
        environment="production",
        client_id="test_client",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="test_refresh",
        site_id="0",
    )

    async def _mock_refresh(failed_token: str | None = None) -> str:
        nonlocal refresh_count
        refresh_count += 1
        await asyncio.sleep(0.01)
        return f"refreshed_token_{refresh_count}"

    client.async_refresh_access_token = AsyncMock(side_effect=_mock_refresh)  # type: ignore[method-assign]
    client.async_get_access_token = AsyncMock(return_value="valid_token")

    res1, res2 = await asyncio.gather(
        client._async_rest_get("https://api.ebay.com/test1", partial_category="test"),
        client._async_rest_get("https://api.ebay.com/test2", partial_category="test"),
    )
    assert res1 == {"result": "ok"}
    assert res2 == {"result": "ok"}
    assert refresh_count == 1
    assert client.async_refresh_access_token.await_count == 1


# ==============================================================================
# OAuth 401 Forced Token Refresh and Deduplication Tests
# ==============================================================================


class _MockConfigEntry:
    def __init__(self, token: dict[str, Any]) -> None:
        self.data = {"token": dict(token)}
        self.entry_id = "test_entry_id"
        self.reauth_started = False

    def async_start_reauth_if_available(self, hass: Any) -> None:
        self.reauth_started = True

    def async_start_reauth(self, hass: Any) -> None:
        self.reauth_started = True


class _MockImplementation:
    def __init__(self, refresh_return: dict[str, Any] | None = None) -> None:
        self.refresh_calls: list[dict[str, Any]] = []
        self.refresh_return = refresh_return or {
            "access_token": "fresh_access_token",
            "expires_in": 7200,
        }
        self.side_effect: Exception | None = None

    async def async_refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        self.refresh_calls.append(dict(token))
        if self.side_effect:
            raise self.side_effect
        return self.refresh_return


class _MockHass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config_entries = Mock()
        self.config_entries.async_update_entry = Mock()


def _build_oauth_client(
    initial_access_token: str = "initial_rejected_token",
    expires_in_future: float = 3600.0,
    session: Any = None,
    retry_policy: BoundedRetryPolicy | None = None,
) -> tuple[EbayApiClient, _MockImplementation, _MockConfigEntry]:
    """Create an EbayApiClient configured with an OAuth2Session."""
    token_dict = {
        "access_token": initial_access_token,
        "refresh_token": "valid_refresh_token",
        "expires_at": time.time() + expires_in_future,
    }
    mock_entry = _MockConfigEntry(token_dict)
    mock_impl = _MockImplementation()
    mock_hass = _MockHass()
    oauth_session = OAuth2Session(mock_hass, mock_entry, mock_impl)

    client = EbayApiClient(
        session or Mock(),
        environment="production",
        client_id="test_client_id",
        client_secret="test_secret",
        runame="test_runame",
        refresh_token="valid_refresh_token",
        site_id="0",
        oauth_session=oauth_session,
        retry_policy=retry_policy,
    )
    return client, mock_impl, mock_entry


@pytest.mark.asyncio
async def test_rest_401_valid_by_expiry_forces_refresh_and_replays() -> None:
    """REST HTTP 401 forces OAuth token refresh on valid-by-expiry token and replays request."""
    client, mock_impl, _ = _build_oauth_client(initial_access_token="tok_401")
    recorded_headers: list[dict[str, str]] = []

    def _mock_get(url: str, **kwargs: Any) -> _FakeContext:
        headers = dict(kwargs.get("headers") or {})
        recorded_headers.append(headers)
        if headers.get("Authorization") == "Bearer tok_401":
            return _FakeContext(_FakeResponse(status=401, text="Unauthorized"))
        return _FakeContext(_FakeResponse(status=200, json_payload={"status": "ok"}))

    client._session.get = Mock(side_effect=_mock_get)

    res = await client._async_rest_get(
        "https://api.ebay.com/test", partial_category="orders"
    )
    assert res == {"status": "ok"}
    assert len(mock_impl.refresh_calls) == 1
    assert mock_impl.refresh_calls[0]["access_token"] == "tok_401"
    assert len(recorded_headers) == 2
    assert recorded_headers[0]["Authorization"] == "Bearer tok_401"
    assert recorded_headers[1]["Authorization"] == "Bearer fresh_access_token"


@pytest.mark.asyncio
async def test_trading_401_valid_by_expiry_forces_refresh_and_replays() -> None:
    """Trading HTTP 401 forces OAuth token refresh on valid-by-expiry token and replays request."""
    client, mock_impl, _ = _build_oauth_client(initial_access_token="tok_401")
    recorded_headers: list[dict[str, str]] = []

    def _mock_post(url: str, **kwargs: Any) -> _FakeContext:
        headers = dict(kwargs.get("headers") or {})
        recorded_headers.append(headers)
        if headers.get("X-EBAY-API-IAF-TOKEN") == "tok_401":
            return _FakeContext(_FakeResponse(status=401, text="Unauthorized"))
        return _FakeContext(
            _FakeResponse(
                status=200,
                text='<?xml version="1.0" encoding="utf-8"?><GetOrdersResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></GetOrdersResponse>',
            )
        )

    client._session.post = Mock(side_effect=_mock_post)

    root = await client.async_call_trading_api("GetOrders", "<GetOrdersRequest/>")
    assert root.tag.endswith("GetOrdersResponse")
    assert len(mock_impl.refresh_calls) == 1
    assert mock_impl.refresh_calls[0]["access_token"] == "tok_401"
    assert len(recorded_headers) == 2
    assert recorded_headers[0]["X-EBAY-API-IAF-TOKEN"] == "tok_401"
    assert recorded_headers[1]["X-EBAY-API-IAF-TOKEN"] == "fresh_access_token"


@pytest.mark.asyncio
async def test_trading_auth_ack_failure_forces_refresh_and_replays() -> None:
    """Trading Ack=Failure with auth error code forces OAuth token refresh and replays."""
    client, mock_impl, _ = _build_oauth_client(initial_access_token="tok_ack_fail")
    recorded_headers: list[dict[str, str]] = []

    def _mock_post(url: str, **kwargs: Any) -> _FakeContext:
        headers = dict(kwargs.get("headers") or {})
        recorded_headers.append(headers)
        if headers.get("X-EBAY-API-IAF-TOKEN") == "tok_ack_fail":
            return _FakeContext(
                _FakeResponse(
                    status=200,
                    text=(
                        '<?xml version="1.0" encoding="utf-8"?>'
                        '<GetOrdersResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
                        "<Ack>Failure</Ack>"
                        "<Errors><ErrorCode>931</ErrorCode><ShortMessage>Auth token is invalid</ShortMessage></Errors>"
                        "</GetOrdersResponse>"
                    ),
                )
            )
        return _FakeContext(
            _FakeResponse(
                status=200,
                text='<?xml version="1.0" encoding="utf-8"?><GetOrdersResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></GetOrdersResponse>',
            )
        )

    client._session.post = Mock(side_effect=_mock_post)

    root = await client.async_call_trading_api("GetOrders", "<GetOrdersRequest/>")
    assert root.tag.endswith("GetOrdersResponse")
    assert len(mock_impl.refresh_calls) == 1
    assert mock_impl.refresh_calls[0]["access_token"] == "tok_ack_fail"
    assert len(recorded_headers) == 2
    assert recorded_headers[0]["X-EBAY-API-IAF-TOKEN"] == "tok_ack_fail"
    assert recorded_headers[1]["X-EBAY-API-IAF-TOKEN"] == "fresh_access_token"


@pytest.mark.asyncio
async def test_concurrent_rest_401_requests_deduplicate_under_lock() -> None:
    """Multiple concurrent requests encountering 401 execute exactly one OAuth token refresh."""
    client, mock_impl, _ = _build_oauth_client(initial_access_token="tok_shared_401")

    # Add slight latency to simulated token exchange
    original_refresh = mock_impl.async_refresh_token

    async def _slow_refresh(token: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return await original_refresh(token)

    mock_impl.async_refresh_token = _slow_refresh  # type: ignore[method-assign]

    def _mock_get(url: str, **kwargs: Any) -> _FakeContext:
        headers = dict(kwargs.get("headers") or {})
        if headers.get("Authorization") == "Bearer tok_shared_401":
            return _FakeContext(_FakeResponse(status=401, text="Unauthorized"))
        return _FakeContext(_FakeResponse(status=200, json_payload={"result": url}))

    client._session.get = Mock(side_effect=_mock_get)

    results = await asyncio.gather(
        client._async_rest_get("https://api.ebay.com/test1", partial_category="test"),
        client._async_rest_get("https://api.ebay.com/test2", partial_category="test"),
        client._async_rest_get("https://api.ebay.com/test3", partial_category="test"),
    )
    assert len(results) == 3
    assert all("result" in r for r in results)
    # Crucial assertion: exactly 1 refresh was performed across all concurrent requests
    assert len(mock_impl.refresh_calls) == 1


@pytest.mark.asyncio
async def test_subsequent_request_reuses_refreshed_token() -> None:
    """Following a successful token refresh, subsequent requests reuse the new token."""
    client, mock_impl, _ = _build_oauth_client(initial_access_token="tok_initial")

    def _mock_get(url: str, **kwargs: Any) -> _FakeContext:
        headers = dict(kwargs.get("headers") or {})
        if headers.get("Authorization") == "Bearer tok_initial":
            return _FakeContext(_FakeResponse(status=401, text="Unauthorized"))
        return _FakeContext(_FakeResponse(status=200, json_payload={"status": "ok"}))

    client._session.get = Mock(side_effect=_mock_get)

    # First request: encounters 401 and refreshes
    await client._async_rest_get(
        "https://api.ebay.com/test1", partial_category="orders"
    )
    assert len(mock_impl.refresh_calls) == 1

    # Second request: immediately uses fresh_access_token without refreshing
    res2 = await client._async_rest_get(
        "https://api.ebay.com/test2", partial_category="orders"
    )
    assert res2 == {"status": "ok"}
    assert len(mock_impl.refresh_calls) == 1


@pytest.mark.asyncio
async def test_rest_401_transient_refresh_failure_halts_immediately() -> None:
    """Transient token refresh failure raises EbayAuthTransientError without endpoint retry."""
    client, mock_impl, _ = _build_oauth_client(
        initial_access_token="tok_transient_fail"
    )
    mock_impl.side_effect = OAuth2TokenRequestTransientError(
        domain="ebay", request_info=MagicMock()
    )

    client._session.get = Mock(
        return_value=_FakeContext(_FakeResponse(status=401, text="Unauthorized"))
    )

    with pytest.raises(EbayAuthTransientError):
        await client._async_rest_get(
            "https://api.ebay.com/test", partial_category="orders"
        )

    # Failed refresh halts immediately: endpoint called only once
    assert client._session.get.call_count == 1
    assert len(mock_impl.refresh_calls) == 1


@pytest.mark.asyncio
async def test_trading_401_reauth_failure_triggers_reauth_and_halts() -> None:
    """Permanent token refresh failure raises EbayAuthError, starts reauth, and halts."""
    client, mock_impl, mock_entry = _build_oauth_client(
        initial_access_token="tok_reauth_fail"
    )
    mock_impl.side_effect = OAuth2TokenRequestReauthError(
        domain="ebay", request_info=MagicMock()
    )

    client._session.post = Mock(
        return_value=_FakeContext(_FakeResponse(status=401, text="Unauthorized"))
    )

    with pytest.raises(EbayAuthError, match="OAuth token refresh failed"):
        await client.async_call_trading_api("GetOrders", "<GetOrdersRequest/>")

    # Endpoint called only once
    assert client._session.post.call_count == 1
    assert len(mock_impl.refresh_calls) == 1
    assert mock_entry.reauth_started is True


@pytest.mark.asyncio
async def test_http_403_never_triggers_refresh_in_rest_or_trading() -> None:
    """HTTP 403 Forbidden strictly executes zero retries and zero token refreshes."""
    client, mock_impl, _ = _build_oauth_client(initial_access_token="valid_tok")

    # REST 403
    client._session.get = Mock(
        return_value=_FakeContext(_FakeResponse(status=403, text="Forbidden"))
    )
    with pytest.raises(EbayPartialFailure):
        await client._async_rest_get(
            "https://api.ebay.com/test", partial_category="orders"
        )
    assert len(mock_impl.refresh_calls) == 0
    assert client._session.get.call_count == 1

    # Trading 403
    client._session.post = Mock(
        return_value=_FakeContext(_FakeResponse(status=403, text="Forbidden"))
    )
    with pytest.raises(EbayApiError) as exc_info:
        await client.async_call_trading_api("GetOrders", "<GetOrdersRequest/>")
    assert exc_info.value.status == 403
    assert len(mock_impl.refresh_calls) == 0
    assert client._session.post.call_count == 1


@pytest.mark.asyncio
async def test_401_on_exhausted_attempt_budget_halts_without_refresh() -> None:
    """HTTP 401 on the final attempt budget halts without refresh or exceeded attempts."""
    # max_retries=0 allows only 1 attempt
    policy = BoundedRetryPolicy(max_retries=0)
    client, mock_impl, _ = _build_oauth_client(
        initial_access_token="tok_budget_exhausted",
        retry_policy=policy,
    )

    # REST on 401 with max_retries=0 (attempt=0 is already attempt+1 == max_attempts)
    client._session.get = Mock(
        return_value=_FakeContext(_FakeResponse(status=401, text="Unauthorized"))
    )
    with pytest.raises(EbayAuthError):
        await client._async_rest_get(
            "https://api.ebay.com/test", partial_category="orders"
        )
    assert len(mock_impl.refresh_calls) == 0
    assert client._session.get.call_count == 1

    # Trading on 401 with max_retries=0
    client._session.post = Mock(
        return_value=_FakeContext(_FakeResponse(status=401, text="Unauthorized"))
    )
    with pytest.raises(EbayAuthError):
        await client.async_call_trading_api("GetOrders", "<GetOrdersRequest/>")
    assert len(mock_impl.refresh_calls) == 0
    assert client._session.post.call_count == 1


def test_ebay_auth_error_privacy_and_token_attribute() -> None:
    """EbayAuthError retains failed_token attribute while never leaking it in str or repr."""
    secret = "v^1.1#i^1#r^1#I^3#f^0#p^3#r^1#t^Ul4xMF82OkREQ0M2"
    err = EbayAuthError("eBay REST auth failed with HTTP 401", failed_token=secret)

    assert err.failed_token == secret
    assert secret not in str(err)
    assert secret not in repr(err)
    assert repr(err) == "EbayAuthError('eBay REST auth failed with HTTP 401')"


# ==============================================================================
# Lifecycle Tests: RateLimitTracker Registry Failure-Safety and Cleanup
# ==============================================================================


@pytest.mark.asyncio
async def test_tracker_lifecycle_first_refresh_failure_cleans_up() -> None:
    """Coordinator first-refresh failure during async_setup_entry releases tracker cleanly."""
    from custom_components.ebay import async_setup_entry
    from custom_components.ebay.const import (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_ENVIRONMENT,
        CONF_OAUTH_SCOPES,
        CONF_REFRESH_TOKEN,
        CONF_RUNAME,
        CONF_SITE_ID,
        DOMAIN,
    )
    from homeassistant.exceptions import ConfigEntryNotReady

    hass = Mock()
    hass.data = {}
    entry = Mock()
    entry.entry_id = "test_entry_fail_1"
    entry.data = {
        CONF_CLIENT_ID: "app_id_fail",
        CONF_CLIENT_SECRET: "secret",
        CONF_ENVIRONMENT: "production",
        CONF_RUNAME: "runame",
        CONF_REFRESH_TOKEN: "refresh",
        CONF_SITE_ID: "0",
        CONF_OAUTH_SCOPES: "https://api.ebay.com/oauth/api_scope",
    }
    entry.options = {}

    with (
        patch("homeassistant.helpers.aiohttp_client.async_get_clientsession"),
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
            "custom_components.ebay.coordinator.EbayDataUpdateCoordinator.async_config_entry_first_refresh",
            side_effect=ConfigEntryNotReady("API down"),
        ),
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

    trackers = hass.data.get(DOMAIN, {}).get("rate_limit_trackers", {})
    assert len(trackers) == 0, "Tracker was not released after first_refresh failure!"


@pytest.mark.asyncio
async def test_tracker_lifecycle_platform_setup_failure_cleans_up() -> None:
    """Platform forwarding failure during async_setup_entry releases tracker cleanly."""
    from custom_components.ebay import async_setup_entry
    from custom_components.ebay.const import (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_ENVIRONMENT,
        CONF_OAUTH_SCOPES,
        CONF_REFRESH_TOKEN,
        CONF_RUNAME,
        CONF_SITE_ID,
        DOMAIN,
    )

    hass = Mock()
    hass.data = {}
    entry = Mock()
    entry.entry_id = "test_entry_fail_2"
    entry.data = {
        CONF_CLIENT_ID: "app_id_platform_fail",
        CONF_CLIENT_SECRET: "secret",
        CONF_ENVIRONMENT: "production",
        CONF_RUNAME: "runame",
        CONF_REFRESH_TOKEN: "refresh",
        CONF_SITE_ID: "0",
        CONF_OAUTH_SCOPES: "https://api.ebay.com/oauth/api_scope",
    }
    entry.options = {}

    with (
        patch("homeassistant.helpers.aiohttp_client.async_get_clientsession"),
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
            "custom_components.ebay.coordinator.EbayDataUpdateCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("Platform error"),
        ),
    ):
        with pytest.raises(RuntimeError):
            await async_setup_entry(hass, entry)

    trackers = hass.data.get(DOMAIN, {}).get("rate_limit_trackers", {})
    assert len(trackers) == 0, "Tracker was not released after platform setup failure!"


@pytest.mark.asyncio
async def test_tracker_lifecycle_two_entries_same_keyset_one_fails_setup() -> None:
    """Entry 1 succeeds while Entry 2 sharing same keyset fails setup; Entry 1 stays active."""
    from custom_components.ebay import async_setup_entry, async_unload_entry
    from custom_components.ebay.const import (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_ENVIRONMENT,
        CONF_OAUTH_SCOPES,
        CONF_REFRESH_TOKEN,
        CONF_RUNAME,
        CONF_SITE_ID,
        DOMAIN,
    )
    from homeassistant.exceptions import ConfigEntryNotReady

    hass = Mock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    entry1 = Mock()
    entry1.entry_id = "entry_1_shared"
    entry1.data = {
        CONF_CLIENT_ID: "shared_keyset_app",
        CONF_CLIENT_SECRET: "secret",
        CONF_ENVIRONMENT: "production",
        CONF_RUNAME: "runame",
        CONF_REFRESH_TOKEN: "refresh",
        CONF_SITE_ID: "0",
        CONF_OAUTH_SCOPES: "https://api.ebay.com/oauth/api_scope",
    }
    entry1.options = {}
    entry1.add_update_listener = Mock()
    entry1.async_on_unload = Mock()

    entry2 = Mock()
    entry2.entry_id = "entry_2_shared"
    entry2.data = dict(entry1.data)
    entry2.options = {}

    with (
        patch("homeassistant.helpers.aiohttp_client.async_get_clientsession"),
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
            "custom_components.ebay.coordinator.EbayDataUpdateCoordinator.async_config_entry_first_refresh",
            new_callable=AsyncMock,
        ),
    ):
        res1 = await async_setup_entry(hass, entry1)
        assert res1 is True

    trackers = hass.data[DOMAIN]["rate_limit_trackers"]
    key = list(trackers.keys())[0]
    assert trackers[key]["ref_count"] == 1
    assert "entry_1_shared" in trackers[key]["entries"]

    # Now Entry 2 attempts setup and fails during first_refresh
    with (
        patch("homeassistant.helpers.aiohttp_client.async_get_clientsession"),
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
            "custom_components.ebay.coordinator.EbayDataUpdateCoordinator.async_config_entry_first_refresh",
            side_effect=ConfigEntryNotReady("Rate limit"),
        ),
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry2)

    # Key still exists with ref_count == 1 and entry_1_shared still registered!
    assert key in trackers
    assert trackers[key]["ref_count"] == 1
    assert trackers[key]["entries"] == {"entry_1_shared"}

    # Unload entry1
    with (
        patch(
            "custom_components.ebay.storage.async_save_repair_streaks",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.ebay.storage.async_save_ending_soon_fired",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.ebay.storage.async_save_inactive_item_sensors",
            new=AsyncMock(),
        ),
    ):
        unload_res = await async_unload_entry(hass, entry1)
    assert unload_res is True
    assert len(trackers) == 0


@pytest.mark.asyncio
async def test_tracker_lifecycle_repeated_reloads_no_leaks() -> None:
    """Repeated reload cycles for a config entry preserve exactly 1 tracker ref count."""
    from custom_components.ebay import async_setup_entry, async_unload_entry
    from custom_components.ebay.const import (
        CONF_CLIENT_ID,
        CONF_CLIENT_SECRET,
        CONF_ENVIRONMENT,
        CONF_OAUTH_SCOPES,
        CONF_REFRESH_TOKEN,
        CONF_RUNAME,
        CONF_SITE_ID,
        DOMAIN,
    )

    hass = Mock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    entry = Mock()
    entry.entry_id = "entry_reload_test"
    entry.data = {
        CONF_CLIENT_ID: "reload_keyset_app",
        CONF_CLIENT_SECRET: "secret",
        CONF_ENVIRONMENT: "production",
        CONF_RUNAME: "runame",
        CONF_REFRESH_TOKEN: "refresh",
        CONF_SITE_ID: "0",
        CONF_OAUTH_SCOPES: "https://api.ebay.com/oauth/api_scope",
    }
    entry.options = {}
    entry.add_update_listener = Mock()
    entry.async_on_unload = Mock()

    for _ in range(5):
        with (
            patch("homeassistant.helpers.aiohttp_client.async_get_clientsession"),
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
                "custom_components.ebay.coordinator.EbayDataUpdateCoordinator.async_config_entry_first_refresh",
                new_callable=AsyncMock,
            ),
        ):
            assert await async_setup_entry(hass, entry) is True

        trackers = hass.data[DOMAIN]["rate_limit_trackers"]
        key = list(trackers.keys())[0]
        assert trackers[key]["ref_count"] == 1
        assert trackers[key]["entries"] == {"entry_reload_test"}

        with (
            patch(
                "custom_components.ebay.storage.async_save_repair_streaks",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.ebay.storage.async_save_ending_soon_fired",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.ebay.storage.async_save_inactive_item_sensors",
                new=AsyncMock(),
            ),
        ):
            assert await async_unload_entry(hass, entry) is True
        assert len(trackers) == 0
