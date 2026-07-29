"""Tests for eBay API parsing helpers."""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.ebay.api import (
    BEST_OFFERS_CALL_NAME,
    DEFAULT_SCOPE,
    EBAY_XML_NS,
    MAX_PAGES,
    READ_ONLY_CALL_NAME,
    SELLER_LIST_CALL_NAME,
    SELLING_CALL_NAME,
    EbayApiClient,
    EbayApiError,
    EbayAuthError,
    active_offers_by_item_id,
    analytics_views_by_item_id,
    build_get_best_offers_xml,
    extract_authorization_code,
    extract_oauth_callback_params,
    pagination_total_pages,
    parse_container,
    parse_selling_container,
    seller_list_views_by_item_id,
    summarize_payload,
)
from custom_components.ebay.oauth_errors import OAuth2TokenRequestError


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


class _TokenResponse:
    status = 502

    async def json(self, *, content_type: str | None = None) -> dict[str, Any]:
        raise json.JSONDecodeError("bad json", "<html>", 0)


class _TokenContext:
    async def __aenter__(self) -> _TokenResponse:
        return _TokenResponse()

    async def __aexit__(self, *args: object) -> None:
        return None


class _TokenSession:
    def post(self, *args: object, **kwargs: object) -> _TokenContext:
        return _TokenContext()


def test_parse_buying_items_normalizes_numbers() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    root = _root(
        f"""
        <GetMyeBayBuyingResponse xmlns="{EBAY_XML_NS}">
          <WatchList><ItemArray><Item>
            <ItemID>123</ItemID><Title>Watch</Title><ListingType>Auction</ListingType>
            <ListingDetails><EndTime>{end_time}</EndTime><ViewItemURL>https://example.test/123</ViewItemURL></ListingDetails>
            <SellingStatus><CurrentPrice currencyID="USD">12.50</CurrentPrice><BidCount>3</BidCount></SellingStatus>
          </Item></ItemArray></WatchList>
        </GetMyeBayBuyingResponse>
        """
    )
    items = parse_container(root, "WatchList", "watched")
    assert items[0]["item_id"] == "123"
    assert items[0]["current_price"] == 12.5
    assert items[0]["currency"] == "USD"
    assert items[0]["bid_count"] == 3
    assert items[0]["seconds_left"] > 0


def test_parse_selling_item_and_optional_views_offers() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    selling = _root(
        f"""
        <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
          <ActiveList><ItemArray><Item>
            <ItemID>456</ItemID><Title>Sell</Title>
            <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
            <SellingStatus><CurrentPrice currencyID="USD">20</CurrentPrice><BidCount>1</BidCount><QuantitySold>2</QuantitySold></SellingStatus>
            <Quantity>5</Quantity><QuantityAvailable>3</QuantityAvailable>
            <HitCount>11</HitCount><WatchCount>4</WatchCount><QuestionCount>1</QuestionCount>
          </Item></ItemArray></ActiveList>
        </GetMyeBaySellingResponse>
        """
    )
    item = parse_selling_container(selling)[0]
    assert item["quantity_sold"] == 2
    assert item["views"] == 11
    assert item["watchers"] == 4
    assert item["questions"] == 1

    seller_list = _root(
        f"""
        <GetSellerListResponse xmlns="{EBAY_XML_NS}">
          <ItemArray><Item><ItemID>456</ItemID><HitCount>99</HitCount></Item></ItemArray>
        </GetSellerListResponse>
        """
    )
    assert seller_list_views_by_item_id(seller_list) == {"456": 99}

    offers = _root(
        f"""
        <GetBestOffersResponse xmlns="{EBAY_XML_NS}">
          <ItemBestOffersArray><ItemBestOffers>
            <Role>Seller</Role><Item><ItemID>456</ItemID></Item>
            <BestOfferArray>
              <BestOffer><Status>Active</Status></BestOffer>
              <BestOffer><Status>Countered</Status></BestOffer>
              <BestOffer><Status>Declined</Status></BestOffer>
            </BestOfferArray>
          </ItemBestOffers></ItemBestOffersArray>
        </GetBestOffersResponse>
        """
    )
    assert active_offers_by_item_id(offers) == {"456": 2}
    assert pagination_total_pages(offers) == 1

    paginated_offers = _root(
        f"""
        <GetBestOffersResponse xmlns="{EBAY_XML_NS}">
          <PaginationResult><TotalNumberOfPages>3</TotalNumberOfPages></PaginationResult>
        </GetBestOffersResponse>
        """
    )
    assert pagination_total_pages(paginated_offers) == 3
    assert "<PageNumber>2</PageNumber>" in build_get_best_offers_xml(page=2)


