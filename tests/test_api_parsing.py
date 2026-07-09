"""Tests for eBay API parsing helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

from custom_components.ebay.api import (
    EBAY_XML_NS,
    active_offers_by_item_id,
    analytics_views_by_item_id,
    parse_container,
    parse_selling_container,
    seller_list_views_by_item_id,
    summarize_payload,
)


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


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
