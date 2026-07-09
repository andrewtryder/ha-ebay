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

from .api import EbayApiClient, EbayAuthError, EbayError
from .const import (
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_ENDING_SOON_THRESHOLD,
    CONF_POLL_INTERVAL,
    CONF_SELLING_ENABLED,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[str, dict[str, Any]], None]


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
        self._event_callbacks: dict[str, EventCallback] = {}
        self._scheduled: dict[tuple[str, str, str, int], CALLBACK_TYPE] = {}
        self._fired_ending_soon: set[tuple[str, str, str, int]] = set()
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

    def register_event_callback(self, kind: str, callback_func: EventCallback) -> None:
        """Register an event entity callback."""
        self._event_callbacks[kind] = callback_func

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
        self.last_error_category = None

        self._detect_transition_events(self.previous_payload, payload)
        self._rebuild_ending_soon_timers(payload)
        self.previous_payload = payload
        return payload

    def _emit(self, kind: str, event_type: str, item: dict[str, Any], **extra: Any) -> None:
        callback_func = self._event_callbacks.get(kind)
        if not callback_func:
            return
        event_data = self._event_data(event_type, item, kind, extra)
        callback_func(event_type, event_data)

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
            "end_time": end_time.isoformat() if isinstance(end_time, datetime) else end_time,
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
        self._detect_selling_events(previous["selling"], current["selling"])
        self._detect_watching_events(previous["watched"], current["watched"])
        self._detect_bidding_events(previous["bidding"], current["bidding"])

    def _detect_selling_events(
        self, previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]
    ) -> None:
        for item_id, item in current.items():
            old = previous.get(item_id)
            if not old:
                continue
            self._emit_if_increased("selling", "bid_count_increased", old, item, "bid_count")
            self._emit_if_increased("selling", "offer_received", old, item, "offers")
            self._emit_if_increased("selling", "question_received", old, item, "questions")
            self._emit_if_increased(
                "selling", "watcher_count_increased", old, item, "watchers"
            )
            self._emit_if_increased(
                "selling", "quantity_sold_increased", old, item, "quantity_sold"
            )
        for item_id, old in previous.items():
            if item_id not in current:
                self._emit("selling", "item_disappeared_unknown", old)

    def _detect_watching_events(
        self, previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]
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
            if (old.get("seconds_left") or 1) > 0 and item.get("seconds_left") == 0:
                self._emit("watching", "watched_item_ended", item)
        for item_id, old in previous.items():
            if item_id not in current:
                self._emit("watching", "watched_item_ended", old)

    def _detect_bidding_events(
        self, previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]
    ) -> None:
        for item_id, item in current.items():
            old = previous.get(item_id)
            if not old:
                continue
            if old.get("winning") is True and item.get("winning") is False:
                self._emit("bidding", "outbid", item, old_value=True, new_value=False)
            if old.get("winning") is False and item.get("winning") is True:
                self._emit("bidding", "winning", item, old_value=False, new_value=True)
            if (old.get("seconds_left") or 1) > 0 and item.get("seconds_left") == 0:
                self._emit("bidding", "bid_item_ended", item)
        for item_id, old in previous.items():
            if item_id not in current:
                self._emit("bidding", "bid_item_ended", old)

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
        wanted: set[tuple[str, str, str, int]] = set()
        threshold = self.ending_soon_threshold_seconds
        for kind, collection, event_type in (
            ("watching", payload["watched"], "watched_item_ending_soon"),
            ("bidding", payload["bidding"], "bidding_item_ending_soon"),
        ):
            for item_id, item in collection.items():
                end_time = item.get("end_time")
                if not isinstance(end_time, datetime):
                    continue
                key = (self.entry.entry_id, kind, item_id, threshold)
                wanted.add(key)
                fire_at = end_time - timedelta(seconds=threshold)
                now = datetime.now(timezone.utc)
                if fire_at <= now:
                    if item.get("seconds_left") is not None and item["seconds_left"] <= threshold:
                        if key not in self._fired_ending_soon:
                            self._fired_ending_soon.add(key)
                            self._emit(kind, event_type, item)
                        continue
                    fire_at = now
                if key in self._scheduled:
                    continue
                self._scheduled[key] = async_track_point_in_utc_time(
                    self.hass,
                    self._scheduled_callback(kind, event_type, item, key),
                    fire_at,
                )

        for key in list(self._scheduled):
            if key not in wanted:
                self._scheduled.pop(key)()

    @callback
    def _scheduled_callback(
        self,
        kind: str,
        event_type: str,
        item: dict[str, Any],
        key: tuple[str, str, str, int],
    ) -> Callable[[datetime], None]:
        @callback
        def fire(_: datetime) -> None:
            self._scheduled.pop(key, None)
            if key in self._fired_ending_soon:
                return
            self._fired_ending_soon.add(key)
            self._emit(kind, event_type, item)

        return fire

    def cancel_scheduled_events(self) -> None:
        """Cancel scheduled ending-soon callbacks."""
        for cancel in self._scheduled.values():
            cancel()
        self._scheduled.clear()