def test_analytics_views_and_summary_are_bounded() -> None:
    assert analytics_views_by_item_id(
        {
            "records": [
                {
                    "dimensionValues": [{"value": "789"}],
                    "metricValues": [{"value": "42", "applicable": True}],
                }
            ]
        }
    ) == {"789": 42}

    item = {
        "kind": "selling",
        "item_id": "789",
        "title": "Summary",
        "current_price": 9.99,
        "currency": "USD",
        "bid_count": 2,
        "watchers": 5,
        "views": 42,
        "offers": 1,
        "questions": 0,
        "quantity_sold": 1,
        "seconds_left": 300,
        "end_time": datetime.now(timezone.utc) + timedelta(minutes=5),
        "url": "https://example.test/789",
    }
    summary = summarize_payload({}, {}, {"789": item}, 3600)
    assert summary["active_selling_items"] == 1
    assert summary["selling_total_bids"] == 2
    assert summary["selling_ending_soon"] == 1
    assert len(summary["items_with_offers"]) == 1


def test_summary_ending_soon_excludes_already_ended_items() -> None:
    summary = summarize_payload(
        watched={
            "ended": {
                "item_id": "ended",
                "title": "Ended",
                "seconds_left": 0,
                "end_time": datetime.now(timezone.utc) - timedelta(minutes=5),
            },
            "soon": {
                "item_id": "soon",
                "title": "Soon",
                "seconds_left": 120,
                "end_time": datetime.now(timezone.utc) + timedelta(minutes=2),
            },
        },
        bidding={},
        selling={
            "ended-selling": {
                "item_id": "ended-selling",
                "title": "Ended selling",
                "seconds_left": 0,
                "end_time": datetime.now(timezone.utc) - timedelta(minutes=5),
            }
        },
        ending_soon_threshold_seconds=3600,
    )

    assert summary["watched_ending_soon"] == 1
    assert summary["selling_ending_soon"] == 0
    assert [item["item_id"] for item in summary["items_ending_soon"]] == ["soon"]


def test_oauth_callback_params_and_raw_code() -> None:
    callback_url = "https://example.test/callback?code=v%5E1&state=expected"

    assert extract_authorization_code(callback_url) == "v^1"
    assert extract_oauth_callback_params(callback_url)["state"] == ["expected"]
    assert extract_authorization_code("raw-code") == "raw-code"
    assert extract_oauth_callback_params("raw-code") == {}


def test_access_token_refreshes_near_expiry() -> None:
    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )
            self.tokens = ["first", "second"]

        async def _token_request(self, data: dict[str, str]) -> dict[str, object]:
            return {"access_token": self.tokens.pop(0), "expires_in": 60}

    async def run() -> tuple[str, str]:
        client = Client()
        first = await client.async_get_access_token()
        second = await client.async_get_access_token()
        return first, second

    assert asyncio.run(run()) == ("first", "second")


