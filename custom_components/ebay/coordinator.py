"""DataUpdateCoordinator for eBay telemetry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections import deque
import logging
import time
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    API_WARNINGS_STATE_ATTR_MAX,
    EbayApiClient,
    EbayAuthError,
    EbayError,
    enabled_sections_for_options,
)
from .const import (
    CONF_ANALYTICS_ENABLED,
    CONF_BUYING_ENABLED,
    CONF_ENDING_SOON_THRESHOLD,
    CONF_ENTITY_MODE,
    CONF_FEEDBACK_ENABLED,
    CONF_FULFILLMENT_ENABLED,
    CONF_MESSAGES_ENABLED,
    CONF_OAUTH_SCOPES,
    CONF_PER_ITEM_CAP,
    CONF_PINNED_ITEM_IDS,
    CONF_PINNED_ITEM_PRICE_TARGETS,
    CONF_POLL_INTERVAL,
    CONF_SELLING_ENABLED,
    CONF_SELLER_STANDARDS_ENABLED,
    CONF_WATCHED_PRICE_CHANGE_MIN_PERCENT,
    CONF_WATCHED_PRICE_DROP_CURRENCY,
    CONF_WATCHED_PRICE_DROP_THRESHOLD,
    DOMAIN,
    ENTITY_MODE_DETAILED,
    ENTITY_MODE_MINIMAL,
    LEGACY_COMPATIBILITY_EVENT_TYPES,
    MANUAL_REFRESH_COOLDOWN_SECONDS,
    MISSING_SCOPE_PARTIAL_FAILURES,
    PARTIAL_FAILURE_SECTION,
    RECENT_EVENTS_MAX,
    REFRESH_RESULT_AUTH_ERROR,
    REFRESH_RESULT_ERROR,
    REFRESH_RESULT_PARTIAL,
    REFRESH_RESULT_SUCCESS,
    REFRESH_RESULT_UNKNOWN,
    TITLE_HISTORY_MAX_LEN,
    section_interval_minutes,
    section_retry_minutes,
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
_BASELINE_ID_LISTS = ("sold_item_ids", "unsold_item_ids")


def _duration_ms(start: float) -> float:
    """Return elapsed monotonic time in milliseconds."""
    return (time.monotonic() - start) * 1000


def _parse_section_timestamp(value: Any) -> datetime | None:
    """Parse a section timestamp from a datetime or ISO string."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _section_timestamps(data: dict[str, Any], *keys: str) -> dict[str, datetime]:
    """Merge section timestamp maps; earlier keys take precedence."""
    result: dict[str, datetime] = {}
    for key in keys:
        raw = data.get(key) or {}
        if not isinstance(raw, dict):
            continue
        for section, value in raw.items():
            if section in result:
                continue
            parsed = _parse_section_timestamp(value)
            if parsed is not None:
                result[section] = parsed
    return result


