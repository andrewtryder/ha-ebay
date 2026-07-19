"""Parse Trading API item containers and views maps."""

from __future__ import annotations

from typing import Any
import xml.etree.ElementTree as ET

from ..const import NS
from .xml import (
    _first_text,
    _money,
    _text,
    _to_bool,
    _to_int,
    format_seconds,
    parse_ebay_time,
    seconds_until,
)


def _base_item(item: ET.Element, kind: str) -> dict[str, Any]:
    end_time_text = _first_text(item, ["e:ListingDetails/e:EndTime", "e:EndTime"])
    end_time = parse_ebay_time(end_time_text)
    seconds_left = seconds_until(end_time)
    price, currency = _money(item, "e:SellingStatus/e:CurrentPrice")
    if price is None:
        price, currency = _money(item, "e:StartPrice")

    return {
        "kind": kind,
        "item_id": _text(item, "e:ItemID"),
        "title": _text(item, "e:Title"),
        "listing_type": _text(item, "e:ListingType"),
        "end_time": end_time,
        "seconds_left": seconds_left,
        "time_left": format_seconds(seconds_left),
        "current_price": price,
        "currency": currency,
        "bid_count": _to_int(_text(item, "e:SellingStatus/e:BidCount")),
        "watchers": None,
        "views": None,
        "analytics_views_30d": None,
        "offers": None,
        "questions": None,
        "quantity": None,
        "quantity_available": None,
        "quantity_sold": None,
        "max_bid": None,
        "winning": None,
        "url": _first_text(
            item,
            [
                "e:ListingDetails/e:ViewItemURL",
                "e:ListingDetails/e:ViewItemURLForNaturalSearch",
                "e:ViewItemURL",
            ],
        ),
        "image": _first_text(item, ["e:PictureDetails/e:GalleryURL", "e:GalleryURL"]),
    }


def parse_buying_item(item: ET.Element, kind: str) -> dict[str, Any]:
    """Parse a WatchList or BidList item."""
    data = _base_item(item, kind)
    if kind == "bidding":
        max_bid, _ = _money(item, "e:BiddingDetails/e:MaxBid")
        data["max_bid"] = max_bid
        data["winning"] = _to_bool(_text(item, "e:BiddingDetails/e:Winning"))
    return data


def parse_selling_item(item: ET.Element) -> dict[str, Any]:
    """Parse an active selling item."""
    data = _base_item(item, "selling")
    data.update(
        {
            "quantity": _to_int(_text(item, "e:Quantity")),
            "quantity_available": _to_int(_text(item, "e:QuantityAvailable")),
            "quantity_sold": _to_int(_text(item, "e:SellingStatus/e:QuantitySold")),
            "views": _to_int(_text(item, "e:HitCount")),
            "watchers": _to_int(_text(item, "e:WatchCount")),
            "questions": _to_int(_text(item, "e:QuestionCount")),
            "offers": _to_int(_text(item, "e:BestOfferDetails/e:BestOfferCount")),
        }
    )
    return data


def parse_container(
    root: ET.Element, container_name: str, kind: str
) -> list[dict[str, Any]]:
    """Parse a buying response container."""
    container = root.find(f"e:{container_name}", namespaces=NS)
    if container is None:
        return []
    return [
        parse_buying_item(item, kind)
        for item in container.findall("e:ItemArray/e:Item", namespaces=NS)
        if _text(item, "e:ItemID")
    ]


def parse_selling_container(root: ET.Element) -> list[dict[str, Any]]:
    """Parse the active selling response container."""
    container = root.find("e:ActiveList", namespaces=NS)
    if container is None:
        return []
    return [
        parse_selling_item(item)
        for item in container.findall("e:ItemArray/e:Item", namespaces=NS)
        if _text(item, "e:ItemID")
    ]


def seller_list_views_by_item_id(root: ET.Element) -> dict[str, int]:
    """Parse GetSellerList hit counts."""
    views: dict[str, int] = {}
    for item in root.findall("e:ItemArray/e:Item", namespaces=NS):
        item_id = _text(item, "e:ItemID")
        hit_count = _to_int(_text(item, "e:HitCount"))
        if item_id and hit_count is not None:
            views[item_id] = hit_count
    return views


def analytics_views_by_item_id(payload: dict[str, Any]) -> dict[str, int]:
    """Parse Sell Analytics traffic report views."""
    views: dict[str, int] = {}
    for record in payload.get("records", []):
        dimension_values = record.get("dimensionValues") or []
        metric_values = record.get("metricValues") or []
        if not dimension_values or not metric_values:
            continue
        item_id = str(dimension_values[0].get("value") or "")
        value = _to_int(metric_values[0].get("value"))
        applicable = metric_values[0].get("applicable", True)
        if item_id and applicable and value is not None:
            views[item_id] = value
    return views


def active_offers_by_item_id(root: ET.Element) -> dict[str, int]:
    """Parse active best offers by item id."""
    offers: dict[str, int] = {}
    for item_offers in root.findall(
        "e:ItemBestOffersArray/e:ItemBestOffers", namespaces=NS
    ):
        role = _text(item_offers, "e:Role")
        if role and role != "Seller":
            continue
        item_id = _text(item_offers, "e:Item/e:ItemID")
        if not item_id:
            continue
        count = 0
        for offer in item_offers.findall("e:BestOfferArray/e:BestOffer", namespaces=NS):
            status = _text(offer, "e:Status")
            if status in {None, "Active", "Countered", "Pending"}:
                count += 1
        offers[item_id] = offers.get(item_id, 0) + count
    return offers


def pagination_total_pages(root: ET.Element) -> int:
    """Parse Trading API PaginationResult total pages."""
    total_pages = _to_int(_text(root, "e:PaginationResult/e:TotalNumberOfPages"))
    return max(1, total_pages or 1)