def test_fetch_data_paginates_buying_selling_and_seller_list_views() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )

        async def async_call_trading_api(
            self, call_name: str, xml_body: str
        ) -> ET.Element:
            page = 2 if "<PageNumber>2</PageNumber>" in xml_body else 1
            if call_name == READ_ONLY_CALL_NAME:
                return _root(
                    f"""
                    <GetMyeBayBuyingResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <WatchList>
                        <PaginationResult><TotalNumberOfPages>2</TotalNumberOfPages></PaginationResult>
                        <ItemArray><Item>
                          <ItemID>w{page}</ItemID><Title>Watch {page}</Title>
                          <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                        </Item></ItemArray>
                      </WatchList>
                      <BidList>
                        <PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>
                      </BidList>
                    </GetMyeBayBuyingResponse>
                    """
                )
            if call_name == SELLING_CALL_NAME:
                return _root(
                    f"""
                    <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <ActiveList>
                        <PaginationResult><TotalNumberOfPages>2</TotalNumberOfPages></PaginationResult>
                        <ItemArray><Item>
                          <ItemID>s{page}</ItemID><Title>Sell {page}</Title>
                          <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                          <SellingStatus><CurrentPrice currencyID="USD">{page}</CurrentPrice></SellingStatus>
                        </Item></ItemArray>
                      </ActiveList>
                    </GetMyeBaySellingResponse>
                    """
                )
            if call_name == SELLER_LIST_CALL_NAME:
                return _root(
                    f"""
                    <GetSellerListResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <PaginationResult><TotalNumberOfPages>2</TotalNumberOfPages></PaginationResult>
                      <ItemArray><Item><ItemID>s{page}</ItemID><HitCount>{page * 10}</HitCount></Item></ItemArray>
                    </GetSellerListResponse>
                    """
                )
            if call_name == BEST_OFFERS_CALL_NAME:
                return _root(
                    f"""
                    <GetBestOffersResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                    </GetBestOffersResponse>
                    """
                )
            raise AssertionError(call_name)

    async def run() -> dict[str, object]:
        return await Client().async_fetch_data(
            buying_enabled=True,
            selling_enabled=True,
            analytics_enabled=False,
            ending_soon_threshold_seconds=3600,
        )

    payload = asyncio.run(run())

    assert set(payload["watched"]) == {"w1", "w2"}
    assert set(payload["selling"]) == {"s1", "s2"}
    assert payload["selling"]["s1"]["views"] == 10
    assert payload["selling"]["s2"]["views"] == 20


def test_core_pagination_truncation_is_reported() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    call_counts = {
        READ_ONLY_CALL_NAME: 0,
        SELLING_CALL_NAME: 0,
        SELLER_LIST_CALL_NAME: 0,
        BEST_OFFERS_CALL_NAME: 0,
    }

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )

        async def async_call_trading_api(
            self, call_name: str, xml_body: str
        ) -> ET.Element:
            call_counts[call_name] += 1
            page = call_counts[call_name]
            if call_name == READ_ONLY_CALL_NAME:
                return _root(
                    f"""
                    <GetMyeBayBuyingResponse xmlns="{EBAY_XML_NS}">
                      <WatchList>
                        <PaginationResult><TotalNumberOfPages>{MAX_PAGES + 1}</TotalNumberOfPages></PaginationResult>
                        <ItemArray><Item>
                          <ItemID>w{page}</ItemID><Title>Watch {page}</Title>
                          <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                        </Item></ItemArray>
                      </WatchList>
                      <BidList>
                        <PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>
                      </BidList>
                    </GetMyeBayBuyingResponse>
                    """
                )
            if call_name == SELLING_CALL_NAME:
                if "<Sort>EndTime</Sort>" not in xml_body:
                    if (
                        "<Include>true</Include>"
                        in xml_body.split("<SoldList>", 1)[1].split("</SoldList>", 1)[0]
                    ):
                        list_name = "SoldList"
                    else:
                        list_name = "UnsoldList"
                    return _root(
                        f"""
                        <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                          <Ack>Success</Ack>
                          <{list_name}>
                            <PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>
                            <ItemArray/>
                          </{list_name}>
                        </GetMyeBaySellingResponse>
                        """
                    )
                return _root(
                    f"""
                    <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                      <ActiveList>
                        <PaginationResult><TotalNumberOfPages>{MAX_PAGES + 1}</TotalNumberOfPages></PaginationResult>
                        <ItemArray><Item>
                          <ItemID>s{page}</ItemID><Title>Sell {page}</Title>
                          <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                          <SellingStatus><CurrentPrice currencyID="USD">{page}</CurrentPrice></SellingStatus>
                        </Item></ItemArray>
                      </ActiveList>
                    </GetMyeBaySellingResponse>
                    """
                )
            if call_name == SELLER_LIST_CALL_NAME:
                return _root(
                    f"""
                    <GetSellerListResponse xmlns="{EBAY_XML_NS}">
                      <PaginationResult><TotalNumberOfPages>{MAX_PAGES + 1}</TotalNumberOfPages></PaginationResult>
                      <ItemArray><Item><ItemID>s{page}</ItemID><HitCount>{page * 10}</HitCount></Item></ItemArray>
                    </GetSellerListResponse>
                    """
                )
            if call_name == BEST_OFFERS_CALL_NAME:
                return _root(
                    f"""
                    <GetBestOffersResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                    </GetBestOffersResponse>
                    """
                )
            raise AssertionError(call_name)

    async def run() -> dict[str, object]:
        return await Client().async_fetch_data(
            buying_enabled=True,
            selling_enabled=True,
            analytics_enabled=False,
            ending_soon_threshold_seconds=3600,
        )

    payload = asyncio.run(run())

    assert call_counts[READ_ONLY_CALL_NAME] == MAX_PAGES
    assert call_counts[SELLING_CALL_NAME] == MAX_PAGES + 2
    assert call_counts[SELLER_LIST_CALL_NAME] == MAX_PAGES
    assert len(payload["watched"]) == MAX_PAGES
    assert len(payload["selling"]) == MAX_PAGES
    assert payload["partial_failures"] == [
        "buying_truncated",
        "selling_truncated",
        "seller_list_views_truncated",
    ]
    assert payload["truncated_collections"] == {
        "watched": True,
        "bidding": False,
        "selling": True,
        "seller_list_views": True,
        "active_offers": False,
        "orders": False,
        "payment_disputes": False,
        "messages": False,
        "sold_unsold": False,
    }


