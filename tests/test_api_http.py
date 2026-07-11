"""HTTP, Ack, warning, and Analytics batching contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock
import xml.etree.ElementTree as ET

import pytest

from custom_components.ebay.api import (
    ANALYTICS_BATCH_SIZE,
    EBAY_XML_NS,
    EbayApiClient,
    EbayApiError,
    EbayAuthError,
    EbayParseError,
    chunk_item_ids,
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
        text: str = "",
        json_payload: Any | None = None,
        raise_json: Exception | None = None,
    ) -> None:
        self.status = status
        self._text = text
        self._json_payload = json_payload
        self._raise_json = raise_json

    async def text(self) -> str:
        return self._text

    async def json(self, *, content_type: str | None = None) -> Any:
        if self._raise_json:
            raise self._raise_json
        return self._json_payload


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

        async def async_get_traffic_report(
            self, item_ids: list[str]
        ) -> dict[str, Any]:
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

        async def async_get_traffic_report(
            self, item_ids: list[str]
        ) -> dict[str, Any]:
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
    root = _root(f'<GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}"><Ack>Success</Ack></GetMyeBaySellingResponse>')
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