def _section_has_missing_scope(partial_failures: set[str], section: str) -> bool:
    """Return whether the section's last failure was a missing-scope condition."""
    for category in MISSING_SCOPE_PARTIAL_FAILURES:
        if category not in partial_failures:
            continue
        if PARTIAL_FAILURE_SECTION.get(category) == section:
            return True
    return False


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
        self.last_attempt_at: datetime | None = None
        self.last_refresh_duration_seconds: float | None = None
        self.last_refresh_result: str = REFRESH_RESULT_UNKNOWN
        self.selected_item_keys: set[SelectedItemKey] = set()
        self._event_callbacks: dict[str, EventCallback] = {}
        self._timer_listeners: list[Callable[[], None]] = []
        self._scheduled: dict[EndingSoonKey, CALLBACK_TYPE] = {}
        self._fired_ending_soon: set[EndingSoonKey] = set()
        # Items currently at/below their effective drop threshold (no repeat events).
        self._price_drop_below_active: set[str] = set()
        self._recent_events: dict[str, deque[dict[str, Any]]] = {
            "watching": deque(maxlen=RECENT_EVENTS_MAX),
            "bidding": deque(maxlen=RECENT_EVENTS_MAX),
            "selling": deque(maxlen=RECENT_EVENTS_MAX),
            "seller_ops": deque(maxlen=RECENT_EVENTS_MAX),
        }
        # Ending-soon keys already recorded to history without an entity callback.
        self._recent_history_ending_soon: set[EndingSoonKey] = set()
        self._manual_refresh_last_requested: float | None = None
        self._force_full_refresh = False
        self._repair_streaks: dict[str, int] = {}
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=int(options[CONF_POLL_INTERVAL])),
        )

    def recent_events(self, kind: str) -> list[dict[str, Any]]:
        """Return newest-first recent activity for a kind."""
        history = self._recent_events.get(kind)
        if not history:
            return []
        return list(reversed(history))

    @property
    def scheduled_ending_soon_count(self) -> int:
        """Return the number of scheduled ending-soon callbacks."""
        return len(self._scheduled)

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

    def async_add_timer_listener(
        self, update_callback: Callable[[], None]
    ) -> CALLBACK_TYPE:
        """Register a listener for scheduled ending-soon timer collection changes."""
        self._timer_listeners.append(update_callback)

        @callback
        def unsubscribe() -> None:
            try:
                self._timer_listeners.remove(update_callback)
            except ValueError:
                pass

        return unsubscribe

    @callback
    def _async_notify_timer_listeners(self) -> None:
        """Notify timer-count consumers without refreshing every coordinator entity."""
        for listener in list(self._timer_listeners):
            listener()

    async def async_request_manual_refresh(self) -> bool:
        """Request a coordinator refresh subject to the manual cooldown.

        Returns True when a refresh was requested, False when ignored.
        """
        now = time.monotonic()
        if self._manual_refresh_last_requested is not None:
            elapsed = now - self._manual_refresh_last_requested
            if elapsed < MANUAL_REFRESH_COOLDOWN_SECONDS:
                _LOGGER.debug(
                    "Ignoring eBay manual refresh during cooldown entry_id=%s remaining_s=%.1f",
                    self.entry.entry_id,
                    MANUAL_REFRESH_COOLDOWN_SECONDS - elapsed,
                )
                return False
        refresh_task = getattr(self, "_refresh_task", None)
        if refresh_task is not None and not refresh_task.done():
            _LOGGER.debug(
                "Ignoring eBay manual refresh while refresh already running entry_id=%s",
                self.entry.entry_id,
            )
            return False
        self._manual_refresh_last_requested = now
        self._force_full_refresh = True
        _LOGGER.debug("Requesting eBay manual refresh entry_id=%s", self.entry.entry_id)
        await self.async_request_refresh()
        return True

    def _enabled_sections(self) -> set[str]:
        """Return sections enabled by the current options."""
        return enabled_sections_for_options(
            buying_enabled=bool(self.options[CONF_BUYING_ENABLED]),
            selling_enabled=bool(self.options[CONF_SELLING_ENABLED]),
            analytics_enabled=bool(self.options[CONF_ANALYTICS_ENABLED]),
            fulfillment_enabled=bool(self.options[CONF_FULFILLMENT_ENABLED]),
            seller_standards_enabled=bool(self.options[CONF_SELLER_STANDARDS_ENABLED]),
            feedback_enabled=bool(self.options[CONF_FEEDBACK_ENABLED]),
            messages_enabled=bool(self.options[CONF_MESSAGES_ENABLED]),
        )

    def _due_sections(self, *, force: bool) -> set[str]:
        """Return enabled sections that are due for refresh."""
        enabled = self._enabled_sections()
        if force or not self.data:
            return enabled
        now = datetime.now(timezone.utc)
        poll_interval = int(self.options[CONF_POLL_INTERVAL])
        last_success = _section_timestamps(
            self.data, "section_last_success", "section_last_fetched"
        )
        last_attempt = _section_timestamps(self.data, "section_last_attempt")
        partial_failures = set(self.data.get("partial_failures") or [])
        due: set[str] = set()
        for section in enabled:
            success = last_success.get(section)
            attempt = last_attempt.get(section)
            if success is None and attempt is None:
                due.add(section)
                continue

            interval = timedelta(
                minutes=section_interval_minutes(section, poll_interval)
            )
            if success is not None and now - success >= interval:
                due.add(section)
                continue

            # Soft-fail retry: last attempt is newer than last success (or no success).
            if attempt is None:
                continue
            if success is not None and attempt <= success:
                continue
            if _section_has_missing_scope(partial_failures, section):
                continue
            retry = timedelta(minutes=section_retry_minutes(section, poll_interval))
            if now - attempt >= retry:
                due.add(section)
        return due

    def _record_refresh_attempt(
        self,
        *,
        start: float,
        result: str,
        error_category: str | None = None,
    ) -> None:
        """Store safe refresh health metadata for diagnostic entities."""
        self.last_attempt_at = datetime.now(timezone.utc)
        self.last_refresh_duration_seconds = max(0.0, time.monotonic() - start)
        self.last_refresh_result = result
        self.last_error_category = error_category

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data and detect events."""
        start = time.monotonic()
        buying_enabled = bool(self.options[CONF_BUYING_ENABLED])
        selling_enabled = bool(self.options[CONF_SELLING_ENABLED])
        analytics_enabled = bool(self.options[CONF_ANALYTICS_ENABLED])
        fulfillment_enabled = bool(self.options[CONF_FULFILLMENT_ENABLED])
        seller_standards_enabled = bool(self.options[CONF_SELLER_STANDARDS_ENABLED])
        feedback_enabled = bool(self.options[CONF_FEEDBACK_ENABLED])
        messages_enabled = bool(self.options[CONF_MESSAGES_ENABLED])
        force = self._force_full_refresh
        self._force_full_refresh = False
        due_sections = self._due_sections(force=force)
        _LOGGER.debug(
            "eBay coordinator refresh started entry_id=%s force=%s due_sections=%s buying_enabled=%s selling_enabled=%s analytics_enabled=%s fulfillment_enabled=%s seller_standards_enabled=%s feedback_enabled=%s messages_enabled=%s",
            self.entry.entry_id,
            force,
            sorted(due_sections),
            buying_enabled,
            selling_enabled,
            analytics_enabled,
            fulfillment_enabled,
            seller_standards_enabled,
            feedback_enabled,
            messages_enabled,
        )
        if not due_sections and self.data is not None:
            self._record_refresh_attempt(
                start=start,
                result=self.last_refresh_result
                if self.last_refresh_result != REFRESH_RESULT_UNKNOWN
                else REFRESH_RESULT_SUCCESS,
                error_category=None,
            )
            # No section attempts this tick — do not advance repair streaks.
            return self.data
        try:
            payload = await self.api.async_fetch_data(
                buying_enabled=buying_enabled,
                selling_enabled=selling_enabled,
                analytics_enabled=analytics_enabled,
                fulfillment_enabled=fulfillment_enabled,
                seller_standards_enabled=seller_standards_enabled,
                feedback_enabled=feedback_enabled,
                messages_enabled=messages_enabled,
                ending_soon_threshold_seconds=self.ending_soon_threshold_seconds,
                granted_scopes=self.entry.data.get(CONF_OAUTH_SCOPES),
                sections=due_sections,
                previous=self.data,
            )
        except EbayAuthError as exc:
            self._record_refresh_attempt(
                start=start,
                result=REFRESH_RESULT_AUTH_ERROR,
                error_category="auth",
            )
            _LOGGER.debug(
                "eBay coordinator refresh auth failed entry_id=%s duration_ms=%.1f",
                self.entry.entry_id,
                _duration_ms(start),
            )
            from .repairs import async_sync_reauth_issue

            await async_sync_reauth_issue(self.hass, self.entry, active=True)
            raise ConfigEntryAuthFailed("eBay authentication failed") from exc
        except EbayError as exc:
            self._record_refresh_attempt(
                start=start,
                result=REFRESH_RESULT_ERROR,
                error_category=type(exc).__name__,
            )
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
        payload["summary"]["missing_scopes"] = payload.get("missing_scopes", {})
        payload["summary"]["missing_scope_features"] = payload.get(
            "missing_scope_features", []
        )
        for key in (
            "section_last_fetched",
            "section_last_attempt",
            "section_last_success",
        ):
            payload["summary"][key] = {
                k: v.isoformat() if isinstance(v, datetime) else v
                for k, v in (payload.get(key) or {}).items()
            }
        warnings = payload.get("api_warnings") or []
        payload["summary"]["api_warnings"] = warnings[:API_WARNINGS_STATE_ATTR_MAX]
        result = (
            REFRESH_RESULT_PARTIAL
            if payload.get("partial_failures")
            else REFRESH_RESULT_SUCCESS
        )
        self._record_refresh_attempt(start=start, result=result, error_category=None)

        self._detect_transition_events(self.previous_payload, payload)
        self._rebuild_ending_soon_timers(payload)
        self.previous_payload = _baseline_payload(self.previous_payload, payload)
        self.refresh_selected_item_keys(payload)

        from .repairs import async_sync_repair_issues

        refreshed = set(payload.get("refreshed_sections") or due_sections)
        await async_sync_repair_issues(
            self.hass,
            self.entry,
            self,
            payload,
            refreshed_sections=refreshed,
        )
        _LOGGER.debug(
            "eBay coordinator refresh completed entry_id=%s refreshed_sections=%s watched_count=%s bidding_count=%s selling_count=%s partial_failures=%s truncated_collections=%s duration_ms=%.1f",
            self.entry.entry_id,
            payload.get("refreshed_sections", []),
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
        self,
        kind: str,
        event_type: str,
        item: dict[str, Any],
        *,
        record_history: bool | None = None,
        history_key: EndingSoonKey | None = None,
        **extra: Any,
    ) -> bool:
        """Emit an event to the entity callback and optionally record history.

        Returns True when the EventEntity callback received the event. History is
        recorded for canonical events even when no callback is registered.
        """
        if record_history is None:
            record_history = event_type not in LEGACY_COMPATIBILITY_EVENT_TYPES
        event_data = self._event_data(event_type, item, kind, extra)
        if record_history:
            if (
                history_key is None
                or history_key not in self._recent_history_ending_soon
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
                str(self.options.get(CONF_PINNED_ITEM_PRICE_TARGETS, "") or "")
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
        prev_total = (
            int(prev_feedback.get("recent_positive") or 0)
            + int(prev_feedback.get("recent_neutral") or 0)
            + int(prev_feedback.get("recent_negative") or 0)
        )
        curr_total = (
            int(curr_feedback.get("recent_positive") or 0)
            + int(curr_feedback.get("recent_neutral") or 0)
            + int(curr_feedback.get("recent_negative") or 0)
        )
        if curr_total > prev_total:
            self._emit(
                "seller_ops",
                "feedback_received",
                {},
                old_value=prev_total,
                new_value=curr_total,
            )
        prev_neg = int(prev_feedback.get("recent_negative") or 0)
        curr_neg = int(curr_feedback.get("recent_negative") or 0)
        if curr_neg > prev_neg:
            self._emit(
                "seller_ops",
                "negative_feedback_received",
                {},
                old_value=prev_neg,
                new_value=curr_neg,
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

    def _rebuild_ending_soon_timers(self, payload: dict[str, Any]) -> None:
        wanted: set[EndingSoonKey] = set()
        previous_keys = set(self._scheduled)
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
                                history_key=key,
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
        self._recent_history_ending_soon.intersection_update(wanted)
        if set(self._scheduled) != previous_keys:
            self._async_notify_timer_listeners()

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
            self._async_notify_timer_listeners()
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
            if self._emit(
                kind,
                event_type,
                _with_current_seconds_left(item, now),
                history_key=key,
            ):
                self._fired_ending_soon.add(key)

        return fire

    def cancel_scheduled_events(self) -> None:
        """Cancel scheduled ending-soon callbacks."""
        if not self._scheduled:
            return
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
        self._async_notify_timer_listeners()


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
    if truncated.get("sold_unsold"):
        for key in _BASELINE_ID_LISTS:
            previous_ids = set(previous.get(key) or [])
            current_ids = set(current.get(key) or [])
            baseline[key] = sorted(previous_ids | current_ids)
        previous_classification = dict(previous.get("selling_classification") or {})
        previous_classification.update(current.get("selling_classification") or {})
        baseline["selling_classification"] = previous_classification
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