def test_optional_analytics_errors_become_partial_failure() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )

        async def async_call_trading_api(
            self, call_name: str, xml_body: str
        ) -> ET.Element:
            if call_name == SELLING_CALL_NAME:
                return _root(
                    f"""
                    <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <ActiveList><ItemArray><Item>
                        <ItemID>s1</ItemID><Title>Sell</Title>
                        <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                      </Item></ItemArray></ActiveList>
                    </GetMyeBaySellingResponse>
                    """
                )
            return _root(
                f"""
                <{call_name}Response xmlns="{EBAY_XML_NS}">
                  <Ack>Success</Ack>
                </{call_name}Response>
                """
            )

        async def async_get_traffic_report(
            self, item_ids: list[str]
        ) -> dict[str, object]:
            raise EbayApiError("analytics exploded")

    async def run() -> dict[str, object]:
        return await Client().async_fetch_data(
            buying_enabled=False,
            selling_enabled=True,
            analytics_enabled=True,
            ending_soon_threshold_seconds=3600,
            granted_scopes=DEFAULT_SCOPE,
        )

    payload = asyncio.run(run())

    assert payload["partial_failures"] == ["analytics_views"]


def test_analytics_auth_error_propagates_from_fetch_data() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )

        async def async_call_trading_api(
            self, call_name: str, xml_body: str
        ) -> ET.Element:
            if call_name == SELLING_CALL_NAME:
                return _root(
                    f"""
                    <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <ActiveList><ItemArray><Item>
                        <ItemID>s1</ItemID><Title>Sell</Title>
                        <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                      </Item></ItemArray></ActiveList>
                    </GetMyeBaySellingResponse>
                    """
                )
            return _root(
                f"""
                <{call_name}Response xmlns="{EBAY_XML_NS}">
                  <Ack>Success</Ack>
                </{call_name}Response>
                """
            )

        async def async_get_traffic_report(
            self, item_ids: list[str]
        ) -> dict[str, object]:
            raise EbayAuthError("analytics auth failed")

    async def run() -> dict[str, object]:
        return await Client().async_fetch_data(
            buying_enabled=False,
            selling_enabled=True,
            analytics_enabled=True,
            ending_soon_threshold_seconds=3600,
            granted_scopes=DEFAULT_SCOPE,
        )

    with pytest.raises(EbayAuthError, match="analytics auth failed"):
        asyncio.run(run())


