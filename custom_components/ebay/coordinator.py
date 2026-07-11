"""DataUpdateCoordinator for eBay telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import time
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EbayApiClient, EbayAuthError, EbayError, API_WARNINGS_STATE_ATTR_MAX
from .const import (
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_ENDING_SOON_THRESHOLD,
    CONF_ENTITY_MODE,
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    CONF_PINNED_ITEM_PRICE_TARGETS,
    CONF_POLL_INTERVAL,
    CONF_SELLING_ENABLED,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
    DOMAIN,
    ENTITY_MODE_DETAILED,
    ENTITY_MODE_MINIMAL,
)
from .options_parse import parse_price_targets, pinned_ids, to_decimal

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]
EndingSoonKey = tuple[str, str, str, int, str]
SelectedItemKey = tuple[str, str]

COLLECTION_BY_EVENT_KIND = {
    "watching": "watched",
    "bidding": "bidding",
}

_BASELINE_COLLECTIONS = ("watched", "bidding", "selling")


def _duration_ms(start: float) -> float:
    """Return elapsed monotonic time in milliseconds."""
    return (time.monotonic() - start) * 1000


class EbayDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate eBay polling, cached diffs, and ending-soon timers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: EbayApiClient,
        options: dict[str, Any],
    ) -> None:
        self.entry = entry
        self.api = api
        self.options = options
        self.previous_payload: dict[str, Any] | None = None
        self.last_error_category: str | None = None
        self.selected_item_keys: set[SelectedItemKey] = set()
        self._event_callbacks: dict[str, EventCallback] = {}
        self._scheduled: dict[EndingSoonKey, CALLBACK_TYPE] = {}
        self._fired_ending_soon: set[EndingSoonKey] = set()
        # Items currently at/below their effective drop threshold (no repeat events).
        self._price_drop_below_active: set[str] = set()
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=int(options[CONF_POLL_INTERVAL])),
        )

    @property
    def ending_soon_threshold_seconds(self) -> int:
        """Return configured ending-soon threshold in seconds."""
        return int(self.options[CONF_ENDING_SOON_THRESHOLD]) * 60

    def register_event_callback(
        self, kind: str, callback_func: EventCallback
    ) -> CALLBACK_TYPE:
        """Register an event entity callback."""
        self._event_callbacks[kind] = callback_func
        if self.data:
            self._rebuild_ending_soon_timers(self.data)

        @callback
        def unsubscribe() -> None:
            if self._event_callbacks.get(kind) is callback_func:
                self._event_callbacks.pop(kind, None)

        return unsubscribe

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data and detect events."""
        start = time.monotonic()
        buying_enabled = bool(self.options[CONF_BUYING_ENABLED])
        selling_enabled = bool(self.options[CONF_SELLING_ENABLED])
        analytics_enabled = bool(self.options[CONF_ANALYTICS_ENABLED])
        _LOGGER.debug(
            "eBay coordinator refresh started entry_id=%s buying_enabled=%s selling_enabled=%s analytics_enabled=%s",
            self.entry.entry_id,
            buying_enabled,
            selling_enabled,
            analytics_enabled,
        )
        try:
            payload = await self.api.async_fetch_data(
                buying_enabled=buying_enabled,
                selling_enabled=selling_enabled,
                analytics_enabled=analytics_enabled,
                ending_soon_threshold_seconds=self.ending_soon_threshold_seconds,
            )
        except EbayAuthError as exc:
            self.last_error_category = "auth"
            _LOGGER.debug(
                "eBay coordinator refresh auth failed entry_id=%s duration_ms=%.1f",
                self.entry.entry_id,
                _duration_ms(start),
            )
            raise ConfigEntryAuthFailed("eBay authentication failed") from exc
        except EbayError as exc:
            self.last_error_category = type(exc).__name__
            _LOGGER.debug(
                "eBay coordinator refresh failed entry_id=%s error=%s duration_ms=%.1f",
                self.entry.entry_id,
                type(exc).__name__,
                _duration_ms(start),
            )
            raise UpdateFailed(f"eBay update failed: {type(exc).__name__}") from exc

        payload["summary"]["last_update"] = payload["last_update"].isoformat()
        payload["summary"]["last_successful_update"] = payload[
            "last_successful_update"
        ].isoformat()
        payload["summary"]["partial_failures"] = payload["partial_failures"]
        payload["summary"]["truncated_collections"] = payload.get(
            "truncated_collections", {}
        )
        warnings = payload.get("api_warnings") or []
        payload["summary"]["api_warnings"] = warnings[:API_WARNINGS_STATE_ATTR_MAX]
        self.last_error_category = None

        self._detect_transition_events(self.previous_payload, payload)
        self._rebuild_ending_soon_timers(payload)
        self.previous_payload = _baseline_payload(self.previous_payload, payload)
        self.refresh_selected_item_keys(payload)
        _LOGGER.debug(
            "eBay coordinator refresh completed entry_id=%s watched_count=%s bidding_count=%s selling_count=%s partial_failures=%s truncated_collections=%s duration_ms=%.1f",
            self.entry.entry_id,
            len(payload["watched"]),
            len(payload["bidding"]),
            len(payload["selling"]),
            payload["partial_failures"],
            payload.get("truncated_collections", {}),
            _duration_ms(start),
        )
        return payload

    def refresh_selected_item_keys(
        self, data: dict[str, Any] | None = None
    ) -> set[SelectedItemKey]:
        """Compute and cache the current per-item selection."""
        payload = data if data is not None else self.data
        mode = self.options[CONF_ENTITY_MODE]
        if mode == ENTITY_MODE_MINIMAL or not payload:
            self.selected_item_keys = set()
            _LOGGER.debug(
                "Selected eBay item entities refreshed entry_id=%s mode=%s selected_count=0",
                self.entry.entry_id,
                mode,
            )
            return self.selected_item_keys
        selected = _select_item_entities(
            payload,
            mode,
            int(self.options[CONF_PER_ITEM_CAP]),
            pinned_ids(self.options.get(CONF_PINNED_ITEM_IDS, "")),
            self.ending_soon_threshold_seconds,
        )
        self.selected_item_keys = set(selected)
        _LOGGER.debug(
            "Selected eBay item entities refreshed entry_id=%s mode=%s selected_count=%s cap=%s",
            self.entry.entry_id,
            mode,
            len(self.selected_item_keys),
            int(self.options[CONF_PER_ITEM_CAP]),
        )
        return self.selected_item_keys

    def _emit(
        self, kind: str, event_type: str, item: dict[str, Any], **extra: Any
    ) -> bool:
        callback_func = self._event_callbacks.get(kind)
        if not callback_func:
            return False
        event_data = self._event_data(event_type, item, kind, extra)
        callback_func(event_type, event_data)
        _LOGGER.debug(
            "Emitted eBay event kind=%s event_type=%s item_id=%s",
            kind,
            event_type,
            item.get("item_id"),
        )
        return True

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
            "item_id": item.get("item_id"),
            "title": item.get("title"),
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
            **extra,
        }

    def _detect_transition_events(
        self, previous: dict[str, Any] | None, current: dict[str, Any]
    ) -> None:
        if previous is None:
            return
        self._detect_selling_events(
            previous["selling"],
            current["selling"],
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

    def _detect_selling_events(
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
        if suppress_disappearance:
            return
        for item_id, old in previous.items():
            if item_id not in current:
                self._emit("selling", "item_disappeared_unknown", old)

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
            ) or Decimal("0")
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

    def _effective_price_drop_target(
        self, item_id: str
    ) -> tuple[Decimal | None, str | None, str | None]:
        """Return (amount, currency, source) for the active drop threshold."""
        try:
            targets = parse_price_targets(
                str(self.options.get(CONF_PINNED_ITEM_PRICE_TARGETS, "") or "")
            )
        except ValueError:
            targets = {}
        if item_id in targets:
            amount, currency = targets[item_id]
            return amount, currency, "pinned_item"
        raw_threshold = self.options.get(CONF_WATCHED_PRICE_DROP_THRESHOLD, "")
        raw_currency = self.options.get(CONF_WATCHED_PRICE_DROP_CURRENCY, "")
        threshold = to_decimal(raw_threshold) if str(raw_threshold or "").strip() else None
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

    def _rebuild_ending_soon_timers(self, payload: dict[str, Any]) -> None:
        wanted: set[EndingSoonKey] = set()
        threshold = self.ending_soon_threshold_seconds
        for kind, collection, event_type in (
            ("watching", payload["watched"], "watched_item_ending_soon"),
            ("bidding", payload["bidding"], "bidding_item_ending_soon"),
        ):
            for item_id, item in collection.items():
                end_time = item.get("end_time")
                if not isinstance(end_time, datetime):
                    continue
                now = datetime.now(timezone.utc)
                if item.get("seconds_left") == 0 or end_time <= now:
                    continue
                normalized_end_time = _normalized_end_time(end_time)
                key = (
                    self.entry.entry_id,
                    kind,
                    item_id,
                    threshold,
                    normalized_end_time,
                )
                wanted.add(key)
                fire_at = end_time - timedelta(seconds=threshold)
                if fire_at <= now:
                    if _is_active_ending_soon(item, threshold, now):
                        if key not in self._fired_ending_soon:
                            _LOGGER.debug(
                                "Firing eBay ending-soon timer immediately kind=%s item_id=%s threshold=%s end_time=%s",
                                kind,
                                item_id,
                                threshold,
                                normalized_end_time,
                            )
                            if self._emit(
                                kind,
                                event_type,
                                _with_current_seconds_left(item, now),
                            ):
                                self._fired_ending_soon.add(key)
                        else:
                            _LOGGER.debug(
                                "Skipping already-fired eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s",
                                kind,
                                item_id,
                                threshold,
                                normalized_end_time,
                            )
                        continue
                    fire_at = now
                if key in self._scheduled:
                    continue
                if any(
                    scheduled_key[:4] == key[:4] and scheduled_key != key
                    for scheduled_key in self._scheduled
                ):
                    _LOGGER.debug(
                        "Rescheduling eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s",
                        kind,
                        item_id,
                        threshold,
                        normalized_end_time,
                    )
                else:
                    _LOGGER.debug(
                        "Scheduling eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s fire_at=%s",
                        kind,
                        item_id,
                        threshold,
                        normalized_end_time,
                        fire_at.isoformat(),
                    )
                self._scheduled[key] = async_track_point_in_utc_time(
                    self.hass,
                    self._scheduled_callback(
                        kind,
                        event_type,
                        item_id,
                        normalized_end_time,
                        key,
                    ),
                    fire_at,
                )

        for key in list(self._scheduled):
            if key not in wanted:
                _, kind, item_id, threshold, normalized_end_time = key
                _LOGGER.debug(
                    "Cancelling eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s",
                    kind,
                    item_id,
                    threshold,
                    normalized_end_time,
                )
                self._scheduled.pop(key)()
        self._fired_ending_soon.intersection_update(wanted)

    @callback
    def _scheduled_callback(
        self,
        kind: str,
        event_type: str,
        item_id: str,
        scheduled_end_time: str,
        key: EndingSoonKey,
    ) -> Callable[[datetime], None]:
        @callback
        def fire(now: datetime) -> None:
            self._scheduled.pop(key, None)
            _LOGGER.debug(
                "Firing scheduled eBay ending-soon timer kind=%s item_id=%s end_time=%s",
                kind,
                item_id,
                scheduled_end_time,
            )
            if key in self._fired_ending_soon:
                _LOGGER.debug(
                    "Skipping already-fired scheduled eBay ending-soon timer kind=%s item_id=%s end_time=%s",
                    kind,
                    item_id,
                    scheduled_end_time,
                )
                return
            item = (
                (self.data or {}).get(COLLECTION_BY_EVENT_KIND[kind], {}).get(item_id)
            )
            end_time = item.get("end_time") if item else None
            if (
                item is None
                or not isinstance(end_time, datetime)
                or _normalized_end_time(end_time) != scheduled_end_time
            ):
                _LOGGER.debug(
                    "Skipping stale eBay ending-soon timer kind=%s item_id=%s end_time=%s",
                    kind,
                    item_id,
                    scheduled_end_time,
                )
                return
            if not _is_active_ending_soon(
                item, self.ending_soon_threshold_seconds, now
            ):
                _LOGGER.debug(
                    "Skipping inactive eBay ending-soon timer kind=%s item_id=%s end_time=%s",
                    kind,
                    item_id,
                    scheduled_end_time,
                )
                return
            if self._emit(kind, event_type, _with_current_seconds_left(item, now)):
                self._fired_ending_soon.add(key)

        return fire

    def cancel_scheduled_events(self) -> None:
        """Cancel scheduled ending-soon callbacks."""
        for key, cancel in list(self._scheduled.items()):
            _, kind, item_id, threshold, normalized_end_time = key
            _LOGGER.debug(
                "Cancelling eBay ending-soon timer kind=%s item_id=%s threshold=%s end_time=%s",
                kind,
                item_id,
                threshold,
                normalized_end_time,
            )
            cancel()
        self._scheduled.clear()


def _baseline_payload(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> dict[str, Any]:
    """Return the next event-comparison baseline, preserving truncated collections.

    Truncated responses omit items outside the page budget. Keep prior items that
    were missing from the truncated collection, but update overlapping items with
    the current values so change events are not repeated on the next poll.
    """
    baseline = dict(current)
    if previous is None:
        return baseline
    truncated = current.get("truncated_collections") or {}
    for key in _BASELINE_COLLECTIONS:
        if truncated.get(key):
            merged = dict(previous.get(key, {}))
            merged.update(current.get(key, {}))
            baseline[key] = merged
    return baseline


def _collection_truncated(
    previous: dict[str, Any], current: dict[str, Any], key: str
) -> bool:
    """Return whether either snapshot reports the collection as truncated."""
    for payload in (previous, current):
        if (payload.get("truncated_collections") or {}).get(key):
            return True
    return False


def _pinned_ids(value: str) -> set[str]:
    """Compatibility wrapper for pinned ID parsing."""
    return pinned_ids(value)


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
    return ((new_price - old_price) / old_price) * Decimal("100")


def _decimal_payload(value: Decimal | None) -> float | int | None:
    """Serialize Decimal for event payloads as int when whole, else float."""
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _select_item_entities(
    data: dict[str, Any],
    mode: str,
    cap: int,
    pinned: set[str],
    ending_soon_threshold: int,
) -> list[SelectedItemKey]:
    candidates: list[tuple[int, str, str]] = []
    for kind in ("selling", "watched", "bidding"):
        for item_id, item in data.get(kind, {}).items():
            priority = 100
            if item_id in pinned:
                priority = 0
            elif kind == "selling" and (
                (item.get("bid_count") or 0) > 0
                or (item.get("offers") or 0) > 0
                or (item.get("questions") or 0) > 0
            ):
                priority = 10
            elif (
                item.get("seconds_left") is not None
                and 0 < item["seconds_left"] <= ending_soon_threshold
            ):
                priority = 20
            elif mode != ENTITY_MODE_DETAILED:
                continue
            candidates.append((priority, kind, item_id))
    candidates.sort(key=lambda value: (value[0], value[1], value[2]))
    return [(kind, item_id) for _, kind, item_id in candidates[:cap]]


def _normalized_end_time(end_time: datetime) -> str:
    """Return a stable UTC identity for an eBay end time."""
    return end_time.astimezone(timezone.utc).isoformat()


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
