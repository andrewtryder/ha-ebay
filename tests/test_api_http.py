"""HTTP, Ack, warning, and Analytics batching contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from custom_components.ebay.api import (
    ANALYTICS_BATCH_SIZE,
    API_WARNINGS_STATE_ATTR_MAX,
    EBAY_XML_NS,
    EbayApiClient,
    EbayApiError,
    EbayAuthError,
    EbayParseError,
    EbayPartialFailure,
    BEST_OFFERS_CALL_NAME,
    analytics_date_range_filter,
    build_get_my_ebay_buying_xml,
    chunk_item_ids,
    dedupe_trading_warnings,
    extract_trading_warnings,
    parse_ebay_time,
    parse_selling_container,
    _to_bool,
    _to_float,
    _to_int,
)


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _client(session: Any) -> EbayApiClient:
    client = EbayApiClient(
        session,
        environment="production",
        client_id="client",
        client_secret="secret",
        runame="runame",
        refresh_token="refresh",
        site_id="0",
    )
    client._access_token = "token"
    client._access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        text: str | None = None,
        json_payload: Any | None = None,
        headers: dict[str, str] | None = None,
        raise_json: Exception | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._text = text
        self._json_payload = json_payload
        self._raise_json = raise_json

    async def text(self) -> str:
        if self._text is not None:
            return self._text
        if self._json_payload is None:
            return ""
        return json.dumps(self._json_payload)

    async def json(self, *, content_type: str | None = None) -> Any:
        if self._raise_json:
            raise self._raise_json
        if self._json_payload is None and self._text is not None:
            if not self._text.strip():
                raise json.JSONDecodeError("Expecting value", self._text, 0)
            return json.loads(self._text)
        return self._json_payload if self._json_payload is not None else {}


class _Context:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *args: object) -> None:
        return None


def test_chunk_item_ids_batches() -> None:
    assert chunk_item_ids([]) == []
    assert chunk_item_ids(["a"], 100) == [["a"]]
    ids = [str(i) for i in range(ANALYTICS_BATCH_SIZE + 3)]
    batches = chunk_item_ids(ids)
    assert len(batches) == 2
    assert len(batches[0]) == ANALYTICS_BATCH_SIZE
    assert batches[1] == ["100", "101", "102"]


def test_analytics_batching_zero_one_and_multiple() -> None:
    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="c",
                client_secret="s",
                runame="r",
                refresh_token="t",
                site_id="0",
            )
            self.calls: list[list[str]] = []

        async def async_get_traffic_report(self, item_ids: list[str]) -> dict[str, Any]:
            self.calls.append(list(item_ids))
            return {
                "records": [
                    {
                        "dimensionValues": [{"value": item_ids[0]}],
                        "metricValues": [{"value": "7"}],
                    }
                ]
            }

    client = Client()
    assert asyncio.run(client.async_fetch_analytics_views([])) == ({}, False)
    views, partial = asyncio.run(client.async_fetch_analytics_views(["1"]))
    assert views == {"1": 7}
    assert partial is False
    assert len(client.calls) == 1

    client.calls.clear()
    many = [str(i) for i in range(ANALYTICS_BATCH_SIZE + 1)]
    views, partial = asyncio.run(client.async_fetch_analytics_views(many))
    assert partial is False
    assert len(client.calls) == 2
    assert "0" in views


def test_analytics_partial_intermediate_batch_failure_keeps_successes() -> None:
    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="c",
                client_secret="s",
                runame="r",
                refresh_token="t",
                site_id="0",
            )
            self.calls = 0

        async def async_get_traffic_report(self, item_ids: list[str]) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 2:
                raise EbayApiError("batch failed")
            return {
                "records": [
                    {
                        "dimensionValues": [{"value": item_ids[0]}],
                        "metricValues": [{"value": str(self.calls * 10)}],
                    }
                ]
            }

    client = Client()
    ids = [str(i) for i in range(ANALYTICS_BATCH_SIZE * 2 + 1)]
    views, partial = asyncio.run(client.async_fetch_analytics_views(ids))
    assert partial is True
    assert views["0"] == 10
    assert "100" not in views
    assert views.get("200") == 30


def test_analytics_batch_failure_debug_log_includes_batch_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.clients.analytics")

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="c",
                client_secret="s",
                runame="r",
                refresh_token="t",
                site_id="0",
            )

        async def async_get_traffic_report(self, item_ids: list[str]) -> dict[str, Any]:
            raise EbayApiError("eBay Sell Analytics failed with HTTP 500")

    views, partial = asyncio.run(Client().async_fetch_analytics_views(["1", "2"]))

    assert views == {}
    assert partial is True
    assert "Sell Analytics batch failed batch=1 item_count=2" in caplog.text
    assert "EbayApiError: eBay Sell Analytics failed with HTTP 500" in caplog.text


def test_optional_failure_debug_log_includes_safe_exception_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.api")

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="c",
                client_secret="s",
                runame="r",
                refresh_token="t",
                site_id="0",
            )

        async def _fetch_selling_pages(self) -> tuple[dict[str, dict[str, Any]], bool]:
            return {}, False

        async def async_fetch_sold_unsold_classification(
            self,
        ) -> tuple[dict[str, str], int, int, bool]:
            return {}, 0, 0, False

        async def _fetch_seller_list_views(self) -> tuple[dict[str, int], bool]:
            return {}, False

        async def async_call_trading_api(
            self, call_name: str, xml_body: str
        ) -> ET.Element:
            assert call_name == BEST_OFFERS_CALL_NAME
            raise EbayApiError("eBay Trading API returned Ack='Failure'")

    asyncio.run(
        Client().async_fetch_data(
            buying_enabled=False,
            selling_enabled=True,
            analytics_enabled=False,
            ending_soon_threshold_seconds=3600,
        )
    )

    assert "Optional GetBestOffers failed error=EbayApiError" in caplog.text
    assert "Ack='Failure'" in caplog.text


def test_trading_api_ack_warning_extracts_sanitized_warnings() -> None:
    xml = f"""
    <GetMyeBayBuyingResponse xmlns="{EBAY_XML_NS}">
      <Ack>Warning</Ack>
      <Errors>
        <SeverityCode>Warning</SeverityCode>
        <ErrorCode>21917053</ErrorCode>
        <ShortMessage>Deprecation notice</ShortMessage>
        <LongMessage>Bearer secret-token will be removed</LongMessage>
      </Errors>
      <WatchList/>
    </GetMyeBayBuyingResponse>
    """
    session = Mock()
    session.post.return_value = _Context(_Response(status=200, text=xml))
    client = _client(session)
    root = asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))
    assert _text_ack(root) == "Warning"
    assert client._api_warnings[0]["code"] == "21917053"
    assert client._api_warnings[0]["long_message"] == "[redacted]"


def test_trading_api_debug_log_includes_safe_call_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.api")
    xml = f"""
    <GetMyeBayBuyingResponse xmlns="{EBAY_XML_NS}">
      <Ack>Success</Ack>
      <WatchList/>
    </GetMyeBayBuyingResponse>
    """
    session = Mock()
    session.post.return_value = _Context(_Response(status=200, text=xml))
    client = _client(session)

    asyncio.run(
        client.async_call_trading_api(
            "GetMyeBayBuying", build_get_my_ebay_buying_xml(page=2)
        )
    )

    assert (
        "Trading API call completed call=GetMyeBayBuying page=2 status=200"
        in caplog.text
    )
    assert "ack=Success" in caplog.text
    assert "duration_ms=" in caplog.text


def test_trading_api_debug_log_includes_ack_failure_without_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ebay.api")
    xml = f"""
    <GetMyeBayBuyingResponse xmlns="{EBAY_XML_NS}">
      <Ack>Failure</Ack>
      <Errors><ErrorCode>1</ErrorCode><ShortMessage>Something else</ShortMessage></Errors>
    </GetMyeBayBuyingResponse>
    """
    session = Mock()
    session.post.return_value = _Context(_Response(status=200, text=xml))
    client = _client(session)

    with pytest.raises(EbayApiError):
        asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))

    assert "Trading API call failed call=GetMyeBayBuying" in caplog.text
    assert "ack=Failure" in caplog.text
    assert "Something else" not in caplog.text


def _text_ack(root: ET.Element) -> str | None:
    return root.findtext(f"{{{EBAY_XML_NS}}}Ack")


def test_trading_api_ack_failure_auth_and_non_auth() -> None:
    auth_xml = f"""
    <GetMyeBayBuyingResponse xmlns="{EBAY_XML_NS}">
      <Ack>Failure</Ack>
      <Errors><ErrorCode>931</ErrorCode><ShortMessage>Auth token is invalid</ShortMessage></Errors>
    </GetMyeBayBuyingResponse>
    """
    other_xml = f"""
    <GetMyeBayBuyingResponse xmlns="{EBAY_XML_NS}">
      <Ack>Failure</Ack>
      <Errors><ErrorCode>1</ErrorCode><ShortMessage>Something else</ShortMessage></Errors>
    </GetMyeBayBuyingResponse>
    """
    session = Mock()
    session.post.return_value = _Context(_Response(status=200, text=auth_xml))
    client = _client(session)
    client.async_get_access_token = AsyncMock(return_value="token")
    with pytest.raises(EbayAuthError):
        asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))

    session.post.return_value = _Context(_Response(status=200, text=other_xml))
    with pytest.raises(EbayApiError):
        asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))


def test_trading_api_http_errors_and_malformed_xml() -> None:
    session = Mock()
    client = _client(session)

    session.post.return_value = _Context(_Response(status=429, text="slow down"))
    with pytest.raises(EbayApiError, match="HTTP 429"):
        asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))

    session.post.return_value = _Context(_Response(status=500, text="oops"))
    with pytest.raises(EbayApiError, match="HTTP 500"):
        asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))

    session.post.return_value = _Context(_Response(status=200, text="<not-xml"))
    with pytest.raises(EbayParseError):
        asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))


def test_trading_api_timeout_and_connection_error() -> None:
    class BoomContext:
        async def __aenter__(self):
            raise TimeoutError

        async def __aexit__(self, *args):
            return None

    session = Mock()
    session.post.return_value = BoomContext()
    client = _client(session)
    with pytest.raises(EbayApiError, match="request failed"):
        asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))


def test_trading_api_401_then_successful_refresh() -> None:
    success_xml = f"""
    <GetMyeBayBuyingResponse xmlns="{EBAY_XML_NS}">
      <Ack>Success</Ack>
    </GetMyeBayBuyingResponse>
    """
    calls = {"n": 0}

    class Session:
        def post(self, *args: object, **kwargs: object) -> _Context:
            calls["n"] += 1
            if calls["n"] == 1:
                return _Context(_Response(status=401, text="unauthorized"))
            return _Context(_Response(status=200, text=success_xml))

    client = _client(Session())
    client.async_get_access_token = AsyncMock(side_effect=["stale", "fresh"])
    root = asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))
    assert _text_ack(root) == "Success"
    assert calls["n"] == 2


def test_trading_api_two_consecutive_auth_failures() -> None:
    class Session:
        def post(self, *args: object, **kwargs: object) -> _Context:
            return _Context(_Response(status=401, text="unauthorized"))

    client = _client(Session())
    client.async_get_access_token = AsyncMock(return_value="token")
    with pytest.raises(EbayAuthError):
        asyncio.run(client.async_call_trading_api("GetMyeBayBuying", "<xml/>"))


def test_malformed_fields_and_missing_containers() -> None:
    assert _to_int("x") is None
    assert _to_float("x") is None
    assert _to_bool("maybe") is None
    assert parse_ebay_time("not-a-time") is None
    root = _root(
        f'<GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack></GetMyeBaySellingResponse>'
    )
    assert parse_selling_container(root) == []


def test_duplicate_item_ids_keep_last() -> None:
    from custom_components.ebay.api import _dict_by_item_id

    items = [
        {"item_id": "1", "title": "first"},
        {"item_id": "1", "title": "second"},
    ]
    assert _dict_by_item_id(items)["1"]["title"] == "second"


def test_extract_trading_warnings_skips_non_warning_severity() -> None:
    root = _root(
        f"""
        <Response xmlns="{EBAY_XML_NS}">
          <Errors>
            <SeverityCode>Error</SeverityCode>
            <ErrorCode>1</ErrorCode>
            <ShortMessage>nope</ShortMessage>
          </Errors>
          <Errors>
            <SeverityCode>Warning</SeverityCode>
            <ErrorCode>2</ErrorCode>
            <ShortMessage>yes</ShortMessage>
          </Errors>
        </Response>
        """
    )
    warnings = extract_trading_warnings(root)
    assert len(warnings) == 1
    assert warnings[0]["code"] == "2"


def test_dedupe_trading_warnings_by_code_and_message() -> None:
    warnings = [
        {"code": "1", "short_message": "a", "long_message": "b"},
        {"code": "1", "short_message": "a", "long_message": "b"},
        {"code": "1", "short_message": "other", "long_message": "b"},
        {"code": "2", "short_message": "a", "long_message": "b"},
    ]
    deduped = dedupe_trading_warnings(warnings)
    assert len(deduped) == 3
    assert [item["code"] for item in deduped] == ["1", "1", "2"]
    assert API_WARNINGS_STATE_ATTR_MAX == 10


def test_analytics_http_403_is_partial_failure_with_api_failure() -> None:
    session = Mock()
    client = _client(session)
    client.async_get_access_token = AsyncMock(return_value="token")
    session.get.return_value = _Context(
        _Response(status=403, json_payload={"errors": [{"message": "forbidden"}]})
    )
    with pytest.raises(EbayPartialFailure, match="analytics_views") as exc_info:
        asyncio.run(client.async_get_traffic_report(["1"]))
    assert session.get.call_count == 1
    assert client._last_api_failure is not None
    assert client._last_api_failure.mapped_category == "analytics_views"
    assert client._last_api_failure.classification == "permission"
    assert client._last_api_failure.http_status == 403
    assert client._last_api_failure.operation == "analytics.traffic_report"
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "permission"


def test_analytics_http_400_is_partial_failure_with_api_failure() -> None:
    session = Mock()
    client = _client(session)
    client.async_get_access_token = AsyncMock(return_value="token")
    session.get.return_value = _Context(
        _Response(status=400, json_payload={"errors": [{"message": "bad request"}]})
    )
    with pytest.raises(EbayPartialFailure, match="analytics_views") as exc_info:
        asyncio.run(client.async_get_traffic_report(["1"]))
    assert session.get.call_count == 1
    assert client._last_api_failure is not None
    assert client._last_api_failure.classification == "malformed_request"
    assert client._last_api_failure.http_status == 400
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "malformed_request"


def test_analytics_http_429_is_transient_partial_failure() -> None:
    session = Mock()
    client = _client(session)
    client.async_get_access_token = AsyncMock(return_value="token")
    session.get.return_value = _Context(
        _Response(status=429, headers={"Retry-After": "30"}, json_payload={})
    )
    with patch(
        "custom_components.ebay.clients.http.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        with pytest.raises(EbayPartialFailure, match="analytics_views") as exc_info:
            asyncio.run(client.async_get_traffic_report(["1"]))
    sleep.assert_awaited_once_with(5)
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "transient"
    assert client._last_api_failure is not None
    assert client._last_api_failure.classification == "transient"
    assert client._last_api_failure.http_status == 429


def test_analytics_http_5xx_is_transient_partial_failure() -> None:
    session = Mock()
    client = _client(session)
    client.async_get_access_token = AsyncMock(return_value="token")
    session.get.return_value = _Context(_Response(status=503, json_payload={}))
    with pytest.raises(EbayPartialFailure, match="analytics_views") as exc_info:
        asyncio.run(client.async_get_traffic_report(["1"]))
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "transient"
    assert client._last_api_failure is not None
    assert client._last_api_failure.classification == "transient"
    assert client._last_api_failure.http_status == 503


def test_analytics_transport_error_is_transient_partial_failure() -> None:
    session = Mock()
    client = _client(session)
    client.async_get_access_token = AsyncMock(return_value="token")
    session.get.side_effect = aiohttp.ClientError("connection reset")
    with pytest.raises(EbayPartialFailure, match="analytics_views") as exc_info:
        asyncio.run(client.async_get_traffic_report(["1"]))
    assert exc_info.value.failure is not None
    assert exc_info.value.failure.classification == "transient"
    assert client._last_api_failure is not None
    assert client._last_api_failure.classification == "transient"
    assert client._last_api_failure.http_status is None


def test_analytics_http_401_retries_then_raises_auth_error() -> None:
    session = Mock()
    client = _client(session)
    client.async_get_access_token = AsyncMock(return_value="token")
    session.get.return_value = _Context(_Response(status=401, text="unauthorized"))
    with pytest.raises(EbayAuthError, match="HTTP 401"):
        asyncio.run(client.async_get_traffic_report(["1"]))
    assert session.get.call_count == 2


def test_analytics_http_401_then_successful_retry() -> None:
    session = Mock()
    client = _client(session)
    client.async_get_access_token = AsyncMock(side_effect=["stale", "fresh"])
    session.get.side_effect = [
        _Context(_Response(status=401, text="unauthorized")),
        _Context(_Response(status=200, json_payload={"records": []})),
    ]
    assert asyncio.run(client.async_get_traffic_report(["1"])) == {"records": []}
    assert session.get.call_count == 2


def test_analytics_date_range_uses_pacific_calendar_days() -> None:
    # 2024-01-02 05:00 UTC is still 2024-01-01 in America/Los_Angeles.
    now = datetime(2024, 1, 2, 5, 0, tzinfo=timezone.utc)
    assert analytics_date_range_filter(now=now) == "date_range:[20231202..20240101]"

    pacific = ZoneInfo("America/Los_Angeles")
    local = datetime(2024, 1, 2, 0, 30, tzinfo=pacific)
    assert analytics_date_range_filter(now=local) == "date_range:[20231203..20240102]"


def test_analytics_request_filter_uses_pacific_date_range() -> None:
    session = Mock()
    client = _client(session)
    client.async_get_access_token = AsyncMock(return_value="token")
    session.get.return_value = _Context(
        _Response(status=200, json_payload={"records": []})
    )
    now = datetime(2024, 1, 2, 5, 0, tzinfo=timezone.utc)
    with patch(
        "custom_components.ebay.clients.analytics.analytics_date_range_filter",
        return_value=analytics_date_range_filter(now=now),
    ):
        asyncio.run(client.async_get_traffic_report(["1", "2"]))
    params = session.get.call_args.kwargs["params"]
    assert params["dimension"] == "LISTING"
    assert params["metric"] == "LISTING_VIEWS_TOTAL"
    assert "listing_ids:{1|2}" in params["filter"]
    assert "date_range:[20231202..20240101]" in params["filter"]


def test_analytics_auth_error_propagates_from_batch_fetch() -> None:
    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="c",
                client_secret="s",
                runame="r",
                refresh_token="t",
                site_id="0",
            )

        async def async_get_traffic_report(self, item_ids: list[str]) -> dict[str, Any]:
            raise EbayAuthError("analytics auth failed")

    with pytest.raises(EbayAuthError, match="analytics auth failed"):
        asyncio.run(Client().async_fetch_analytics_views(["1"]))