def test_optional_transport_timeout_becomes_partial_failure() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )

        async def async_call_trading_api(
            self, call_name: str, xml_body: str
        ) -> ET.Element:
            if call_name == SELLING_CALL_NAME:
                return _root(
                    f"""
                    <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <ActiveList><ItemArray><Item>
                        <ItemID>s1</ItemID><Title>Sell</Title>
                        <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                      </Item></ItemArray></ActiveList>
                    </GetMyeBaySellingResponse>
                    """
                )
            if call_name == SELLER_LIST_CALL_NAME:
                raise TimeoutError
            if call_name == BEST_OFFERS_CALL_NAME:
                return _root(
                    f"""
                    <GetBestOffersResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                    </GetBestOffersResponse>
                    """
                )
            raise AssertionError(call_name)

    async def run() -> dict[str, object]:
        return await Client().async_fetch_data(
            buying_enabled=False,
            selling_enabled=True,
            analytics_enabled=False,
            ending_soon_threshold_seconds=3600,
        )

    payload = asyncio.run(run())

    assert payload["partial_failures"] == ["seller_list_views"]


def test_analytics_views_do_not_overwrite_seller_list_views() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )

        async def async_call_trading_api(
            self, call_name: str, xml_body: str
        ) -> ET.Element:
            if call_name == SELLING_CALL_NAME:
                return _root(
                    f"""
                    <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <ActiveList><ItemArray><Item>
                        <ItemID>s1</ItemID><Title>Sell</Title>
                        <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                      </Item></ItemArray></ActiveList>
                    </GetMyeBaySellingResponse>
                    """
                )
            if call_name == SELLER_LIST_CALL_NAME:
                return _root(
                    f"""
                    <GetSellerListResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <ItemArray><Item><ItemID>s1</ItemID><HitCount>10</HitCount></Item></ItemArray>
                    </GetSellerListResponse>
                    """
                )
            if call_name == BEST_OFFERS_CALL_NAME:
                return _root(
                    f"""
                    <GetBestOffersResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                    </GetBestOffersResponse>
                    """
                )
            raise AssertionError(call_name)

        async def async_get_traffic_report(
            self, item_ids: list[str]
        ) -> dict[str, object]:
            return {
                "records": [
                    {
                        "dimensionValues": [{"value": "s1"}],
                        "metricValues": [{"value": "99"}],
                    }
                ]
            }

    async def run() -> dict[str, object]:
        return await Client().async_fetch_data(
            buying_enabled=False,
            selling_enabled=True,
            analytics_enabled=True,
            ending_soon_threshold_seconds=3600,
            granted_scopes=DEFAULT_SCOPE,
        )

    payload = asyncio.run(run())

    assert payload["selling"]["s1"]["views"] == 10
    assert payload["selling"]["s1"]["analytics_views_30d"] == 99


def test_token_request_wraps_non_json_response() -> None:
    client = EbayApiClient(
        _TokenSession(),
        environment="production",
        client_id="client",
        client_secret="secret",
        runame="runame",
        refresh_token=None,
        site_id="0",
    )

    async def request() -> None:
        await client._token_request({"grant_type": "authorization_code"})

    with pytest.raises(OAuth2TokenRequestError, match="not valid JSON; HTTP 502"):
        asyncio.run(request())


