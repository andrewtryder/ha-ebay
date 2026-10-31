"""Comprehensive reliability, rate limit, and lifecycle regression tests for Milestone 11."""

from __future__ import annotations

import asyncio
import email.utils
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
import xml.etree.ElementTree as ET

import aiohttp
import pytest

from custom_components.ebay.api import (
    EbayApiClient,
    EbayApiError,
    EbayAuthError,
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
        await client._async_rest_get_once(
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
        root = await client._async_call_trading_api_once("GetMyeBaySelling", "<xml/>")
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

    data = await client._async_rest_get_once(
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

    data = await client._async_rest_get_once(
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
        await client._async_rest_get_once(
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

    root = await client._async_call_trading_api_once("GetMyeBaySelling", "<xml/>")
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
