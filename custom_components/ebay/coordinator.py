"""DataUpdateCoordinator for eBay telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
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
    CONF_POLL_INTERVAL,
    CONF_SELLING_ENABLED,
    DOMAIN,
    ENTITY_MODE_DETAILED,
    ENTITY_MODE_MINIMAL,
)

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]
EndingSoonKey = tuple[str, str, str, int, str]
SelectedItemKey = tuple[str, str]

COLLECTION_BY_EVENT_KIND = {
    "watching": "watched",
    "bidding": "bidding",
}

_BASELINE_COLLECTIONS = ("watched", "bidding", "selling")


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
        try:
            payload = await self.api.async_fetch_data(
                buying_enabled=bool(self.options[CONF_BUYING_ENABLED]),
                selling_enabled=bool(self.options[CONF_SELLING_ENABLED]),
                analytics_enabled=bool(self.options[CONF_ANALYTICS_ENABLED]),
                ending_soon_threshold_seconds=self.ending_soon_threshold_seconds,
            )
        except EbayAuthError as exc:
            self.last_error_category = "auth"
            raise ConfigEntryAuthFailed("eBay authentication failed") from exc
        except EbayError as exc:
            self.last_error_category = type(exc).__name__
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
        return payload

    def refresh_selected_item_keys(
        self, data: dict[str, Any] | None = None
    ) -> set[SelectedItemKey]:
        """Compute and cache the current per-item selection."""
        payload = data if data is not None else self.data
        mode = self.options[CONF_ENTITY_MODE]
        if mode == ENTITY_MODE_MINIMAL or not payload:
            self.selected_item_keys = set()
            return self.selected_item_keys
        selected = _select_item_entities(
            payload,
            mode,
            int(self.options[CONF_PER_ITEM_CAP]),
            _pinned_ids(self.options.get(CONF_PINNED_ITEM_IDS, "")),
            self.ending_soon_threshold_seconds,
        )
        self.selected_item_keys = set(selected)
        return self.selected_item_keys

    def _emit(
        self, kind: str, event_type: str, item: dict[str, Any], **extra: Any
    ) -> bool:
        callback_func = self._event_callbacks.get(kind)
        if not callback_func:
            return False
        event_data = self._event_data(event_type, item, kind, extra)
        callback_func(event_type, event_data)
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
            suppress_disappearance=_collection_truncated(previous, current, "selling"),
        )
        self._detect_watching_events(
            previous["watched"],
            current["watched"],
            suppress_disappearance=_collection_truncated(previous, current, "watched"),
        )
        self._detect_bidding_events(
            previous["bidding"],
            current["bidding"],
            suppress_disappearance=_collection_truncated(previous, current, "bidding"),
        )

    def _detect_selling_events(
        self,
        previous: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        *,
        suppress_disappearance: bool,
    ) -> None:
        for item_id, item in current.items():
            old = previous.get(item_id)
            if not old:
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
        suppress_disappearance: bool,
    ) -> None:
        for item_id, item in current.items():
            old = previous.get(item_id)
            if not old:
                continue
            if old.get("current_price") != item.get("current_price"):
                self._emit(
                    "watching",
                    "watched_item_price_changed",
                    item,
                    old_value=old.get("current_price"),
                    new_value=item.get("current_price"),
                )
            if old.get("bid_count") != item.get("bid_count"):
                self._emit(
                    "watching",
                    "watched_item_bid_count_changed",
                    item,
                    old_value=old.get("bid_count"),
                    new_value=item.get("bid_count"),
                )
            if _was_running(old) and item.get("seconds_left") == 0:
                self._emit("watching", "watched_item_ended", item)
        if suppress_disappearance:
            return
        for item_id, old in previous.items():
            if item_id not in current and _was_running(old):
                self._emit("watching", "watched_item_disappeared_unknown", old)

    def _detect_bidding_events(
        self,
        previous: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        *,
        suppress_disappearance: bool,
    ) -> None:
        for item_id, item in current.items():
            old = previous.get(item_id)
            if not old:
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
                            if self._emit(
                                kind,
                                event_type,
                                _with_current_seconds_left(item, now),
                            ):
                                self._fired_ending_soon.add(key)
                        continue
                    fire_at = now
                if key in self._scheduled:
                    continue
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
            if key in self._fired_ending_soon:
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
                return
            if not _is_active_ending_soon(
                item, self.ending_soon_threshold_seconds, now
            ):
                return
            if self._emit(kind, event_type, _with_current_seconds_left(item, now)):
                self._fired_ending_soon.add(key)

        return fire

    def cancel_scheduled_events(self) -> None:
        """Cancel scheduled ending-soon callbacks."""
        for cancel in self._scheduled.values():
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
    return {part.strip() for part in value.replace("\n", ",").split(",") if part.strip()}


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


def _is_active_ending_soon(
    item: dict[str, Any], threshold: int, now: datetime
) -> bool:
    """Return whether an item is still active and inside the ending-soon window."""
    end_time = item.get("end_time")
    if not isinstance(end_time, datetime):
        return False
    seconds_left = _seconds_until(end_time, now)
    return 0 < seconds_left <= threshold


def _with_current_seconds_left(
    item: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Return event item data with seconds_left calculated for the emission time."""
    end_time = item.get("end_time")
    if not isinstance(end_time, datetime):
        return item
    return {**item, "seconds_left": _seconds_until(end_time, now)}


def _seconds_until(end_time: datetime, now: datetime) -> int:
    """Return whole seconds from now until end_time."""
    return max(0, int((end_time - now.astimezone(timezone.utc)).total_seconds()))