def test_fetch_data_paginates_best_offers() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    selling = _root(
        f"""
        <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
          <ActiveList><ItemArray><Item>
            <ItemID>456</ItemID><Title>Sell</Title>
            <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
            <SellingStatus><CurrentPrice currencyID="USD">20</CurrentPrice></SellingStatus>
            <BestOfferDetails><BestOfferCount>99</BestOfferCount></BestOfferDetails>
          </Item></ItemArray></ActiveList>
        </GetMyeBaySellingResponse>
        """
    )
    seller_list = _root(f"""<GetSellerListResponse xmlns="{EBAY_XML_NS}" />""")
    offer_pages = [
        _root(
            f"""
            <GetBestOffersResponse xmlns="{EBAY_XML_NS}">
              <PaginationResult><TotalNumberOfPages>2</TotalNumberOfPages></PaginationResult>
              <ItemBestOffersArray><ItemBestOffers>
                <Role>Seller</Role><Item><ItemID>456</ItemID></Item>
                <BestOfferArray><BestOffer><Status>Active</Status></BestOffer></BestOfferArray>
              </ItemBestOffers></ItemBestOffersArray>
            </GetBestOffersResponse>
            """
        ),
        _root(
            f"""
            <GetBestOffersResponse xmlns="{EBAY_XML_NS}">
              <PaginationResult><TotalNumberOfPages>2</TotalNumberOfPages></PaginationResult>
              <ItemBestOffersArray><ItemBestOffers>
                <Role>Seller</Role><Item><ItemID>456</ItemID></Item>
                <BestOfferArray><BestOffer><Status>Countered</Status></BestOffer></BestOfferArray>
              </ItemBestOffers></ItemBestOffersArray>
            </GetBestOffersResponse>
            """
        ),
    ]
    calls: list[tuple[str, str]] = []
    client = EbayApiClient(
        _TokenSession(),
        environment="production",
        client_id="client",
        client_secret="secret",
        runame="runame",
        refresh_token=None,
        site_id="0",
    )

    async def call_trading_api(call_name: str, xml_body: str) -> ET.Element:
        calls.append((call_name, xml_body))
        if call_name == SELLING_CALL_NAME:
            return selling
        if call_name == SELLER_LIST_CALL_NAME:
            return seller_list
        if call_name == BEST_OFFERS_CALL_NAME:
            return offer_pages.pop(0)
        raise AssertionError(f"Unexpected call: {call_name}")

    client.async_call_trading_api = call_trading_api

    async def fetch() -> dict[str, Any]:
        return await client.async_fetch_data(
            buying_enabled=False,
            selling_enabled=True,
            analytics_enabled=False,
            ending_soon_threshold_seconds=3600,
        )

    payload = asyncio.run(fetch())

    assert payload["selling"]["456"]["offers"] == 2
    best_offer_calls = [
        body for call_name, body in calls if call_name == BEST_OFFERS_CALL_NAME
    ]
    assert len(best_offer_calls) == 2
    assert "<PageNumber>1</PageNumber>" in best_offer_calls[0]
    assert "<PageNumber>2</PageNumber>" in best_offer_calls[1]


def test_parse_sold_unsold_classification_prefers_sold() -> None:
    from custom_components.ebay.api import parse_sold_unsold_classification

    root = _root(
        f"""
        <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
          <Ack>Success</Ack>
          <SoldList>
            <ItemArray>
              <Item><ItemID>both</ItemID></Item>
              <Item><ItemID>sold-only</ItemID></Item>
            </ItemArray>
          </SoldList>
          <UnsoldList>
            <ItemArray>
              <Item><ItemID>both</ItemID></Item>
              <Item><ItemID>unsold-only</ItemID></Item>
            </ItemArray>
          </UnsoldList>
        </GetMyeBaySellingResponse>
        """
    )
    classified = parse_sold_unsold_classification(root)
    assert classified == {
        "both": "sold",
        "sold-only": "sold",
        "unsold-only": "unsold",
    }


def test_build_selling_list_xml_includes_duration() -> None:
    from custom_components.ebay.api import (
        SOLD_UNSOLD_DURATION_DAYS,
        build_get_my_ebay_selling_list_xml,
    )

    sold_xml = build_get_my_ebay_selling_list_xml("SoldList", page=2)
    assert "<SoldList>" in sold_xml
    assert "<Include>true</Include>" in sold_xml
    assert f"<DurationInDays>{SOLD_UNSOLD_DURATION_DAYS}</DurationInDays>" in sold_xml
    assert "<PageNumber>2</PageNumber>" in sold_xml
    assert "<UnsoldList><Include>false</Include></UnsoldList>" in sold_xml


