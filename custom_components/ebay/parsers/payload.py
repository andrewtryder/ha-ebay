"""Coordinator payload summary helpers."""

from __future__ import annotations

from typing import Any


def _dict_by_item_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["item_id"]): item for item in items if item.get("item_id")}


def summarize_payload(
    watched: dict[str, dict[str, Any]],
    bidding: dict[str, dict[str, Any]],
    selling: dict[str, dict[str, Any]],
    ending_soon_threshold_seconds: int,
) -> dict[str, Any]:
    """Build bounded summary metrics and attributes."""

    def under_threshold(item: dict[str, Any]) -> bool:
        seconds_left = item.get("seconds_left")
        return (
            seconds_left is not None
            and 0 < seconds_left <= ending_soon_threshold_seconds
        )

    selling_items = list(selling.values())
    watched_items = list(watched.values())
    items_with_bids = [
        item for item in selling_items if (item.get("bid_count") or 0) > 0
    ]
    items_with_offers = [
        item for item in selling_items if (item.get("offers") or 0) > 0
    ]
    items_with_questions = [
        item for item in selling_items if (item.get("questions") or 0) > 0
    ]
    selling_ending = [item for item in selling_items if under_threshold(item)]
    watched_ending = [item for item in watched_items if under_threshold(item)]

    def small(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "item_id": item.get("item_id"),
            "title": item.get("title"),
            "price": item.get("current_price"),
            "currency": item.get("currency"),
            "end_time": item.get("end_time").isoformat()
            if item.get("end_time")
            else None,
            "seconds_left": item.get("seconds_left"),
            "url": item.get("url"),
        }

    highest_watchers = max(
        selling_items, key=lambda item: item.get("watchers") or -1, default=None
    )
    highest_views = max(
        selling_items, key=lambda item: item.get("views") or -1, default=None
    )
    return {
        "active_selling_items": len(selling),
        "watched_items": len(watched),
        "bidding_items": len(bidding),
        "selling_total_bids": sum(item.get("bid_count") or 0 for item in selling_items),
        "selling_total_offers": sum(item.get("offers") or 0 for item in selling_items),
        "active_listings_quantity_sold": sum(
            item.get("quantity_sold") or 0 for item in selling_items
        ),
        "selling_total_watchers": sum(
            item.get("watchers") or 0 for item in selling_items
        ),
        "selling_total_views": sum(item.get("views") or 0 for item in selling_items),
        "watched_ending_soon": len(watched_ending),
        "selling_ending_soon": len(selling_ending),
        "items_ending_soon": [
            small(item) for item in (watched_ending + selling_ending)[:10]
        ],
        "items_with_offers": [small(item) for item in items_with_offers[:10]],
        "items_with_bids": [small(item) for item in items_with_bids[:10]],
        "items_with_questions": [small(item) for item in items_with_questions[:10]],
        "highest_watcher_count_item": small(highest_watchers)
        if highest_watchers
        else None,
        "highest_view_count_item": small(highest_views) if highest_views else None,
    }
