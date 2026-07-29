"""Domain event detection and emission for eBay coordinator payloads.

Distinct from the Home Assistant event platform module (event.py).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .const import (
    CONF_PINNED_ITEM_PRICE_TARGETS,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
    LEGACY_COMPATIBILITY_EVENT_TYPES,
    TITLE_HISTORY_MAX_LEN,
)
from .options_parse import parse_price_targets, to_decimal

_LOGGER = logging.getLogger(__name__)


def _normalize_currency(value: Any) -> str | None:
    """Return an uppercase currency code, or None if blank."""
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _currencies_compatible(left: str | None, right: str | None) -> bool:
    """Return whether two currency codes may be compared."""
    if left is None and right is None:
        return True
    return bool(left and right and left == right)


def _percent_change(old_price: Decimal, new_price: Decimal) -> Decimal | None:
    """Return percent change from old to new, or None when undefined."""
    if old_price == 0:
        return None
    return ((new_price - old_price) / old_price) * Decimal(100)


def _decimal_payload(value: Decimal | None) -> float | int | None:
    """Serialize Decimal for event payloads as int when whole, else float."""
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _collection_truncated(
    previous: dict[str, Any], current: dict[str, Any], key: str
) -> bool:
    """Return whether either snapshot reports the collection as truncated."""
    for payload in (previous, current):
        if (payload.get("truncated_collections") or {}).get(key):
            return True
    return False


def _was_running(item: dict[str, Any]) -> bool:
    """Return whether an item had positive time remaining."""
    seconds_left = item.get("seconds_left")
    return seconds_left is not None and seconds_left > 0


def _is_active_ending_soon(item: dict[str, Any], threshold: int, now: datetime) -> bool:
    """Return whether an item is still active and inside the ending-soon window."""
    end_time = item.get("end_time")
    if not isinstance(end_time, datetime):
        return False
    seconds_left = _seconds_until(end_time, now)
    return 0 < seconds_left <= threshold


def _with_current_seconds_left(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Return event item data with seconds_left calculated for the emission time."""
    end_time = item.get("end_time")
    if not isinstance(end_time, datetime):
        return item
    return {**item, "seconds_left": _seconds_until(end_time, now)}


def _seconds_until(end_time: datetime, now: datetime) -> int:
    """Return whole seconds from now until end_time."""
    return max(0, int((end_time - now.astimezone(timezone.utc)).total_seconds()))


