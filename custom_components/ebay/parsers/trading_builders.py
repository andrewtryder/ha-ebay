"""Build Trading API XML requests and sold/unsold helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import xml.etree.ElementTree as ET

from ..const import COMPATIBILITY_LEVEL, EBAY_XML_NS, NS
from .xml import _text, ebay_time

READ_ONLY_CALL_NAME = "GetMyeBayBuying"
SELLING_CALL_NAME = "GetMyeBaySelling"
SELLER_LIST_CALL_NAME = "GetSellerList"
BEST_OFFERS_CALL_NAME = "GetBestOffers"
GET_USER_CALL_NAME = "GetUser"
BEST_OFFERS_ENTRIES_PER_PAGE = 200
BEST_OFFERS_MAX_PAGES = 25


def build_get_my_ebay_buying_xml(entries_per_page: int = 100, page: int = 1) -> str:
    """Build GetMyeBayBuying XML."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingRequest xmlns="{EBAY_XML_NS}">
  <Version>{COMPATIBILITY_LEVEL}</Version>
  <WatchList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
    <Sort>EndTime</Sort>
  </WatchList>
  <BidList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
    <Sort>EndTime</Sort>
  </BidList>
</GetMyeBayBuyingRequest>
"""


def build_get_my_ebay_selling_xml(entries_per_page: int = 100, page: int = 1) -> str:
    """Build GetMyeBaySelling XML."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <ActiveList>
    <Include>true</Include>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
    <Sort>EndTime</Sort>
  </ActiveList>
  <ScheduledList><Include>false</Include></ScheduledList>
  <SoldList><Include>false</Include></SoldList>
  <UnsoldList><Include>false</Include></UnsoldList>
  <SellingSummary><Include>true</Include></SellingSummary>
</GetMyeBaySellingRequest>
"""


SOLD_UNSOLD_DURATION_DAYS = 60
SOLD_UNSOLD_REFRESH_HOURS = 6


def _parse_optional_datetime(value: Any) -> datetime | None:
    """Parse an optional datetime or ISO string."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _should_fetch_sold_unsold(
    previous_selling: dict[str, Any],
    current_selling: dict[str, Any],
    last_fetched: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether SoldList/UnsoldList classification should be refreshed."""
    if set(previous_selling) - set(current_selling):
        return True
    if last_fetched is None:
        return True
    stamp = now or datetime.now(timezone.utc)
    return stamp - last_fetched >= timedelta(hours=SOLD_UNSOLD_REFRESH_HOURS)


def build_get_my_ebay_selling_list_xml(
    list_name: str,
    *,
    entries_per_page: int = 100,
    page: int = 1,
    duration_in_days: int = SOLD_UNSOLD_DURATION_DAYS,
) -> str:
    """Build GetMyeBaySelling XML for one SoldList or UnsoldList page."""
    if list_name not in {"SoldList", "UnsoldList"}:
        raise ValueError(f"Unsupported selling list: {list_name}")
    sold_block = (
        f"""  <SoldList>
    <Include>true</Include>
    <DurationInDays>{duration_in_days}</DurationInDays>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </SoldList>"""
        if list_name == "SoldList"
        else "  <SoldList><Include>false</Include></SoldList>"
    )
    unsold_block = (
        f"""  <UnsoldList>
    <Include>true</Include>
    <DurationInDays>{duration_in_days}</DurationInDays>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </UnsoldList>"""
        if list_name == "UnsoldList"
        else "  <UnsoldList><Include>false</Include></UnsoldList>"
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <ActiveList><Include>false</Include></ActiveList>
  <ScheduledList><Include>false</Include></ScheduledList>
{sold_block}
{unsold_block}
</GetMyeBaySellingRequest>
"""


def build_get_my_ebay_selling_classification_xml(
    entries_per_page: int = 100,
    page: int = 1,
    *,
    duration_in_days: int = SOLD_UNSOLD_DURATION_DAYS,
) -> str:
    """Build a combined sold/unsold lookup request (single page of each list)."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <ActiveList><Include>false</Include></ActiveList>
  <ScheduledList><Include>false</Include></ScheduledList>
  <SoldList>
    <Include>true</Include>
    <DurationInDays>{duration_in_days}</DurationInDays>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </SoldList>
  <UnsoldList>
    <Include>true</Include>
    <DurationInDays>{duration_in_days}</DurationInDays>
    <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  </UnsoldList>
</GetMyeBaySellingRequest>
"""


def parse_sold_unsold_classification(root: ET.Element) -> dict[str, str]:
    """Parse sold/unsold ItemIDs from a GetMyeBaySelling response.

    Unsold is applied first, then sold, so SoldList wins if an ItemID appears in both.
    """
    classified: dict[str, str] = {}
    for container_name, status in (("UnsoldList", "unsold"), ("SoldList", "sold")):
        container = root.find(f"e:{container_name}", namespaces=NS)
        if container is None:
            continue
        for item in container.findall("e:ItemArray/e:Item", namespaces=NS):
            item_id = _text(item, "e:ItemID")
            if item_id:
                classified[item_id] = status
    return classified


def parse_selling_list_item_ids(root: ET.Element, list_name: str) -> list[str]:
    """Return ItemIDs from one SoldList or UnsoldList container."""
    container = root.find(f"e:{list_name}", namespaces=NS)
    if container is None:
        return []
    item_ids: list[str] = []
    for item in container.findall("e:ItemArray/e:Item", namespaces=NS):
        item_id = _text(item, "e:ItemID")
        if item_id:
            item_ids.append(item_id)
    return item_ids


def build_get_seller_list_xml(entries_per_page: int = 200, page: int = 1) -> str:
    """Build GetSellerList XML for active listings."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=120)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetSellerListRequest xmlns="{EBAY_XML_NS}">
  <GranularityLevel>Fine</GranularityLevel>
  <EndTimeFrom>{ebay_time(now)}</EndTimeFrom>
  <EndTimeTo>{ebay_time(end)}</EndTimeTo>
  <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
</GetSellerListRequest>
"""


def build_get_best_offers_xml(
    entries_per_page: int = BEST_OFFERS_ENTRIES_PER_PAGE,
    page: int = 1,
) -> str:
    """Build GetBestOffers XML."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetBestOffersRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnAll</DetailLevel>
  <BestOfferStatus>Active</BestOfferStatus>
  <Pagination><EntriesPerPage>{entries_per_page}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
</GetBestOffersRequest>
"""


def build_get_user_xml() -> str:
    """Build GetUser XML for the OAuth-authenticated account."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<GetUserRequest xmlns="{EBAY_XML_NS}">
  <DetailLevel>ReturnSummary</DetailLevel>
</GetUserRequest>
"""