def test_sold_unsold_classification_fetch_soft_fails() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )

        async def async_call_trading_api(
            self, call_name: str, xml_body: str
        ) -> ET.Element:
            if call_name != SELLING_CALL_NAME:
                return _root(
                    f"""
                    <{call_name}Response xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                    </{call_name}Response>
                    """
                )
            if "<Sort>EndTime</Sort>" in xml_body:
                return _root(
                    f"""
                    <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <ActiveList><ItemArray><Item>
                        <ItemID>s1</ItemID><Title>Sell</Title>
                        <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                      </Item></ItemArray></ActiveList>
                    </GetMyeBaySellingResponse>
                    """
                )
            raise EbayApiError("classification unavailable")

    async def run() -> dict[str, object]:
        return await Client().async_fetch_data(
            buying_enabled=False,
            selling_enabled=True,
            analytics_enabled=False,
            ending_soon_threshold_seconds=3600,
        )

    payload = asyncio.run(run())
    assert "s1" in payload["selling"]
    assert "sold_unsold_classification" in payload["partial_failures"]
    assert payload["summary"]["sold_items_count"] is None
    assert payload["selling_classification"] == {}


def test_sold_unsold_classification_fetch_counts() -> None:
    end_time = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    class Client(EbayApiClient):
        def __init__(self) -> None:
            super().__init__(
                None,  # type: ignore[arg-type]
                environment="production",
                client_id="client",
                client_secret="secret",
                runame="runame",
                refresh_token="refresh",
                site_id="0",
            )

        async def async_call_trading_api(
            self, call_name: str, xml_body: str
        ) -> ET.Element:
            if call_name != SELLING_CALL_NAME:
                return _root(
                    f"""
                    <{call_name}Response xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                    </{call_name}Response>
                    """
                )
            if (
                "<SoldList>" in xml_body
                and "<Include>true</Include>"
                in xml_body.split("<SoldList>")[1].split("</SoldList>")[0]
            ):
                return _root(
                    f"""
                    <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <SoldList>
                        <PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>
                        <ItemArray>
                          <Item><ItemID>sold1</ItemID></Item>
                          <Item><ItemID>sold2</ItemID></Item>
                        </ItemArray>
                      </SoldList>
                    </GetMyeBaySellingResponse>
                    """
                )
            if (
                "<UnsoldList>" in xml_body
                and "<Include>true</Include>"
                in xml_body.split("<UnsoldList>")[1].split("</UnsoldList>")[0]
            ):
                return _root(
                    f"""
                    <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                      <Ack>Success</Ack>
                      <UnsoldList>
                        <PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>
                        <ItemArray>
                          <Item><ItemID>unsold1</ItemID></Item>
                        </ItemArray>
                      </UnsoldList>
                    </GetMyeBaySellingResponse>
                    """
                )
            return _root(
                f"""
                <GetMyeBaySellingResponse xmlns="{EBAY_XML_NS}">
                  <Ack>Success</Ack>
                  <ActiveList><ItemArray><Item>
                    <ItemID>s1</ItemID><Title>Sell</Title>
                    <ListingDetails><EndTime>{end_time}</EndTime></ListingDetails>
                  </Item></ItemArray></ActiveList>
                </GetMyeBaySellingResponse>
                """
            )

    async def run() -> dict[str, object]:
        return await Client().async_fetch_data(
            buying_enabled=False,
            selling_enabled=True,
            analytics_enabled=False,
            ending_soon_threshold_seconds=3600,
        )

    payload = asyncio.run(run())
    assert payload["summary"]["sold_items_count"] == 2
    assert payload["summary"]["unsold_items_count"] == 1
    assert payload["selling_classification"] == {
        "sold1": "sold",
        "sold2": "sold",
        "unsold1": "unsold",
    }
    assert payload["sold_item_ids"] == ["sold1", "sold2"]
    assert payload["unsold_item_ids"] == ["unsold1"]