class EbayEventMixin:
    """Detect and emit eBay domain events from coordinator payload diffs."""

    def _emit(
        self,
        kind: str,
        event_type: str,
        item: dict[str, Any],
        *,
        record_history: bool | None = None,
        history_key: tuple[str, str, str, int, str] | None = None,
        **extra: Any,
    ) -> bool:
        """Emit an event to the entity callback and optionally record history.

        Returns True when the EventEntity callback received the event. History is
        recorded for canonical events even when no callback is registered.
        """
        if record_history is None:
            record_history = event_type not in LEGACY_COMPATIBILITY_EVENT_TYPES
        event_data = self._event_data(event_type, item, kind, extra)
        if record_history and (
            history_key is None or history_key not in self._recent_history_ending_soon
        ):
            self._append_recent_event(kind, event_data)
            if history_key is not None:
                self._recent_history_ending_soon.add(history_key)
        callback_func = self._event_callbacks.get(kind)
        if not callback_func:
            return False
        callback_func(event_type, event_data)
        _LOGGER.debug(
            "Emitted eBay event kind=%s event_type=%s item_id=%s",
            kind,
            event_type,
            item.get("item_id"),
        )
        return True

    def _append_recent_event(self, kind: str, event_data: dict[str, Any]) -> None:
        """Append a bounded safe summary of a canonical event."""
        history = self._recent_events.get(kind)
        if history is None:
            return
        title = event_data.get("title")
        if isinstance(title, str) and len(title) > TITLE_HISTORY_MAX_LEN:
            title = title[: TITLE_HISTORY_MAX_LEN - 1] + "…"
        entry = {
            "event_type": event_data.get("event_type"),
            "kind": event_data.get("kind"),
            "item_id": event_data.get("item_id"),
            "title": title,
            "detected_at": event_data.get("detected_at"),
            "price": event_data.get("price"),
            "currency": event_data.get("currency"),
            "end_time": event_data.get("end_time"),
            "seconds_left": event_data.get("seconds_left"),
            "url": event_data.get("url"),
        }
        for key in (
            "old_value",
            "new_value",
            "threshold",
            "threshold_currency",
            "threshold_source",
            "percent_change",
            "direction",
            "order_id",
            "listing_id",
            "subject",
            "conversation_id",
            "dispute_id",
            "fulfillment_status",
            "payment_status",
        ):
            if key in event_data:
                entry[key] = event_data[key]
        history.append(entry)

    def _event_data(
        self,
        event_type: str,
        item: dict[str, Any],
        kind: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        end_time = item.get("end_time")
        return {
            "event_type": event_type,
            "item_id": item.get("item_id") or item.get("listing_id"),
            "title": item.get("title") or item.get("subject"),
            "kind": kind,
            "price": item.get("current_price"),
            "currency": item.get("currency"),
            "end_time": end_time.isoformat()
            if isinstance(end_time, datetime)
            else end_time,
            "seconds_left": item.get("seconds_left"),
            "url": item.get("url"),
            "image": item.get("image"),
            "detected_at": datetime.now(timezone.utc).isoformat(),
            **{
                key: value
                for key, value in {
                    "order_id": item.get("order_id"),
                    "listing_id": item.get("listing_id"),
                    "subject": item.get("subject"),
                    "conversation_id": item.get("conversation_id"),
                    "dispute_id": item.get("dispute_id"),
                    "fulfillment_status": item.get("fulfillment_status"),
                    "payment_status": item.get("payment_status"),
                }.items()
                if value is not None
            },
            **extra,
        }

    def _detect_transition_events(
        self, previous: dict[str, Any] | None, current: dict[str, Any]
    ) -> None:
        if previous is None:
            return
        previous_sold_ids_raw = previous.get("sold_item_ids")
        self._detect_selling_events(
            previous["selling"],
            current["selling"],
            classification=current.get("selling_classification") or {},
            previous_sold_ids=(
                None if previous_sold_ids_raw is None else set(previous_sold_ids_raw)
            ),
            current_sold_ids=set(current.get("sold_item_ids") or []),
            classification_incomplete=_collection_truncated(
                previous, current, "sold_unsold"
            )
            or "sold_unsold_classification" in (current.get("partial_failures") or []),
            suppress_incomplete=_collection_truncated(previous, current, "selling"),
        )
        self._detect_watching_events(
            previous["watched"],
            current["watched"],
            suppress_incomplete=_collection_truncated(previous, current, "watched"),
        )
        self._detect_bidding_events(
            previous["bidding"],
            current["bidding"],
            suppress_incomplete=_collection_truncated(previous, current, "bidding"),
        )
        self._detect_seller_ops_events(
            previous.get("seller_ops") or {},
            current.get("seller_ops") or {},
            suppress_orders=_collection_truncated(previous, current, "orders"),
            suppress_disputes=_collection_truncated(
                previous, current, "payment_disputes"
            ),
            suppress_messages=_collection_truncated(previous, current, "messages"),
        )

    def _detect_selling_events(
        self,
        previous: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        *,
        classification: dict[str, str] | None = None,
        previous_sold_ids: set[str] | None = None,
        current_sold_ids: set[str] | None = None,
        classification_incomplete: bool = False,
        suppress_incomplete: bool = False,
        suppress_disappearance: bool | None = None,
    ) -> None:
        if suppress_disappearance is None:
            suppress_disappearance = suppress_incomplete
        classification = classification or {}
        current_sold_ids = current_sold_ids or set()
        sold_via_disappearance: set[str] = set()

        for item_id, item in current.items():
            old = previous.get(item_id)
            if not old:
                if not suppress_incomplete:
                    self._emit("selling", "selling_item_added", item)
                continue
            self._emit_if_increased(
                "selling", "bid_count_increased", old, item, "bid_count"
            )
            self._emit_if_increased("selling", "offer_received", old, item, "offers")
            self._emit_if_increased(
                "selling", "question_received", old, item, "questions"
            )
            self._emit_if_increased(
                "selling", "watcher_count_increased", old, item, "watchers"
            )
            self._emit_if_increased(
                "selling", "quantity_sold_increased", old, item, "quantity_sold"
            )
        if not suppress_disappearance:
            for item_id, old in previous.items():
                if item_id in current:
                    continue
                status = classification.get(item_id)
                if classification_incomplete or status is None:
                    self._emit("selling", "item_disappeared_unknown", old)
                elif status == "sold":
                    self._emit("selling", "item_sold", old)
                    sold_via_disappearance.add(item_id)
                elif status == "unsold":
                    self._emit("selling", "item_ended_unsold", old)
                else:
                    self._emit("selling", "item_disappeared_unknown", old)

        if (
            classification_incomplete
            or suppress_incomplete
            or previous_sold_ids is None
        ):
            return
        for item_id in sorted(current_sold_ids - previous_sold_ids):
            if item_id in sold_via_disappearance:
                continue
            item = current.get(item_id) or previous.get(item_id) or {"item_id": item_id}
            self._emit("selling", "item_sale_completed", item)

    def _detect_watching_events(
        self,
        previous: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        *,
        suppress_incomplete: bool = False,
        suppress_disappearance: bool | None = None,
    ) -> None:
        if suppress_disappearance is None:
            suppress_disappearance = suppress_incomplete
        for item_id, item in current.items():
            old = previous.get(item_id)
            if not old:
                if not suppress_incomplete:
                    self._emit("watching", "watched_item_added", item)
                self._normalize_price_drop_active_for_item(item)
                continue
            self._detect_watched_price_events(
                old, item, allow_dropped_below=not suppress_incomplete
            )
            self._detect_watched_bid_count_events(old, item)
            if _was_running(old) and item.get("seconds_left") == 0:
                self._emit("watching", "watched_item_ended", item)
        if suppress_disappearance:
            return
        for item_id, old in previous.items():
            if item_id not in current and _was_running(old):
                # WatchList APIs do not affirm manual unwatch; never emit
                # watched_item_removed with the current read-only buying APIs.
                self._price_drop_below_active.discard(item_id)
                self._emit("watching", "watched_item_disappeared_unknown", old)

    def _detect_bidding_events(
        self,
        previous: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        *,
        suppress_incomplete: bool = False,
        suppress_disappearance: bool | None = None,
    ) -> None:
        if suppress_disappearance is None:
            suppress_disappearance = suppress_incomplete
        for item_id, item in current.items():
            old = previous.get(item_id)
            if not old:
                if not suppress_incomplete:
                    self._emit("bidding", "bid_item_added", item)
                continue
            if old.get("winning") is True and item.get("winning") is False:
                self._emit("bidding", "outbid", item, old_value=True, new_value=False)
            if old.get("winning") is False and item.get("winning") is True:
                self._emit("bidding", "winning", item, old_value=False, new_value=True)
            if _was_running(old) and item.get("seconds_left") == 0:
                self._emit("bidding", "bid_item_ended", item)
        if suppress_disappearance:
            return
        for item_id, old in previous.items():
            if item_id not in current and _was_running(old):
                self._emit("bidding", "bid_item_disappeared_unknown", old)

    def _detect_watched_price_events(
        self,
        old: dict[str, Any],
        item: dict[str, Any],
        *,
        allow_dropped_below: bool,
    ) -> None:
        """Emit legacy + directional watched price events and drop-below crossings."""
        old_price = to_decimal(old.get("current_price"))
        new_price = to_decimal(item.get("current_price"))
        old_currency = _normalize_currency(old.get("currency"))
        new_currency = _normalize_currency(item.get("currency"))
        item_id = str(item.get("item_id") or "")

        if (
            old_price is not None
            and new_price is not None
            and _currencies_compatible(old_currency, new_currency)
            and old_price != new_price
        ):
            direction = "increased" if new_price > old_price else "decreased"
            percent_change = _percent_change(old_price, new_price)
            min_percent = to_decimal(
                self.options.get(CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT, 0)
            ) or Decimal(0)
            meets_threshold = (
                old_price > 0
                and percent_change is not None
                and abs(percent_change) >= min_percent
            )
            if meets_threshold:
                extras = {
                    "old_value": _decimal_payload(old_price),
                    "new_value": _decimal_payload(new_price),
                    "percent_change": _decimal_payload(percent_change),
                    "direction": direction,
                }
                # Legacy compatibility first; canonical directional second.
                self._emit(
                    "watching",
                    "watched_item_price_changed",
                    item,
                    **extras,
                )
                canonical = (
                    "watched_item_price_increased"
                    if direction == "increased"
                    else "watched_item_price_decreased"
                )
                self._emit("watching", canonical, item, **extras)

        if allow_dropped_below:
            self._detect_price_dropped_below(old, item, old_price, new_price, item_id)

    def _detect_price_dropped_below(
        self,
        old: dict[str, Any],
        item: dict[str, Any],
        old_price: Decimal | None,
        new_price: Decimal | None,
        item_id: str,
    ) -> None:
        """Emit dropped-below only on a true threshold crossing."""
        threshold, currency, source = self._effective_price_drop_target(item_id)
        if threshold is None or not currency or not item_id:
            if item_id:
                self._price_drop_below_active.discard(item_id)
            return
        item_currency = _normalize_currency(item.get("currency"))
        old_currency = _normalize_currency(old.get("currency"))
        if (
            old_price is None
            or new_price is None
            or not item_currency
            or item_currency != currency
            or old_currency != currency
        ):
            return

        below = new_price <= threshold
        was_above = old_price > threshold
        if not below:
            self._price_drop_below_active.discard(item_id)
            return
        if item_id in self._price_drop_below_active:
            return
        if not was_above:
            # Already at/below without a crossing in this transition.
            self._price_drop_below_active.add(item_id)
            return

        percent_change = _percent_change(old_price, new_price)
        self._emit(
            "watching",
            "watched_item_price_dropped_below",
            item,
            old_value=_decimal_payload(old_price),
            new_value=_decimal_payload(new_price),
            percent_change=_decimal_payload(percent_change)
            if percent_change is not None
            else None,
            direction="decreased",
            threshold=_decimal_payload(threshold),
            threshold_currency=currency,
            threshold_source=source,
        )
        self._price_drop_below_active.add(item_id)

    def _normalize_price_drop_active_for_item(self, item: dict[str, Any]) -> None:
        """Seed or clear threshold-active state when a watched item first appears."""
        item_id = str(item.get("item_id") or "")
        if not item_id:
            return
        threshold, currency, _source = self._effective_price_drop_target(item_id)
        price = to_decimal(item.get("current_price"))
        item_currency = _normalize_currency(item.get("currency"))
        if (
            threshold is None
            or not currency
            or price is None
            or not item_currency
            or item_currency != currency
        ):
            self._price_drop_below_active.discard(item_id)
            return
        if price <= threshold:
            self._price_drop_below_active.add(item_id)
        else:
            self._price_drop_below_active.discard(item_id)

    def _effective_price_drop_target(
        self, item_id: str
    ) -> tuple[Decimal | None, str | None, str | None]:
        """Return (amount, currency, source) for the active drop threshold."""
        try:
            targets = parse_price_targets(
                self.options.get(CONF_PINNED_ITEM_PRICE_TARGETS, [])
            )
        except ValueError:
            targets = {}
        if item_id in targets:
            amount, currency = targets[item_id]
            return amount, currency, "pinned_item"
        raw_threshold = self.options.get(CONF_WATCHED_PRICE_DROP_THRESHOLD, "")
        raw_currency = self.options.get(CONF_WATCHED_PRICE_DROP_CURRENCY, "")
        threshold = (
            to_decimal(raw_threshold) if str(raw_threshold or "").strip() else None
        )
        currency = _normalize_currency(raw_currency)
        if threshold is None or not currency:
            return None, None, None
        return threshold, currency, "global"

    def _detect_watched_bid_count_events(
        self, old: dict[str, Any], item: dict[str, Any]
    ) -> None:
        old_count = old.get("bid_count")
        new_count = item.get("bid_count")
        if old_count is None or new_count is None or old_count == new_count:
            return
        extras = {"old_value": old_count, "new_value": new_count}
        self._emit(
            "watching",
            "watched_item_bid_count_changed",
            item,
            **extras,
        )
        if new_count > old_count:
            self._emit(
                "watching",
                "watched_item_bid_count_increased",
                item,
                **extras,
            )

    def _emit_if_increased(
        self,
        kind: str,
        event_type: str,
        old: dict[str, Any],
        new: dict[str, Any],
        field: str,
    ) -> None:
        old_value = old.get(field)
        new_value = new.get(field)
        if old_value is not None and new_value is not None and new_value > old_value:
            self._emit(kind, event_type, new, old_value=old_value, new_value=new_value)

    def _detect_seller_ops_events(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
        *,
        suppress_orders: bool = False,
        suppress_disputes: bool = False,
        suppress_messages: bool = False,
    ) -> None:
        """Emit seller-ops transition events from order/standards/feedback/message diffs."""
        prev_orders = (previous.get("orders") or {}).get("by_id") or {}
        curr_orders = (current.get("orders") or {}).get("by_id") or {}
        for order_id, order in curr_orders.items():
            old = prev_orders.get(order_id)
            if not old:
                if not suppress_orders:
                    self._emit("seller_ops", "order_received", order)
                    if order.get("paid"):
                        self._emit("seller_ops", "order_paid", order)
                    if order.get("due_soon"):
                        self._emit("seller_ops", "shipment_due_soon", order)
                    if order.get("overdue"):
                        self._emit("seller_ops", "shipment_overdue", order)
                continue
            if not old.get("paid") and order.get("paid"):
                self._emit("seller_ops", "order_paid", order)
            if not old.get("shipped") and order.get("shipped"):
                self._emit("seller_ops", "order_marked_shipped", order)
            if not old.get("has_tracking") and order.get("has_tracking"):
                self._emit("seller_ops", "tracking_added", order)
            if not old.get("due_soon") and order.get("due_soon"):
                self._emit("seller_ops", "shipment_due_soon", order)
            if not old.get("overdue") and order.get("overdue"):
                self._emit("seller_ops", "shipment_overdue", order)

        prev_disputes = (previous.get("disputes") or {}).get("by_id") or {}
        curr_disputes = (current.get("disputes") or {}).get("by_id") or {}
        for dispute_id, dispute in curr_disputes.items():
            if dispute_id not in prev_disputes and not suppress_disputes:
                self._emit(
                    "seller_ops",
                    "payment_dispute_opened",
                    {
                        "dispute_id": dispute_id,
                        "order_id": dispute.get("order_id"),
                        "title": dispute.get("reason"),
                    },
                )

        prev_standards = previous.get("standards") or {}
        curr_standards = current.get("standards") or {}
        prev_level = prev_standards.get("seller_level")
        curr_level = curr_standards.get("seller_level")
        if prev_level and curr_level and prev_level != curr_level:
            self._emit(
                "seller_ops",
                "seller_level_changed",
                {"title": curr_level},
                old_value=prev_level,
                new_value=curr_level,
            )
        if not prev_standards.get("at_risk") and curr_standards.get("at_risk"):
            self._emit(
                "seller_ops",
                "seller_standard_at_risk",
                {"title": curr_level},
            )
        for flag_key in (
            "item_not_received_above_benchmark",
            "item_not_as_described_above_benchmark",
        ):
            if not prev_standards.get(flag_key) and curr_standards.get(flag_key):
                self._emit(
                    "seller_ops",
                    "service_metric_above_peer_benchmark",
                    {"title": flag_key},
                    new_value=flag_key,
                )

        prev_feedback = previous.get("feedback") or {}
        curr_feedback = current.get("feedback") or {}
        prev_ids = prev_feedback.get("recent_feedback_ids")
        curr_ids = curr_feedback.get("recent_feedback_ids")
        feedback_ids_usable = (
            isinstance(prev_ids, list)
            and isinstance(curr_ids, list)
            and not prev_feedback.get("items_truncated")
            and not curr_feedback.get("items_truncated")
        )
        if feedback_ids_usable:
            prev_id_set = set(prev_ids)
            new_ids = [fid for fid in curr_ids if fid not in prev_id_set]
            if new_ids:
                self._emit(
                    "seller_ops",
                    "feedback_received",
                    {},
                    old_value=len(prev_ids),
                    new_value=len(curr_ids),
                )
            curr_negative_ids = curr_feedback.get("recent_negative_feedback_ids")
            if isinstance(curr_negative_ids, list):
                negative_id_set = set(curr_negative_ids)
                new_negative_ids = [fid for fid in new_ids if fid in negative_id_set]
                if new_negative_ids:
                    prev_negative_ids = prev_feedback.get(
                        "recent_negative_feedback_ids"
                    )
                    self._emit(
                        "seller_ops",
                        "negative_feedback_received",
                        {},
                        old_value=(
                            len(prev_negative_ids)
                            if isinstance(prev_negative_ids, list)
                            else 0
                        ),
                        new_value=len(curr_negative_ids),
                    )
        prev_score = prev_feedback.get("score")
        curr_score = curr_feedback.get("score")
        if (
            prev_score is not None
            and curr_score is not None
            and prev_score != curr_score
        ):
            self._emit(
                "seller_ops",
                "feedback_rating_changed",
                {},
                old_value=prev_score,
                new_value=curr_score,
            )

        prev_messages = (previous.get("messages") or {}).get("by_id") or {}
        curr_messages = (current.get("messages") or {}).get("by_id") or {}
        for conversation_id, convo in curr_messages.items():
            old = prev_messages.get(conversation_id)
            if old:
                continue
            if suppress_messages:
                continue
            subject = convo.get("subject") or convo.get("title")
            payload = {
                "conversation_id": conversation_id,
                "listing_id": convo.get("listing_id"),
                "item_id": convo.get("listing_id"),
                "subject": subject,
                "title": subject,
            }
            if convo.get("is_buyer_question"):
                self._emit("seller_ops", "new_buyer_question", payload)
            else:
                self._emit("seller_ops", "new_message_received", payload)
